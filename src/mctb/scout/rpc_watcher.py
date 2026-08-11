"""Independent data path straight from a Solana RPC node.

The point of this module is that it shares nothing with PumpPortal. If that
service goes down, rate limits us, or starts reporting stale prices, this keeps
producing data — and while both are up, we cross-check one against the other.

The spec called for "automatic fallback and cross-provider validation". This
is that, and it also does something quietly important: it is the authority for
*price*. Feed prices can lag; the bonding curve account is the chain's own
answer, so every position is marked against a curve read rather than a
third-party quote.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections.abc import Callable
from typing import Any

import websockets

from ..models import NewTokenEvent
from ..solana.curve import PUMP_PROGRAM_ID
from ..solana.rpc import SolanaRPC

log = logging.getLogger(__name__)


class RpcWatcher:
    """Watches pump.fun program logs and prices tokens directly from chain state."""

    def __init__(
        self,
        ws_url: str,
        rpc: SolanaRPC,
        on_new_token: Callable[[NewTokenEvent], Any] | None = None,
        enabled: bool = True,
    ) -> None:
        self.ws_url = ws_url
        self.rpc = rpc
        self.on_new_token = on_new_token
        self.enabled = enabled
        self._running = False
        self._seen: dict[str, float] = {}

        self.logs_received = 0
        self.last_log_ts = 0.0
        self.connected = False

    async def run(self) -> None:
        """Subscribe to pump.fun program logs.

        Note this is a firehose. We do minimal work per message and only
        escalate to a full RPC read for logs that look like a token creation,
        because doing otherwise will exhaust any RPC quota in minutes.
        """
        if not self.enabled:
            log.info("RPC watcher disabled")
            return

        self._running = True
        backoff = 1.0

        while self._running:
            try:
                log.info("connecting RPC log watcher to %s", self.ws_url)
                async with websockets.connect(
                    self.ws_url, ping_interval=25, ping_timeout=25, max_queue=4096
                ) as ws:
                    await ws.send(
                        json.dumps(
                            {
                                "jsonrpc": "2.0",
                                "id": 1,
                                "method": "logsSubscribe",
                                "params": [
                                    {"mentions": [PUMP_PROGRAM_ID]},
                                    {"commitment": "processed"},
                                ],
                            }
                        )
                    )
                    self.connected = True
                    backoff = 1.0
                    log.info("RPC log watcher connected")

                    async for raw in ws:
                        try:
                            await self._handle(json.loads(raw))
                        except Exception as exc:  # noqa: BLE001
                            log.debug("log handling error: %s", exc)

            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                log.warning("RPC log watcher dropped (%s); retrying in %.0fs", exc, backoff)
            finally:
                self.connected = False

            if self._running:
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 60.0)

    async def _handle(self, msg: dict[str, Any]) -> None:
        params = msg.get("params")
        if not params:
            return
        value = params.get("result", {}).get("value", {})
        logs: list[str] = value.get("logs", []) or []
        signature: str = value.get("signature", "")
        if not logs or value.get("err"):
            return

        self.logs_received += 1
        self.last_log_ts = time.time()

        # The program emits this exact log line on token creation.
        if not any("Instruction: Create" in line for line in logs):
            return
        if signature in self._seen:
            return
        self._seen[signature] = time.time()
        self._prune_seen()

        # The log alone does not carry the mint, so resolve it from the
        # transaction. This is one extra RPC read per *creation*, not per log.
        asyncio.create_task(self._resolve_creation(signature))

    async def _resolve_creation(self, signature: str) -> None:
        try:
            # Give the node a moment to make the transaction queryable.
            await asyncio.sleep(0.8)
            tx = await self.rpc.get_transaction(signature)
            if not tx:
                return

            mint, creator = self._extract_mint_and_creator(tx)
            if not mint:
                return

            curve = await self.rpc.get_bonding_curve(mint, commitment="processed")
            event = NewTokenEvent(
                source="rpc_watcher",
                mint=mint,
                creator=creator or (curve.creator if curve else "") or "",
                sol_in_curve=curve.sol_in_curve if curve else 0.0,
                market_cap_sol=curve.market_cap_sol if curve else 0.0,
                signature=signature,
            )
            if self.on_new_token:
                result = self.on_new_token(event)
                if asyncio.iscoroutine(result):
                    await result
        except Exception as exc:  # noqa: BLE001
            log.debug("could not resolve creation %s: %s", signature[:16], exc)

    @staticmethod
    def _extract_mint_and_creator(tx: dict[str, Any]) -> tuple[str, str]:
        """Pull the new mint and its creator out of a creation transaction.

        The mint is the account that goes from zero to a nonzero token supply,
        which shows up in postTokenBalances but not preTokenBalances.
        """
        meta = tx.get("meta", {}) or {}
        message = tx.get("transaction", {}).get("message", {}) or {}

        pre = {b.get("mint") for b in (meta.get("preTokenBalances") or [])}
        post = [b.get("mint") for b in (meta.get("postTokenBalances") or [])]
        new_mints = [m for m in post if m and m not in pre]

        creator = ""
        accounts = message.get("accountKeys") or []
        if accounts:
            first = accounts[0]
            creator = first.get("pubkey", "") if isinstance(first, dict) else str(first)

        return (new_mints[0] if new_mints else ""), creator

    def _prune_seen(self, max_age_s: float = 600.0) -> None:
        cutoff = time.time() - max_age_s
        self._seen = {sig: ts for sig, ts in self._seen.items() if ts > cutoff}

    async def stop(self) -> None:
        self._running = False


class PriceOracle:
    """Authoritative price source: reads bonding curve accounts directly.

    Batches every open position into one `getMultipleAccounts` call, because
    pricing five positions with five separate reads every second is the
    fastest way to get rate limited off your own RPC endpoint.
    """

    def __init__(self, rpc: SolanaRPC, commitment: str = "processed") -> None:
        self.rpc = rpc
        self.commitment = commitment
        self._cache: dict[str, tuple[float, float]] = {}  # mint -> (ts, price)
        self.failures = 0

    async def price(self, mint: str, max_age_s: float = 2.0) -> float:
        cached = self._cache.get(mint)
        if cached and (time.time() - cached[0]) <= max_age_s:
            return cached[1]
        prices = await self.prices([mint])
        return prices.get(mint, 0.0)

    async def prices(self, mints: list[str]) -> dict[str, float]:
        """Price many mints in one round trip. A mint whose curve has vanished
        (migrated to a DEX) returns 0.0, which downstream code treats as a
        stale-price exit rather than as a real price of zero."""
        if not mints:
            return {}
        out: dict[str, float] = {}
        try:
            curves = await self.rpc.get_bonding_curves(mints, commitment=self.commitment)
            now = time.time()
            for mint, curve in curves.items():
                price = curve.price_sol if curve else 0.0
                out[mint] = price
                if price > 0:
                    self._cache[mint] = (now, price)
        except Exception as exc:  # noqa: BLE001
            self.failures += 1
            log.warning("price read failed for %d mint(s): %s", len(mints), exc)
            # Fall back to the last known price so one bad read does not
            # trigger every stop loss at once.
            for mint in mints:
                cached = self._cache.get(mint)
                out[mint] = cached[1] if cached else 0.0
        return out

    async def quote_sell(self, mint: str, tokens: float) -> float:
        """What this many tokens would actually fetch, including price impact.

        Spot price times quantity is a lie on a thin curve; this is what you
        would really receive.
        """
        try:
            curve = await self.rpc.get_bonding_curve(mint, commitment=self.commitment)
            return curve.sol_out_for_tokens(tokens) if curve else 0.0
        except Exception:  # noqa: BLE001
            return 0.0

    def last_known(self, mint: str) -> tuple[float, float] | None:
        return self._cache.get(mint)


async def cross_validate(
    feed_price: float, chain_price: float, tolerance_pct: float = 25.0
) -> tuple[bool, str]:
    """Compare a feed-reported price against the chain's own answer.

    A large disagreement means one source is stale or wrong. During the
    volatility that matters most, that is exactly when it happens — so we
    prefer the chain and say so.
    """
    if feed_price <= 0 or chain_price <= 0:
        return True, ""
    divergence = abs(feed_price - chain_price) / chain_price * 100.0
    if divergence > tolerance_pct:
        return False, (
            f"price sources disagree by {divergence:.0f}% "
            f"(feed {feed_price:.10f} vs chain {chain_price:.10f}); trusting chain"
        )
    return True, ""
