"""Preflight checks — run this before you trade, and especially before you go live.

Every check here corresponds to something that has silently broken a running
bot: a clock that drifted enough to reject transactions, an RPC endpoint slow
enough to miss every fill, a keystore that decrypts to the wrong wallet, a
config that says live but has no funds behind it.
"""

from __future__ import annotations

import asyncio
import shutil
import sys
import time

import httpx
from rich.console import Console
from rich.table import Table

from .config import Config

out = Console()


class Check:
    def __init__(self, name: str) -> None:
        self.name = name
        self.status = "pending"
        self.detail = ""
        self.fatal = False

    def ok(self, detail: str = "") -> Check:
        self.status = "PASS"
        self.detail = detail
        return self

    def warn(self, detail: str) -> Check:
        self.status = "WARN"
        self.detail = detail
        return self

    def fail(self, detail: str, fatal: bool = True) -> Check:
        self.status = "FAIL"
        self.detail = detail
        self.fatal = fatal
        return self


async def _check_python() -> Check:
    check = Check("Python version")
    version = sys.version_info
    if version < (3, 10):
        return check.fail(f"{version.major}.{version.minor} — 3.10+ required")
    return check.ok(f"{version.major}.{version.minor}.{version.micro}")


async def _check_packages(config: Config) -> list[Check]:
    checks = []
    core = {
        "pydantic": "config validation",
        "redis": "message bus",
        "aiosqlite": "database",
        "httpx": "HTTP",
        "websockets": "live feeds",
        "yaml": "config files",
    }
    for module, purpose in core.items():
        check = Check(f"package: {module}")
        try:
            __import__(module)
            check.ok(purpose)
        except ImportError:
            check.fail(f"missing ({purpose}) — run: pip install -e .")
        checks.append(check)

    # Role-specific extras.
    if config.role == "brain":
        for module, purpose in [
            ("solders", "transaction signing"),
            ("cryptography", "keystore encryption"),
        ]:
            check = Check(f"package: {module}")
            try:
                __import__(module)
                check.ok(purpose)
            except ImportError:
                check.fail(
                    f"missing ({purpose}) — run: pip install -e '.[trade]'",
                    fatal=config.is_live,
                )
            checks.append(check)

    if config.role == "console":
        for module in ("fastapi", "uvicorn"):
            check = Check(f"package: {module}")
            try:
                __import__(module)
                check.ok("dashboard")
            except ImportError:
                check.fail("missing — run: pip install -e '.[console]'")
            checks.append(check)

    return checks


async def _check_clock() -> Check:
    """A drifting clock silently breaks trading.

    Solana rejects transactions built against a blockhash it considers
    expired, and your own timing logic (age gates, staleness exits) goes wrong
    in ways that look like bad luck rather than a bug. Raspberry Pis have no
    battery-backed clock, so this matters more than usual here.
    """
    check = Check("System clock")
    try:
        async with httpx.AsyncClient(timeout=8.0) as client:
            started = time.time()
            resp = await client.head("https://api.mainnet-beta.solana.com")
            rtt = time.time() - started
            server_date = resp.headers.get("date")
            if not server_date:
                return check.warn("could not read a reference time")

            from email.utils import parsedate_to_datetime

            server_ts = parsedate_to_datetime(server_date).timestamp()
            drift = abs(time.time() - server_ts - rtt / 2)

            if drift > 5:
                return check.fail(
                    f"clock is off by ~{drift:.0f}s. Fix with: "
                    "sudo timedatectl set-ntp true"
                )
            if drift > 2:
                return check.warn(f"clock is off by ~{drift:.1f}s")
            return check.ok(f"within {drift:.1f}s")
    except Exception as exc:  # noqa: BLE001
        return check.warn(f"could not verify ({exc})")


