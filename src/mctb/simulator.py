"""Synthetic market simulator.

Runs the real gates, the real scorer, the real sizing, the real exit state
machine and the real learning layer against generated launches. No network, no
Redis, no RPC — so you can test a config change in seconds and see roughly how
often it would fire and what it would do.

**What this is good for:** catching a config that never triggers, or one that
triggers on everything; sanity-checking that a change to the exit ladder does
what you expected; watching the learning layer accumulate a table.

**What this is not:** a backtest. The price paths are drawn from a plausible
distribution, not from history, and the distribution is my estimate rather
than a measurement. A strategy that looks good here has not been validated;
it has merely failed to embarrass itself. Real paper trading against the live
feed is the actual test, and it is the one that takes weeks.

The outcome mix below is deliberately harsh — most launches go to roughly
zero — because that is the honest shape of this market, and a simulator that
flatters you is worse than none.
"""

from __future__ import annotations

import logging
import random
import time
from dataclasses import dataclass, field

from rich.console import Console as RichConsole
from rich.table import Table

from .brain.edge import PersonalEdge
from .brain.executor import Executor
from .brain.gates import RiskGate, apply_gates
from .brain.portfolio import Portfolio
from .brain.scoring import Scorer
from .config import Config
from .db import Database
from .models import Candidate, Features, Fill, TokenSafety

log = logging.getLogger(__name__)
out = RichConsole()


# Outcome archetypes: (label, probability, peak multiple range, seconds to peak,
# floor multiple the price decays to).
#
# The `generational` row matters more than its probability suggests. Without a
# representable fat tail, a simulator structurally cannot evaluate any strategy
# whose entire thesis is catching one — it would show `moonshot` losing slowly
# and `balanced` winning slowly, every time, purely as an artefact of the
# distribution rather than of the strategies.
#
# 1-in-400 at 100-2000x is roughly the observed shape of "a launch that reaches
# nine figures". It is a rough estimate, not a measurement, and the whole
# distribution below is my construction rather than fitted history. Treat sim
# results as a coherence check on your config, never as evidence of edge.
ARCHETYPES = [
    ("instant_rug", 0.34, (0.6, 1.1), (5, 40), (0.01, 0.05)),
    ("slow_bleed", 0.2975, (1.0, 1.6), (30, 180), (0.15, 0.45)),
    ("modest_pump", 0.22, (1.6, 3.0), (60, 400), (0.4, 1.1)),
    ("strong_run", 0.11, (3.0, 9.0), (120, 700), (0.8, 2.5)),
    ("moonshot", 0.03, (9.0, 45.0), (200, 1100), (1.5, 8.0)),
    ("generational", 0.0025, (100.0, 2000.0), (900, 7200), (20.0, 400.0)),
]


@dataclass
class SyntheticToken:
    mint: str
    symbol: str
    creator: str
    archetype: str
    entry_price: float
    peak_multiple: float
    seconds_to_peak: float
    floor_multiple: float
    born: float
    features: Features
    safety: TokenSafety
    smart_wallets: list[str] = field(default_factory=list)
    dev_sells_at: float | None = None

    def price_at(self, elapsed: float) -> float:
        """Price path: rise to a peak, then decay toward a floor.

        Noise is multiplicative and fairly heavy, because these markets do not
        move smoothly and an exit rule that only works on a clean curve is not
        a working exit rule.
        """
        if elapsed <= 0:
            return self.entry_price

        if elapsed <= self.seconds_to_peak:
            progress = elapsed / self.seconds_to_peak
            # Ease-out: most of the move happens early, which is what makes
            # these things so hard to exit well.
            shaped = progress ** 0.55
            multiple = 1.0 + (self.peak_multiple - 1.0) * shaped
        else:
            decay_elapsed = elapsed - self.seconds_to_peak
            half_life = max(30.0, self.seconds_to_peak * 0.6)
            decay = 0.5 ** (decay_elapsed / half_life)
            multiple = self.floor_multiple + (self.peak_multiple - self.floor_multiple) * decay

        noise = random.gauss(1.0, 0.06)
        return max(1e-12, self.entry_price * multiple * noise)


