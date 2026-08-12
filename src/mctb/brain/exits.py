"""Exit rules.

Entries get all the attention, but exits are where the money is decided. A
mediocre entry with disciplined exits is profitable; a brilliant entry held
too long is not. Memecoins in particular give back their entire move, often
in under a minute, so the exit logic has to be mechanical and it has to run
without waiting for anything.

This module is a pure function of position state and price — no I/O, no
network, no clock beyond what is passed in. That makes every rule directly
testable, which matters because these are the rules you will be relying on at
3am when a position is down 40% and nobody is watching.

Rules are evaluated in strict priority order and the first match wins:

  1. **Unsellable** — no price, no curve. Written off; nothing to do but stop
     pretending it is worth something.
  2. **Rug signals** — creator dumping, tracked wallets leaving. Exit
     immediately at any price. Being 20% worse off right now beats being 100%
     worse off in ten seconds.
  3. **Stop loss** — the line you decided on before you had money on it.
  4. **Trailing stop** — protects a gain that has already happened.
  5. **Take-profit ladder** — sells into strength in tranches, so a runner is
     not fully exited at 2x but a reversal does not give everything back.
  6. **Stagnation / max hold** — capital in a flat coin is capital not
     working, and the longer you hold the more chances there are for
     something to go wrong.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Literal

from ..config import ExitConfig
from ..models import Position, PositionState

log = logging.getLogger(__name__)


@dataclass
class ExitAction:
    kind: Literal["none", "partial", "full"]
    fraction: float = 0.0  # of tokens currently held
    reason: str = ""
    urgent: bool = False
    ladder_rung: int | None = None

    @property
    def should_sell(self) -> bool:
        return self.kind != "none" and self.fraction > 0


NO_EXIT = ExitAction(kind="none")


def evaluate_exit(
    position: Position,
    config: ExitConfig,
    *,
    now: float | None = None,
    dev_sold: bool = False,
    smart_wallets_exited: bool = False,
    migrated: bool = False,
) -> ExitAction:
    """Decide what to do with one open position right now."""
    now = now or time.time()

    if position.state not in (PositionState.OPEN, PositionState.OPENING):
        return NO_EXIT
    if position.tokens_held <= 0:
        return NO_EXIT

    price = position.current_price_sol
    entry = position.entry_price_sol
    age = now - position.opened_at
    price_age = now - position.last_price_ts if position.last_price_ts else 0.0

    # --- 1. Unsellable ------------------------------------------------------
    if price <= 0 and price_age > config.stale_price_exit_s:
        return ExitAction(
            kind="full",
            fraction=1.0,
            reason="no_liquidity",
            urgent=True,
        )

    # Stale but nonzero price: we are flying blind. Get out while a price
    # still exists rather than waiting to find out why the feed stopped.
    if price_age > config.stale_price_exit_s:
        return ExitAction(
            kind="full",
            fraction=1.0,
            reason=f"stale_price_{price_age:.0f}s",
            urgent=True,
        )

    if entry <= 0 or price <= 0:
        return NO_EXIT

    multiple = price / entry
    gain_pct = multiple - 1.0

    # --- The runner ---------------------------------------------------------
    # Once a position has paid for itself and only the reserved runner is
    # left, the rules that would close it are switched off. Every large
    # memecoin run contains drawdowns that a trailing stop or a max-hold timer
    # would exit into, so a system with those always on cannot produce an
    # outsized outcome no matter how good its entries are.
    #
    # The downside is already bounded: the stake is back, so the runner can go
    # to zero and cost nothing.
    riding = (
        config.runner_fraction > 0
        and position.is_runner_only(config.runner_fraction)
        and (position.cost_recovered or not config.runner_requires_cost_recovered)
    )

    # --- 2. Rug signals -----------------------------------------------------
    # A rug still empties even a free runner — salvaging something beats
    # riding to zero deliberately.
    rug_exits_runner = config.runner_exits_on_rug

    if dev_sold and config.exit_on_dev_sell and (rug_exits_runner or not riding):
        return ExitAction(kind="full", fraction=1.0, reason="dev_sold", urgent=True)

    if (
        smart_wallets_exited
        and config.exit_on_smart_wallet_exit
        and (rug_exits_runner or not riding)
    ):
        return ExitAction(
            kind="full", fraction=1.0, reason="smart_wallets_exited", urgent=True
        )

    if migrated and config.exit_on_migration:
        return ExitAction(kind="full", fraction=1.0, reason="migrated", urgent=False)

    # --- 3. Stop loss -------------------------------------------------------
    # Skipped for a free runner: there is nothing left to protect. Stopping
    # out a house-money position at -35% only guarantees you are not holding
    # it if it later runs.
    if not riding and gain_pct <= -abs(config.stop_loss_pct):
        return ExitAction(
            kind="full",
            fraction=1.0,
            reason=f"stop_loss_{gain_pct:+.0%}",
            urgent=True,
        )

    # --- 4. Trailing stop ---------------------------------------------------
    # Arms once the position has proved itself, then follows the peak down.
    # Never applied to a free runner — a 30% pullback is normal inside a 100x,
    # and trailing out of it is how a runner becomes a 3x.
    if not riding and position.trailing_armed and position.peak_price_sol > 0:
        drop = (position.peak_price_sol - price) / position.peak_price_sol
        if drop >= config.trailing_distance_pct:
            return ExitAction(
                kind="full",
                fraction=1.0,
                reason=f"trailing_stop_{drop:.0%}_off_peak",
                urgent=True,
            )

    # --- 5. Take-profit ladder ---------------------------------------------
    # Highest untaken rung first, so a fast spike past several rungs takes the
    # biggest one rather than dribbling out the smallest.
    for index in sorted(
        range(len(config.take_profit_ladder)),
        key=lambda i: config.take_profit_ladder[i]["multiple"],
        reverse=True,
    ):
        rung = config.take_profit_ladder[index]
        if index in position.ladder_filled:
            continue
        if multiple >= rung["multiple"]:
            # The rung's fraction is of the ORIGINAL position, so convert it
            # to a fraction of what is left.
            target_tokens = position.tokens_bought * rung["fraction"]

            # Never sell into the reserved runner. Without this the ladder
            # eventually consumes the whole position and there is nothing left
            # to catch a large move.
            runner = position.runner_tokens(config.runner_fraction)
            sellable = max(0.0, position.tokens_held - runner)
            if sellable <= 0:
                continue
            target_tokens = min(target_tokens, sellable)

            fraction_of_held = min(1.0, target_tokens / position.tokens_held)
            if fraction_of_held <= 0.01:
                continue
            return ExitAction(
                kind="full" if fraction_of_held >= 0.99 else "partial",
                fraction=fraction_of_held,
                reason=f"take_profit_{rung['multiple']:.1f}x",
                urgent=False,
                ladder_rung=index,
            )

    # --- 6. Time-based ------------------------------------------------------
    # A free runner has no clock. This is the single most important exemption
    # in the file: a 100x takes hours or days, and any max-hold timer
    # guarantees you are not holding when it happens.
    if riding:
        return NO_EXIT

    if age >= config.max_hold_s:
        return ExitAction(
            kind="full",
            fraction=1.0,
            reason=f"max_hold_{age:.0f}s",
            urgent=False,
        )

    if age >= config.stagnant_after_s and gain_pct < config.stagnant_min_gain_pct:
        return ExitAction(
            kind="full",
            fraction=1.0,
            reason=f"stagnant_{gain_pct:+.0%}_after_{age:.0f}s",
            urgent=False,
        )

    return NO_EXIT


def update_position_price(position: Position, price: float, now: float | None = None) -> None:
    """Mark a position to market and maintain peak / trailing state.

    Kept separate from `evaluate_exit` so that evaluation has no side effects
    and can be called repeatedly in tests without changing anything.
    """
    now = now or time.time()
    if price <= 0:
        # Do not overwrite a good price with zero — that would trip the stop
        # loss on a transient read failure. Let the staleness timer handle it.
        return

    position.current_price_sol = price
    position.last_price_ts = now
    position.peak_price_sol = max(position.peak_price_sol, price)


def maybe_arm_trailing(position: Position, config: ExitConfig) -> bool:
    """Arm the trailing stop once the position is far enough ahead.

    Returns True on the transition, so the caller can log it once rather than
    every tick.
    """
    if position.trailing_armed or position.entry_price_sol <= 0:
        return False
    gain = (position.current_price_sol / position.entry_price_sol) - 1.0
    if gain >= config.trailing_activate_pct:
        position.trailing_armed = True
        return True
    return False


def apply_partial_fill(position: Position, tokens_sold: float, proceeds_sol: float, fee_sol: float, rung: int | None) -> None:
    """Update position accounting after a sell fills."""
    position.tokens_held = max(0.0, position.tokens_held - tokens_sold)
    position.realised_sol += proceeds_sol
    position.fees_sol += fee_sol
    if rung is not None and rung not in position.ladder_filled:
        position.ladder_filled.append(rung)

    # Dust: below this it is not worth another transaction's fees.
    if position.tokens_held <= position.tokens_bought * 0.005:
        position.tokens_held = 0.0


def describe_exit(action: ExitAction, position: Position) -> str:
    """One-line human summary for logs and alerts."""
    pnl = position.total_pnl_sol
    return (
        f"{position.symbol or position.mint[:10]}: {action.reason} — "
        f"selling {action.fraction:.0%} of holdings at {position.multiple:.2f}x "
        f"({pnl:+.4f} SOL)"
    )