async def _check_redis(config: Config) -> Check:
    check = Check("Redis bus")
    try:
        from .bus import Bus

        bus = Bus(config.bus.redis_url, config.bus.redis_password, source="preflight")
        started = time.time()
        await bus.connect(retries=1)
        latency_ms = (time.time() - started) * 1000
        await bus.set_json("mctb:preflight", {"ts": time.time()}, ttl_s=60)
        value = await bus.get_json("mctb:preflight")
        await bus.close()
        if not value:
            return check.fail("connected but could not read back a test key")
        return check.ok(f"{config.bus.redis_url.split('@')[-1]} ({latency_ms:.0f}ms)")
    except Exception as exc:  # noqa: BLE001
        return check.fail(
            f"{exc}\n      All three devices must reach the same Redis. "
            "Check MCTB_REDIS_URL and that redis-server is running."
        )


async def _check_rpc(config: Config) -> list[Check]:
    checks = []

    check = Check("Solana RPC")
    try:
        from .solana.rpc import SolanaRPC

        async with SolanaRPC(
            config.rpc.url, config.rpc.fallback_url, config.rpc.timeout_s
        ) as rpc:
            started = time.time()
            slot = await rpc.get_slot()
            latency_ms = (time.time() - started) * 1000

            if slot <= 0:
                check.fail("endpoint returned no slot")
            elif latency_ms > 800:
                check.warn(
                    f"slot {slot}, but {latency_ms:.0f}ms latency is too slow for sniping. "
                    "Get a dedicated endpoint (Helius/QuickNode/Triton)."
                )
            elif latency_ms > 300:
                check.warn(f"slot {slot}, {latency_ms:.0f}ms — usable but not fast")
            else:
                check.ok(f"slot {slot}, {latency_ms:.0f}ms")
            checks.append(check)

            # Rate-limit probe: the public endpoint fails this constantly and
            # that is exactly what costs you fills.
            burst = Check("RPC rate limit")
            try:
                started = time.time()
                results = await asyncio.gather(
                    *[rpc.get_slot() for _ in range(8)], return_exceptions=True
                )
                failed = sum(1 for r in results if isinstance(r, Exception))
                elapsed = time.time() - started
                if failed > 2:
                    burst.warn(
                        f"{failed}/8 concurrent reads failed — you are being rate limited. "
                        "A dedicated endpoint is close to mandatory for live trading."
                    )
                else:
                    burst.ok(f"8 concurrent reads in {elapsed:.2f}s, {failed} failed")
            except Exception as exc:  # noqa: BLE001
                burst.warn(str(exc))
            checks.append(burst)

    except Exception as exc:  # noqa: BLE001
        checks.append(check.fail(f"{exc} — check MCTB_RPC_URL"))

    return checks


async def _check_pumpportal(config: Config) -> Check:
    check = Check("PumpPortal feed")
    try:
        import json as _json

        import websockets

        url = "wss://pumpportal.fun/api/data"
        if config.pumpportal_api_key:
            url += f"?api-key={config.pumpportal_api_key}"

        async with websockets.connect(url, open_timeout=12, close_timeout=3) as ws:
            await ws.send(_json.dumps({"method": "subscribeNewToken"}))
            try:
                raw = await asyncio.wait_for(ws.recv(), timeout=25)
                _json.loads(raw)
                return check.ok("connected, receiving data")
            except asyncio.TimeoutError:
                return check.warn(
                    "connected but no message in 25s — may just be a quiet moment"
                )
    except Exception as exc:  # noqa: BLE001
        return check.fail(f"{exc}", fatal=False)


