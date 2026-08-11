"""Exit rules.

These decide when money comes back, so every rule and every priority
interaction is tested explicitly. The priority tests matter most: a stop loss
that loses a race against a take-profit rung would sell into strength instead
of cutting a loss.
"""

from __future__ import annotations

import time

import pytest

from mctb.brain.exits import (
    apply_partial_fill,
    evaluate_exit,
    maybe_arm_trailing,
    update_position_price,
)
from mctb.config import ExitConfig
from mctb.models import Position, PositionState


@pytest.fixture
def exits() -> ExitConfig:
    return ExitConfig(
        stop_loss_pct=0.35,
        take_profit_ladder=[
            {"multiple": 1.6, "fraction": 0.34},
            {"multiple": 2.5, "fraction": 0.33},
            {"multiple": 5.0, "fraction": 0.20},
        ],
        trailing_activate_pct=0.45,
        trailing_distance_pct=0.25,
        max_hold_s=900,
        stagnant_after_s=240,
        stagnant_min_gain_pct=0.10,
        stale_price_exit_s=90,
    )


def make_position(price_multiple: float = 1.0, age_s: float = 10.0) -> Position:
    now = time.time()
    entry = 1.0e-7
    return Position(
        mint="M" * 44,
        symbol="TEST",
        state=PositionState.OPEN,
        opened_at=now - age_s,
        entry_price_sol=entry,
        current_price_sol=entry * price_multiple,
        peak_price_sol=entry * max(1.0, price_multiple),
        last_price_ts=now,
        cost_sol=0.05,
        tokens_bought=500_000.0,
        tokens_held=500_000.0,
    )


class TestStopLoss:
    def test_fires_below_threshold(self, exits: ExitConfig) -> None:
        action = evaluate_exit(make_position(0.60), exits)
        assert action.kind == "full"
        assert "stop_loss" in action.reason
        assert action.urgent is True

    def test_does_not_fire_above_threshold(self, exits: ExitConfig) -> None:
        assert evaluate_exit(make_position(0.70), exits).kind == "none"

    def test_fires_exactly_at_threshold(self, exits: ExitConfig) -> None:
        assert evaluate_exit(make_position(0.65), exits).kind == "full"


class TestTakeProfitLadder:
    def test_first_rung(self, exits: ExitConfig) -> None:
        action = evaluate_exit(make_position(1.7), exits)
        assert action.kind == "partial"
        assert action.ladder_rung == 0
        assert action.fraction == pytest.approx(0.34)
        assert action.urgent is False

    def test_skips_already_filled_rung(self, exits: ExitConfig) -> None:
        position = make_position(1.7)
        position.ladder_filled = [0]
        assert evaluate_exit(position, exits).kind == "none"

    def test_spike_takes_highest_rung_reached(self, exits: ExitConfig) -> None:
        """A price that gaps past several rungs should take the biggest one,
        not dribble out the smallest first while the move reverses."""
        action = evaluate_exit(make_position(6.0), exits)
        assert action.ladder_rung == 2

    def test_fraction_is_of_original_not_remaining(self, exits: ExitConfig) -> None:
        """Rung fractions are of the original position. After selling 34%,
        a 33% rung must sell 33% of the original, which is half of what is
        left — not 33% of the remainder."""
        position = make_position(2.6)
        position.ladder_filled = [0]
        position.tokens_held = 500_000.0 * 0.66
        action = evaluate_exit(position, exits)
        assert action.ladder_rung == 1
        expected = (500_000.0 * 0.33) / (500_000.0 * 0.66)
        assert action.fraction == pytest.approx(expected, rel=1e-6)

    def test_no_rung_below_first_multiple(self, exits: ExitConfig) -> None:
        assert evaluate_exit(make_position(1.4), exits).kind == "none"


