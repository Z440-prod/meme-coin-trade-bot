"""Hard gates and portfolio risk limits."""

from __future__ import annotations

from mctb.brain.gates import RiskGate, apply_gates
from mctb.config import Config
from mctb.models import Candidate


class TestTokenGates:
    def test_good_candidate_passes(self, good_candidate: Candidate, config: Config) -> None:
        result = apply_gates(good_candidate, config)
        assert result.passed, result.failures

    def test_freeze_authority_blocks(self, good_candidate: Candidate, config: Config) -> None:
        """The honeypot check. Nothing else in the system should be able to
        override this."""
        good_candidate.safety.freeze_authority_revoked = False
        result = apply_gates(good_candidate, config)
        assert not result.passed
        assert any("freeze authority" in f for f in result.failures)

    def test_mint_authority_blocks(self, good_candidate: Candidate, config: Config) -> None:
        good_candidate.safety.mint_authority_revoked = False
        result = apply_gates(good_candidate, config)
        assert not result.passed
        assert any("mint authority" in f for f in result.failures)

    def test_too_young(self, good_candidate: Candidate, config: Config) -> None:
        good_candidate.features.age_s = 2.0
        assert not apply_gates(good_candidate, config).passed

    def test_too_old(self, good_candidate: Candidate, config: Config) -> None:
        good_candidate.features.age_s = 10_000.0
        assert not apply_gates(good_candidate, config).passed

    def test_curve_too_thin(self, good_candidate: Candidate, config: Config) -> None:
        good_candidate.features.sol_in_curve = 0.5
        result = apply_gates(good_candidate, config)
        assert any("too thin" in f for f in result.failures)

    def test_curve_too_full(self, good_candidate: Candidate, config: Config) -> None:
        good_candidate.features.sol_in_curve = 200.0
        assert not apply_gates(good_candidate, config).passed

    def test_too_few_buyers(self, good_candidate: Candidate, config: Config) -> None:
        good_candidate.features.unique_buyers = 2
        assert not apply_gates(good_candidate, config).passed

    def test_weak_buy_pressure(self, good_candidate: Candidate, config: Config) -> None:
        good_candidate.features.buy_sell_ratio = 0.5
        assert not apply_gates(good_candidate, config).passed

    def test_dev_holding_too_high(self, good_candidate: Candidate, config: Config) -> None:
        good_candidate.safety.dev_holding_pct = 0.4
        assert not apply_gates(good_candidate, config).passed

    def test_concentration_too_high(self, good_candidate: Candidate, config: Config) -> None:
        good_candidate.safety.top10_holding_pct = 0.9
        assert not apply_gates(good_candidate, config).passed

    def test_bundled_launch(self, good_candidate: Candidate, config: Config) -> None:
        good_candidate.features.bundle_pct = 0.5
        result = apply_gates(good_candidate, config)
        assert any("launch slot" in f for f in result.failures)

    def test_blocked_creator(self, good_candidate: Candidate, config: Config) -> None:
        result = apply_gates(
            good_candidate, config, blocked_creators={good_candidate.creator}
        )
        assert any("rugged you before" in f for f in result.failures)

    def test_already_holding(self, good_candidate: Candidate, config: Config) -> None:
        result = apply_gates(good_candidate, config, already_holding=True)
        assert any("already holding" in f for f in result.failures)

    def test_recently_traded(self, good_candidate: Candidate, config: Config) -> None:
        result = apply_gates(good_candidate, config, recently_traded=True)
        assert any("within the last" in f for f in result.failures)

    def test_unchecked_safety_blocks(self, good_candidate: Candidate, config: Config) -> None:
        """A token whose screen did not run is not a token we bet on."""
        good_candidate.safety.checked = False
        result = apply_gates(good_candidate, config)
        assert any("did not run" in f for f in result.failures)

    def test_name_blocklist(self, good_candidate: Candidate, config: Config) -> None:
        config.gates.name_blocklist = ["test"]
        result = apply_gates(good_candidate, config)
        assert any("blocklist" in f for f in result.failures)

    def test_require_smart_wallet(self, good_candidate: Candidate, config: Config) -> None:
        config.gates.require_smart_wallet = True
        good_candidate.smart_wallets = []
        assert not apply_gates(good_candidate, config).passed

    def test_reports_all_failures_not_just_first(
        self, good_candidate: Candidate, config: Config
    ) -> None:
        good_candidate.features.age_s = 1.0
        good_candidate.features.unique_buyers = 1
        good_candidate.safety.freeze_authority_revoked = False
        assert len(apply_gates(good_candidate, config).failures) >= 3


