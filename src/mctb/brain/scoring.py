"""Deterministic fast-path scorer.

This is the component that actually decides trades, and it is deliberately
plain arithmetic: normalise each feature to 0..1, multiply by a weight, sum,
rescale to 0..100. It runs in microseconds and produces the same answer every
time for the same inputs, which means you can test it, reason about it, and
reproduce any decision it ever made from the stored feature vector.

**Why the LLM is not here.** The source specification put a language model in
the decision path. On a Raspberry Pi a quantized 3B model generates on the
order of 5-15 tokens per second, so a 200-token answer takes 15-40 seconds.
The entire opportunity window on a pump.fun launch is a few seconds. An LLM in
this path does not make the bot smarter, it makes it too late to trade — and
worse, it makes decisions non-reproducible.

So the split is: arithmetic decides, the LLM advises. `llm.py` runs off the
hot path, can only adjust a score already computed here, and is bounded in how
far it can move it. Where the LLM genuinely adds value is the nightly review,
where latency does not matter and reading 200 trades for patterns is exactly
what it is good at.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from ..config import Config
from ..models import Candidate, Features, ScoreBreakdown
from .edge import EdgeContribution, PersonalEdge

log = logging.getLogger(__name__)


def _clamp01(value: float) -> float:
    return max(0.0, min(1.0, value))


def _ramp(value: float, low: float, high: float) -> float:
    """Linear 0..1 ramp between low and high."""
    if high <= low:
        return 0.0
    return _clamp01((value - low) / (high - low))


def _window(value: float, ideal_low: float, ideal_high: float, hard_low: float, hard_high: float) -> float:
    """Score 1.0 inside the ideal band, tapering to 0 at the hard edges.

    Used where both too little and too much are bad — curve depth being the
    obvious case: too thin and you cannot exit, too full and the move is over.
    """
    if ideal_low <= value <= ideal_high:
        return 1.0
    if value < ideal_low:
        return _ramp(value, hard_low, ideal_low)
    return 1.0 - _ramp(value, ideal_high, hard_high)


@dataclass
class ScoredCandidate:
    candidate: Candidate
    breakdown: ScoreBreakdown
    edge_contributions: list[EdgeContribution]

    @property
    def score(self) -> float:
        return self.breakdown.final_score


class Scorer:
    def __init__(self, config: Config, edge: PersonalEdge | None = None) -> None:
        self.config = config
        self.edge = edge

    # -- feature sub-scores -------------------------------------------------

    def _smart_wallet_component(self, f: Features) -> float:
        """Quality first, count second.

        One wallet with a 0.9 reputation is a better signal than four wallets
        at 0.5, so quality sets the ceiling and count only tops it up.
        """
        if f.smart_wallet_count == 0:
            return 0.0
        quality = _clamp01(f.smart_wallet_score)
        count_bonus = _ramp(float(f.smart_wallet_count), 1.0, 4.0) * 0.3
        return _clamp01(quality * 0.8 + count_bonus)

    def _buy_pressure_component(self, f: Features) -> float:
        # 3:1 buys to sells is strong; beyond 6:1 usually means the sells just
        # have not arrived yet rather than that demand is infinite.
        return _ramp(f.buy_sell_ratio, 1.0, 5.0)

    def _unique_buyers_component(self, f: Features) -> float:
        # Log-ish ramp: going from 5 to 25 buyers matters much more than 200
        # to 220.
        return _ramp(float(f.unique_buyers), 5.0, 60.0)

    def _curve_component(self, f: Features) -> float:
        gates = self.config.gates
        return _window(
            f.sol_in_curve,
            ideal_low=max(gates.min_sol_in_curve, 5.0),
            ideal_high=min(gates.max_sol_in_curve, 40.0),
            hard_low=0.0,
            hard_high=85.0,
        )

    def _distribution_component(self, f: Features) -> float:
        """Reward tokens whose supply is spread out.

        Concentration is the thing that turns a paper gain into a real loss:
        if ten wallets can end the run whenever they choose, your exit is at
        their discretion, not yours.
        """
        top10 = 1.0 - _ramp(f.top10_holding_pct, 0.10, 0.50)
        dev = 1.0 - _ramp(f.dev_holding_pct, 0.005, 0.08)
        bundle = 1.0 - _ramp(f.bundle_pct, 0.02, 0.30)
        return _clamp01(0.45 * top10 + 0.30 * dev + 0.25 * bundle)

    def _social_component(self, f: Features) -> float:
        if f.social_mentions == 0:
            return 0.0
        count = _ramp(float(f.social_mentions), 1.0, 8.0)
        # Reach is log10(followers) summed, so ~15 is a couple of large accounts.
        reach = _ramp(f.social_reach, 4.0, 20.0)
        return _clamp01(0.5 * count + 0.5 * reach)

    def _velocity_component(self, f: Features) -> float:
        """Net SOL inflow per second — is the move happening right now?

        The single most perishable feature in the set, and the reason the
        Scout keeps a rolling window instead of lifetime counters.
        """
        if f.velocity_sol_per_s <= 0:
            return 0.0
        return _ramp(f.velocity_sol_per_s, 0.01, 0.8)

    def _creator_component(self, f: Features) -> float:
        if f.creator_prior_rugs > 0:
            return 0.0
        if f.creator_prior_wins > 0:
            return _clamp01(0.6 + 0.1 * f.creator_prior_wins)
        return 0.5  # unknown creator — neutral, which is most of them

    # -- main entry point ---------------------------------------------------

    def score(self, candidate: Candidate) -> ScoredCandidate:
        f = candidate.features
        w = self.config.scoring

        components = {
            "smart_wallets": (self._smart_wallet_component(f), w.w_smart_wallets),
            "buy_pressure": (self._buy_pressure_component(f), w.w_buy_pressure),
            "unique_buyers": (self._unique_buyers_component(f), w.w_unique_buyers),
            "curve_progress": (self._curve_component(f), w.w_curve_progress),
            "holder_distribution": (self._distribution_component(f), w.w_holder_distribution),
            "social": (self._social_component(f), w.w_social),
            "velocity": (self._velocity_component(f), w.w_velocity),
            "creator_history": (self._creator_component(f), w.w_creator_history),
        }

        total_weight = sum(weight for _sub, weight in components.values())
        if total_weight <= 0:
            raise ValueError(
                "all scoring weights are zero — check the `scoring:` section of your config"
            )

        weighted = sum(sub * weight for sub, weight in components.values())
        base = 100.0 * weighted / total_weight

        breakdown = ScoreBreakdown(
            base_score=round(base, 2),
            components={
                name: round(sub * weight, 2) for name, (sub, weight) in components.items()
            },
        )

        # Learned adjustment from your own trade history.
        contributions: list[EdgeContribution] = []
        if self.edge is not None and w.personal_edge_enabled:
            adjust, contributions = self.edge.adjustment(f)
            breakdown.personal_edge_adjust = adjust
            if contributions:
                top = contributions[0]
                breakdown.notes.append(
                    f"personal edge {adjust:+.1f} "
                    f"(strongest: {top.feature}={top.bucket}, "
                    f"{top.trades} trades, {top.avg_pnl:+.1%} avg)"
                )

        breakdown.final_score = round(
            max(0.0, min(100.0, base + breakdown.personal_edge_adjust)), 2
        )

        return ScoredCandidate(
            candidate=candidate, breakdown=breakdown, edge_contributions=contributions
        )

    def size_position(self, score: float, bankroll_sol: float) -> float:
        """How much SOL to risk on a given conviction level.

        Scaled by conviction above the entry threshold, capped by both a hard
        SOL ceiling and a fraction of bankroll. The fraction matters more than
        it looks: it makes the bot shrink its own size automatically as it
        loses, which is the mechanism that turns a drawdown into a survivable
        one instead of a terminal one.
        """
        risk = self.config.risk
        threshold = self.config.scoring.entry_threshold

        if score < threshold:
            return 0.0

        # 0 at the threshold, 1 at 100.
        conviction = _clamp01((score - threshold) / max(1.0, 100.0 - threshold))
        # Never size below 40% of max on a qualifying signal — a trade too
        # small to matter still pays full fees.
        scale = 0.4 + 0.6 * conviction

        by_cap = risk.max_position_sol * scale
        by_bankroll = bankroll_sol * risk.position_pct_of_bankroll * scale
        size = min(by_cap, by_bankroll)

        # Leave the fee reserve intact.
        spendable = max(0.0, bankroll_sol - risk.min_sol_reserve)
        size = min(size, spendable)

        # Below this a position is all fees and no exposure.
        return round(size, 6) if size >= 0.001 else 0.0
