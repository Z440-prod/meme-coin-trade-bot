"""Unified command line: `mctb <command>`.

One entry point on all three devices; `--role` (or the service file) decides
what the process actually becomes.
"""

from __future__ import annotations

import asyncio
import getpass
import json
import sys
from pathlib import Path

import typer
from rich.console import Console as RichConsole
from rich.table import Table

from .config import REPO_ROOT, load_config

app = typer.Typer(
    add_completion=False,
    help="MCTB — distributed memecoin sniping system (2x Raspberry Pi + host PC)",
    no_args_is_help=True,
)
keystore_app = typer.Typer(help="Manage the encrypted wallet keystore")
app.add_typer(keystore_app, name="keystore")

out = RichConsole()

ConfigOpt = typer.Option(None, "--config", "-c", help="Path to config.yaml")


def _load(config_path: Path | None, role: str | None = None):
    try:
        return load_config(config_path, role=role)
    except FileNotFoundError as exc:
        out.print(f"[red]{exc}[/red]")
        raise typer.Exit(1) from exc
    except Exception as exc:  # noqa: BLE001
        out.print(f"[red]Config error:[/red] {exc}")
        raise typer.Exit(1) from exc


# --------------------------------------------------------------------------
#  Services
# --------------------------------------------------------------------------


def _boot(cfg) -> None:
    """Apply runtime tuning before any event loop is created."""
    from .runtime import apply

    if apply(gc_tuning=cfg.tune_gc, uvloop_enabled=cfg.use_uvloop).get("uvloop"):
        out.print("[dim]uvloop active[/dim]")


@app.command()
def scout(config: Path | None = ConfigOpt) -> None:
    """Run the Scout (RPi 1) — data collection and screening."""
    from .scout.main import run_scout

    cfg = _load(config, "scout")
    _boot(cfg)
    asyncio.run(run_scout(cfg))


@app.command()
def brain(config: Path | None = ConfigOpt) -> None:
    """Run the Brain (RPi 2) — scoring, execution and position management."""
    from .brain.main import run_brain

    cfg = _load(config, "brain")
    _boot(cfg)
    asyncio.run(run_brain(cfg))


@app.command()
def console(config: Path | None = ConfigOpt) -> None:
    """Run the Console (host PC) — dashboard, terminal and supervision."""
    from .console.main import run_console

    cfg = _load(config, "console")
    _boot(cfg)
    asyncio.run(run_console(cfg))


@app.command(name="all")
def run_all(config: Path | None = ConfigOpt) -> None:
    """Run all three services in one process.

    Useful for testing on a single machine before you have the Pis wired up.
    Not what you want in production — the whole point of three devices is that
    one crashing does not take the others with it.
    """
    from .brain.main import Brain
    from .console.main import run_console
    from .scout.main import Scout

    cfg = _load(config)
    out.print("[yellow]Running all three services in one process (development mode).[/yellow]")

    async def main() -> None:
        scout_cfg = cfg.model_copy(deep=True)
        scout_cfg.role = "scout"
        brain_cfg = cfg.model_copy(deep=True)
        brain_cfg.role = "brain"
        console_cfg = cfg.model_copy(deep=True)
        console_cfg.role = "console"

        from .logging_setup import setup_logging

        setup_logging(cfg.log_level, cfg.resolve(cfg.paths.log_dir), "all")

        await asyncio.gather(
            Scout(scout_cfg).run(),
            Brain(brain_cfg).run(),
            run_console(console_cfg),
        )

    asyncio.run(main())


# --------------------------------------------------------------------------
#  Keystore
# --------------------------------------------------------------------------