class TestTrailingStop:
    def test_arms_after_activation_gain(self, exits: ExitConfig) -> None:
        position = make_position(1.5)
        assert maybe_arm_trailing(position, exits) is True
        assert position.trailing_armed is True

    def test_does_not_arm_too_early(self, exits: ExitConfig) -> None:
        position = make_position(1.2)
        assert maybe_arm_trailing(position, exits) is False

    def test_arms_only_once(self, exits: ExitConfig) -> None:
        position = make_position(1.5)
        assert maybe_arm_trailing(position, exits) is True
        assert maybe_arm_trailing(position, exits) is False

    def test_fires_on_drop_from_peak(self, exits: ExitConfig) -> None:
        position = make_position(2.0)
        position.trailing_armed = True
        position.peak_price_sol = position.entry_price_sol * 3.0
        # Now at 2.0x, peak was 3.0x — a 33% drop, past the 25% distance.
        action = evaluate_exit(position, exits)
        assert action.kind == "full"
        assert "trailing_stop" in action.reason

    def test_does_not_fire_within_distance(self, exits: ExitConfig) -> None:
        position = make_position(2.8)
        position.trailing_armed = True
        position.peak_price_sol = position.entry_price_sol * 3.0
        # ~7% off peak, well inside the 25% band. Ladder rung 1 (2.5x) fires
        # instead, which is correct behaviour.
        action = evaluate_exit(position, exits)
        assert "trailing_stop" not in action.reason

    def test_inactive_when_not_armed(self, exits: ExitConfig) -> None:
        position = make_position(1.0)
        position.peak_price_sol = position.entry_price_sol * 3.0
        assert "trailing" not in evaluate_exit(position, exits).reason


class TestEmergencyExits:
    def test_dev_sell(self, exits: ExitConfig) -> None:
        action = evaluate_exit(make_position(1.2), exits, dev_sold=True)
        assert action.kind == "full"
        assert action.reason == "dev_sold"
        assert action.urgent is True

    def test_dev_sell_ignored_when_disabled(self, exits: ExitConfig) -> None:
        exits.exit_on_dev_sell = False
        assert evaluate_exit(make_position(1.2), exits, dev_sold=True).kind == "none"

    def test_smart_wallets_exiting(self, exits: ExitConfig) -> None:
        action = evaluate_exit(make_position(1.2), exits, smart_wallets_exited=True)
        assert action.reason == "smart_wallets_exited"

    def test_migration_off_by_default(self, exits: ExitConfig) -> None:
        assert evaluate_exit(make_position(1.2), exits, migrated=True).kind == "none"

    def test_migration_when_enabled(self, exits: ExitConfig) -> None:
        exits.exit_on_migration = True
        assert evaluate_exit(make_position(1.2), exits, migrated=True).reason == "migrated"

    def test_dev_sell_beats_take_profit(self, exits: ExitConfig) -> None:
        """Priority check: a rug signal at a profitable price must dump the
        whole position, not take a partial ladder rung and keep the rest."""
        action = evaluate_exit(make_position(2.0), exits, dev_sold=True)
        assert action.kind == "full"
        assert action.fraction == 1.0

    def test_dev_sell_beats_stop_loss(self, exits: ExitConfig) -> None:
        action = evaluate_exit(make_position(0.5), exits, dev_sold=True)
        assert action.reason == "dev_sold"


class TestStaleAndDeadPrices:
    def test_stale_price_exits(self, exits: ExitConfig) -> None:
        position = make_position(1.1)
        position.last_price_ts = time.time() - 120
        action = evaluate_exit(position, exits)
        assert action.kind == "full"
        assert "stale_price" in action.reason
        assert action.urgent is True

    def test_zero_price_after_staleness_is_no_liquidity(self, exits: ExitConfig) -> None:
        position = make_position(1.0)
        position.current_price_sol = 0.0
        position.last_price_ts = time.time() - 120
        assert evaluate_exit(position, exits).reason == "no_liquidity"

    def test_momentary_zero_price_does_not_exit(self, exits: ExitConfig) -> None:
        """A single failed price read must not trigger an exit. This is a real
        failure mode: an RPC hiccup returning 0 would otherwise dump every
        position at once."""
        position = make_position(1.0)
        position.current_price_sol = 0.0
        position.last_price_ts = time.time()
        assert evaluate_exit(position, exits).kind == "none"

    def test_update_price_ignores_zero(self) -> None:
        position = make_position(1.0)
        original = position.current_price_sol
        update_position_price(position, 0.0)
        assert position.current_price_sol == original

    def test_update_price_tracks_peak(self) -> None:
        position = make_position(1.0)
        update_position_price(position, 5.0e-7)
        update_position_price(position, 2.0e-7)
        assert position.peak_price_sol == pytest.approx(5.0e-7)
        assert position.current_price_sol == pytest.approx(2.0e-7)


