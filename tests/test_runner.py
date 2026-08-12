"""The runner: the mechanism that makes an asymmetric outcome possible.

A large memecoin run contains repeated 40-60% drawdowns. Any system with a
trailing stop and a max-hold timer permanently on will exit into the first one
and convert a would-be 1000x into a 3x. These tests pin the exemptions that
prevent that, and equally pin the safety properties that must survive them.
"""

from __future__ import annotations

import time

import pytest

from mctb.brain.exits import apply_partial_fill, evaluate_exit
from mctb.config import ExitConfig, load_config
from mctb.models import Position, PositionState


def make_position(
    *, multiple: float = 1.0, age_s: float = 10.0, cost_sol: float = 0.05,
    realised_sol: float = 0.0, tokens_held_fraction: float = 1.0,
) -> Position:
    entry = 1.0e-7
    now = time.time()
    bought = 500_000.0
    return Position(
        mint="M" * 44, symbol="MOON", state=PositionState.OPEN,
        opened_at=now - age_s,
        entry_price_sol=entry,
        current_price_sol=entry * multiple,
        peak_price_sol=entry * max(1.0, multiple),
        last_price_ts=now,
        cost_sol=cost_sol, fees_sol=0.0005,
        tokens_bought=bought,
        tokens_held=bought * tokens_held_fraction,
        realised_sol=realised_sol,
    )


@pytest.fixture
def moonshot() -> ExitConfig:
    return ExitConfig(
        stop_loss_pct=0.55,
        take_profit_ladder=[
            {"multiple": 2.0, "fraction": 0.55},
            {"multiple": 5.0, "fraction": 0.15},
        ],
        runner_fraction=0.30,
        runner_requires_cost_recovered=True,
        runner_exits_on_rug=True,
        trailing_activate_pct=1.0,
        trailing_distance_pct=0.45,
        max_hold_s=1800,
        stagnant_after_s=420,
        stagnant_min_gain_pct=0.20,
    )


class TestCostRecovery:
    def test_not_recovered_initially(self) -> None:
        assert not make_position().cost_recovered

    def test_recovered_after_selling_enough(self) -> None:
        position = make_position(cost_sol=0.05, realised_sol=0.06)
        assert position.cost_recovered

    def test_fees_count_against_recovery(self) -> None:
        """Recovering the stake but not the fees is not recovery."""
        position = make_position(cost_sol=0.05, realised_sol=0.0502)
        position.fees_sol = 0.001
        assert not position.cost_recovered

    def test_runner_token_reservation(self) -> None:
        position = make_position()
        assert position.runner_tokens(0.30) == pytest.approx(150_000.0)
        assert position.runner_tokens(0.0) == 0.0


class TestRunnerIsProtected:
    """Once free, the rules that would close a big winner are switched off."""

    def _free_runner(self) -> Position:
        # 30% left, stake already recovered.
        return make_position(
            multiple=8.0, cost_sol=0.05, realised_sol=0.12, tokens_held_fraction=0.30
        )

    def test_max_hold_does_not_close_a_runner(self, moonshot: ExitConfig) -> None:
        position = self._free_runner()
        position.opened_at = time.time() - 86_400 * 3      # three days old
        assert evaluate_exit(position, moonshot).kind == "none"

    def test_stagnation_does_not_close_a_runner(self, moonshot: ExitConfig) -> None:
        position = self._free_runner()
        position.current_price_sol = position.entry_price_sol * 1.01
        position.opened_at = time.time() - 3600
        assert evaluate_exit(position, moonshot).kind == "none"

    def test_trailing_stop_does_not_close_a_runner(self, moonshot: ExitConfig) -> None:
        """A 60% pullback from the peak is routine inside a real run."""
        position = self._free_runner()
        position.trailing_armed = True
        position.peak_price_sol = position.entry_price_sol * 20.0
        position.current_price_sol = position.entry_price_sol * 8.0
        assert evaluate_exit(position, moonshot).kind == "none"

    def test_stop_loss_does_not_close_a_free_runner(self, moonshot: ExitConfig) -> None:
        """Below entry but already paid for — there is nothing left to protect,
        and stopping out only guarantees you are not holding if it runs."""
        position = self._free_runner()
        position.current_price_sol = position.entry_price_sol * 0.2
        assert evaluate_exit(position, moonshot).kind == "none"

    def test_rug_still_empties_a_runner(self, moonshot: ExitConfig) -> None:
        """Riding to zero on purpose is not the same as holding through noise."""
        action = evaluate_exit(self._free_runner(), moonshot, dev_sold=True)
        assert action.kind == "full"
        assert action.reason == "dev_sold"

    def test_runner_can_be_configured_to_ride_through_rugs(self, moonshot: ExitConfig) -> None:
        moonshot.runner_exits_on_rug = False
        assert evaluate_exit(self._free_runner(), moonshot, dev_sold=True).kind == "none"

    def test_stale_price_still_exits(self, moonshot: ExitConfig) -> None:
        """Flying blind is not the same as holding conviction."""
        position = self._free_runner()
        position.last_price_ts = time.time() - 600
        assert evaluate_exit(position, moonshot).kind == "full"