@keystore_app.command("create")
def keystore_create(
    path: Path | None = typer.Option(None, help="Where to write the keystore"),
    config: Path | None = ConfigOpt,
) -> None:
    """Encrypt a Solana private key into a keystore file.

    Run this ONLY on the Brain (RPi 2). Nothing is echoed to the terminal and
    the key is never written to disk unencrypted.
    """
    from .keystore import create_keystore

    cfg = _load(config)
    target = path or cfg.resolve(cfg.paths.keystore_path)

    if Path(target).exists():
        if not typer.confirm(f"{target} already exists. Overwrite it?"):
            raise typer.Exit(0)

    out.print("\n[bold]Paste the private key of the wallet this bot will trade with.[/bold]")
    out.print("[yellow]Use a dedicated wallet holding only what you can afford to lose.[/yellow]")
    out.print("[dim]In Phantom: Settings → Manage Accounts → (account) → Show Private Key.[/dim]")
    out.print("[dim]It is a long base58 string, NOT your seed phrase.[/dim]\n")

    private_key = getpass.getpass("Private key (hidden): ").strip()
    if not private_key:
        out.print("[red]Nothing entered.[/red]")
        raise typer.Exit(1)

    passphrase = getpass.getpass("Choose a passphrase: ")
    if len(passphrase) < 8:
        out.print("[red]Use at least 8 characters.[/red]")
        raise typer.Exit(1)
    if passphrase != getpass.getpass("Confirm passphrase: "):
        out.print("[red]Passphrases do not match.[/red]")
        raise typer.Exit(1)

    try:
        result = create_keystore(private_key, passphrase, target)
    except Exception as exc:  # noqa: BLE001
        out.print(f"[red]Failed:[/red] {exc}")
        raise typer.Exit(1) from exc

    out.print(f"\n[green]Keystore written to {result['path']}[/green]")
    out.print(f"[bold]Wallet public key:[/bold] {result['pubkey']}")
    out.print("\nFund this address with the SOL you intend to trade — and nothing more.")
    out.print("[dim]For unattended restarts, put the passphrase in .env as")
    out.print("MCTB_KEYSTORE_PASSPHRASE and run: chmod 600 .env[/dim]")


@keystore_app.command("show")
def keystore_show(config: Path | None = ConfigOpt) -> None:
    """Show the public key and balance of the configured wallet."""
    from .keystore import derive_pubkey, load_keystore
    from .solana.rpc import SolanaRPC

    cfg = _load(config)
    path = cfg.resolve(cfg.paths.keystore_path)

    try:
        pubkey = derive_pubkey(load_keystore(path, cfg.keystore_passphrase))
    except Exception as exc:  # noqa: BLE001
        out.print(f"[red]{exc}[/red]")
        raise typer.Exit(1) from exc

    out.print(f"[bold]Wallet:[/bold] {pubkey}")

    async def balance() -> None:
        async with SolanaRPC(cfg.rpc.url, cfg.rpc.fallback_url) as rpc:
            out.print(f"[bold]Balance:[/bold] {await rpc.get_balance_sol(pubkey):.6f} SOL")

    try:
        asyncio.run(balance())
    except Exception as exc:  # noqa: BLE001
        out.print(f"[yellow]Could not read balance: {exc}[/yellow]")


# --------------------------------------------------------------------------
#  Operations
# --------------------------------------------------------------------------


@app.command()
def preflight(config: Path | None = ConfigOpt) -> None:
    """Check every dependency and endpoint before you trade."""
    from .preflight import run_preflight

    cfg = _load(config)
    if not asyncio.run(run_preflight(cfg)):
        raise typer.Exit(1)


@app.command()
def kill(config: Path | None = ConfigOpt, reason: str = "manual") -> None:
    """Engage the kill switch — stops all new entries immediately."""
    from .bus import Bus

    cfg = _load(config)

    async def main() -> None:
        bus = Bus(cfg.bus.redis_url, cfg.bus.redis_password, source="cli")
        await bus.connect()
        await bus.set_kill_switch(True, reason)
        await bus.close()

    asyncio.run(main())
    # Also drop the file, so the halt survives Redis being restarted.
    cfg.resolve(cfg.risk.kill_switch_file).touch()
    out.print("[red bold]KILL SWITCH ENGAGED[/red bold] — no new entries.")
    out.print("Open positions keep their exit rules. Use [bold]mctb close-all[/bold] to exit them.")


@app.command()
def resume(config: Path | None = ConfigOpt) -> None:
    """Clear the kill switch."""
    from .bus import Bus

    cfg = _load(config)

    async def main() -> None:
        bus = Bus(cfg.bus.redis_url, cfg.bus.redis_password, source="cli")
        await bus.connect()
        await bus.set_kill_switch(False)
        from .models import ControlCommand, Topic

        await bus.publish(Topic.CONTROL, ControlCommand(source="cli", command="resume"))
        await bus.close()

    asyncio.run(main())
    path = cfg.resolve(cfg.risk.kill_switch_file)
    if path.exists():
        path.unlink()
    out.print("[green]Kill switch cleared.[/green]")


