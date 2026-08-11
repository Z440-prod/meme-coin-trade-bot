"""Deterministic scorer and position sizing."""

from __future__ import annotations

import pytest

from mctb.brain.scoring import Scorer
from mctb.config import Config
from mctb.models import Candidate, Features


@pytest.fixture
def scorer(config: Config) -> Scorer:
    return Scorer(config, edge=None)


class TestScoring:
    def test_good_candidate_scores_well(
        self, scorer: Scorer, good_candidate: Candidate
    ) -> None:
        assert scorer.score(good_candidate).score >= 60

    def test_empty_candidate_scores_low(self, scorer: Scorer) -> None:
        candidate = Candidate(mint="M" * 44, features=Features())
        assert scorer.score(candidate).score < 30

    def test_score_is_bounded(self, scorer: Scorer, good_candidate: Candidate) -> None:
        good_candidate.features.buy_sell_ratio = 10_000
        good_candidate.features.unique_buyers = 100_000
        good_candidate.features.velocity_sol_per_s = 1_000
        good_candidate.features.smart_wallet_count = 500
        assert 0 <= scorer.score(good_candidate).score <= 100

    def test_deterministic(self, scorer: Scorer, good_candidate: Candidate) -> None:
        """Same inputs must always give the same answer — this is the whole
        argument for keeping the LLM out of the decision path."""
        scores = {scorer.score(good_candidate).score for _ in range(20)}
        assert len(scores) == 1

    def test_breakdown_covers_every_component(
        self, scorer: Scorer, good_candidate: Candidate
    ) -> None:
        components = scorer.score(good_candidate).breakdown.components
        for name in (
            "smart_wallets",
            "buy_pressure",
            "unique_buyers",
            "curve_progress",
            "holder_distribution",
            "social",
            "velocity",
            "creator_history",
        ):
            assert name in components

    def test_smart_wallet_quality_beats_quantity(self, scorer: Scorer) -> None:
        """One reputable wallet should outscore several mediocre ones."""
        base = dict(
            age_s=60, sol_in_curve=15, unique_buyers=30, buy_sell_ratio=3.0,
            velocity_sol_per_s=0.3, top10_holding_pct=0.2,
        )
        one_good = Candidate(
            mint="A" * 44,
            features=Features(**base, smart_wallet_count=1, smart_wallet_score=0.95),
        )
        many_weak = Candidate(
            mint="B" * 44,
            features=Features(**base, smart_wallet_count=5, smart_wallet_score=0.45),
        )
        assert scorer.score(one_good).score > scorer.score(many_weak).score

    def test_rug_creator_scores_worse(self, scorer: Scorer, good_candidate: Candidate) -> None:
        clean = scorer.score(good_candidate).score
        good_candidate.features.creator_prior_rugs = 2
        good_candidate.features.creator_prior_wins = 0
        assert scorer.score(good_candidate).score < clean

    def test_concentration_hurts(self, scorer: Scorer, good_candidate: Candidate) -> None:
        clean = scorer.score(good_candidate).score
        good_candidate.features.top10_holding_pct = 0.55
        good_candidate.features.dev_holding_pct = 0.07
        assert scorer.score(good_candidate).score < clean

    def test_zero_weights_raise(self, config: Config, good_candidate: Candidate) -> None:
        for field in list(config.scoring.model_fields):
            if field.startswith("w_"):
                setattr(config.scoring, field, 0.0)
        with pytest.raises(ValueError, match="weights are zero"):
            Scorer(config).score(good_candidate)


class TestSizing:
    def test_zero_below_threshold(self, scorer: Scorer, config: Config) -> None:
        assert scorer.size_position(config.scoring.entry_threshold - 1, 1.0) == 0.0

    def test_scales_with_conviction(self, scorer: Scorer, config: Config) -> None:
        low = scorer.size_position(config.scoring.entry_threshold + 1, 10.0)
        high = scorer.size_position(98.0, 10.0)
        assert high > low

    def test_respects_absolute_cap(self, scorer: Scorer, config: Config) -> None:
        assert scorer.size_position(100.0, 1_000.0) <= config.risk.max_position_sol

    def test_respects_bankroll_fraction(self, scorer: Scorer, config: Config) -> None:
        """The mechanism that shrinks size automatically during a drawdown."""
        size = scorer.size_position(100.0, 0.5)
        assert size <= 0.5 * config.risk.position_pct_of_bankroll

    def test_shrinks_as_bankroll_shrinks(self, scorer: Scorer) -> None:
        big = scorer.size_position(85.0, 5.0)
        small = scorer.size_position(85.0, 0.5)
        assert small < big

    def test_never_breaches_fee_reserve(self, scorer: Scorer, config: Config) -> None:
        size = scorer.size_position(100.0, config.risk.min_sol_reserve + 0.005)
        assert size <= 0.005 + 1e-9

    def test_dust_sizes_become_zero(self, scorer: Scorer, config: Config) -> None:
        """A position too small to matter still pays full fees, so it is
        not worth opening at all."""
        assert scorer.size_position(100.0, config.risk.min_sol_reserve + 0.0001) == 0.0
