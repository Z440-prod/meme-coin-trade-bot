"""Trade execution: paper simulation and live on-chain signing.

Both backends implement the same interface, so every other module is identical
in paper and live mode. That is deliberate — the code you test is the code
that trades, and switching modes changes exactly one object.

**Live signing is non-custodial.** PumpPortal's `trade-local` endpoint returns
an *unsigned* serialised transaction. We sign it here, on the Brain, with a key
that never leaves the machine, and submit it to an RPC node ourselves. The
alternative "lightning" API holds your keys on someone else's server; this
system does not implement that, and you should not use it.
"""

from __future__ import annotations

import asyncio
import base64
import logging
import random
import time
from abc import ABC, abstractmethod
from typing import Any

import httpx

from ..config import Config
from ..models import Fill
from ..scout.rpc_watcher import PriceOracle
from ..solana.rpc import SolanaRPC

log = logging.getLogger(__name__)

PUMPPORTAL_TRADE_LOCAL = "https://pumpportal.fun/api/trade-local"


class Executor(ABC):
    """Common interface. `paper` tells everything downstream which one it got."""

    paper: bool = True

    @abstractmethod
    async def buy(self, mint: str, sol_amount: float, symbol: str = "") -> Fill: ...

    @abstractmethod
    async def sell(
        self, mint: str, token_amount: float, symbol: str = "", reason: str = "", urgent: bool = False
    ) -> Fill: ...

    @abstractmethod
    async def bankroll(self) -> float: ...

    async def close(self) -> None:
        return None


