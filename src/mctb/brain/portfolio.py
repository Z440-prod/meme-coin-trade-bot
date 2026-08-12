"""Position lifecycle and accounting.

Owns every open position, marks them to market, runs the exit rules, and
closes the loop by feeding realised outcomes back into the learning layers.

One rule shapes the whole design: **exits must never be blocked by anything
else.** Pricing, scoring, LLM calls, dashboard updates — all of it can stall
without consequence. An exit that has to wait is an exit that happens at a
worse price, so the exit loop runs on its own timer, holds no locks it does
not need, and treats every other subsystem as optional.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable
from datetime import datetime, timezone

from ..config import Config
from ..db import Database
from ..models import (
    Candidate,
    Features,
    Position,
    PositionState,
    ScoreBreakdown,
)
from ..scout.rpc_watcher import PriceOracle
from .edge import PersonalEdge
from .executor import Executor, PaperExecutor
from .exits import (
    apply_partial_fill,
    evaluate_exit,
    maybe_arm_trailing,
    update_position_price,
)

log = logging.getLogger(__name__)


def _utc_day() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


class Portfolio:
    def __init__(
        self,
        config: Config,
        db: Database,
        executor: Executor,
        oracle: PriceOracle,
        edge: PersonalEdge | None = None,
        clock: Callable[[], float] | None = None,
    ) -> None:
        self.config = config
        self.db = db
        self.executor = executor
        self.oracle = oracle
        self.edge = edge
        # Injectable so the simulator can run on a virtual clock. Everything
        # that stamps or compares a time goes through this.
        self.clock = clock or time.time

        self.positions: dict[str, Position] = {}
        self.closed_today = 0
        self.wins_today = 0
        self.losses_today = 0
        self.realised_today = 0.0
        self._day = _utc_day()

        # Signals arriving from the Scout that the exit rules care about.
        self.dev_sold: set[str] = set()
        self.smart_exited: set[str] = set()
        self.migrated: set[str] = set()

        # Callbacks wired up by the Brain.
        self.on_fill = None
        self.on_position_closed = None

        self._closing: set[str] = set()

    # -- state --------------------------------------------------------------

    @property
    def open_positions(self) -> list[Position]:
        return [
            p
            for p in self.positions.values()
            if p.state in (PositionState.OPEN, PositionState.OPENING, PositionState.CLOSING)
        ]

    @property
    def open_count(self) -> int:
        return len(self.open_positions)

    @property
    def at_risk_positions(self) -> list[Position]:
        """Positions that still have capital at risk.

        A free runner — cost recovered, only the reserved fraction left — is
        excluded. It cannot lose anything beyond profit already banked, so it
        consumes no risk budget.

        This matters more than it looks. Runners never close by design, so
        counting them against `max_concurrent_positions` would mean the bot
        stops taking new trades once it has accumulated that many winners:
        the strategy would shut itself down precisely by succeeding.
        """
        fraction = self.config.exits.runner_fraction
        if fraction <= 0:
            return self.open_positions
        return [
            p
            for p in self.open_positions
            if not (p.is_runner_only(fraction) and p.cost_recovered)
        ]

    @property
    def at_risk_count(self) -> int:
        return len(self.at_risk_positions)

    @property
    def free_runner_count(self) -> int:
        return self.open_count - self.at_risk_count

    def holds(self, mint: str) -> bool:
        position = self.positions.get(mint)
        return bool(position and position.state in (PositionState.OPEN, PositionState.OPENING, PositionState.CLOSING))

    async def bankroll(self) -> float:
        return await self.executor.bankroll()

    def _roll_day(self) -> None:
        today = _utc_day()
        if today != self._day:
            log.info(
                "new UTC day — resetting daily counters (yesterday: %d trades, %+.4f SOL)",
                self.closed_today,
                self.realised_today,
            )
            self._day = today
            self.closed_today = 0
            self.wins_today = 0
            self.losses_today = 0
            self.realised_today = 0.0

    @property
    def daily_pnl_sol(self) -> float:
        self._roll_day()
        return self.realised_today

    # -- opening ------------------------------------------------------------

    async def open_position(
        self, candidate: Candidate, size_sol: float, score: float, breakdown: ScoreBreakdown
    ) -> Position | None:
        """Buy, and register the resulting position. None if the buy failed."""
        if self.holds(candidate.mint):
            return None

        position = Position(
            mint=candidate.mint,
            symbol=candidate.symbol,
            name=candidate.name,
            creator=candidate.creator,
            state=PositionState.OPENING,
            entry_score=score,
            entry_features=candidate.features,
            smart_wallets=list(candidate.smart_wallets),
            paper=self.executor.paper,
        )
        self.positions[candidate.mint] = position

        log.warning(
            "OPENING %s (%s) — %.4f SOL at score %.1f%s",
            candidate.symbol or candidate.mint[:10],
            "paper" if self.executor.paper else "LIVE",
            size_sol,
            score,
            f", {len(candidate.smart_wallets)} tracked wallet(s) in"
            if candidate.smart_wallets
            else "",
        )

        fill = await self.executor.buy(candidate.mint, size_sol, candidate.symbol)
        await self.db.record_fill(fill)
        if self.on_fill:
            await self.on_fill(fill)

        if not fill.ok or fill.token_amount <= 0:
            position.state = PositionState.FAILED
            position.exit_reason = fill.error or "buy failed"
            await self.db.upsert_position(position)
            log.error(
                "buy failed for %s: %s", candidate.symbol or candidate.mint[:10], fill.error
            )
            # Leave it out of the way so a later signal on the same mint is
            # not blocked by a position that never existed.
            self.positions.pop(candidate.mint, None)
            return None

        position.state = PositionState.OPEN
        position.entry_price_sol = fill.price_sol
        position.current_price_sol = fill.price_sol
        position.peak_price_sol = fill.price_sol
        position.last_price_ts = self.clock()
        position.cost_sol = fill.sol_amount
        position.tokens_bought = fill.token_amount
        position.tokens_held = fill.token_amount
        position.fees_sol = fill.fee_sol

        await self.db.upsert_position(position)
        log.warning(
            "OPENED %s: %.0f tokens at %.10f SOL (%.4f SOL committed)",
            position.symbol or position.mint[:10],
            position.tokens_bought,
            position.entry_price_sol,
            position.cost_sol,
        )
        return position

    # -- pricing ------------------------------------------------------------

    async def mark_to_market(self) -> None:
        """Refresh every open position's price in a single batched read."""
        positions = [p for p in self.open_positions if p.tokens_held > 0]
        if not positions:
            return

        mints = [p.mint for p in positions]
        try:
            prices = await self.oracle.prices(mints)
        except Exception as exc:  # noqa: BLE001
            log.warning("mark-to-market failed: %s", exc)
            return

        for position in positions:
            price = prices.get(position.mint, 0.0)
            update_position_price(position, price)
            if maybe_arm_trailing(position, self.config.exits):
                log.info(
                    "trailing stop armed on %s at %.2fx (will exit %.0f%% off peak)",
                    position.symbol or position.mint[:10],
                    position.multiple,
                    self.config.exits.trailing_distance_pct * 100,
                )

    # -- exits --------------------------------------------------------------

    async def process_exits(self) -> None:
        """Evaluate and execute exits for every open position."""
        for position in list(self.open_positions):
            if position.mint in self._closing:
                continue
            if position.state == PositionState.OPENING:
                continue

            action = evaluate_exit(
                position,
                self.config.exits,
                dev_sold=position.mint in self.dev_sold,
                smart_wallets_exited=position.mint in self.smart_exited,
                migrated=position.mint in self.migrated,
            )
            if not action.should_sell:
                continue

            self._closing.add(position.mint)
            try:
                await self._execute_exit(position, action)
            except Exception as exc:  # noqa: BLE001
                log.exception("exit failed for %s: %s", position.mint, exc)
            finally:
                self._closing.discard(position.mint)

    async def _execute_exit(self, position: Position, action) -> None:
        tokens_to_sell = position.tokens_held * action.fraction
        if tokens_to_sell <= 0:
            return

        was_full = action.fraction >= 0.99
        if was_full:
            position.state = PositionState.CLOSING

        log.warning(
            "EXIT %s: %s — selling %.0f%% at %.2fx",
            position.symbol or position.mint[:10],
            action.reason,
            action.fraction * 100,
            position.multiple,
        )

        fill = await self.executor.sell(
            position.mint,
            tokens_to_sell,
            symbol=position.symbol,
            reason=action.reason,
            urgent=action.urgent,
        )
        await self.db.record_fill(fill)
        if self.on_fill:
            await self.on_fill(fill)

        if not fill.ok:
            # Could not exit. Stay open and try again next tick — the retry
            # logic inside the executor has already escalated fees.
            position.state = PositionState.OPEN
            log.error(
                "exit failed for %s (%s); will retry next tick",
                position.symbol or position.mint[:10],
                fill.error,
            )
            return

        apply_partial_fill(
            position,
            tokens_sold=fill.token_amount,
            proceeds_sol=fill.sol_amount,
            fee_sol=fill.fee_sol,
            rung=action.ladder_rung,
        )

        if position.tokens_held <= 0:
            await self._finalise(position, action.reason)
        else:
            position.state = PositionState.OPEN
            await self.db.upsert_position(position)
            log.info(
                "partial exit done on %s: %.4f SOL realised, %.0f%% still held",
                position.symbol or position.mint[:10],
                fill.sol_amount,
                100 * position.tokens_held / position.tokens_bought,
            )

    async def _finalise(self, position: Position, reason: str) -> None:
        """Close out a position and propagate the result to every learner."""
        position.state = PositionState.CLOSED
        position.closed_at = self.clock()
        position.exit_reason = reason
        position.current_price_sol = (
            position.realised_sol / position.tokens_bought if position.tokens_bought else 0.0
        )

        pnl = position.total_pnl_sol
        win = pnl > 0

        self._roll_day()
        self.closed_today += 1
        self.realised_today += pnl
        if win:
            self.wins_today += 1
        else:
            self.losses_today += 1

        await self.db.upsert_position(position)
        await self.db.bump_daily(
            self._day,
            win=win,
            pnl_sol=pnl,
            fees_sol=position.fees_sol,
            volume_sol=position.cost_sol,
        )

        # Learn: feature-bucket expectancy.
        if self.edge is not None:
            await self.edge.learn(position.entry_features, pnl, position.cost_sol)

        # Learn: creator reputation. A position that lost most of its value
        # very quickly is treated as a rug, and that creator is blocked.
        if position.creator:
            rugged = position.pnl_pct < -0.7 and position.age_s < 600
            await self.db.bump_creator(
                position.creator, rug=rugged, win=win, pnl_sol=pnl
            )
            if rugged:
                await self.db.block_creator(position.creator)
                log.warning(
                    "creator %s… blocked after %s lost %.0f%% in %.0fs",
                    position.creator[:8],
                    position.symbol or position.mint[:10],
                    -100 * position.pnl_pct,
                    position.age_s,
                )

        log.warning(
            "CLOSED %s: %+.4f SOL (%+.1f%%) in %.0fs via %s",
            position.symbol or position.mint[:10],
            pnl,
            100 * position.pnl_pct,
            position.age_s,
            reason,
        )

        if self.on_position_closed:
            await self.on_position_closed(position)

        # Keep it around briefly so the dashboard can show the close, then
        # drop it so the map does not grow without bound.
        asyncio.get_event_loop().call_later(120, self.positions.pop, position.mint, None)

    async def force_close(self, mint: str, reason: str = "manual") -> bool:
        """Close a position on command from the Console."""
        position = self.positions.get(mint)
        if position is None or position.tokens_held <= 0:
            return False

        from .exits import ExitAction

        self._closing.add(mint)
        try:
            await self._execute_exit(
                position, ExitAction(kind="full", fraction=1.0, reason=reason, urgent=True)
            )
        finally:
            self._closing.discard(mint)
        return True

    async def close_all(self, reason: str = "close_all") -> int:
        closed = 0
        for position in list(self.open_positions):
            if await self.force_close(position.mint, reason):
                closed += 1
        return closed

    # -- recovery -----------------------------------------------------------

    async def restore(self) -> int:
        """Reload open positions after a restart.

        Without this, restarting the Brain orphans every open position: no
        stop loss, no take profit, nothing watching. This is the single most
        important piece of crash recovery in the system.
        """
        rows = await self.db.open_positions()
        restored = 0

        for row in rows:
            # A paper position cannot be restored into a live session or the
            # accounting would be fiction.
            if bool(row["paper"]) != self.executor.paper:
                continue
            try:
                position = Position(
                    id=row["id"],
                    mint=row["mint"],
                    symbol=row["symbol"] or "",
                    creator=row["creator"] or "",
                    state=PositionState.OPEN,
                    opened_at=row["opened_at"],
                    entry_price_sol=row["entry_price_sol"],
                    cost_sol=row["cost_sol"],
                    tokens_bought=row["tokens_bought"],
                    tokens_held=row["tokens_held"],
                    realised_sol=row["realised_sol"],
                    fees_sol=row["fees_sol"],
                    peak_price_sol=row["peak_price_sol"],
                    entry_score=row["entry_score"] or 0.0,
                    entry_features=Features.model_validate_json(row["entry_features"])
                    if row["entry_features"]
                    else Features(),
                    paper=bool(row["paper"]),
                    last_price_ts=self.clock(),
                )
                if position.tokens_held <= 0:
                    continue
                self.positions[position.mint] = position
                restored += 1
            except Exception as exc:  # noqa: BLE001
                log.error("could not restore position %s: %s", row.get("mint"), exc)

        if restored:
            log.warning(
                "restored %d open position(s) from the database — exit rules are active on them again",
                restored,
            )
        return restored

    async def reconcile_live(self) -> None:
        """Check restored live positions against actual wallet balances.

        The database says what we think we hold. The chain says what we
        actually hold. If a sell landed just before a crash they will
        disagree, and the chain is right.
        """
        if self.executor.paper or not hasattr(self.executor, "_token_balance"):
            return

        for position in list(self.open_positions):
            try:
                actual = await self.executor._token_balance(position.mint)  # noqa: SLF001
            except Exception:  # noqa: BLE001
                continue

            if actual <= 0:
                log.warning(
                    "position %s exists in the database but the wallet holds none — "
                    "marking closed",
                    position.symbol or position.mint[:10],
                )
                position.tokens_held = 0.0
                await self._finalise(position, "reconciled_gone")
            elif abs(actual - position.tokens_held) / max(position.tokens_held, 1e-9) > 0.02:
                log.warning(
                    "position %s: database said %.0f tokens, wallet holds %.0f — trusting the chain",
                    position.symbol or position.mint[:10],
                    position.tokens_held,
                    actual,
                )
                position.tokens_held = actual
                await self.db.upsert_position(position)

    # -- signals from the Scout ---------------------------------------------

    def note_dev_sell(self, mint: str) -> None:
        if self.holds(mint):
            log.warning("creator is selling %s — exit will fire on the next tick", mint[:10])
        self.dev_sold.add(mint)

    def note_smart_exit(self, mint: str) -> None:
        if self.holds(mint):
            log.warning("tracked wallets are exiting %s", mint[:10])
        self.smart_exited.add(mint)

    def note_migration(self, mint: str) -> None:
        self.migrated.add(mint)

    def prune_signals(self) -> None:
        """Forget signals for tokens we no longer hold."""
        live = {p.mint for p in self.open_positions}
        self.dev_sold &= live
        self.smart_exited &= live
        self.migrated &= live

    # -- reporting ----------------------------------------------------------

    @property
    def unrealised_pnl_sol(self) -> float:
        return sum(p.unrealised_pnl_sol for p in self.open_positions)

    async def equity_sol(self) -> float:
        """Cash plus the market value of open positions."""
        return await self.bankroll() + sum(p.value_sol for p in self.open_positions)

    def credit_paper(self, sol: float) -> None:
        if isinstance(self.executor, PaperExecutor):
            self.executor.credit(sol)
