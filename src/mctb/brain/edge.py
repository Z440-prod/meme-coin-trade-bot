"""The personal edge layer — what this bot learns from *your* results.

Every public snipe bot ships the same heuristics, which is a large part of
why so few of them make money: if the signal is public, it is already priced
in by people with better latency than a Raspberry Pi. What is not public is
your own trade history — which setups your execution, your RPC latency, your
position sizes and your exit rules actually converted into SOL.

So this module keeps score. For every trade it buckets the entry features,
records the outcome, and builds a posterior estimate of how that bucket
performs *for you*. Future candidates get scored up or down accordingly.

Two design choices worth understanding, because they are the difference
between learning and overfitting:

  **Shrinkage.** A bucket with four trades tells you almost nothing. Every
  adjustment is multiplied by a confidence factor that grows with sample size,
  so early results nudge and only sustained results steer.

  **Expectancy, not just hit rate.** Memecoin returns are fat-tailed: a setup
  that wins 25% of the time and returns 8x on the winners beats one that wins
  70% of the time for 15% a go. Win rate alone would rank those backwards, so
  the adjustment blends hit rate with realised P&L per trade.

The whole thing is bounded by `scoring.personal_edge_max_adjust`. Even a
strong learned signal can only move the score so far, because a run of luck
should not be able to rewrite your strategy.
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass

from ..db import Database
from ..models import Features

log = logging.getLogger(__name__)

# Beta prior. (2, 2) is weakly informative: it says "assume roughly a coin
# flip until the data says otherwise" without being so strong it drowns out
# genuine signal.
PRIOR_ALPHA = 2.0
PRIOR_BETA = 2.0


def _bucket_numeric(value: float, edges: list[float], labels: list[str]) -> str:
    for edge, label in zip(edges, labels, strict=False):
        if value < edge:
            return label
    return labels[-1]


def bucketize(features: Features) -> dict[str, str]:
    """Turn a continuous feature vector into discrete buckets.

    Bucketing rather than regressing keeps this interpretable — you can read
    the learned table and understand it — and it needs far less data than
    fitting a model, which matters when a month of trading might be 300 rows.
    """
    return {
        "age": _bucket_numeric(
            features.age_s, [15, 60, 180, 600], ["<15s", "15-60s", "1-3m", "3-10m", ">10m"]
        ),
        "curve_sol": _bucket_numeric(
            features.sol_in_curve, [5, 15, 30, 55], ["<5", "5-15", "15-30", "30-55", ">55"]
        ),
        "unique_buyers": _bucket_numeric(
            float(features.unique_buyers), [10, 25, 50, 100], ["<10", "10-25", "25-50", "50-100", ">100"]
        ),
        "buy_sell_ratio": _bucket_numeric(
            features.buy_sell_ratio, [1.2, 1.8, 3.0, 6.0], ["<1.2", "1.2-1.8", "1.8-3", "3-6", ">6"]
        ),
        "velocity": _bucket_numeric(
            features.velocity_sol_per_s, [0.02, 0.1, 0.3, 1.0], ["<.02", ".02-.1", ".1-.3", ".3-1", ">1"]
        ),
        "smart_wallets": _bucket_numeric(
            float(features.smart_wallet_count), [1, 2, 4], ["0", "1", "2-3", "4+"]
        ),
        "smart_quality": _bucket_numeric(
            features.smart_wallet_score, [0.01, 0.6, 0.75, 0.9], ["none", "<.6", ".6-.75", ".75-.9", ">.9"]
        ),
        "top10": _bucket_numeric(
            features.top10_holding_pct, [0.15, 0.25, 0.35], ["<15%", "15-25%", "25-35%", ">35%"]
        ),
        "dev_holding": _bucket_numeric(
            features.dev_holding_pct, [0.005, 0.02, 0.05], ["~0", "<2%", "2-5%", ">5%"]
        ),
        "bundle": _bucket_numeric(
            features.bundle_pct, [0.02, 0.10, 0.25], ["~0", "<10%", "10-25%", ">25%"]
        ),
        "social": _bucket_numeric(
            float(features.social_mentions), [1, 3, 10], ["0", "1-2", "3-9", "10+"]
        ),
        "creator": (
            "rugged_before"
            if features.creator_prior_rugs > 0
            else ("proven" if features.creator_prior_wins > 0 else "unknown")
        ),
    }


@dataclass
class BucketStat:
    trades: int = 0
    wins: int = 0
    pnl_sol: float = 0.0

    @property
    def win_rate_posterior(self) -> float:
        return (self.wins + PRIOR_ALPHA) / (self.trades + PRIOR_ALPHA + PRIOR_BETA)

    @property
    def avg_pnl(self) -> float:
        return self.pnl_sol / self.trades if self.trades else 0.0


@dataclass
class EdgeContribution:
    feature: str
    bucket: str
    points: float
    trades: int
    win_rate: float
    avg_pnl: float


class PersonalEdge:
    """Learned adjustment layer, backed by the local trade database."""

    def __init__(
        self,
        db: Database,
        min_samples: int = 12,
        max_adjust: float = 15.0,
        enabled: bool = True,
    ) -> None:
        self.db = db
        self.min_samples = min_samples
        self.max_adjust = max_adjust
        self.enabled = enabled

        self._stats: dict[tuple[str, str], BucketStat] = {}
        self._global_trades = 0
        self._global_wins = 0
        self._global_pnl = 0.0

    async def load(self) -> None:
        """Read the learned table from disk."""
        raw = await self.db.edge_stats()
        self._stats = {
            key: BucketStat(trades=v["trades"], wins=v["wins"], pnl_sol=v["pnl_sol"])
            for key, v in raw.items()
        }

        summary = await self.db.performance_summary()
        self._global_trades = summary.get("trades", 0)
        self._global_wins = summary.get("wins", 0)
        self._global_pnl = summary.get("total_pnl_sol", 0.0)

        if self._global_trades:
            log.info(
                "personal edge loaded: %d trades, %.0f%% win rate, %+.4f SOL, %d learned buckets",
                self._global_trades,
                100 * self._global_wins / self._global_trades,
                self._global_pnl,
                len(self._stats),
            )
        else:
            log.info(
                "personal edge has no history yet — it contributes nothing until you have "
                "closed roughly %d trades. Paper trading builds this too.",
                self.min_samples,
            )

    # -- inference ----------------------------------------------------------

    @property
    def _global_win_rate(self) -> float:
        return (self._global_wins + PRIOR_ALPHA) / (
            self._global_trades + PRIOR_ALPHA + PRIOR_BETA
        )

    def _baseline_normalised_pnl(self) -> float:
        """Average P&L per trade, in the same units the buckets are stored in.

        Buckets store P&L normalised by position size (so a big trade does not
        dominate the learning purely by being big), while `_global_pnl` is raw
        SOL. Comparing the two directly would be a unit mismatch, so the
        baseline is derived from the bucket table itself.

        Every trade contributes exactly once to each feature, so summing one
        feature's buckets recovers the global total.
        """
        by_feature: dict[str, tuple[int, float]] = {}
        for (feature, _bucket), stat in self._stats.items():
            trades, pnl = by_feature.get(feature, (0, 0.0))
            by_feature[feature] = (trades + stat.trades, pnl + stat.pnl_sol)

        if not by_feature:
            return 0.0
        # Use the feature with the most observations; they should all agree,
        # but this is robust to a feature added partway through a history.
        trades, pnl = max(by_feature.values(), key=lambda pair: pair[0])
        return pnl / trades if trades else 0.0

    def adjustment(self, features: Features) -> tuple[float, list[EdgeContribution]]:
        """Score adjustment in points, plus a per-feature explanation.

        Positive means this pattern has historically paid you; negative means
        it has cost you. The explanation is returned so the dashboard and the
        logs can show *why*, which is what lets you sanity-check the learning
        instead of trusting it blindly.
        """
        if not self.enabled or self._global_trades < self.min_samples:
            return 0.0, []

        baseline_wr = self._global_win_rate
        baseline_pnl = self._baseline_normalised_pnl()
        contributions: list[EdgeContribution] = []
        total_logit = 0.0

        for feature, bucket in bucketize(features).items():
            stat = self._stats.get((feature, bucket))
            if stat is None or stat.trades < self.min_samples:
                continue

            # Confidence saturates as the sample grows; at min_samples it is
            # 0.5, at 4x min_samples about 0.8.
            confidence = stat.trades / (stat.trades + self.min_samples)

            # Component 1: hit rate versus your overall hit rate, in log-odds.
            p = min(0.95, max(0.05, stat.win_rate_posterior))
            p0 = min(0.95, max(0.05, baseline_wr))
            wr_logit = math.log(p / (1 - p)) - math.log(p0 / (1 - p0))

            # Component 2: realised expectancy versus your overall expectancy.
            # Both sides are in "multiples of position size", and the scale
            # floor keeps a near-zero baseline from exploding the ratio.
            scale = max(abs(baseline_pnl), 0.15)
            pnl_logit = max(-2.0, min(2.0, (stat.avg_pnl - baseline_pnl) / scale))

            # Expectancy dominates, and the weighting is lopsided on purpose.
            # Memecoin returns are fat-tailed: a setup that wins 15% of the
            # time at +8x beats one that wins 80% of the time at +10%, and
            # weighting hit rate evenly would rank those two backwards. Hit
            # rate is kept only as a low-variance regulariser for small
            # samples, where a handful of outliers can distort the mean.
            combined = (0.2 * wr_logit + 0.8 * pnl_logit) * confidence
            total_logit += combined

            contributions.append(
                EdgeContribution(
                    feature=feature,
                    bucket=bucket,
                    points=combined,
                    trades=stat.trades,
                    win_rate=stat.win_rate_posterior,
                    avg_pnl=stat.avg_pnl,
                )
            )

        if not contributions:
            return 0.0, []

        # Squash to bounded points. Dividing by sqrt(n) before the tanh means
        # more agreeing features still increase confidence, but sub-linearly —
        # without it, a dozen weakly-agreeing features saturate the cap and
        # every candidate gets the same maximum adjustment, which discriminates
        # between nothing.
        evidence = total_logit / math.sqrt(len(contributions))
        points = self.max_adjust * math.tanh(evidence / 1.5)
        contributions.sort(key=lambda c: abs(c.points), reverse=True)
        # Rescale the per-feature attributions so they add up to the final
        # number the user sees.
        if total_logit:
            factor = points / total_logit
            for contribution in contributions:
                contribution.points *= factor

        return round(points, 2), contributions[:6]

    # -- learning -----------------------------------------------------------

    async def learn(self, features: Features, pnl_sol: float, cost_sol: float) -> None:
        """Record a closed trade's outcome against its entry features.

        Called once per position close. A "win" is P&L after fees, so a trade
        that went up 3% and cost 4% in fees and slippage correctly counts as a
        loss — otherwise the bot learns to love trades that feel good and lose
        money.
        """
        if not self.enabled:
            return

        win = pnl_sol > 0
        self._global_trades += 1
        self._global_wins += int(win)
        self._global_pnl += pnl_sol

        # Normalise P&L by position size so a big trade does not dominate the
        # learning purely by being big.
        normalised = pnl_sol / cost_sol if cost_sol > 0 else pnl_sol

        for feature, bucket in bucketize(features).items():
            key = (feature, bucket)
            stat = self._stats.setdefault(key, BucketStat())
            stat.trades += 1
            stat.wins += int(win)
            stat.pnl_sol += normalised
            await self.db.bump_edge(feature, bucket, win=win, pnl_sol=normalised)

        log.info(
            "edge updated from %s trade (%+.4f SOL, %+.1f%%); %d trades learned",
            "winning" if win else "losing",
            pnl_sol,
            100 * normalised,
            self._global_trades,
        )

    # -- introspection ------------------------------------------------------

    def report(self, min_trades: int | None = None) -> list[dict]:
        """The learned table, best buckets first. Rendered on the dashboard
        and fed to the nightly LLM review."""
        floor = self.min_samples if min_trades is None else min_trades
        rows = [
            {
                "feature": feature,
                "bucket": bucket,
                "trades": stat.trades,
                "win_rate": round(stat.win_rate_posterior, 3),
                "avg_pnl_pct": round(100 * stat.avg_pnl, 2),
                "total_pnl": round(stat.pnl_sol, 4),
            }
            for (feature, bucket), stat in self._stats.items()
            if stat.trades >= floor
        ]
        rows.sort(key=lambda r: r["avg_pnl_pct"], reverse=True)
        return rows

    @property
    def trades_learned(self) -> int:
        return self._global_trades

    @property
    def ready(self) -> bool:
        return self.enabled and self._global_trades >= self.min_samples