class PaperExecutor(Executor):
    """Simulated fills against live on-chain prices.

    The simulation is intentionally pessimistic. Latency, slippage and fees are
    all modelled, because a paper run with none of those will report a
    profitable strategy that loses money the moment it touches a real chain —
    and the whole point of paper trading is to find that out for free.

    Fills are priced through the real bonding curve, so price impact from your
    own order size is included rather than assumed away.
    """

    paper = True

    def __init__(self, config: Config, rpc: SolanaRPC, oracle: PriceOracle) -> None:
        self.config = config
        self.rpc = rpc
        self.oracle = oracle
        self._bankroll = config.execution.paper_starting_bankroll_sol
        self.fills = 0

    async def bankroll(self) -> float:
        return self._bankroll

    def credit(self, sol: float) -> None:
        self._bankroll += sol

    async def _simulate_latency(self) -> None:
        base = self.config.execution.paper_latency_ms / 1000.0
        # Jitter: real networks are not constant, and a strategy that only
        # works at exactly 400ms is not a strategy.
        await asyncio.sleep(max(0.0, random.gauss(base, base * 0.3)))

    async def buy(self, mint: str, sol_amount: float, symbol: str = "") -> Fill:
        started = time.time()
        exec_cfg = self.config.execution

        if sol_amount > self._bankroll:
            return Fill(
                source="paper", mint=mint, symbol=symbol, side="buy", ok=False,
                error=f"insufficient paper bankroll ({self._bankroll:.4f} SOL)",
                paper=True,
            )

        await self._simulate_latency()

        # Price through the actual curve so order size matters.
        tokens = 0.0
        price = 0.0
        try:
            curve = await self.rpc.get_bonding_curve(mint, self.config.rpc.price_commitment)
            if curve:
                tokens = curve.tokens_out_for_sol(sol_amount, apply_fee=True)
                price = curve.price_sol
        except Exception as exc:  # noqa: BLE001
            log.debug("paper buy curve read failed for %s: %s", mint, exc)

        if tokens <= 0 or price <= 0:
            price = await self.oracle.price(mint)
            if price <= 0:
                return Fill(
                    source="paper", mint=mint, symbol=symbol, side="buy", ok=False,
                    error="no price available", paper=True,
                )
            tokens = sol_amount / price

        # Adverse fill on top of curve impact: someone else's transaction
        # landed between your decision and your inclusion.
        slip = 1.0 + (exec_cfg.paper_slippage_pct / 100.0) * random.uniform(0.3, 1.0)
        tokens /= slip

        fee = sol_amount * (exec_cfg.paper_fee_pct / 100.0) + exec_cfg.priority_fee_sol
        self._bankroll -= sol_amount + exec_cfg.priority_fee_sol
        self.fills += 1

        return Fill(
            source="paper",
            mint=mint,
            symbol=symbol,
            side="buy",
            ok=True,
            sol_amount=sol_amount,
            token_amount=tokens,
            price_sol=sol_amount / tokens if tokens else 0.0,
            fee_sol=fee,
            signature=f"paper-buy-{self.fills}",
            paper=True,
            latency_ms=(time.time() - started) * 1000,
        )

    async def sell(
        self, mint: str, token_amount: float, symbol: str = "", reason: str = "", urgent: bool = False
    ) -> Fill:
        started = time.time()
        exec_cfg = self.config.execution

        await self._simulate_latency()

        proceeds = 0.0
        try:
            curve = await self.rpc.get_bonding_curve(mint, self.config.rpc.price_commitment)
            if curve:
                proceeds = curve.sol_out_for_tokens(token_amount, apply_fee=True)
        except Exception as exc:  # noqa: BLE001
            log.debug("paper sell curve read failed for %s: %s", mint, exc)

        if proceeds <= 0:
            price = await self.oracle.price(mint)
            proceeds = token_amount * price

        if proceeds <= 0:
            # A token whose curve has vanished cannot be sold. Modelling this
            # as a total loss is correct — it is what happens in practice.
            self.fills += 1
            return Fill(
                source="paper", mint=mint, symbol=symbol, side="sell", ok=True,
                sol_amount=0.0, token_amount=token_amount, price_sol=0.0,
                fee_sol=exec_cfg.priority_fee_sol, signature=f"paper-sell-{self.fills}",
                paper=True, exit_reason=reason,
                latency_ms=(time.time() - started) * 1000,
                error="no liquidity — position written off",
            )

        # Exits slip worse than entries, and worse again when urgent, because
        # everyone is trying to leave through the same door.
        severity = 1.5 if urgent else 1.0
        slip = 1.0 - (exec_cfg.paper_slippage_pct / 100.0) * random.uniform(0.3, 1.0) * severity
        proceeds *= max(0.1, slip)

        fee_sol = exec_cfg.exit_priority_fee_sol if urgent else exec_cfg.priority_fee_sol
        self._bankroll += proceeds - fee_sol
        self.fills += 1

        return Fill(
            source="paper",
            mint=mint,
            symbol=symbol,
            side="sell",
            ok=True,
            sol_amount=proceeds,
            token_amount=token_amount,
            price_sol=proceeds / token_amount if token_amount else 0.0,
            fee_sol=fee_sol,
            signature=f"paper-sell-{self.fills}",
            paper=True,
            exit_reason=reason,
            latency_ms=(time.time() - started) * 1000,
        )


