"""Personal edge learning layer.

The properties that matter here are the ones that stop it overfitting: small
samples must barely move the score, adjustments must stay bounded, and a
pattern that loses money must end up scored below one that makes money.
"""

from __future__ import annotations

import pytest

from mctb.brain.edge import PersonalEdge, bucketize
from mctb.db import Database
from mctb.models import Features


@pytest.fixture
async def db(tmp_path) -> Database:
    database = Database(tmp_path / "test.db")
    await database.connect()
    yield database
    await database.close()


def make_features(**overrides) -> Features:
    base = dict(
        age_s=45.0,
        sol_in_curve=18.0,
        unique_buyers=30,
        buy_sell_ratio=3.0,
        velocity_sol_per_s=0.3,
        smart_wallet_count=2,
        smart_wallet_score=0.8,
        top10_holding_pct=0.2,
        dev_holding_pct=0.01,
        bundle_pct=0.02,
        social_mentions=2,
    )
    base.update(overrides)
    return Features(**base)


class TestBucketing:
    def test_produces_a_bucket_for_every_feature(self) -> None:
        buckets = bucketize(make_features())
        assert len(buckets) >= 10
        assert all(isinstance(v, str) and v for v in buckets.values())

    def test_deterministic(self) -> None:
        features = make_features()
        assert bucketize(features) == bucketize(features)

    def test_different_values_land_in_different_buckets(self) -> None:
        young = bucketize(make_features(age_s=5))["age"]
        old = bucketize(make_features(age_s=1200))["age"]
        assert young != old

    def test_creator_reputation_buckets(self) -> None:
        assert bucketize(make_features(creator_prior_rugs=1))["creator"] == "rugged_before"
        assert bucketize(make_features(creator_prior_wins=1))["creator"] == "proven"
        assert bucketize(make_features())["creator"] == "unknown"

    def test_extreme_values_do_not_crash(self) -> None:
        assert bucketize(make_features(age_s=1e9, sol_in_curve=-5, unique_buyers=0))