@app.command(name="close-all")
def close_all(config: Path | None = ConfigOpt) -> None:
    """Sell every open position at market, right now."""
    from .bus import Bus
    from .models import ControlCommand, Topic

    cfg = _load(config)
    if not typer.confirm("Sell EVERY open position at market?"):
        raise typer.Exit(0)

    async def main() -> None:
        bus = Bus(cfg.bus.redis_url, cfg.bus.redis_password, source="cli")
        await bus.connect()
        await bus.publish(Topic.CONTROL, ControlCommand(source="cli", command="close_all"))
        await bus.close()

    asyncio.run(main())
    out.print("[yellow]Close-all sent. Watch the dashboard for the fills.[/yellow]")


@app.command()
def stats(
    config: Path | None = ConfigOpt,
    edge: bool = typer.Option(False, "--edge", help="Show the learned feature table"),
    limit: int = typer.Option(20, help="Recent trades to list"),
) -> None:
    """Show performance and what the bot has learned."""
    from .db import Database

    cfg = _load(config)

    async def main() -> None:
        db = Database(cfg.resolve(cfg.paths.db_file))
        await db.connect()
        try:
            perf = await db.performance_summary()
            if not perf.get("trades"):
                out.print("[yellow]No closed trades yet.[/yellow]")
                return

            table = Table(title="Performance", show_header=False)
            table.add_column("Metric", style="dim")
            table.add_column("Value", justify="right")
            pf = perf["profit_factor"]
            for label, value in [
                ("Trades", str(perf["trades"])),
                ("Win rate", f"{perf['win_rate']:.1%}"),
                ("Total P&L", f"{perf['total_pnl_sol']:+.4f} SOL"),
                ("Average per trade", f"{perf['avg_pnl_sol']:+.4f} SOL"),
                ("Profit factor", "∞" if pf == float("inf") else f"{pf:.2f}"),
                ("Max drawdown", f"{perf['max_drawdown_sol']:.4f} SOL"),
                ("Sharpe per trade", f"{perf['sharpe_per_trade']:.2f}"),
                ("Best trade", f"{perf['best_sol']:+.4f} SOL"),
                ("Worst trade", f"{perf['worst_sol']:+.4f} SOL"),
                ("Fees paid", f"{perf['total_fees_sol']:.4f} SOL"),
                ("Average hold", f"{perf['avg_hold_s']:.0f}s"),
            ]:
                table.add_row(label, value)
            out.print(table)

            reasons = Table(title="Exit reasons")
            reasons.add_column("Reason")
            reasons.add_column("Count", justify="right")
            for reason, count in sorted(
                perf["exit_reasons"].items(), key=lambda kv: kv[1], reverse=True
            ):
                reasons.add_row(reason, str(count))
            out.print(reasons)

            if edge:
                rows = await db.edge_stats()
                learned = Table(title="Learned edge (buckets with 5+ trades)")
                learned.add_column("Feature")
                learned.add_column("Bucket")
                learned.add_column("N", justify="right")
                learned.add_column("Win rate", justify="right")
                learned.add_column("Avg P&L", justify="right")
                items = [
                    (f, b, v)
                    for (f, b), v in rows.items()
                    if v["trades"] >= 5
                ]
                items.sort(key=lambda i: i[2]["pnl_sol"] / i[2]["trades"], reverse=True)
                for feature, bucket, value in items[:30]:
                    avg = 100 * value["pnl_sol"] / value["trades"]
                    learned.add_row(
                        feature,
                        bucket,
                        str(value["trades"]),
                        f"{value['wins'] / value['trades']:.0%}",
                        f"[{'green' if avg >= 0 else 'red'}]{avg:+.1f}%[/]",
                    )
                out.print(learned)

            recent = await db.closed_positions(limit)
            if recent:
                trades = Table(title=f"Last {len(recent)} trades")
                trades.add_column("Token")
                trades.add_column("P&L", justify="right")
                trades.add_column("%", justify="right")
                trades.add_column("Held", justify="right")
                trades.add_column("Score", justify="right")
                trades.add_column("Exit")
                for row in recent:
                    pnl = row["pnl_sol"] or 0.0
                    colour = "green" if pnl > 0 else "red"
                    held = (row["closed_at"] or 0) - (row["opened_at"] or 0)
                    trades.add_row(
                        row["symbol"] or row["mint"][:10],
                        f"[{colour}]{pnl:+.4f}[/]",
                        f"[{colour}]{100 * (row['pnl_pct'] or 0):+.0f}%[/]",
                        f"{held:.0f}s",
                        f"{row['entry_score'] or 0:.0f}",
                        (row["exit_reason"] or "")[:24],
                    )
                out.print(trades)
        finally:
            await db.close()

    asyncio.run(main())


