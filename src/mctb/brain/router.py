"""Execution routing — choosing where and how to trade.

A pump.fun token's life has two phases, and they need different execution:

**On the bonding curve.** Only pump.fun can fill you. The curve is a
deterministic price function, so we can compute our own fill exactly and there
is no route to search. PumpPortal builds the transaction.

**After graduation.** The curve is gone and liquidity has moved to a DEX. A
curve-only bot simply cannot trade the token any more — including selling a
position it already holds, which is the dangerous case. Jupiter routes to
wherever the liquidity actually went.

The router picks per trade, and it re-checks on exit rather than assuming the
venue is the one we entered on. Tokens graduate mid-position; a bot that
cannot follow them there is a bot that watches an open position become
unsellable.

It also does one thing that pays for itself: **pre-trade cost estimation.**
Before committing, it asks what a round trip would actually cost — fees plus
price impact both ways. On a thin token that can exceed 20%, meaning the trade
needs a 20% move just to break even. Knowing that before you buy is worth more
than shaving milliseconds off the entry.
"""

from __future__ import annotations

import base64
import logging
import time
from dataclasses import dataclass
from enum import Enum
from typing import Any

from ..models import Fill
from ..providers import ProviderRegistry
from ..providers.execution import LAMPORTS_PER_SOL, SOL_MINT
from ..solana.rpc import SolanaRPC

log = logging.getLogger(__name__)


class Venue(str, Enum):
    PUMP_CURVE = "pump_curve"
    JUPITER = "jupiter"
    UNAVAILABLE = "unavailable"


@dataclass
class RouteDecision:
    venue: Venue
    reason: str
    expected_out: float = 0.0
    price_impact_pct: float = 0.0
    route_labels: list[str] | None = None
    raw_quote: dict[str, Any] | None = None

    @property
    def tradeable(self) -> bool:
        return self.venue != Venue.UNAVAILABLE


@dataclass
class CostEstimate:
    """What a round trip would really cost, before you commit."""

    round_trip_pct: float
    buy_impact_pct: float
    sell_impact_pct: float
    sellable: bool
    reason: str = ""
    buy_route: list[str] | None = None
    sell_route: list[str] | None = None

    @property
    def acceptable(self) -> bool:
        return self.sellable and self.round_trip_pct < 25.0