class TestRunnerNotYetFree:
    """Before the stake is recovered, all the normal rules still apply."""

    def _unrecovered(self) -> Position:
        return make_position(
            multiple=1.2, cost_sol=0.05, realised_sol=0.0, tokens_held_fraction=0.30
        )

    def test_stop_loss_applies(self, moonshot: ExitConfig) -> None:
        position = self._unrecovered()
        position.current_price_sol = position.entry_price_sol * 0.3
        action = evaluate_exit(position, moonshot)
        assert action.kind == "full" and "stop_loss" in action.reason

    def test_max_hold_applies(self, moonshot: ExitConfig) -> None:
        position = self._unrecovered()
        position.opened_at = time.time() - 7200
        assert evaluate_exit(position, moonshot).kind == "full"

    def test_can_be_disabled_entirely(self, moonshot: ExitConfig) -> None:
        """runner_requires_cost_recovered=False rides from the start."""
        moonshot.runner_requires_cost_recovered = False
        position = self._unrecovered()
        position.opened_at = time.time() - 7200
        assert evaluate_exit(position, moonshot).kind == "none"


class TestLadderNeverEatsTheRunner:
    def test_ladder_stops_at_the_reserved_fraction(self, moonshot: ExitConfig) -> None:
        """Without this the ladder eventually sells everything and there is
        nothing left to catch a large move."""
        position = make_position(multiple=6.0)
        position.ladder_filled = [0]
        position.tokens_held = position.tokens_bought * 0.45
        position.realised_sol = 0.12

        action = evaluate_exit(position, moonshot)
        assert action.should_sell
        remaining = position.tokens_held * (1 - action.fraction)
        assert remaining >= position.runner_tokens(0.30) * 0.99

    def test_no_sale_when_only_the_runner_remains(self, moonshot: ExitConfig) -> None:
        position = make_position(multiple=20.0, tokens_held_fraction=0.30)
        position.ladder_filled = [0, 1]
        position.realised_sol = 0.20
        assert evaluate_exit(position, moonshot).kind == "none"

    def test_runner_disabled_sells_everything(self) -> None:
        """runner_fraction=0 restores classic full-exit behaviour."""
        config = ExitConfig(
            runner_fraction=0.0,
            take_profit_ladder=[{"multiple": 2.0, "fraction": 1.0}],
        )
        action = evaluate_exit(make_position(multiple=3.0), config)
        assert action.should_sell and action.fraction == pytest.approx(1.0)


class TestMoonshotPresetIsCoherent:
    def test_preset_loads(self, tmp_path) -> None:
        import yaml

        path = tmp_path / "config.yaml"
        path.write_text(yaml.safe_dump({"strategy_preset": "moonshot"}))
        config = load_config(path)
        assert config.exits.runner_fraction == 0.30

    def test_authority_checks_still_mandatory(self, tmp_path) -> None:
        """No preset, however aggressive, may disable the checks for whether a
        token can be sold at all."""
        import yaml

        path = tmp_path / "config.yaml"
        path.write_text(yaml.safe_dump({"strategy_preset": "moonshot"}))
        config = load_config(path)
        assert config.gates.require_mint_authority_revoked
        assert config.gates.require_freeze_authority_revoked
        assert config.intel.require_sellable

    def test_it_does_not_arm_live_trading(self, tmp_path) -> None:
        import yaml

        path = tmp_path / "config.yaml"
        path.write_text(yaml.safe_dump({"strategy_preset": "moonshot"}))
        assert load_config(path).is_live is False

    def test_position_size_survives_a_long_losing_streak(self, tmp_path) -> None:
        """The survival property the whole strategy depends on: a 1 SOL
        bankroll must absorb 200+ consecutive total losses, because at a 10%
        hit rate long streaks are routine rather than exceptional."""
        import yaml

        path = tmp_path / "config.yaml"
        path.write_text(yaml.safe_dump({"strategy_preset": "moonshot"}))
        config = load_config(path)

        bankroll = 1.0
        spendable = bankroll - config.risk.min_sol_reserve
        shots = spendable / config.risk.max_position_sol
        assert shots >= 200, f"only {shots:.0f} shots before ruin"

    def test_ladder_recovers_cost_before_the_runner_rides(self, tmp_path) -> None:
        """The first rung alone must return more than the stake, or the runner
        never becomes free and every exemption above stays switched off."""
        import yaml

        path = tmp_path / "config.yaml"
        path.write_text(yaml.safe_dump({"strategy_preset": "moonshot"}))
        config = load_config(path)

        first = config.exits.take_profit_ladder[0]
        # Selling `fraction` of the position at `multiple` returns
        # multiple * fraction of the original cost.
        recovered = first["multiple"] * first["fraction"]
        assert recovered > 1.0, (
            f"first rung returns only {recovered:.2f}x the stake; "
            "the runner would never become free"
        )