class SimulatedExecutor(Executor):
    """Fills against the synthetic price path, with fees and slippage."""

    paper = True

    def __init__(self, config: Config, market: dict[str, SyntheticToken]) -> None:
        self.config = config
        self.market = market
        self._bankroll = config.execution.paper_starting_bankroll_sol
        self.clock = time.time()
        self.fills = 0

    async def bankroll(self) -> float:
        return self._bankroll

    def _price(self, mint: str) -> float:
        token = self.market.get(mint)
        if token is None:
            return 0.0
        return token.price_at(self.clock - token.born)

    async def buy(self, mint: str, sol_amount: float, symbol: str = "") -> Fill:
        price = self._price(mint)
        if price <= 0 or sol_amount > self._bankroll:
            return Fill(mint=mint, symbol=symbol, side="buy", ok=False, error="cannot fill", paper=True)

        slip = 1 + self.config.execution.paper_slippage_pct / 100 * random.uniform(0.2, 1.0)
        effective = price * slip
        fee = sol_amount * self.config.execution.paper_fee_pct / 100
        tokens = (sol_amount - fee) / effective

        self._bankroll -= sol_amount
        self.fills += 1
        return Fill(
            mint=mint, symbol=symbol, side="buy", ok=True,
            sol_amount=sol_amount, token_amount=tokens, price_sol=effective,
            fee_sol=fee, signature=f"sim-{self.fills}", paper=True,
        )

    async def sell(
        self, mint: str, token_amount: float, symbol: str = "", reason: str = "", urgent: bool = False
    ) -> Fill:
        price = self._price(mint)
        severity = 1.6 if urgent else 1.0
        slip = 1 - self.config.execution.paper_slippage_pct / 100 * random.uniform(0.2, 1.0) * severity
        proceeds = max(0.0, token_amount * price * max(0.05, slip))
        fee = proceeds * self.config.execution.paper_fee_pct / 100
        proceeds -= fee

        self._bankroll += proceeds
        self.fills += 1
        return Fill(
            mint=mint, symbol=symbol, side="sell", ok=True,
            sol_amount=proceeds, token_amount=token_amount,
            price_sol=price, fee_sol=fee, signature=f"sim-{self.fills}",
            paper=True, exit_reason=reason,
        )


class SimulatedOracle:
    def __init__(self, executor: SimulatedExecutor) -> None:
        self.executor = executor

    async def prices(self, mints: list[str]) -> dict[str, float]:
        return {m: self.executor._price(m) for m in mints}  # noqa: SLF001

    async def price(self, mint: str, max_age_s: float = 2.0) -> float:
        return self.executor._price(mint)  # noqa: SLF001


def _pick_archetype() -> tuple[str, tuple, tuple, tuple]:
    roll = random.random()
    cumulative = 0.0
    for label, probability, peak, seconds, floor in ARCHETYPES:
        cumulative += probability
        if roll <= cumulative:
            return label, peak, seconds, floor
    label, _p, peak, seconds, floor = ARCHETYPES[-1]
    return label, peak, seconds, floor


def generate_token(index: int, now: float) -> SyntheticToken:
    label, peak_range, seconds_range, floor_range = _pick_archetype()
    peak = random.uniform(*peak_range)
    seconds_to_peak = random.uniform(*seconds_range)
    floor = random.uniform(*floor_range)

    # Better setups correlate with better outcomes, but only loosely — the
    # correlation being weak is precisely what makes this hard, and a
    # simulator with a strong correlation would make any strategy look good.
    quality = min(1.0, max(0.0, (peak - 0.6) / 8.0 + random.gauss(0, 0.32)))

    smart_count = 0
    if random.random() < 0.25 + 0.45 * quality:
        smart_count = random.randint(1, 4)

    features = Features(
        age_s=random.uniform(10, 200),
        sol_in_curve=random.uniform(1.5, 60) * (0.6 + 0.8 * quality),
        market_cap_sol=random.uniform(20, 300),
        unique_buyers=int(random.uniform(4, 120) * (0.4 + 1.2 * quality)),
        buy_count=int(random.uniform(10, 300)),
        sell_count=int(random.uniform(3, 150)),
        velocity_sol_per_s=random.uniform(0, 1.2) * (0.3 + 1.4 * quality),
        smart_wallet_count=smart_count,
        smart_wallet_score=random.uniform(0.5, 0.95) if smart_count else 0.0,
        social_mentions=int(random.expovariate(1.2)) if random.random() < 0.3 else 0,
        social_reach=random.uniform(3, 18) if random.random() < 0.3 else 0.0,
        dev_holding_pct=abs(random.gauss(0.015, 0.02)),
        top10_holding_pct=min(0.9, abs(random.gauss(0.22, 0.12))),
        bundle_pct=abs(random.gauss(0.05, 0.08)),
        creator_prior_rugs=1 if random.random() < 0.06 else 0,
        creator_prior_wins=1 if random.random() < 0.08 else 0,
        price_sol=0.0,
    )
    features.buy_sell_ratio = features.buy_count / max(1, features.sell_count)

    safety = TokenSafety(
        checked=True,
        mint_authority_revoked=random.random() > 0.05,
        freeze_authority_revoked=random.random() > 0.05,
        dev_holding_pct=features.dev_holding_pct,
        top10_holding_pct=features.top10_holding_pct,
        holder_count=features.unique_buyers,
        bundle_pct=features.bundle_pct,
    )
    safety.passed = (
        safety.mint_authority_revoked
        and safety.freeze_authority_revoked
        and safety.top10_holding_pct < 0.6
    )
    if not safety.passed:
        safety.failures = ["synthetic safety failure"]

    entry_price = random.uniform(1e-8, 5e-7)
    return SyntheticToken(
        mint=f"SIM{index:04d}{'x' * 28}pump",
        symbol=f"SIM{index}",
        creator=f"creator{index % 25:03d}",
        archetype=label,
        entry_price=entry_price,
        peak_multiple=peak,
        seconds_to_peak=seconds_to_peak,
        floor_multiple=floor,
        born=now,
        features=features,
        safety=safety,
        smart_wallets=[f"wallet{i:03d}" for i in range(smart_count)],
        dev_sells_at=(now + random.uniform(20, 200)) if label == "instant_rug" and random.random() < 0.6 else None,
    )


