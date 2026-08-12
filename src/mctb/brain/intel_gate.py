"""Applying fused intelligence to trading decisions.

The deterministic scorer in `scoring.py` works from on-chain features and is
deliberately unchanged by all of this — it must keep working with zero
providers configured. This module is the layer that *adds* provider
intelligence on top, and like every other adjustment in the system it is
bounded and fully explained.

Two kinds of output:

**Blockers.** Binary vetoes from things that make a token untradeable rather
than merely unattractive: no sell route, a live freeze authority confirmed by
a second source, a round trip that costs more than the trade can plausibly
return. These behave exactly like the hard gates.

**Adjustments.** Bounded point movements for graded evidence — sybil-collapsed
consensus, wash-trading suspicion, astroturfed attention, insider clusters.

The sybil check is the one that most changes behaviour. A naive bot sees five
smart wallets and scores it as five independent confirmations. This collapses
them to distinct entities first, so manufactured consensus scores as what it
is: one actor, possibly one actor who knows what your bot is looking for.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from ..config import Config
from ..intel.graph import EntityGraph
from ..intel.types import TokenDossier

log = logging.getLogger(__name__)


@dataclass
class IntelVerdict:
    """What fused intel says about a candidate."""

    blockers: list[str] = field(default_factory=list)
    adjustment: float = 0.0
    notes: list[str] = field(default_factory=list)
    # Distinct actors behind the apparent smart-wallet consensus.
    effective_smart_wallets: int = 0
    sybil_ratio: float = 0.0

    @property
    def passed(self) -> bool:
        return not self.blockers

    def to_dict(self) -> dict[str, Any]:
        return {
            "passed": self.passed,
            "blockers": self.blockers,
            "adjustment": round(self.adjustment, 2),
            "notes": self.notes,
            "effective_smart_wallets": self.effective_smart_wallets,
            "sybil_ratio": round(self.sybil_ratio, 3),
        }


def evaluate_intel(
    dossier: TokenDossier,
    config: Config,
    *,
    graph: EntityGraph | None = None,
    smart_wallets: list[str] | None = None,
    round_trip_cost_pct: float | None = None,
) -> IntelVerdict:
    """Turn a dossier into blockers and a bounded score adjustment."""
    verdict = IntelVerdict()
    cfg = config.intel

    if not cfg.enabled:
        verdict.effective_smart_wallets = len(smart_wallets or [])
        return verdict

    security = dossier.security
    market = dossier.market
    narrative = dossier.narrative

    # --- Blockers ----------------------------------------------------------

    # Security blockers, but only when something actually answered. An
    # unscreened token is handled by the existing on-chain gates; we must not
    # double-block on a provider outage.
    if security.provenance.get("sources"):
        for blocker in security.blockers:
            verdict.blockers.append(f"intel: {blocker}")

    if cfg.require_sellable and security.provenance.get("sources"):
        if not security.sellable:
            # Already covered by security.blockers, but make the reason
            # unmistakable in the logs — this is the honeypot catch.
            if not any("sell route" in b for b in verdict.blockers):
                verdict.blockers.append("intel: no live sell route exists for this token")

    cost = round_trip_cost_pct if round_trip_cost_pct is not None else security.round_trip_cost_pct
    if cost and cost > cfg.max_round_trip_cost_pct:
        verdict.blockers.append(
            f"intel: a round trip would cost {cost:.0f}% "
            f"(limit {cfg.max_round_trip_cost_pct:.0f}%) — the trade needs that "
            "much just to break even"
        )

    if market.wash_trade_suspicion > cfg.max_wash_suspicion:
        verdict.blockers.append(
            f"intel: volume looks manufactured "
            f"(suspicion {market.wash_trade_suspicion:.0%}, "
            f"vol/liq {market.volume_to_liquidity:.0f}x)"
        )

    # --- Entity resolution -------------------------------------------------
    wallets = smart_wallets or []
    verdict.effective_smart_wallets = len(wallets)

    if graph is not None and cfg.entity_resolution and wallets:
        unique, warnings = graph.deduplicate(wallets)
        verdict.effective_smart_wallets = len(unique)
        verdict.sybil_ratio = 1.0 - (len(unique) / len(wallets)) if wallets else 0.0

        for warning in warnings:
            verdict.notes.append(warning)

        if verdict.sybil_ratio > cfg.max_sybil_ratio:
            verdict.blockers.append(
                f"intel: {verdict.sybil_ratio:.0%} of the apparent smart-wallet "
                f"consensus is one entity ({len(wallets)} wallets, "
                f"{len(unique)} actual actors)"
            )

        fresh_ratio = graph.fresh_wallet_ratio(wallets)
        if fresh_ratio > cfg.max_fresh_wallet_ratio:
            verdict.blockers.append(
                f"intel: {fresh_ratio:.0%} of these wallets were created in the last 24h"
            )

    # Insider clusters found during dossier assembly.
    for warning in dossier.cluster_warnings:
        verdict.notes.append(warning)
        if "insider" in warning.lower():
            try:
                pct = float(warning.split("(")[1].split("%")[0])
                if pct / 100.0 > cfg.max_insider_ratio:
                    verdict.blockers.append(f"intel: {warning}")
            except (IndexError, ValueError):
                pass

    # --- Graded adjustments ------------------------------------------------
    adjustment = 0.0

    # Security risk score, when we actually have one.
    if security.provenance.get("sources"):
        # 0 risk -> +6, 50 -> 0, 100 -> -12.
        adjustment += (50.0 - security.risk_score) * 0.12
        if security.provenance.get("confidence") == "high":
            adjustment *= 1.15
        elif security.provenance.get("confidence") == "low":
            adjustment *= 0.6

    # LP burned or locked is genuine, checkable reassurance.
    if security.lp_burned:
        adjustment += 3.0
        verdict.notes.append("LP burned")
    elif security.lp_locked_pct > 0.8:
        adjustment += 2.0
        verdict.notes.append(f"LP {security.lp_locked_pct:.0%} locked")

    # Wash-trading suspicion below the blocking threshold still costs points.
    if market.wash_trade_suspicion > 0.3:
        penalty = market.wash_trade_suspicion * 10.0
        adjustment -= penalty
        verdict.notes.append(
            f"volume quality questionable ({market.wash_trade_suspicion:.0%} suspicion)"
        )

    # Real unique traders are hard to fake and are worth rewarding.
    if market.unique_wallets_1h > 50:
        adjustment += 3.0
    elif market.unique_wallets_1h and market.unique_wallets_1h < 8:
        adjustment -= 3.0
        verdict.notes.append(f"only {market.unique_wallets_1h} unique traders in the last hour")

    # Narrative: organic attention helps, manufactured attention hurts more.
    if narrative.mention_count > 0:
        if narrative.astroturf_score > cfg.astroturf_threshold:
            penalty = 8.0 * narrative.astroturf_score
            adjustment -= penalty
            reason = narrative.themes[0] if narrative.themes else "coordinated posting"
            verdict.notes.append(f"attention looks manufactured: {reason}")
        else:
            bonus = min(5.0, narrative.mention_count * 0.6)
            if narrative.quality_accounts > 0:
                bonus += min(3.0, narrative.quality_accounts * 1.5)
            adjustment += bonus
            verdict.notes.append(
                f"{narrative.mention_count} organic mentions"
                + (f", {narrative.quality_accounts} from accounts with real reach"
                   if narrative.quality_accounts else "")
            )

    # Paid promotion is not a quality signal. A launch with organic interest
    # does not need to buy visibility.
    if market.boosted:
        adjustment -= 2.0
        verdict.notes.append(f"paid promotion active ({market.boosted} boosts)")

    # Concentration beyond what the on-chain gates already checked.
    if security.top10_holding_pct > 0.5:
        adjustment -= 5.0

    verdict.adjustment = max(
        -cfg.max_intel_penalty, min(cfg.max_intel_bonus, adjustment)
    )
    return verdict


def summarise(verdict: IntelVerdict, dossier: TokenDossier) -> str:
    """One line for the decision log."""
    parts = [
        f"risk {dossier.security.risk_score:.0f}",
        f"conf {dossier.security.provenance.get('confidence', 'none')}",
    ]
    if verdict.sybil_ratio > 0:
        parts.append(f"sybil {verdict.sybil_ratio:.0%}")
    if dossier.market.wash_trade_suspicion > 0.2:
        parts.append(f"wash {dossier.market.wash_trade_suspicion:.0%}")
    if dossier.narrative.mention_count:
        parts.append(
            f"social {dossier.narrative.mention_count}"
            + (f" (astroturf {dossier.narrative.astroturf_score:.0%})"
               if dossier.narrative.astroturf_score > 0.3 else "")
        )
    parts.append(f"intel {verdict.adjustment:+.1f}")
    return " | ".join(parts)