@app.command()
def review(config: Path | None = ConfigOpt) -> None:
    """Run the LLM trade review now instead of waiting for tonight."""
    from .brain.main import Brain

    cfg = _load(config, "brain")

    async def main() -> None:
        brain_instance = Brain(cfg)
        await brain_instance.start()
        try:
            result = await brain_instance.run_review()
            if result is None:
                out.print("[yellow]No review produced — not enough trades, or the LLM is offline.[/yellow]")
            else:
                out.print(json.dumps(result, indent=2))
        finally:
            await brain_instance.stop()

    asyncio.run(main())


@app.command()
def config_show(config: Path | None = ConfigOpt) -> None:
    """Print the fully resolved configuration, with secrets masked."""
    cfg = _load(config)
    data = cfg.model_dump()
    for key in (
        "pumpportal_api_key",
        "x_bearer_token",
        "telegram_bot_token",
        "telegram_chat_id",
        "keystore_passphrase",
    ):
        if data.get(key):
            data[key] = "***set***"
    if data.get("bus", {}).get("redis_password"):
        data["bus"]["redis_password"] = "***set***"
    out.print_json(json.dumps(data, default=str))


@app.command()
def simulate(
    config: Path | None = ConfigOpt,
    tokens: int = typer.Option(60, help="Synthetic launches to run"),
    seed: int = typer.Option(42, help="Random seed, for repeatable runs"),
) -> None:
    """Run the full pipeline against a synthetic market — no network needed.

    This exercises gates, scoring, sizing, exits and the learning layer end to
    end. Use it to sanity-check a config change before pointing it at a live
    feed, and to see roughly how often your settings would actually fire.
    """
    from .simulator import run_simulation

    cfg = _load(config, "brain")
    asyncio.run(run_simulation(cfg, tokens=tokens, seed=seed))


@app.command()
def providers(config: Path | None = ConfigOpt, probe: bool = False) -> None:
    """Show every data provider, its status and what it can answer."""
    from .providers import ProviderRegistry

    cfg = _load(config)

    async def main_() -> None:
        registry = ProviderRegistry(cfg)
        registry.build()
        await registry.start()
        try:
            probe_results: dict[str, bool] = {}
            if probe:
                out.print("[dim]probing live endpoints…[/dim]")
                probe_results = await registry.probe_all()

            table = Table(title="Data providers")
            table.add_column("Provider")
            table.add_column("Status")
            table.add_column("Tier")
            table.add_column("Cost")
            table.add_column("Capabilities", overflow="fold")
            if probe:
                table.add_column("Live")

            for row in registry.describe():
                colour = {
                    "ready": "green", "unconfigured": "yellow",
                    "no_public_api": "dim", "disabled": "dim",
                }.get(row["status"], "red")
                cells = [
                    row["display_name"],
                    f"[{colour}]{row['status']}[/]",
                    row["tier"],
                    "paid" if row["paid"] else "free",
                    ", ".join(c.replace("token_", "").replace("wallet_", "w:")
                              for c in row["capabilities"][:5]) or "—",
                ]
                if probe:
                    result = probe_results.get(row["name"])
                    cells.append(
                        "[green]ok[/]" if result else ("[red]fail[/]" if result is False else "—")
                    )
                table.add_row(*cells)
            out.print(table)

            coverage = registry.coverage_report()
            out.print(
                f"\n[bold]{coverage['ready']}[/bold] ready · "
                f"{coverage['unconfigured']} awaiting keys · "
                f"{coverage['link_only']} link-only"
            )
            if coverage["critical_gaps"]:
                out.print(f"[red]Critical gaps:[/red] {', '.join(coverage['critical_gaps'])}")
            else:
                out.print("[green]All critical capabilities are covered.[/green]")
        finally:
            await registry.close()

    asyncio.run(main_())