async def run_simulation(config: Config, tokens: int = 60, seed: int = 42) -> dict:
    """Run the pipeline over synthetic launches and print a report."""
    random.seed(seed)

    out.print(
        f"\n[bold]Simulating {tokens} launches[/bold] with preset "
        f"'{config.strategy_preset or 'custom'}' (seed {seed})\n"
    )

    # Isolated database so a simulation never pollutes real learned state.
    db_path = config.resolve(config.paths.data_dir) / "simulation.db"
    if db_path.exists():
        db_path.unlink()
    db = Database(db_path)
    await db.connect()

    market: dict[str, SyntheticToken] = {}
    executor = SimulatedExecutor(config, market)
    oracle = SimulatedOracle(executor)
    edge = PersonalEdge(
        db,
        min_samples=config.scoring.personal_edge_min_samples,
        max_adjust=config.scoring.personal_edge_max_adjust,
        enabled=config.scoring.personal_edge_enabled,
    )
    await edge.load()

    scorer = Scorer(config, edge)
    risk = RiskGate(config, clock=lambda: executor.clock)

    start = time.time()
    executor.clock = start
    # The portfolio stamps every timestamp from the simulator's virtual clock,
    # so hold times and age-based exits are measured in simulated seconds.
    portfolio = Portfolio(
        config, db, executor, oracle, edge, clock=lambda: executor.clock  # type: ignore[arg-type]
    )
    considered = skipped_gates = skipped_score = skipped_risk = 0
    gate_reasons: dict[str, int] = {}

    # Virtual clock: 1 simulated second per step, launches arriving over time.
    # Long enough for a multi-hour runner to play out. A shorter horizon
    # would force-close every tail at 'simulation_end' and make any
    # runner-based strategy look broken.
    total_steps = 14_400
    launch_steps = sorted(random.sample(range(0, max(1, total_steps - 7200)), tokens))
    launch_index = 0

    for step in range(total_steps):
        executor.clock = start + step

        while launch_index < len(launch_steps) and launch_steps[launch_index] == step:
            token = generate_token(launch_index, executor.clock)
            market[token.mint] = token
            launch_index += 1
            considered += 1

            candidate = Candidate(
                mint=token.mint,
                symbol=token.symbol,
                creator=token.creator,
                features=token.features,
                safety=token.safety,
                smart_wallets=token.smart_wallets,
                trigger="smart_wallet" if token.smart_wallets else "new_token",
            )

            gate_result = apply_gates(
                candidate,
                config,
                blocked_creators=await db.blocked_creators(),
                already_holding=portfolio.holds(token.mint),
            )
            if not gate_result.passed:
                skipped_gates += 1
                key = gate_result.failures[0].split("(")[0].strip()[:44]
                gate_reasons[key] = gate_reasons.get(key, 0) + 1
                continue

            scored = scorer.score(candidate)
            if scored.score < config.scoring.entry_threshold:
                skipped_score += 1
                continue

            bankroll = await portfolio.bankroll()
            size = scorer.size_position(scored.score, bankroll)
            risk_result = risk.check(
                open_positions=portfolio.at_risk_count,
                bankroll_sol=bankroll,
                daily_pnl_sol=portfolio.daily_pnl_sol,
                proposed_size_sol=size,
            )
            if not risk_result.passed:
                skipped_risk += 1
                continue

            position = await portfolio.open_position(
                candidate, size, scored.score, scored.breakdown
            )
            if position is not None:
                # Position() defaults opened_at to real wall-clock time; the
                # simulation runs on the virtual one.
                position.opened_at = executor.clock
                risk.record_open(size)

        # Dev-sell events.
        for token in market.values():
            if token.dev_sells_at and executor.clock >= token.dev_sells_at:
                portfolio.note_dev_sell(token.mint)

        # Mark to market and run exits against the virtual clock.
        for position in portfolio.open_positions:
            price = executor._price(position.mint)  # noqa: SLF001
            if price > 0:
                position.current_price_sol = price
                position.last_price_ts = executor.clock
                position.peak_price_sol = max(position.peak_price_sol, price)

        from .brain.exits import ExitAction, evaluate_exit, maybe_arm_trailing

        for position in list(portfolio.open_positions):
            maybe_arm_trailing(position, config.exits)
            action = evaluate_exit(
                position,
                config.exits,
                now=executor.clock,
                dev_sold=position.mint in portfolio.dev_sold,
                smart_wallets_exited=False,
                migrated=False,
            )
            if action.should_sell:
                await portfolio._execute_exit(position, action)  # noqa: SLF001
                if position.state.value == "closed":
                    risk.record_close(position.total_pnl_sol)

    # Close anything still open at the end of the run.
    for position in list(portfolio.open_positions):

        await portfolio._execute_exit(  # noqa: SLF001
            position, ExitAction(kind="full", fraction=1.0, reason="simulation_end")
        )

    # --- Report -------------------------------------------------------------
    performance = await db.performance_summary()
    final_bankroll = await executor.bankroll()
    starting = config.execution.paper_starting_bankroll_sol

    funnel = Table(title="Funnel")
    funnel.add_column("Stage")
    funnel.add_column("Count", justify="right")
    funnel.add_column("Note", style="dim")
    funnel.add_row("Launches seen", str(considered), "")
    funnel.add_row("Blocked by gates", str(skipped_gates), "hard filters")
    funnel.add_row("Below score threshold", str(skipped_score), f"< {config.scoring.entry_threshold:.0f}")
    funnel.add_row("Blocked by risk limits", str(skipped_risk), "position/exposure caps")
    funnel.add_row("Entered", str(performance.get("trades", 0)), "")
    out.print(funnel)

    if gate_reasons:
        reasons = Table(title="Top gate rejections")
        reasons.add_column("Reason")
        reasons.add_column("Count", justify="right")
        for reason, count in sorted(gate_reasons.items(), key=lambda kv: kv[1], reverse=True)[:8]:
            reasons.add_row(reason, str(count))
        out.print(reasons)

    if performance.get("trades"):
        results = Table(title="Results")
        results.add_column("Metric")
        results.add_column("Value", justify="right")
        pnl = final_bankroll - starting
        colour = "green" if pnl >= 0 else "red"
        pf = performance["profit_factor"]
        for label, value in [
            ("Starting bankroll", f"{starting:.4f} SOL"),
            ("Final bankroll", f"[{colour}]{final_bankroll:.4f} SOL[/]"),
            ("Net", f"[{colour}]{pnl:+.4f} SOL ({100 * pnl / starting:+.1f}%)[/]"),
            ("Trades", str(performance["trades"])),
            ("Win rate", f"{performance['win_rate']:.1%}"),
            ("Profit factor", "∞" if pf == float("inf") else f"{pf:.2f}"),
            ("Max drawdown", f"{performance['max_drawdown_sol']:.4f} SOL"),
            ("Best / worst", f"{performance['best_sol']:+.4f} / {performance['worst_sol']:+.4f}"),
            ("Fees paid", f"{performance['total_fees_sol']:.4f} SOL"),
            ("Average hold", f"{performance['avg_hold_s']:.0f}s"),
        ]:
            results.add_row(label, value)
        out.print(results)

        exits = Table(title="Exit reasons")
        exits.add_column("Reason")
        exits.add_column("Count", justify="right")
        for reason, count in sorted(
            performance["exit_reasons"].items(), key=lambda kv: kv[1], reverse=True
        ):
            exits.add_row(reason, str(count))
        out.print(exits)
    else:
        out.print(
            "\n[yellow]No trades were taken.[/yellow] Your gates rejected everything — "
            "look at the rejection table above and loosen whichever filter dominates, "
            "or try a less strict preset."
        )

    out.print(
        "\n[dim]Reminder: these price paths are generated, not historical. "
        "A good result here means your config is coherent, not that it is profitable. "
        "Paper trade against the live feed before believing anything.[/dim]\n"
    )

    await db.close()
    return performance