class TestTimeExits:
    def test_max_hold(self, exits: ExitConfig) -> None:
        action = evaluate_exit(make_position(1.05, age_s=1000), exits)
        assert action.kind == "full"
        assert "max_hold" in action.reason

    def test_stagnant(self, exits: ExitConfig) -> None:
        action = evaluate_exit(make_position(1.02, age_s=300), exits)
        assert "stagnant" in action.reason

    def test_not_stagnant_when_moving(self, exits: ExitConfig) -> None:
        assert evaluate_exit(make_position(1.3, age_s=300), exits).kind == "none"

    def test_stop_loss_beats_stagnation(self, exits: ExitConfig) -> None:
        action = evaluate_exit(make_position(0.5, age_s=300), exits)
        assert "stop_loss" in action.reason


class TestGuards:
    def test_closed_position_ignored(self, exits: ExitConfig) -> None:
        position = make_position(0.1)
        position.state = PositionState.CLOSED
        assert evaluate_exit(position, exits).kind == "none"

    def test_zero_tokens_ignored(self, exits: ExitConfig) -> None:
        position = make_position(0.1)
        position.tokens_held = 0.0
        assert evaluate_exit(position, exits).kind == "none"

    def test_zero_entry_price_does_not_crash(self, exits: ExitConfig) -> None:
        position = make_position(1.0)
        position.entry_price_sol = 0.0
        assert evaluate_exit(position, exits).kind == "none"


class TestPartialFillAccounting:
    def test_reduces_holdings_and_records_proceeds(self) -> None:
        position = make_position(2.0)
        apply_partial_fill(position, tokens_sold=170_000, proceeds_sol=0.034, fee_sol=0.0004, rung=0)
        assert position.tokens_held == pytest.approx(330_000)
        assert position.realised_sol == pytest.approx(0.034)
        assert 0 in position.ladder_filled

    def test_dust_is_zeroed(self) -> None:
        """Leaving 0.2% of a position behind means paying another
        transaction's fees to sell nothing. Treat it as closed."""
        position = make_position(2.0)
        apply_partial_fill(position, tokens_sold=499_000, proceeds_sol=0.1, fee_sol=0.0004, rung=None)
        assert position.tokens_held == 0.0

    def test_rung_not_double_recorded(self) -> None:
        position = make_position(2.0)
        apply_partial_fill(position, 100_000, 0.02, 0.0004, rung=0)
        apply_partial_fill(position, 100_000, 0.02, 0.0004, rung=0)
        assert position.ladder_filled == [0]


class TestPositionAccounting:
    def test_pnl_after_full_exit(self) -> None:
        position = make_position(2.0)
        position.tokens_held = 0.0
        position.realised_sol = 0.10
        position.fees_sol = 0.002
        position.state = PositionState.CLOSED
        # 0.10 realised - 0.05 cost - 0.002 fees
        assert position.total_pnl_sol == pytest.approx(0.048)
        assert position.pnl_pct == pytest.approx(0.96)

    def test_unrealised_uses_remaining_cost_basis(self) -> None:
        position = make_position(2.0)
        position.tokens_held = 250_000.0  # half sold
        # Half the position at 2x is worth 0.05; its cost basis is 0.025.
        assert position.unrealised_pnl_sol == pytest.approx(0.025, rel=1e-6)

    def test_multiple(self) -> None:
        assert make_position(3.5).multiple == pytest.approx(3.5)