@app.command()
def token(mint: str, config: Path | None = ConfigOpt, deep: bool = False) -> None:
    """Assemble and print an intelligence dossier for one token."""
    from .intel import IntelEngine
    from .providers import ProviderRegistry, Tier
    from .solana.rpc import SolanaRPC

    cfg = _load(config)

    async def main_() -> None:
        registry = ProviderRegistry(cfg)
        registry.build()
        await registry.start()
        # Without an RPC, a token still on its bonding curve reports $0
        # liquidity here — no DEX pool exists for it yet.
        rpc = SolanaRPC(cfg.rpc.url, cfg.rpc.fallback_url, cfg.rpc.timeout_s)
        await rpc.connect()
        engine = IntelEngine(registry, rpc=rpc)
        try:
            dossier = await engine.assemble(
                mint, tier=Tier.MANUAL if deep else Tier.COLD,
                timeout_s=20.0 if deep else 8.0,
            )
            sec, mk = dossier.security, dossier.market

            out.print(
                f"\n[bold]{dossier.symbol or mint[:12]}[/bold]  {mint}\n"
                f"[dim]assembled in {dossier.assembly_ms:.0f}ms at {dossier.tier_used} tier[/dim]\n"
            )

            if sec.blockers:
                out.print("[red bold]NOT TRADEABLE[/red bold]")
                for blocker in sec.blockers:
                    out.print(f"  [red]•[/red] {blocker}")
            elif sec.provenance.get("sources"):
                out.print("[green]No blockers found[/green]")
            else:
                out.print("[yellow]No security provider could screen this token[/yellow]")

            table = Table(show_header=False, box=None)
            table.add_column("k", style="dim")
            table.add_column("v", justify="right")
            for label, value in [
                ("Price", f"${mk.price_usd:.10f}" if mk.price_usd else "—"),
                ("Liquidity", f"${mk.liquidity_usd:,.0f}"),
                ("Market cap", f"${mk.market_cap:,.0f}"),
                ("Volume 24h", f"${mk.volume_24h:,.0f}"),
                ("Vol / Liq", f"{mk.volume_to_liquidity:.1f}x"),
                ("Wash suspicion", f"{mk.wash_trade_suspicion:.0%}"),
                ("Unique 1h", str(mk.unique_wallets_1h)),
                ("Risk score", f"{sec.risk_score:.0f}/100"),
                ("Confidence", sec.provenance.get("confidence", "none")),
                ("Sources", ", ".join(sec.provenance.get("sources", [])) or "none"),
                ("Mint authority", "LIVE" if sec.mint_authority_live else "revoked"),
                ("Freeze authority", "LIVE" if sec.freeze_authority_live else "revoked"),
                ("Sellable", "yes" if sec.sellable else "NO"),
                ("Top 10 hold", f"{sec.top10_holding_pct:.1%}"),
            ]:
                table.add_row(label, value)
            out.print(table)

            if sec.risk_flags:
                out.print(f"\n[yellow]Flags:[/yellow] {', '.join(sec.risk_flags[:10])}")
            for warning in dossier.cluster_warnings:
                out.print(f"[yellow]⚠[/yellow]  {warning}")

            out.print("\n[dim]" + "  ".join(
                f"{k}: {v}" for k, v in list(dossier.links.items())[:4]
            ) + "[/dim]")
        finally:
            await rpc.close()
            await registry.close()

    asyncio.run(main_())


@app.command()
def webhook_test(
    url: str,
    action: str = "signal",
    mint: str = "",
    config: Path | None = ConfigOpt,
) -> None:
    """Send a signed test webhook, to verify your automation wiring."""
    import httpx
    import orjson

    from .automation import sign_payload

    cfg = _load(config)
    secret = cfg.webhooks.secret or cfg.webhook_secret
    if not secret:
        out.print("[red]No webhook secret configured. Set MCTB_WEBHOOK_SECRET.[/red]")
        raise typer.Exit(1)

    body = orjson.dumps({"action": action, "mint": mint, "reason": "cli test"})
    headers = sign_payload(secret, body)
    headers["Content-Type"] = "application/json"

    try:
        resp = httpx.post(url.rstrip("/") + f"/webhook/{secret}", content=body,
                          headers=headers, timeout=15.0)
        out.print(f"HTTP {resp.status_code}")
        out.print_json(resp.text)
    except Exception as exc:  # noqa: BLE001
        out.print(f"[red]Request failed:[/red] {exc}")
        raise typer.Exit(1) from exc


def main() -> None:
    # Make `src/` importable when running from a source checkout.
    src = REPO_ROOT / "src"
    if src.exists() and str(src) not in sys.path:
        sys.path.insert(0, str(src))
    app()


if __name__ == "__main__":
    main()