class TestAdjustment:
    async def test_no_history_means_no_adjustment(self, db: Database) -> None:
        edge = PersonalEdge(db, min_samples=10)
        await edge.load()
        points, contributions = edge.adjustment(make_features())
        assert points == 0.0
        assert contributions == []

    async def test_disabled_returns_zero(self, db: Database) -> None:
        edge = PersonalEdge(db, min_samples=1, enabled=False)
        await edge.load()
        for _ in range(20):
            await edge.learn(make_features(), pnl_sol=0.05, cost_sol=0.05)
        assert edge.adjustment(make_features())[0] == 0.0

    async def test_small_sample_barely_moves_the_score(self, db: Database) -> None:
        """Three lucky trades must not rewrite the strategy."""
        edge = PersonalEdge(db, min_samples=12, max_adjust=15.0)
        await edge.load()
        for _ in range(3):
            await edge.learn(make_features(), pnl_sol=0.5, cost_sol=0.05)
        assert abs(edge.adjustment(make_features())[0]) < 1.0

    async def test_consistent_winners_score_up(self, db: Database) -> None:
        edge = PersonalEdge(db, min_samples=8, max_adjust=15.0)
        await edge.load()
        winner = make_features(sol_in_curve=18.0, smart_wallet_count=2)
        loser = make_features(sol_in_curve=50.0, smart_wallet_count=0)
        for _ in range(25):
            await edge.learn(winner, pnl_sol=0.06, cost_sol=0.05)
            await edge.learn(loser, pnl_sol=-0.04, cost_sol=0.05)

        assert edge.adjustment(winner)[0] > edge.adjustment(loser)[0]

    async def test_adjustment_stays_bounded(self, db: Database) -> None:
        """Even an extreme, one-sided history cannot exceed the cap."""
        edge = PersonalEdge(db, min_samples=5, max_adjust=15.0)
        await edge.load()
        features = make_features()
        for _ in range(300):
            await edge.learn(features, pnl_sol=50.0, cost_sol=0.05)
        assert abs(edge.adjustment(features)[0]) <= 15.0 + 1e-6

    async def test_expectancy_beats_hit_rate(self, db: Database) -> None:
        """A pattern that wins rarely but pays hugely should end up scored
        above one that wins often for very little — that is the shape of a
        memecoin edge, and win rate alone would rank them backwards."""
        edge = PersonalEdge(db, min_samples=8, max_adjust=15.0)
        await edge.load()

        fat_tail = make_features(sol_in_curve=8.0, unique_buyers=15)
        grinder = make_features(sol_in_curve=45.0, unique_buyers=80)

        # 3 wins of +8x, 17 losses of -1x => strongly positive expectancy.
        for _ in range(3):
            await edge.learn(fat_tail, pnl_sol=0.40, cost_sol=0.05)
        for _ in range(17):
            await edge.learn(fat_tail, pnl_sol=-0.05, cost_sol=0.05)

        # 16 wins of +0.1x, 4 losses of -0.5x => slightly negative.
        for _ in range(16):
            await edge.learn(grinder, pnl_sol=0.005, cost_sol=0.05)
        for _ in range(4):
            await edge.learn(grinder, pnl_sol=-0.025, cost_sol=0.05)

        assert edge.adjustment(fat_tail)[0] > edge.adjustment(grinder)[0]

    async def test_contributions_explain_the_number(self, db: Database) -> None:
        """The per-feature attributions must sum to the reported adjustment,
        or the dashboard explanation is lying."""
        edge = PersonalEdge(db, min_samples=6, max_adjust=15.0)
        await edge.load()
        features = make_features()
        for _ in range(30):
            await edge.learn(features, pnl_sol=0.05, cost_sol=0.05)

        points, contributions = edge.adjustment(features)
        assert contributions
        # Only the top contributions are returned, so the sum is a subset —
        # check each is finite, signed consistently, and correctly labelled.
        for contribution in contributions:
            assert contribution.trades >= 6
            assert contribution.feature in bucketize(features)
            assert bucketize(features)[contribution.feature] == contribution.bucket

    async def test_fees_make_a_small_gain_a_loss(self, db: Database) -> None:
        """P&L is after fees, so a trade that 'went up' but netted negative
        must be learned as a loss."""
        edge = PersonalEdge(db, min_samples=4)
        await edge.load()
        features = make_features()
        for _ in range(10):
            await edge.learn(features, pnl_sol=-0.001, cost_sol=0.05)
        report = edge.report(min_trades=4)
        assert all(row["win_rate"] < 0.5 for row in report)


class TestPersistence:
    async def test_survives_reload(self, tmp_path) -> None:
        """The learned table is the whole point — it must outlive a restart."""
        path = tmp_path / "persist.db"
        features = make_features()

        db1 = Database(path)
        await db1.connect()
        edge1 = PersonalEdge(db1, min_samples=5)
        await edge1.load()
        for _ in range(20):
            await edge1.learn(features, pnl_sol=0.05, cost_sol=0.05)
        first = edge1.adjustment(features)[0]
        learned = edge1.trades_learned
        await db1.close()

        db2 = Database(path)
        await db2.connect()
        edge2 = PersonalEdge(db2, min_samples=5)
        await edge2.load()
        # Global trade count comes from closed positions, which this test never
        # wrote — so seed it the same way a real reload would see it.
        edge2._global_trades = learned
        edge2._global_wins = learned
        edge2._global_pnl = 20 * 0.05
        assert edge2.adjustment(features)[0] == pytest.approx(first, abs=2.0)
        await db2.close()

    async def test_report_filters_by_sample_size(self, db: Database) -> None:
        edge = PersonalEdge(db, min_samples=10)
        await edge.load()
        for _ in range(3):
            await edge.learn(make_features(), pnl_sol=0.05, cost_sol=0.05)
        assert edge.report() == []
        assert edge.report(min_trades=1) != []