class TestRiskGate:
    def test_allows_normal_trade(self, config: Config) -> None:
        gate = RiskGate(config)
        result = gate.check(
            open_positions=0, bankroll_sol=1.0, daily_pnl_sol=0.0, proposed_size_sol=0.02
        )
        assert result.passed, result.failures

    def test_position_limit(self, config: Config) -> None:
        gate = RiskGate(config)
        result = gate.check(
            open_positions=config.risk.max_concurrent_positions,
            bankroll_sol=1.0,
            daily_pnl_sol=0.0,
            proposed_size_sol=0.02,
        )
        assert any("position limit" in f for f in result.failures)

    def test_daily_loss_limit_halts(self, config: Config) -> None:
        gate = RiskGate(config)
        result = gate.check(
            open_positions=0,
            bankroll_sol=1.0,
            daily_pnl_sol=-config.risk.daily_loss_limit_sol - 0.01,
            proposed_size_sol=0.02,
        )
        assert not result.passed
        assert gate.halted

    def test_fee_reserve_protected(self, config: Config) -> None:
        """Never spend into the gas reserve — a wallet with no SOL cannot
        pay the fee to sell its own tokens."""
        gate = RiskGate(config)
        result = gate.check(
            open_positions=0,
            bankroll_sol=config.risk.min_sol_reserve + 0.001,
            daily_pnl_sol=0.0,
            proposed_size_sol=0.02,
        )
        assert any("fee reserve" in f for f in result.failures)

    def test_zero_size_rejected(self, config: Config) -> None:
        gate = RiskGate(config)
        result = gate.check(
            open_positions=0, bankroll_sol=1.0, daily_pnl_sol=0.0, proposed_size_sol=0.0
        )
        assert any("zero" in f for f in result.failures)

    def test_hourly_trade_cap(self, config: Config) -> None:
        gate = RiskGate(config)
        for _ in range(config.risk.max_positions_per_hour):
            gate.record_open(0.001)
        result = gate.check(
            open_positions=0, bankroll_sol=1.0, daily_pnl_sol=0.0, proposed_size_sol=0.001
        )
        assert any("hourly trade limit" in f for f in result.failures)

    def test_hourly_deployment_cap(self, config: Config) -> None:
        gate = RiskGate(config)
        gate.record_open(config.risk.max_sol_deployed_per_hour)
        result = gate.check(
            open_positions=0, bankroll_sol=5.0, daily_pnl_sol=0.0, proposed_size_sol=0.02
        )
        assert any("deployment cap" in f for f in result.failures)

    def test_consecutive_losses_halt(self, config: Config) -> None:
        gate = RiskGate(config)
        for _ in range(config.risk.consecutive_loss_limit):
            gate.record_close(-0.01)
        assert gate.halted
        assert "consecutive" in gate.halt_reason

    def test_a_win_resets_the_streak(self, config: Config) -> None:
        gate = RiskGate(config)
        for _ in range(config.risk.consecutive_loss_limit - 1):
            gate.record_close(-0.01)
        gate.record_close(0.02)
        assert gate.consecutive_losses == 0
        assert not gate.halted

    def test_halt_blocks_then_expires(self, config: Config) -> None:
        gate = RiskGate(config)
        gate.halt("test halt")
        assert not gate.check(
            open_positions=0, bankroll_sol=1.0, daily_pnl_sol=0.0, proposed_size_sol=0.01
        ).passed

        # Simulate the cooldown elapsing.
        gate.halted_until = 0.0
        result = gate.check(
            open_positions=0, bankroll_sol=1.0, daily_pnl_sol=0.0, proposed_size_sol=0.01
        )
        assert result.passed
        assert not gate.halted