class ExecutionRouter:
    """Decides venue, estimates cost, and builds Jupiter swaps."""

    def __init__(
        self,
        config: Any,
        registry: ProviderRegistry,
        rpc: SolanaRPC,
    ) -> None:
        self.config = config
        self.registry = registry
        self.rpc = rpc
        self.routes_chosen: dict[str, int] = {}

    @property
    def jupiter(self) -> Any:
        provider = self.registry.get("jupiter")
        return provider if provider is not None and provider.available else None

    # -- venue selection ----------------------------------------------------

    async def choose_buy_venue(self, mint: str, sol_amount: float) -> RouteDecision:
        """Where to buy. Prefers the curve while it exists — the fill is
        deterministic and there is no routing risk."""
        try:
            curve = await self.rpc.get_bonding_curve(mint, self.config.rpc.price_commitment)
        except Exception as exc:  # noqa: BLE001
            log.debug("curve read failed for %s: %s", mint, exc)
            curve = None

        if curve is not None and not curve.complete:
            tokens_out = curve.tokens_out_for_sol(sol_amount)
            if tokens_out > 0:
                self._count(Venue.PUMP_CURVE)
                return RouteDecision(
                    venue=Venue.PUMP_CURVE,
                    reason="still on the bonding curve",
                    expected_out=tokens_out,
                    price_impact_pct=curve.price_impact_pct(sol_amount),
                )

        jupiter = self.jupiter
        if jupiter is None:
            return RouteDecision(
                venue=Venue.UNAVAILABLE,
                reason="curve complete or missing and Jupiter is unavailable",
            )

        quote = await jupiter.get_quote(
            SOL_MINT, mint, int(sol_amount * LAMPORTS_PER_SOL),
            slippage_bps=int(self.config.execution.buy_slippage_pct * 100),
        )
        if not quote.usable:
            return RouteDecision(venue=Venue.UNAVAILABLE, reason=f"no route: {quote.error}")

        d = quote.data or {}
        self._count(Venue.JUPITER)
        return RouteDecision(
            venue=Venue.JUPITER,
            reason="token has graduated; routing via aggregator",
            expected_out=d["out_amount"],
            price_impact_pct=d["price_impact_pct"],
            route_labels=d["route_labels"],
            raw_quote=d["raw_quote"],
        )

    async def choose_sell_venue(self, mint: str, token_amount: float) -> RouteDecision:
        """Where to sell.

        Checked fresh on every exit rather than reusing the entry venue,
        because tokens graduate mid-position and the curve we bought on may no
        longer exist.
        """
        try:
            curve = await self.rpc.get_bonding_curve(mint, self.config.rpc.price_commitment)
        except Exception:  # noqa: BLE001
            curve = None

        if curve is not None and not curve.complete:
            sol_out = curve.sol_out_for_tokens(token_amount)
            if sol_out > 0:
                self._count(Venue.PUMP_CURVE)
                return RouteDecision(
                    venue=Venue.PUMP_CURVE,
                    reason="still on the bonding curve",
                    expected_out=sol_out,
                )

        jupiter = self.jupiter
        if jupiter is None:
            return RouteDecision(
                venue=Venue.UNAVAILABLE,
                reason="curve gone and Jupiter unavailable — cannot exit",
            )

        # Wider slippage on exits: getting out matters more than the price.
        decimals = 6
        quote = await jupiter.get_quote(
            mint, SOL_MINT, int(token_amount * (10**decimals)),
            slippage_bps=int(self.config.execution.sell_slippage_pct * 100),
        )
        if not quote.usable:
            return RouteDecision(
                venue=Venue.UNAVAILABLE,
                reason=f"no sell route exists: {quote.error}",
            )

        d = quote.data or {}
        self._count(Venue.JUPITER)
        return RouteDecision(
            venue=Venue.JUPITER,
            reason="routing exit via aggregator",
            expected_out=d["out_amount"] / LAMPORTS_PER_SOL,
            price_impact_pct=d["price_impact_pct"],
            route_labels=d["route_labels"],
            raw_quote=d["raw_quote"],
        )

    # -- pre-trade cost -----------------------------------------------------

    async def estimate_cost(self, mint: str, sol_amount: float) -> CostEstimate:
        """Round-trip cost before committing.

        Cheap (two quotes, no gas) and it catches the case where a buy route
        exists but a sell route does not — which is a honeypot by any
        practical definition, regardless of what the contract flags say.
        """
        jupiter = self.jupiter
        if jupiter is None:
            # Fall back to curve maths, which models pump.fun's own fee and
            # impact accurately.
            try:
                curve = await self.rpc.get_bonding_curve(mint)
            except Exception:  # noqa: BLE001
                curve = None
            if curve is None:
                return CostEstimate(100.0, 0.0, 0.0, False, "cannot price this token")
            tokens = curve.tokens_out_for_sol(sol_amount)
            back = curve.sol_out_for_tokens(tokens)
            pct = (sol_amount - back) / sol_amount * 100 if sol_amount else 100.0
            return CostEstimate(
                round_trip_pct=pct,
                buy_impact_pct=curve.price_impact_pct(sol_amount),
                sell_impact_pct=0.0,
                sellable=back > 0,
                reason="estimated from bonding curve",
            )

        result = await jupiter.round_trip_cost_pct(mint, sol_amount)
        if not result.usable:
            return CostEstimate(100.0, 0.0, 0.0, False, result.error or "no quote")

        d = result.data or {}
        return CostEstimate(
            round_trip_pct=float(d.get("round_trip_cost_pct") or 100.0),
            buy_impact_pct=float(d.get("buy_impact_pct") or 0),
            sell_impact_pct=float(d.get("sell_impact_pct") or 0),
            sellable=bool(d.get("sellable", False)),
            reason=str(d.get("reason", "")),
            buy_route=d.get("buy_route"),
            sell_route=d.get("sell_route"),
        )

    # -- Jupiter execution --------------------------------------------------

    async def execute_jupiter(
        self,
        decision: RouteDecision,
        keypair: Any,
        *,
        side: str,
        mint: str,
        symbol: str = "",
        reason: str = "",
        urgent: bool = False,
    ) -> Fill:
        """Build, sign locally and submit a Jupiter swap."""
        started = time.time()
        jupiter = self.jupiter
        if jupiter is None or not decision.raw_quote:
            return Fill(
                mint=mint, symbol=symbol, side=side, ok=False,  # type: ignore[arg-type]
                error="no Jupiter quote to execute", paper=False,
            )

        fee_sol = (
            self.config.execution.exit_priority_fee_sol
            if urgent
            else self.config.execution.priority_fee_sol
        )

        build = await jupiter.build_swap(
            decision.raw_quote,
            str(keypair.pubkey()),
            priority_fee_lamports=int(fee_sol * LAMPORTS_PER_SOL),
        )
        if not build.usable:
            return Fill(
                mint=mint, symbol=symbol, side=side, ok=False,  # type: ignore[arg-type]
                error=f"swap build failed: {build.error}", paper=False,
                latency_ms=(time.time() - started) * 1000,
            )

        try:
            from solders.transaction import VersionedTransaction

            raw = base64.b64decode((build.data or {})["swap_transaction"])
            unsigned = VersionedTransaction.from_bytes(raw)
            signed = VersionedTransaction(unsigned.message, [keypair])
            encoded = base64.b64encode(bytes(signed)).decode()
        except Exception as exc:  # noqa: BLE001
            return Fill(
                mint=mint, symbol=symbol, side=side, ok=False,  # type: ignore[arg-type]
                error=f"signing failed: {exc}", paper=False,
            )

        try:
            signature = await self.rpc.send_transaction(encoded, skip_preflight=True)
        except Exception as exc:  # noqa: BLE001
            return Fill(
                mint=mint, symbol=symbol, side=side, ok=False,  # type: ignore[arg-type]
                error=f"submit failed: {exc}", paper=False,
                latency_ms=(time.time() - started) * 1000,
            )

        log.warning(
            "jupiter %s submitted for %s via %s — %s",
            side, symbol or mint[:10],
            " > ".join(decision.route_labels or []), signature,
        )

        status = await self.rpc.confirm_transaction(signature, timeout_s=45.0)
        if not status["confirmed"]:
            detail = (
                "timed out — verify on chain before assuming it failed"
                if status.get("timeout")
                else f"failed on chain: {status.get('err')}"
            )
            return Fill(
                mint=mint, symbol=symbol, side=side, ok=False,  # type: ignore[arg-type]
                signature=signature, error=detail, paper=False,
                latency_ms=(time.time() - started) * 1000,
            )

        return Fill(
            mint=mint, symbol=symbol, side=side, ok=True,  # type: ignore[arg-type]
            signature=signature, fee_sol=fee_sol, paper=False,
            exit_reason=reason,
            latency_ms=(time.time() - started) * 1000,
        )

    def _count(self, venue: Venue) -> None:
        self.routes_chosen[venue.value] = self.routes_chosen.get(venue.value, 0) + 1

    def stats(self) -> dict[str, Any]:
        return {"routes": dict(self.routes_chosen)}
