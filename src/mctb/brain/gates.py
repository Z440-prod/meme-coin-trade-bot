"""Hard gates — binary pass/fail filters applied before anything is scored.

The distinction between a gate and a score matters. A score says "this looks
somewhat worse"; a gate says "this is not a trade at any price". Freeze
authority being live is not a 10-point deduction, it means you may be unable
to sell at all, and no amount of momentum compensates for that.

Gates run first because they are cheap and because letting a high score
override them is exactly the failure mode that empties wallets.
"""

from __future__ import annotations

import logging
import time

from ..config import Config
from ..models import Candidate

log = logging.getLogger(__name__)


class GateResult:
    def __init__(self) -> None:
        self.failures: list[str] = []

    @property
    def passed(self) -> bool:
        return not self.failures

    def fail(self, reason: str) -> None:
        self.failures.append(reason)

    def __repr__(self) -> str:
        return f"GateResult(passed={self.passed}, failures={self.failures})"


def apply_gates(
    candidate: Candidate,
    config: Config,
    *,
    blocked_creators: set[str] | None = None,
    already_holding: bool = False,
    recently_traded: bool = False,
) -> GateResult:
    """Run every hard filter. Returns all failures, not just the first, so the
    logs tell you what to loosen rather than making you bisect config."""
    gates = config.gates
    feat = candidate.features
    result = GateResult()
    blocked_creators = blocked_creators or set()

    # --- Position hygiene ---------------------------------------------------
    if already_holding:
        result.fail("already holding this token")
    if recently_traded:
        result.fail(
            f"traded this mint within the last {config.risk.same_mint_cooldown_s:.0f}s"
        )

    # --- Age ----------------------------------------------------------------
    if feat.age_s < gates.min_age_s:
        result.fail(
            f"too young ({feat.age_s:.0f}s < {gates.min_age_s:.0f}s) — "
            "the first seconds belong to bundled snipers"
        )
    if feat.age_s > gates.max_age_s:
        result.fail(f"too old ({feat.age_s:.0f}s > {gates.max_age_s:.0f}s)")

    # --- Liquidity ----------------------------------------------------------
    if feat.sol_in_curve < gates.min_sol_in_curve:
        result.fail(
            f"curve too thin ({feat.sol_in_curve:.2f} < {gates.min_sol_in_curve:.2f} SOL) — "
            "you would move the price against yourself getting in and out"
        )
    if feat.sol_in_curve > gates.max_sol_in_curve:
        result.fail(
            f"curve too full ({feat.sol_in_curve:.2f} > {gates.max_sol_in_curve:.2f} SOL) — "
            "most of the move already happened"
        )

    # --- Participation ------------------------------------------------------
    if feat.unique_buyers < gates.min_unique_buyers:
        result.fail(f"only {feat.unique_buyers} unique buyers (need {gates.min_unique_buyers})")
    if feat.buy_sell_ratio < gates.min_buy_sell_ratio:
        result.fail(
            f"buy/sell ratio {feat.buy_sell_ratio:.2f} below {gates.min_buy_sell_ratio:.2f} — "
            "more people leaving than arriving"
        )

    # --- On-chain safety ----------------------------------------------------
    safety = candidate.safety
    if not safety.checked:
        result.fail("on-chain safety screen did not run")
    else:
        if gates.require_mint_authority_revoked and not safety.mint_authority_revoked:
            result.fail("mint authority live — supply can be inflated against you")
        if gates.require_freeze_authority_revoked and not safety.freeze_authority_revoked:
            result.fail("freeze authority live — your tokens can be frozen (honeypot)")
        if safety.dev_holding_pct > gates.max_dev_holding_pct:
            result.fail(
                f"creator holds {safety.dev_holding_pct:.1%} "
                f"(limit {gates.max_dev_holding_pct:.1%})"
            )
        if safety.top10_holding_pct > gates.max_top10_holding_pct:
            result.fail(
                f"top 10 hold {safety.top10_holding_pct:.0%} "
                f"(limit {gates.max_top10_holding_pct:.0%})"
            )
        # Any failure the Scout found that we did not re-derive here.
        for failure in safety.failures:
            if failure not in result.failures:
                result.fail(failure)

    if feat.bundle_pct > gates.max_bundle_pct:
        result.fail(
            f"{feat.bundle_pct:.0%} of supply bought in the launch slot "
            f"(limit {gates.max_bundle_pct:.0%}) — bundled launch"
        )

    # --- Reputation ---------------------------------------------------------
    if gates.block_known_rug_creators and candidate.creator in blocked_creators:
        result.fail(f"creator {candidate.creator[:8]}… has rugged you before")

    if gates.require_smart_wallet and not candidate.smart_wallets:
        result.fail("no tracked wallet has bought this")

    # --- Name blocklist -----------------------------------------------------
    haystack = f"{candidate.name} {candidate.symbol}".lower()
    for pattern in gates.name_blocklist:
        if pattern.lower() in haystack:
            result.fail(f"name matches blocklist pattern '{pattern}'")

    return result


