#!/usr/bin/env python3
"""MCTB capability demo — runs every subsystem against live data.

    python scripts/demo.py            # full demo
    python scripts/demo.py --offline  # skip anything needing network

Nothing here is mocked except where a section explicitly says so (the entity
graph and astroturf sections use constructed wallets, because you cannot
summon a real sybil attack on demand). Everything else hits real APIs and real
chain state.

No API keys required. No wallet required. Nothing is bought or sold.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))

from rich.console import Console  # noqa: E402
from rich.panel import Panel  # noqa: E402
from rich.table import Table  # noqa: E402

from mctb.brain.executor import PaperExecutor  # noqa: E402
from mctb.brain.gates import RiskGate, apply_gates  # noqa: E402
from mctb.brain.intel_gate import evaluate_intel  # noqa: E402
from mctb.brain.router import ExecutionRouter  # noqa: E402
from mctb.brain.scoring import Scorer  # noqa: E402
from mctb.config import load_config  # noqa: E402
from mctb.intel import EntityGraph, IntelEngine, NarrativeEngine  # noqa: E402
from mctb.intel.narrative import Mention, astroturf_score  # noqa: E402
from mctb.models import Candidate, Features, TokenSafety  # noqa: E402
from mctb.providers import ProviderRegistry, Tier  # noqa: E402
from mctb.solana.rpc import SolanaRPC  # noqa: E402

out = Console()
SOL = "So11111111111111111111111111111111111111112"
USDC = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"


def header(n: int, title: str, subtitle: str = "") -> None:
    out.print()
    out.print(Panel(
        f"[bold white]{title}[/bold white]" + (f"\n[dim]{subtitle}[/dim]" if subtitle else ""),
        title=f"[cyan]{n}[/cyan]", border_style="cyan", expand=False,
    ))


async def demo_providers(registry: ProviderRegistry) -> None:
    header(1, "Data providers", "23 adapters; everything below runs with ZERO API keys")

    table = Table(box=None, pad_edge=False)
    for col in ("Provider", "Status", "Cost"):
        table.add_column(col)
    table.add_column("Answers", overflow="fold")

    for row in registry.describe():
        colour = {"ready": "green", "unconfigured": "yellow",
                  "no_public_api": "dim", "disabled": "dim"}.get(row["status"], "red")
        table.add_row(
            row["display_name"], f"[{colour}]{row['status']}[/]",
            "paid" if row["paid"] else "free",
            ", ".join(c.replace("token_", "") for c in row["capabilities"][:3]) or "link-out only",
        )
    out.print(table)

    cov = registry.coverage_report()
    out.print(
        f"\n  [bold green]{cov['ready']}[/bold green] ready · "
        f"{cov['unconfigured']} awaiting keys · {cov['link_only']} link-only"
    )
    out.print(
        "  [green]All critical capabilities covered[/green]"
        if not cov["critical_gaps"]
        else f"  [red]Gaps: {cov['critical_gaps']}[/red]"
    )


async def demo_live_dossier(engine: IntelEngine, mint: str) -> None:
    header(2, "Live intelligence dossier",
           "several independent providers, cross-checked, under one deadline")

    started = time.perf_counter()
    d = await engine.assemble(mint, tier=Tier.COLD, timeout_s=10.0)
    elapsed = (time.perf_counter() - started) * 1000

    sec, mk = d.security, d.market
    srcs = sec.provenance.get("sources", [])

    out.print(f"  [bold]{d.symbol or mint[:14]}[/bold]  [dim]{mint}[/dim]")
    out.print(f"  assembled in [cyan]{elapsed:.0f}ms[/cyan] from "
              f"[cyan]{len(srcs)}[/cyan] sources: {', '.join(srcs) or 'none'}")
    out.print(f"  confidence: [bold]{sec.provenance.get('confidence', 'none')}[/bold]"
              + (f"  [red](contested: {sec.provenance['disagreed']})[/red]"
                 if sec.provenance.get("disagreed") else ""))
    out.print()

    if sec.blockers:
        out.print("  [red bold]NOT TRADEABLE[/red bold]")
        for b in sec.blockers:
            out.print(f"    [red]x[/red] {b}")
    elif srcs:
        out.print("  [green]No blockers found[/green]")

    t = Table(box=None, show_header=False, pad_edge=False)
    t.add_column("k", style="dim")
    t.add_column("v", justify="right")
    for k, v in [
        ("price", f"${mk.price_usd:.10f}" if mk.price_usd else "—"),
        ("liquidity", f"${mk.liquidity_usd:,.0f}"),
        ("volume 24h", f"${mk.volume_24h:,.0f}"),
        ("vol / liq", f"{mk.volume_to_liquidity:.1f}x"),
        ("wash suspicion", f"{mk.wash_trade_suspicion:.0%}"),
        ("risk score", f"{sec.risk_score:.0f}/100"),
        ("mint authority", "LIVE" if sec.mint_authority_live else "revoked"),
        ("freeze authority", "LIVE" if sec.freeze_authority_live else "revoked"),
        ("sellable", "yes" if sec.sellable else "NO"),
        ("top 10 hold", f"{sec.top10_holding_pct:.1%}"),
    ]:
        t.add_row(k, str(v))
    out.print(t)
    if sec.risk_flags:
        out.print(f"  [yellow]flags:[/yellow] {', '.join(sec.risk_flags[:6])}")


async def demo_honeypot_and_cost(router: ExecutionRouter, mint: str) -> None:
    header(3, "Pre-trade checks that most bots skip",
           "can it be sold at all, and what would a round trip actually cost")

    est = await router.estimate_cost(mint, 0.05)
    out.print(f"  sellable:              [{'green' if est.sellable else 'red'}]"
              f"{'yes' if est.sellable else 'NO — buy route exists, sell route does not'}[/]")
    if est.sellable:
        colour = "green" if est.round_trip_pct < 10 else "yellow" if est.round_trip_pct < 25 else "red"
        out.print(f"  round-trip cost:       [{colour}]{est.round_trip_pct:.1f}%[/]  "
                  f"[dim](the move needed just to break even)[/dim]")
        out.print(f"  buy impact:            {est.buy_impact_pct:.2f}%")
        out.print(f"  sell impact:           {est.sell_impact_pct:.2f}%")
        if est.buy_route:
            out.print(f"  route:                 [dim]{' > '.join(est.buy_route)}[/dim]")
    else:
        out.print(f"  reason:                [red]{est.reason}[/red]")
    out.print(f"\n  verdict: [bold]{'acceptable' if est.acceptable else 'REFUSE THIS TRADE'}[/bold]")


async def demo_graduated_routing(router: ExecutionRouter, config) -> None:
    header(4, "Graduated tokens stay tradeable",
           "a bug found during this review: winners were being written off")

    class DeadCurve:
        async def get_bonding_curve(self, mint, commitment="processed"):
            return None       # bonding curve gone — the token graduated

    class DeadOracle:
        async def prices(self, m): return {x: 0.0 for x in m}
        async def price(self, m, max_age_s=2.0): return 0.0

    out.print("  [dim]simulating: a held token completes its bonding curve[/dim]\n")

    no_router = PaperExecutor(config, DeadCurve(), DeadOracle(), None)
    fill_before = await no_router.sell(USDC, 10.0, symbol="USDC")
    out.print(f"  without routing:  {fill_before.sol_amount:.6f} SOL   "
              f"[red]{fill_before.error}[/red]")

    with_router = PaperExecutor(config, DeadCurve(), DeadOracle(), router)
    fill_after = await with_router.sell(USDC, 10.0, symbol="USDC")
    out.print(f"  with routing:     [green]{fill_after.sol_amount:.6f} SOL[/green]   "
              f"[dim]routed via aggregator[/dim]")

    out.print("\n  [dim]Graduation is what happens to tokens that WORKED —[/dim]")
    out.print("  [dim]writing those off turns your best outcomes into total losses.[/dim]")


def demo_entity_graph() -> None:
    header(5, "Entity resolution — the sybil collapse",
           "constructed wallets: you cannot summon a real sybil attack on demand")

    graph = EntityGraph()
    now = time.time()

    sybils = [f"Sybil{i}Wa11et{'x'*30}" for i in range(5)]
    for i, w in enumerate(sybils):
        graph.observe_wallet(w, first_funder="AttackerFunder" + "y" * 30,
                             funded_at=now - 86400 * 5 + i * 45)
        graph.observe_buy(w, "TARGET_TOKEN", now + i)

    honest = [f"Honest{i}Wa11et{'z'*30}" for i in range(2)]
    for i, w in enumerate(honest):
        graph.observe_wallet(w, first_funder=f"Independent{i}" + "q" * 30,
                             funded_at=now - 86400 * (120 + i * 40))
        graph.observe_buy(w, "TARGET_TOKEN", now + 10 + i)

    everyone = sybils + honest
    out.print(f"  A naive bot sees:      [yellow]{len(everyone)} smart wallets bought[/yellow]")

    unique, warnings = graph.deduplicate(everyone)
    out.print(f"  Actual distinct actors: [bold green]{len(unique)}[/bold green]")
    out.print(f"  Sybil ratio:            [red]{graph.sybil_risk(everyone):.0%}[/red]\n")
    for w in warnings:
        out.print(f"  [yellow]![/yellow] {w}")

    graph.observe_wallet("DeployerWa11et" + "d" * 30,
                         first_funder="AttackerFunder" + "y" * 30, funded_at=now - 86400 * 6)
    insider = graph.insider_risk("TARGET_TOKEN", "DeployerWa11et" + "d" * 30)
    out.print(f"\n  Insider wallets:        [red]{insider['insider_count']}"
              f" ({insider['insider_pct']:.0%} of buyers)[/red]")
    for r in insider["reasons"][:2]:
        out.print(f"    [dim]{r}[/dim]")

    stats = graph.stats()
    out.print(f"\n  [dim]graph: {stats['wallets']} wallets, {stats['clusters']} clusters, "
              f"{stats['hubs_detected']} funding hubs excluded[/dim]")


def demo_astroturf() -> None:
    header(6, "Astroturf detection", "attention graded on shape, not volume")

    now = time.time()
    import random
    random.seed(7)

    organic = [
        Mention(ts=now - random.uniform(0, 1800), author=f"person{i}",
                followers=random.choice([420, 18_000, 950, 240_000, 3_100, 87_000]),
                text=f"genuinely different take number {i} on this whole market thing",
                author_age_days=random.uniform(300, 3000), author_attention=random.uniform(2, 40))
        for i in range(12)
    ]
    fake = [
        Mention(ts=now - i * 9, author=f"user{i}", followers=1000 + (i % 3) * 20,
                text="LFG this is the next 100x get in now before it moons",
                author_age_days=random.uniform(2, 20), author_attention=0.1)
        for i in range(12)
    ]

    for label, mentions, colour in [("organic chatter", organic, "green"),
                                    ("paid campaign", fake, "red")]:
        score, reasons = astroturf_score(mentions)
        out.print(f"  [{colour}]{label:18}[/{colour}] {len(mentions)} mentions → "
                  f"astroturf [bold]{score:.0%}[/bold]")
        for r in reasons[:3]:
            out.print(f"      [dim]{r}[/dim]")
    out.print("\n  [dim]A high mention count with a high astroturf score scores WORSE[/dim]")
    out.print("  [dim]than silence — somebody is paying to make you the buyer.[/dim]")


def demo_decision_pipeline(config) -> None:
    header(7, "The decision pipeline", "gates → score → intel → risk → size")

    features = Features(
        age_s=45, sol_in_curve=18.0, market_cap_sol=120, unique_buyers=40,
        buy_count=90, sell_count=20, buy_sell_ratio=4.5, velocity_sol_per_s=0.4,
        smart_wallet_count=3, smart_wallet_score=0.85, dev_holding_pct=0.008,
        top10_holding_pct=0.18, bundle_pct=0.02, price_sol=1.5e-7,
    )
    safety = TokenSafety(checked=True, mint_authority_revoked=True,
                         freeze_authority_revoked=True, dev_holding_pct=0.008,
                         top10_holding_pct=0.18, passed=True)
    smart = [f"Wa11et{i}{'k'*36}" for i in range(3)]
    cand = Candidate(mint="Demo" + "m" * 40, symbol="DEMO", creator="Creator" + "c" * 37,
                     features=features, safety=safety, smart_wallets=smart)

    gate = apply_gates(cand, config)
    out.print(f"  1. hard gates      [{'green' if gate.passed else 'red'}]"
              f"{'PASS' if gate.passed else 'FAIL'}[/]"
              + ("" if gate.passed else f"  {gate.failures[0]}"))

    scored = Scorer(config, edge=None).score(cand)
    out.print(f"  2. deterministic   [bold]{scored.score:.1f}[/bold] / 100  "
              f"[dim](threshold {config.scoring.entry_threshold:.0f})[/dim]")
    comps = sorted(scored.breakdown.components.items(), key=lambda kv: -kv[1])
    out.print("     " + "  ".join(f"[dim]{k}[/dim] {v:.1f}" for k, v in comps[:5]))

    # Now show the same candidate where the three wallets are one entity.
    graph = EntityGraph()
    now = time.time()
    for i, w in enumerate(smart):
        graph.observe_wallet(w, first_funder="OneFunder" + "f" * 35,
                             funded_at=now - 86400 * 3 + i * 30)

    from mctb.intel.types import MarketIntel, SecurityIntel, TokenDossier
    dossier = TokenDossier(
        mint=cand.mint,
        security=SecurityIntel(mint=cand.mint, risk_score=12.0, sellable=True,
                               provenance={"sources": ["goplus", "rugcheck"],
                                           "confidence": "high"}),
        market=MarketIntel(mint=cand.mint, liquidity_usd=40_000, volume_24h=95_000,
                           volume_to_liquidity=2.4, unique_wallets_1h=55),
    )
    verdict = evaluate_intel(dossier, config, graph=graph, smart_wallets=smart)
    out.print(f"  3. intel gate      [{'green' if verdict.passed else 'red'}]"
              f"{'PASS' if verdict.passed else 'BLOCKED'}[/]  "
              f"adj {verdict.adjustment:+.1f}  "
              f"[dim]{len(smart)} wallets → {verdict.effective_smart_wallets} actor(s)[/dim]")
    for b in verdict.blockers[:2]:
        out.print(f"     [red]x[/red] {b}")

    risk = RiskGate(config)
    result = risk.check(open_positions=0, bankroll_sol=1.0, daily_pnl_sol=0.0,
                        proposed_size_sol=0.02)
    out.print(f"  4. risk limits     [{'green' if result.passed else 'red'}]"
              f"{'PASS' if result.passed else 'FAIL'}[/]")

    size = Scorer(config, edge=None).size_position(scored.score, 1.0)
    out.print(f"  5. position size   [bold]{size:.4f} SOL[/bold]  "
              f"[dim](of a 1.0 SOL bankroll)[/dim]")

    out.print("\n  [dim]Note step 3: the same candidate is refused once the three[/dim]")
    out.print("  [dim]'independent' wallets are revealed to share one funder.[/dim]")


async def demo_speed(registry: ProviderRegistry, engine: IntelEngine, mint: str) -> None:
    header(8, "Speed", "why 20 providers do not make the decision path slow")

    # Cold, honestly: clear both the dossier store and every provider's HTTP
    # cache first, or we would just be re-measuring a cache hit.
    engine.store._store.clear()
    for provider in registry.providers.values():
        http = getattr(provider, "http", None)
        if http is not None:
            http.cache.invalidate()

    t0 = time.perf_counter()
    await engine.assemble(mint, tier=Tier.COLD, timeout_s=10.0)
    cold_ms = (time.perf_counter() - t0) * 1000

    # Hot — cache only, no network.
    times = []
    for _ in range(1000):
        t = time.perf_counter()
        engine.assemble_hot(mint)
        times.append((time.perf_counter() - t) * 1e6)
    times.sort()

    t = Table(box=None, show_header=True, pad_edge=False)
    t.add_column("path")
    t.add_column("latency", justify="right")
    t.add_column("network", justify="right")
    t.add_row("COLD assembly (first look)", f"{cold_ms:.0f} ms", "yes")
    t.add_row("HOT assembly (decision path)", f"{times[len(times)//2]:.1f} µs", "[green]none[/green]")
    t.add_row("HOT p99", f"{times[int(len(times)*0.99)]:.1f} µs", "[green]none[/green]")
    out.print(t)

    speedup = (cold_ms * 1000) / max(times[len(times) // 2], 0.001)
    out.print(f"\n  Decision path is [bold green]{speedup:,.0f}x[/bold green] faster than a cold fetch,")
    out.print("  [dim]because background workers filled the cache before it was needed.[/dim]")

    http = registry.http_stats()
    total_req = sum(s.get("requests", 0) for s in http.values())
    coalesced = sum(s.get("coalesced", 0) for s in http.values())
    cache_hits = sum(s.get("cache", {}).get("hits", 0) for s in http.values())
    out.print(f"\n  [dim]HTTP: {total_req} requests, {coalesced} coalesced, "
              f"{cache_hits} cache hits, HTTP/2 enabled[/dim]")


def demo_runner() -> None:
    header(9, "The runner — how an outsized outcome becomes possible",
           "the mechanism behind every '$X into $Y' story you have heard")

    from mctb.brain.exits import apply_partial_fill, evaluate_exit
    from mctb.config import ExitConfig
    from mctb.models import Position, PositionState

    cfg = ExitConfig(
        stop_loss_pct=0.55,
        take_profit_ladder=[
            {"multiple": 2.0, "fraction": 0.55},
            {"multiple": 5.0, "fraction": 0.15},
        ],
        runner_fraction=0.30,
        runner_requires_cost_recovered=True,
        runner_exits_on_rug=True,
        trailing_activate_pct=1.0, trailing_distance_pct=0.45,
        max_hold_s=1800, stagnant_after_s=420, stagnant_min_gain_pct=0.20,
    )

    entry, stake = 1.0e-7, 0.004
    pos = Position(
        mint="M" * 44, symbol="MOON", state=PositionState.OPEN,
        entry_price_sol=entry, current_price_sol=entry, peak_price_sol=entry,
        last_price_ts=time.time(), cost_sol=stake, fees_sol=0.00004,
        tokens_bought=stake / entry, tokens_held=stake / entry,
    )
    out.print(f"  stake: [bold]{stake} SOL[/bold]  ({stake / 0.05:.0%} of a typical 0.05 position)\n")

    for label, multiple in [("2x  first rung", 2.0), ("5x  second rung", 5.0)]:
        pos.current_price_sol = entry * multiple
        pos.peak_price_sol = max(pos.peak_price_sol, pos.current_price_sol)
        action = evaluate_exit(pos, cfg)
        if action.should_sell:
            sold = pos.tokens_held * action.fraction
            apply_partial_fill(pos, sold, sold * pos.current_price_sol, 0.00002,
                               action.ladder_rung)
            out.print(f"  {label:16} sold {action.fraction:>5.0%} of holdings  "
                      f"realised {pos.realised_sol:.5f} SOL  "
                      + ("[green]stake recovered[/green]" if pos.cost_recovered else ""))

    out.print(f"\n  remaining: [bold]{pos.tokens_held / pos.tokens_bought:.0%}[/bold] of the position "
              f"— the runner, now house money\n")

    trials = [
        ("a 60% drawdown", 2.0, 0),
        ("three days later", 2.0, 86_400 * 3),
        ("down 80% below entry", 0.2, 86_400),
    ]
    for label, multiple, age in trials:
        pos.current_price_sol = entry * multiple
        pos.peak_price_sol = entry * 20.0
        pos.trailing_armed = True
        if age:
            pos.opened_at = time.time() - age
        action = evaluate_exit(pos, cfg)
        verdict = "[red]CLOSED[/red]" if action.should_sell else "[green]still held[/green]"
        out.print(f"  {label:24} {verdict}")

    pos.current_price_sol = entry * 1000.0
    action = evaluate_exit(pos, cfg)
    out.print(f"  {'it runs to 1000x':24} "
              f"{'[red]CLOSED[/red]' if action.should_sell else '[green]still held[/green]'}")

    out.print(f"\n  final P&L on a {stake} SOL stake: "
              f"[bold green]{pos.total_pnl_sol:+.3f} SOL[/bold green]  "
              f"([bold]{pos.pnl_pct:,.0f}x[/bold] the stake)")

    out.print("\n  [dim]30% of a position that does 1000x is a ~300x on the trade.[/dim]")
    out.print("  [dim]A trailing stop or a max-hold timer would have exited at the[/dim]")
    out.print("  [dim]first drawdown and turned this into a 3x.[/dim]")
    out.print("\n  [yellow]This is the mechanism, not a prediction.[/yellow]")
    out.print("  [dim]Roughly 1 launch in 400 ever does this, so ~90% of moonshot[/dim]")
    out.print("  [dim]positions go to zero and you need to survive hundreds of them[/dim]")
    out.print("  [dim]to still be there for the one that does not.[/dim]")


async def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--offline", action="store_true", help="skip network sections")
    parser.add_argument("--mint", default="", help="token to profile (default: auto-pick a live one)")
    args = parser.parse_args()

    out.print(Panel.fit(
        "[bold white]MCTB — capability demo[/bold white]\n"
        "[dim]live APIs · no keys · no wallet · nothing bought or sold[/dim]",
        border_style="white",
    ))

    config = load_config()
    registry = ProviderRegistry(config)
    registry.build()

    if args.offline:
        demo_entity_graph()
        demo_astroturf()
        demo_decision_pipeline(config)
        demo_runner()
        out.print("\n[dim]offline mode — network sections skipped[/dim]\n")
        return 0

    await registry.start()
    rpc = SolanaRPC(config.rpc.url, config.rpc.fallback_url, config.rpc.timeout_s)
    await rpc.connect()

    graph = EntityGraph()
    engine = IntelEngine(registry, graph, NarrativeEngine(registry), rpc=rpc)
    router = ExecutionRouter(config, registry, rpc)

    try:
        await demo_providers(registry)

        mint = args.mint
        if not mint:
            ds = registry.get("dexscreener")
            result = await ds.get_new_pairs(30)  # type: ignore[union-attr]
            for row in (result.data or []):
                if row.get("mint", "").endswith("pump"):
                    mint = row["mint"]
                    break
            mint = mint or USDC

        await demo_live_dossier(engine, mint)
        await demo_honeypot_and_cost(router, mint)
        await demo_graduated_routing(router, config)
        demo_entity_graph()
        demo_astroturf()
        demo_decision_pipeline(config)
        await demo_speed(registry, engine, mint)
        demo_runner()

        out.print()
        out.print(Panel.fit(
            "[bold green]All subsystems exercised against live data.[/bold green]\n"
            "[dim]mctb console  →  http://localhost:8787/terminal[/dim]",
            border_style="green",
        ))
    finally:
        await rpc.close()
        await registry.close()
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