async def _check_llm(config: Config) -> Check:
    check = Check("Local LLM")
    if not config.llm.enabled:
        return check.ok("disabled in config")
    try:
        async with httpx.AsyncClient(base_url=config.llm.base_url, timeout=8.0) as client:
            for path in ("/health", "/v1/models"):
                try:
                    resp = await client.get(path)
                    if resp.status_code < 400:
                        # Time a real generation — this is what tells you
                        # whether the model is fast enough to be useful.
                        started = time.time()
                        gen = await client.post(
                            "/v1/chat/completions",
                            json={
                                "model": config.llm.model,
                                "messages": [{"role": "user", "content": "Reply with: ok"}],
                                "max_tokens": 8,
                                "temperature": 0,
                            },
                            timeout=45.0,
                        )
                        elapsed = time.time() - started
                        if gen.status_code >= 400:
                            return check.warn(f"reachable but generation failed ({gen.status_code})")
                        if elapsed > config.llm.timeout_s:
                            return check.warn(
                                f"a tiny generation took {elapsed:.1f}s, longer than your "
                                f"{config.llm.timeout_s:.1f}s timeout. The LLM will be skipped "
                                "on live candidates — use a smaller model or accept that it "
                                "only does the nightly review."
                            )
                        return check.ok(f"responding in {elapsed:.1f}s")
                except httpx.HTTPError:
                    continue
        return check.warn(
            f"not reachable at {config.llm.base_url} — the bot trades fine without it. "
            "Start it with: ./scripts/setup_llm.sh serve"
        )
    except Exception as exc:  # noqa: BLE001
        return check.warn(str(exc))


async def _check_wallet(config: Config) -> list[Check]:
    checks = []
    if config.role != "brain":
        return checks

    check = Check("Wallet keystore")
    path = config.resolve(config.paths.keystore_path)

    if not path.exists():
        if config.is_live:
            check.fail(f"no keystore at {path} — run: mctb keystore create")
        else:
            check.ok("not needed in paper mode")
        checks.append(check)
        return checks

    try:
        import stat

        from .keystore import derive_pubkey, load_keystore

        mode = path.stat().st_mode
        if mode & (stat.S_IRGRP | stat.S_IROTH):
            checks.append(
                Check("Keystore permissions").warn(
                    f"{path} is readable by other users — run: chmod 600 {path}"
                )
            )
        else:
            checks.append(Check("Keystore permissions").ok("owner-only"))

        pubkey = derive_pubkey(load_keystore(path, config.keystore_passphrase))
        check.ok(pubkey)
        checks.append(check)

        balance_check = Check("Wallet balance")
        try:
            from .solana.rpc import SolanaRPC

            async with SolanaRPC(config.rpc.url, config.rpc.fallback_url) as rpc:
                balance = await rpc.get_balance_sol(pubkey)
            needed = config.risk.min_sol_reserve + config.risk.max_position_sol

            if balance == 0:
                balance_check.fail(
                    f"wallet is empty. Fund {pubkey} before trading live.",
                    fatal=config.is_live,
                )
            elif balance < needed:
                balance_check.warn(
                    f"{balance:.4f} SOL is below reserve + one position ({needed:.4f} SOL)"
                )
            else:
                balance_check.ok(f"{balance:.4f} SOL")
        except Exception as exc:  # noqa: BLE001
            balance_check.warn(f"could not read balance: {exc}")
        checks.append(balance_check)

    except Exception as exc:  # noqa: BLE001
        check.fail(str(exc), fatal=config.is_live)
        checks.append(check)

    return checks


async def _check_paths(config: Config) -> list[Check]:
    checks = []
    for label, path_str in [
        ("data directory", config.paths.data_dir),
        ("log directory", config.paths.log_dir),
    ]:
        check = Check(label)
        path = config.resolve(path_str)
        try:
            path.mkdir(parents=True, exist_ok=True)
            probe = path / ".mctb_write_test"
            probe.write_text("ok")
            probe.unlink()
            check.ok(str(path))
        except Exception as exc:  # noqa: BLE001
            check.fail(f"not writable: {exc}")
        checks.append(check)

    disk = Check("Disk space")
    try:
        usage = shutil.disk_usage(config.resolve(config.paths.data_dir))
        free_gb = usage.free / 1e9
        if free_gb < 0.5:
            disk.fail(f"only {free_gb:.1f} GB free — logs and the database will fail")
        elif free_gb < 2:
            disk.warn(f"{free_gb:.1f} GB free")
        else:
            disk.ok(f"{free_gb:.1f} GB free")
    except Exception as exc:  # noqa: BLE001
        disk.warn(str(exc))
    checks.append(disk)

    return checks