class TestRunnersDoNotBlockNewEntries:
    """BUG found while simulating `moonshot`: runners never close by design,
    so counting them against `max_concurrent_positions` meant the bot stopped
    trading once it had accumulated that many winners. The strategy would shut
    itself down precisely by succeeding.

    A free runner has no capital at risk, so it consumes no risk budget.
    """

    async def _portfolio(self, tmp_path, runner_fraction: float = 0.30):
        from mctb.brain.portfolio import Portfolio
        from mctb.config import Config
        from mctb.db import Database

        config = Config()
        config.exits.runner_fraction = runner_fraction
        config.risk.max_concurrent_positions = 3

        db = Database(tmp_path / "runner.db")
        await db.connect()

        class _Ex:
            paper = True

            async def bankroll(self):
                return 1.0

            async def buy(self, *a, **k): ...
            async def sell(self, *a, **k): ...

        class _Oracle:
            async def prices(self, mints):
                return {}

        return Portfolio(config, db, _Ex(), _Oracle()), db  # type: ignore[arg-type]

    async def test_free_runners_are_excluded_from_the_risk_budget(self, tmp_path) -> None:
        portfolio, db = await self._portfolio(tmp_path)
        try:
            for i in range(3):
                position = make_position(
                    multiple=10.0, cost_sol=0.05, realised_sol=0.20,
                    tokens_held_fraction=0.30,
                )
                position.mint = f"RUNNER{i}"
                portfolio.positions[position.mint] = position

            assert portfolio.open_count == 3
            assert portfolio.at_risk_count == 0, "free runners must not block entries"
            assert portfolio.free_runner_count == 3
        finally:
            await db.close()

    async def test_unrecovered_positions_still_count(self, tmp_path) -> None:
        portfolio, db = await self._portfolio(tmp_path)
        try:
            position = make_position(cost_sol=0.05, realised_sol=0.0)
            position.mint = "OPEN"
            portfolio.positions["OPEN"] = position
            assert portfolio.at_risk_count == 1
        finally:
            await db.close()

    async def test_partially_sold_but_unrecovered_still_counts(self, tmp_path) -> None:
        portfolio, db = await self._portfolio(tmp_path)
        try:
            position = make_position(
                cost_sol=0.05, realised_sol=0.01, tokens_held_fraction=0.30
            )
            position.mint = "PARTIAL"
            portfolio.positions["PARTIAL"] = position
            assert portfolio.at_risk_count == 1
        finally:
            await db.close()

    async def test_disabled_runner_counts_everything(self, tmp_path) -> None:
        portfolio, db = await self._portfolio(tmp_path, runner_fraction=0.0)
        try:
            position = make_position(cost_sol=0.05, realised_sol=0.20)
            position.mint = "X"
            portfolio.positions["X"] = position
            assert portfolio.at_risk_count == 1
        finally:
            await db.close()


class TestRunnerAccountingEndToEnd:
    def test_a_1000x_runner_is_actually_realised(self, moonshot: ExitConfig) -> None:
        """The scenario this whole feature exists for, played out."""
        position = make_position(cost_sol=0.05)
        entry = position.entry_price_sol

        # 2x — first rung sells 55%.
        position.current_price_sol = entry * 2.0
        action = evaluate_exit(position, moonshot)
        assert action.should_sell
        sold = position.tokens_held * action.fraction
        apply_partial_fill(position, sold, sold * position.current_price_sol,
                           0.0002, action.ladder_rung)

        assert position.cost_recovered, "stake must be back after the first rung"

        # 5x — second rung sells 15%.
        position.current_price_sol = entry * 5.0
        action = evaluate_exit(position, moonshot)
        if action.should_sell:
            sold = position.tokens_held * action.fraction
            apply_partial_fill(position, sold, sold * position.current_price_sol,
                               0.0002, action.ladder_rung)

        # Now only the runner is left, and it must survive everything.
        assert position.is_runner_only(moonshot.runner_fraction)

        position.current_price_sol = entry * 2.0        # a brutal drawdown
        position.peak_price_sol = entry * 5.0
        position.trailing_armed = True
        position.opened_at = time.time() - 86_400 * 2   # days later
        assert evaluate_exit(position, moonshot).kind == "none", "runner was closed"

        # ...and it runs.
        position.current_price_sol = entry * 1000.0
        assert evaluate_exit(position, moonshot).kind == "none"

        # 30% of the position at 1000x, on a 0.05 SOL stake.
        assert position.total_pnl_sol > 10.0
        assert position.pnl_pct > 200