class LiveExecutor(Executor):
    """Real trades: build via PumpPortal, sign locally, submit to your own RPC."""

    paper = False

    def __init__(self, config: Config, rpc: SolanaRPC, keypair: Any) -> None:
        self.config = config
        self.rpc = rpc
        self.keypair = keypair
        self.pubkey = str(keypair.pubkey())
        self._client: httpx.AsyncClient | None = None
        self._bankroll_cache: tuple[float, float] = (0.0, 0.0)

    async def connect(self) -> None:
        self._client = httpx.AsyncClient(timeout=httpx.Timeout(15.0))
        balance = await self.rpc.get_balance_sol(self.pubkey)
        log.warning(
            "LIVE EXECUTION ARMED — wallet %s holds %.4f SOL. Real money is now at risk.",
            self.pubkey,
            balance,
        )

    async def close(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    async def bankroll(self, max_age_s: float = 10.0) -> float:
        ts, value = self._bankroll_cache
        if time.time() - ts < max_age_s:
            return value
        try:
            balance = await self.rpc.get_balance_sol(self.pubkey)
            self._bankroll_cache = (time.time(), balance)
            return balance
        except Exception as exc:  # noqa: BLE001
            log.warning("balance read failed: %s", exc)
            return value

    async def _build_and_send(
        self,
        action: str,
        mint: str,
        amount: float | str,
        denominated_in_sol: bool,
        slippage_pct: float,
        priority_fee_sol: float,
    ) -> tuple[bool, str, str]:
        """Returns (ok, signature, error)."""
        if self._client is None:
            return False, "", "executor not connected"

        try:
            from solders.transaction import VersionedTransaction
        except ImportError:
            return False, "", "solders not installed (pip install -e '.[trade]')"

        payload = {
            "publicKey": self.pubkey,
            "action": action,
            "mint": mint,
            "amount": amount,
            "denominatedInSol": "true" if denominated_in_sol else "false",
            "slippage": slippage_pct,
            "priorityFee": priority_fee_sol,
            "pool": self.config.execution.pool,
        }

        try:
            resp = await self._client.post(PUMPPORTAL_TRADE_LOCAL, data=payload)
        except Exception as exc:  # noqa: BLE001
            return False, "", f"trade build request failed: {exc}"

        if resp.status_code != 200:
            return False, "", f"trade build returned HTTP {resp.status_code}: {resp.text[:200]}"

        raw = resp.content
        if not raw or raw[:1] == b"{":
            return False, "", f"trade build returned no transaction: {raw[:200]!r}"

        try:
            unsigned = VersionedTransaction.from_bytes(raw)
            # Re-sign the message with our key. The transaction arrives with a
            # placeholder signature; this replaces it with a real one.
            signed = VersionedTransaction(unsigned.message, [self.keypair])
            encoded = base64.b64encode(bytes(signed)).decode()
        except Exception as exc:  # noqa: BLE001
            return False, "", f"could not sign transaction: {exc}"

        try:
            signature = await self.rpc.send_transaction(encoded, skip_preflight=True)
        except Exception as exc:  # noqa: BLE001
            # NOT retried — see SolanaRPC.send_transaction. A duplicate buy is
            # worse than a missed one.
            return False, "", f"submit failed: {exc}"

        return True, signature, ""

    async def buy(self, mint: str, sol_amount: float, symbol: str = "") -> Fill:
        started = time.time()
        ok, signature, error = await self._build_and_send(
            "buy",
            mint,
            sol_amount,
            denominated_in_sol=True,
            slippage_pct=self.config.execution.buy_slippage_pct,
            priority_fee_sol=self.config.execution.priority_fee_sol,
        )
        latency_ms = (time.time() - started) * 1000

        if not ok:
            log.error("live buy of %s failed: %s", symbol or mint[:10], error)
            return Fill(
                source="live", mint=mint, symbol=symbol, side="buy", ok=False,
                error=error, paper=False, latency_ms=latency_ms,
            )

        log.warning("live BUY submitted: %.4f SOL of %s — %s", sol_amount, symbol or mint[:10], signature)

        status = await self.rpc.confirm_transaction(signature, timeout_s=45.0)
        if not status["confirmed"]:
            detail = (
                "timed out — verify on-chain before assuming it failed"
                if status.get("timeout")
                else f"failed on chain: {status.get('err')}"
            )
            return Fill(
                source="live", mint=mint, symbol=symbol, side="buy", ok=False,
                signature=signature, error=detail, paper=False, latency_ms=latency_ms,
            )

        # Read what we actually received rather than trusting the quote.
        tokens = await self._token_balance(mint)
        price = sol_amount / tokens if tokens > 0 else 0.0

        return Fill(
            source="live", mint=mint, symbol=symbol, side="buy", ok=True,
            sol_amount=sol_amount, token_amount=tokens, price_sol=price,
            fee_sol=self.config.execution.priority_fee_sol,
            signature=signature, paper=False,
            latency_ms=(time.time() - started) * 1000,
        )

    async def sell(
        self, mint: str, token_amount: float, symbol: str = "", reason: str = "", urgent: bool = False
    ) -> Fill:
        started = time.time()
        exec_cfg = self.config.execution
        balance_before = await self.rpc.get_balance_sol(self.pubkey)

        fee = exec_cfg.exit_priority_fee_sol if urgent else exec_cfg.priority_fee_sol
        last_error = ""

        # Exits are retried with an escalating fee. Failing to get out is the
        # expensive failure, so it is worth paying up.
        for attempt in range(exec_cfg.exit_max_retries):
            ok, signature, error = await self._build_and_send(
                "sell",
                mint,
                token_amount,
                denominated_in_sol=False,
                slippage_pct=exec_cfg.sell_slippage_pct,
                priority_fee_sol=fee,
            )

            if ok:
                status = await self.rpc.confirm_transaction(signature, timeout_s=45.0)
                if status["confirmed"]:
                    balance_after = await self.rpc.get_balance_sol(self.pubkey)
                    proceeds = max(0.0, balance_after - balance_before)
                    log.warning(
                        "live SELL confirmed: %s for ~%.4f SOL (%s) — %s",
                        symbol or mint[:10], proceeds, reason, signature,
                    )
                    return Fill(
                        source="live", mint=mint, symbol=symbol, side="sell", ok=True,
                        sol_amount=proceeds, token_amount=token_amount,
                        price_sol=proceeds / token_amount if token_amount else 0.0,
                        fee_sol=fee, signature=signature, paper=False,
                        exit_reason=reason,
                        latency_ms=(time.time() - started) * 1000,
                    )
                last_error = f"not confirmed: {status.get('err') or 'timeout'}"
            else:
                last_error = error

            fee *= exec_cfg.exit_retry_fee_multiplier
            log.warning(
                "sell attempt %d/%d failed (%s); retrying with %.5f SOL priority fee",
                attempt + 1, exec_cfg.exit_max_retries, last_error, fee,
            )
            await asyncio.sleep(0.6 * (attempt + 1))

        log.critical(
            "COULD NOT EXIT %s after %d attempts: %s — MANUAL INTERVENTION NEEDED",
            symbol or mint, exec_cfg.exit_max_retries, last_error,
        )
        return Fill(
            source="live", mint=mint, symbol=symbol, side="sell", ok=False,
            token_amount=token_amount, error=last_error, paper=False,
            exit_reason=reason, latency_ms=(time.time() - started) * 1000,
        )

    async def _token_balance(self, mint: str) -> float:
        try:
            accounts = await self.rpc.get_token_accounts_by_owner(self.pubkey, mint)
            total = 0.0
            for account in accounts:
                info = account.get("account", {}).get("data", {}).get("parsed", {}).get("info", {})
                total += float(info.get("tokenAmount", {}).get("uiAmount") or 0.0)
            return total
        except Exception as exc:  # noqa: BLE001
            log.warning("token balance read failed for %s: %s", mint, exc)
            return 0.0


async def build_executor(
    config: Config, rpc: SolanaRPC, oracle: PriceOracle
) -> Executor:
    """Construct the right executor, enforcing both safety latches.

    If live mode is requested but not fully armed, we fall back to paper and
    say so loudly rather than refusing to start — a bot that will not boot is
    a bot whose open positions nobody is managing.
    """
    if not config.is_live:
        if config.execution.mode == "live":
            log.warning(
                "execution.mode is 'live' but execution.live_trading_enabled is false. "
                "Running in PAPER mode. Set both to trade real money."
            )
        log.info("paper executor: starting bankroll %.4f SOL", config.execution.paper_starting_bankroll_sol)
        return PaperExecutor(config, rpc, oracle)

    from ..keystore import keypair_from_keystore

    keystore_path = config.resolve(config.paths.keystore_path)
    keypair = keypair_from_keystore(keystore_path, config.keystore_passphrase)

    executor = LiveExecutor(config, rpc, keypair)
    await executor.connect()

    balance = await executor.bankroll()
    if balance < config.risk.min_sol_reserve + config.risk.max_position_sol:
        log.error(
            "wallet holds %.4f SOL, which is below the reserve (%.4f) plus one position (%.4f). "
            "Fund the wallet or lower risk.max_position_sol.",
            balance, config.risk.min_sol_reserve, config.risk.max_position_sol,
        )
    return executor