def _check_config_sanity(config: Config) -> list[Check]:
    checks = []

    mode = Check("Execution mode")
    if config.is_live:
        mode.warn("LIVE — real money will be spent")
    elif config.execution.mode == "live":
        mode.warn("mode is 'live' but live_trading_enabled is false → running as PAPER")
    else:
        mode.ok("paper (no real money at risk)")
    checks.append(mode)

    kill = Check("Kill switch")
    path = config.resolve(config.risk.kill_switch_file)
    if path.exists():
        kill.warn(f"engaged — {path} exists. No entries will be taken until you remove it.")
    else:
        kill.ok("clear")
    checks.append(kill)

    risk = Check("Risk limits")
    issues = []
    if config.risk.max_position_sol > 0.5:
        issues.append(f"max_position_sol is {config.risk.max_position_sol} SOL — that is large")
    if config.risk.daily_loss_limit_sol > config.risk.max_position_sol * 20:
        issues.append("daily_loss_limit_sol allows a lot of losing trades before it stops you")
    if config.exits.stop_loss_pct > 0.6:
        issues.append(f"stop_loss_pct of {config.exits.stop_loss_pct:.0%} is very wide")
    if config.execution.buy_slippage_pct > 30:
        issues.append(
            f"buy_slippage_pct of {config.execution.buy_slippage_pct}% invites sandwich attacks"
        )
    risk.warn("; ".join(issues)) if issues else risk.ok(
        f"max {config.risk.max_position_sol} SOL/position, "
        f"{config.risk.daily_loss_limit_sol} SOL daily loss limit"
    )
    checks.append(risk)

    strategy = Check("Strategy")
    strategy.ok(
        f"{config.strategy_preset or 'custom'}, entry threshold "
        f"{config.scoring.entry_threshold:.0f}, "
        f"stop {config.exits.stop_loss_pct:.0%}, "
        f"{len(config.exits.take_profit_ladder)} take-profit rungs"
    )
    checks.append(strategy)

    return checks


async def run_preflight(config: Config) -> bool:
    """Run every check. Returns False if anything fatal failed."""
    out.print(f"\n[bold]MCTB preflight — role: {config.role}[/bold]\n")

    checks: list[Check] = [await _check_python()]
    checks += await _check_packages(config)
    checks += _check_config_sanity(config)
    checks += await _check_paths(config)
    checks.append(await _check_clock())
    checks.append(await _check_redis(config))
    checks += await _check_rpc(config)

    if config.role in ("scout", "brain"):
        checks.append(await _check_pumpportal(config))
    if config.role == "brain":
        checks.append(await _check_llm(config))
        checks += await _check_wallet(config)

    table = Table(show_header=True, header_style="bold")
    table.add_column("", width=6)
    table.add_column("Check", width=26)
    table.add_column("Detail")

    for check in checks:
        colour = {"PASS": "green", "WARN": "yellow", "FAIL": "red"}[check.status]
        table.add_row(f"[{colour}]{check.status}[/]", check.name, check.detail)
    out.print(table)

    failures = [c for c in checks if c.status == "FAIL"]
    fatal = [c for c in failures if c.fatal]
    warnings = [c for c in checks if c.status == "WARN"]

    out.print(
        f"\n{len(checks) - len(failures) - len(warnings)} passed, "
        f"{len(warnings)} warnings, {len(failures)} failed"
    )

    if fatal:
        out.print("\n[red bold]Not ready to run.[/red bold] Fix these first:")
        for check in fatal:
            out.print(f"  [red]•[/red] {check.name}: {check.detail}")
        return False

    if failures:
        out.print("\n[yellow]Non-fatal failures — the affected features will be unavailable.[/yellow]")

    if config.is_live:
        out.print(
            "\n[red bold]LIVE MODE.[/red bold] Real SOL will be spent. "
            "If you have not run at least 100 paper trades with these settings, stop and do that first."
        )
    else:
        out.print("\n[green]Ready.[/green] Running in paper mode — nothing at risk.")

    return True