class RiskGate:
    """Portfolio-level limits. These are about your account, not the token.

    A perfect setup is still a bad trade if you are already at maximum
    exposure or down to your last SOL of gas money.
    """

    def __init__(self, config: Config) -> None:
        self.config = config
        self._recent_opens: list[float] = []
        self._recent_sol: list[tuple[float, float]] = []
        self.consecutive_losses = 0
        self.halted = False
        self.halt_reason = ""
        self.halted_until = 0.0

    def record_open(self, size_sol: float) -> None:
        now = time.time()
        self._recent_opens.append(now)
        self._recent_sol.append((now, size_sol))

    def record_close(self, pnl_sol: float) -> None:
        if pnl_sol > 0:
            self.consecutive_losses = 0
        else:
            self.consecutive_losses += 1
            if self.consecutive_losses >= self.config.risk.consecutive_loss_limit:
                self.halt(
                    f"{self.consecutive_losses} consecutive losing trades — "
                    "cooling off rather than chasing"
                )

    def halt(self, reason: str) -> None:
        self.halted = True
        self.halt_reason = reason
        self.halted_until = time.time() + self.config.risk.cooldown_after_halt_s
        log.warning("TRADING HALTED: %s", reason)

    def resume(self) -> None:
        self.halted = False
        self.halt_reason = ""
        self.halted_until = 0.0
        self.consecutive_losses = 0
        log.info("trading resumed")

    def _prune(self) -> None:
        cutoff = time.time() - 3600
        self._recent_opens = [t for t in self._recent_opens if t > cutoff]
        self._recent_sol = [(t, s) for t, s in self._recent_sol if t > cutoff]

    def check(
        self,
        *,
        open_positions: int,
        bankroll_sol: float,
        daily_pnl_sol: float,
        proposed_size_sol: float,
    ) -> GateResult:
        self._prune()
        result = GateResult()
        risk = self.config.risk

        if self.halted:
            if time.time() >= self.halted_until:
                self.resume()
            else:
                remaining = self.halted_until - time.time()
                result.fail(f"halted for another {remaining / 60:.0f} min: {self.halt_reason}")
                return result

        if open_positions >= risk.max_concurrent_positions:
            result.fail(
                f"at position limit ({open_positions}/{risk.max_concurrent_positions})"
            )

        if len(self._recent_opens) >= risk.max_positions_per_hour:
            result.fail(
                f"hourly trade limit reached ({len(self._recent_opens)}/{risk.max_positions_per_hour})"
            )

        deployed = sum(s for _t, s in self._recent_sol)
        if deployed + proposed_size_sol > risk.max_sol_deployed_per_hour:
            result.fail(
                f"hourly deployment cap: {deployed:.3f} + {proposed_size_sol:.3f} SOL "
                f"exceeds {risk.max_sol_deployed_per_hour:.3f}"
            )

        if daily_pnl_sol <= -abs(risk.daily_loss_limit_sol):
            self.halt(
                f"daily loss limit hit ({daily_pnl_sol:+.4f} SOL). "
                "Stopping for the day is the whole point of having a limit."
            )
            result.fail(self.halt_reason)

        if bankroll_sol - proposed_size_sol < risk.min_sol_reserve:
            result.fail(
                f"would drop below the {risk.min_sol_reserve:.3f} SOL fee reserve "
                "— a wallet with no SOL cannot even sell its way out"
            )

        if proposed_size_sol <= 0:
            result.fail("computed position size is zero")

        return result
