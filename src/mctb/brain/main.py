"""Brain service — RPi 2.

Consumes candidates from the Scout, scores them, sizes and executes trades,
and manages every open position through to exit.

Loop structure, and why it is split this way:

  **exit loop** (every ~1.2s) — marks positions to market and runs the exit
  rules. Highest priority, shares nothing with the entry path, and keeps
  running even when the Brain is halted or the Scout is dead. If you only
  keep one loop alive, keep this one.

  **entry path** (event-driven) — reacts to candidates as they arrive. Purely
  arithmetic and fast; the LLM is consulted concurrently and only for
  candidates that already qualify.

  **snapshot loop** (every 1s) — publishes state for the Console.

  **review loop** (nightly) — LLM reads the trade history and reports.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import signal
from datetime import datetime, timezone

from ..automation import OutboundNotifier
from ..bus import Bus
from ..config import Config
from ..db import Database
from ..intel import EntityGraph, IntelEngine, NarrativeEngine
from ..logging_setup import setup_logging
from ..models import (
    Alert,
    Candidate,
    ControlCommand,
    Decision,
    Fill,
    Heartbeat,
    LLMNote,
    MigrationEvent,
    PortfolioSnapshot,
    Position,
    Topic,
    TradeEvent,
)
from ..providers import ProviderRegistry, Tier
from ..runtime import LATENCY
from ..scout.rpc_watcher import PriceOracle
from ..scout.wallets import WalletTracker
from ..solana.rpc import SolanaRPC
from .edge import PersonalEdge
from .executor import build_executor
from .gates import RiskGate, apply_gates
from .intel_gate import evaluate_intel, summarise
from .llm import LLMAdvisor, validate_proposed_change
from .portfolio import Portfolio
from .router import ExecutionRouter
from .scoring import Scorer

log = logging.getLogger(__name__)


class Brain:
    def __init__(self, config: Config) -> None:
        self.config = config
        self.bus = Bus(
            config.bus.redis_url,
            config.bus.redis_password,
            source="brain",
            journal_maxlen=config.bus.journal_maxlen,
        )
        self.db = Database(config.resolve(config.paths.db_file))
        self.rpc = SolanaRPC(
            config.rpc.url,
            config.rpc.fallback_url,
            config.rpc.timeout_s,
            config.rpc.max_retries,
        )
        self.oracle = PriceOracle(self.rpc, config.rpc.price_commitment)
        self.edge = PersonalEdge(
            self.db,
            min_samples=config.scoring.personal_edge_min_samples,
            max_adjust=config.scoring.personal_edge_max_adjust,
            enabled=config.scoring.personal_edge_enabled,
        )
        self.scorer = Scorer(config, self.edge)
        self.risk = RiskGate(config)
        self.llm = LLMAdvisor(config)
        self.wallets = WalletTracker(self.db, seed_wallets=config.wallets.seed_wallets)

        # --- Intelligence stack --------------------------------------------
        self.registry = ProviderRegistry(config)
        self.graph = EntityGraph()
        self.intel = IntelEngine(
            self.registry, self.graph, NarrativeEngine(self.registry, config.intel.narrative_ttl_s)
        )
        self.router = ExecutionRouter(config, self.registry, self.rpc)
        self.notifier = OutboundNotifier(
            telegram_token=config.telegram_bot_token if config.alerts.telegram else None,
            telegram_chat_id=config.telegram_chat_id if config.alerts.telegram else None,
            discord_webhook=config.discord_webhook_url if config.alerts.discord else None,
            generic_webhooks=config.alerts.webhooks,
            min_interval_s=config.alerts.min_interval_s,
            dedupe_window_s=config.alerts.dedupe_window_s,
        )
        self._intel_tasks: list[asyncio.Task] = []

        self.portfolio: Portfolio | None = None
        self._running = False
        self._kill_switch = False
        self._kill_reason = ""
        self._blocked_creators: set[str] = set()
        self._starting_bankroll = 0.0

        self.candidates_seen = 0
        self.decisions_made = 0
        self.entries_taken = 0
        self._last_review_day = ""

    # -- lifecycle ----------------------------------------------------------

    async def start(self) -> None:
        await self.bus.connect()
        await self.db.connect()
        await self.rpc.connect()
        await self.edge.load()
        await self.wallets.load()
        await self.llm.connect()

        # Providers and intel. All optional — a failure here degrades the
        # system's information, never its ability to trade or exit.
        if self.config.intel.enabled:
            try:
                self.registry.build()
                await self.registry.start()
                self._intel_tasks = await self.intel.start(
                    workers=self.config.providers.enrich_workers
                )
                coverage = self.registry.coverage_report()
                log.info(
                    "intel online: %d/%d providers ready%s",
                    coverage["ready"], coverage["total"],
                    f", gaps: {', '.join(coverage['critical_gaps'])}"
                    if coverage["critical_gaps"] else "",
                )
            except Exception as exc:  # noqa: BLE001
                log.error("intel stack failed to start (continuing without it): %s", exc)
        await self.notifier.start()

        executor = await build_executor(self.config, self.rpc, self.oracle)
        self.portfolio = Portfolio(self.config, self.db, executor, self.oracle, self.edge)
        self.portfolio.on_fill = self._publish_fill
        self.portfolio.on_position_closed = self._on_position_closed

        restored = await self.portfolio.restore()
        if restored and not executor.paper:
            await self.portfolio.reconcile_live()

        self._blocked_creators = await self.db.blocked_creators()
        self._starting_bankroll = await self.portfolio.bankroll()
        self._running = True

        mode = "LIVE — REAL MONEY" if self.config.is_live else "paper"
        log.warning(
            "brain ready — mode: %s, bankroll %.4f SOL, %d learned trades, LLM %s",
            mode,
            self._starting_bankroll,
            self.edge.trades_learned,
            "on" if self.llm.available else "off",
        )

    async def run(self) -> None:
        await self.start()
        tasks = [
            asyncio.create_task(self._exit_loop(), name="exits"),
            asyncio.create_task(self._candidate_loop(), name="candidates"),
            asyncio.create_task(self._market_event_loop(), name="market_events"),
            asyncio.create_task(self._control_loop(), name="control"),
            asyncio.create_task(self._snapshot_loop(), name="snapshot"),
            asyncio.create_task(self._maintenance_loop(), name="maintenance"),
            asyncio.create_task(self._review_loop(), name="review"),
            asyncio.create_task(self._graph_loop(), name="graph"),
        ]
        tasks.extend(self._intel_tasks)
        try:
            await asyncio.gather(*tasks)
        except asyncio.CancelledError:
            pass
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            await self.stop()

    async def stop(self) -> None:
        self._running = False
        if self.portfolio is not None:
            open_count = self.portfolio.open_count
            if open_count:
                # Deliberately NOT auto-selling here. A restart is not a
                # reason to dump at market; positions are restored with their
                # exit rules intact when the Brain comes back.
                log.warning(
                    "shutting down with %d position(s) still open. They are saved and will "
                    "be restored on restart. To exit them now: mctb close-all",
                    open_count,
                )
            await self.portfolio.executor.close()
        self.intel.stop()
        with contextlib.suppress(Exception):
            await self.registry.close()
        with contextlib.suppress(Exception):
            await self.notifier.close()
        await self.llm.close()
        await self.rpc.close()
        await self.db.close()
        await self.bus.close()
        log.info("brain stopped")

    # -- kill switch --------------------------------------------------------

    async def _check_kill_switch(self) -> bool:
        """Three independent ways to stop the bot, checked every tick.

        A file, a bus flag, and the Console button. Any one of them stops
        entries; none of them stop exits, because abandoning open positions
        is not a safe state.
        """
        path = self.config.resolve(self.config.risk.kill_switch_file)
        if path.exists():
            if not self._kill_switch:
                log.critical("KILL SWITCH FILE PRESENT at %s — no new entries", path)
            self._kill_switch = True
            self._kill_reason = f"kill switch file at {path}"
            return True

        engaged, reason = await self.bus.kill_switch_engaged()
        if engaged:
            if not self._kill_switch:
                log.critical("KILL SWITCH ENGAGED: %s", reason)
            self._kill_switch = True
            self._kill_reason = reason or "engaged remotely"
            return True

        if self._kill_switch:
            log.warning("kill switch cleared — entries re-enabled")
            self._kill_switch = False
            self._kill_reason = ""
        return False

    # -- entry path ---------------------------------------------------------

    async def _candidate_loop(self) -> None:
        async for _topic, message in self.bus.subscribe(Topic.CANDIDATE):
            if not self._running:
                break
            if not isinstance(message, Candidate):
                continue
            try:
                await self._consider(message)
            except Exception as exc:  # noqa: BLE001
                log.exception("error considering candidate %s: %s", message.mint, exc)

    async def _consider(self, candidate: Candidate) -> None:
        assert self.portfolio is not None
        self.candidates_seen += 1

        # 1. Hard gates — cheap, and they veto outright.
        recently = await self.db.recent_mint_trade(
            candidate.mint, self.config.risk.same_mint_cooldown_s
        )
        gate_result = apply_gates(
            candidate,
            self.config,
            blocked_creators=self._blocked_creators,
            already_holding=self.portfolio.holds(candidate.mint),
            recently_traded=recently,
        )
        if not gate_result.passed:
            log.debug(
                "gated out %s: %s",
                candidate.symbol or candidate.mint[:10],
                "; ".join(gate_result.failures[:3]),
            )
            return

        # 2. Deterministic score — microseconds.
        with LATENCY.timer("score"):
            scored = self.scorer.score(candidate)
        breakdown = scored.breakdown

        # 2b. Fused provider intelligence.
        #
        # Reads from the HOT path first — cache only, no network — so the
        # common case adds microseconds. Only a candidate that already scores
        # near the entry threshold is worth spending a network round trip on,
        # and even then the budget is tight and a timeout simply means less
        # information rather than a missed trade.
        intel_verdict = None
        if self.config.intel.enabled and self.registry.providers:
            with LATENCY.timer("intel"):
                dossier = self.intel.assemble_hot(candidate.mint, candidate.symbol)

                near_threshold = (
                    breakdown.final_score
                    >= self.config.scoring.entry_threshold - 8.0
                )
                if near_threshold and not dossier.security.provenance.get("sources"):
                    try:
                        dossier = await self.intel.assemble(
                            candidate.mint,
                            candidate.symbol,
                            tier=Tier.WARM,
                            timeout_s=self.config.providers.warm_timeout_s,
                            include_narrative=False,
                            creator=candidate.creator,
                        )
                    except Exception as exc:  # noqa: BLE001
                        log.debug("warm intel assembly failed for %s: %s", candidate.mint, exc)

                intel_verdict = evaluate_intel(
                    dossier,
                    self.config,
                    graph=self.graph,
                    smart_wallets=candidate.smart_wallets,
                )

            if not intel_verdict.passed:
                decision = Decision(
                    source="brain",
                    mint=candidate.mint,
                    symbol=candidate.symbol,
                    action="skip",
                    score=breakdown.final_score,
                    breakdown=breakdown,
                    reason="; ".join(intel_verdict.blockers[:3]),
                )
                log.info(
                    "intel blocked %s: %s",
                    candidate.symbol or candidate.mint[:10],
                    intel_verdict.blockers[0],
                )
                await self._publish_decision(decision)
                return

            if intel_verdict.adjustment:
                breakdown.components["intel"] = round(intel_verdict.adjustment, 2)
                breakdown.final_score = round(
                    max(0.0, min(100.0, breakdown.final_score + intel_verdict.adjustment)), 2
                )
                breakdown.notes.append(summarise(intel_verdict, dossier))
            for note in intel_verdict.notes[:3]:
                breakdown.notes.append(note)

            # Sybil collapse changes the meaning of the smart-wallet feature,
            # so record what the consensus really was.
            if intel_verdict.sybil_ratio > 0:
                breakdown.notes.append(
                    f"{len(candidate.smart_wallets)} wallets collapse to "
                    f"{intel_verdict.effective_smart_wallets} distinct actors"
                )

        # 3. LLM advisory, only if the score already qualifies. Bounded, and
        #    skipped entirely if it would delay us past the opportunity.
        if self.llm.available and breakdown.final_score >= self.config.llm.min_score_to_consult:
            adjust, reasoning, _raw = await self.llm.evaluate_candidate(
                candidate, breakdown.final_score
            )
            if adjust:
                breakdown.llm_adjust = adjust
                breakdown.final_score = round(
                    max(0.0, min(100.0, breakdown.final_score + adjust)), 2
                )
                breakdown.notes.append(f"llm {adjust:+.1f}: {reasoning[:160]}")
        else:
            reasoning = ""

        score = breakdown.final_score
        threshold = self.config.scoring.entry_threshold

        decision = Decision(
            source="brain",
            mint=candidate.mint,
            symbol=candidate.symbol,
            score=score,
            breakdown=breakdown,
            llm_reasoning=reasoning,
        )

        if score < threshold:
            decision.action = "skip"
            decision.reason = f"score {score:.1f} below threshold {threshold:.1f}"
            await self._publish_decision(decision)
            return

        # 4. Risk gates — portfolio-level, not token-level.
        bankroll = await self.portfolio.bankroll()
        size = self.scorer.size_position(score, bankroll)

        if await self._check_kill_switch():
            decision.action = "skip"
            decision.reason = f"kill switch active: {self._kill_reason}"
            await self._publish_decision(decision)
            return

        risk_result = self.risk.check(
            open_positions=self.portfolio.open_count,
            bankroll_sol=bankroll,
            daily_pnl_sol=self.portfolio.daily_pnl_sol,
            proposed_size_sol=size,
        )
        if not risk_result.passed:
            decision.action = "skip"
            decision.reason = "; ".join(risk_result.failures)
            log.info(
                "risk blocked %s (score %.1f): %s",
                candidate.symbol or candidate.mint[:10],
                score,
                decision.reason,
            )
            await self._publish_decision(decision)
            return

        # 5. Go.
        # 5. Pre-trade cost check.
        #
        # Two quotes and no gas, and it catches the case a contract scan
        # cannot: a token you can buy but not sell. On a thin token the round
        # trip alone can exceed 20%, which means the position needs a 20% move
        # just to break even — worth knowing before committing, not after.
        if self.config.intel.enabled and self.registry.get("jupiter"):
            try:
                estimate = await asyncio.wait_for(
                    self.router.estimate_cost(candidate.mint, size), timeout=2.0
                )
                if not estimate.sellable:
                    decision.action = "skip"
                    decision.reason = f"no sell route: {estimate.reason}"
                    log.warning(
                        "REFUSED %s — buy route exists but sell route does not",
                        candidate.symbol or candidate.mint[:10],
                    )
                    await self._publish_decision(decision)
                    return
                if estimate.round_trip_pct > self.config.intel.max_round_trip_cost_pct:
                    decision.action = "skip"
                    decision.reason = (
                        f"round trip would cost {estimate.round_trip_pct:.0f}% "
                        f"(limit {self.config.intel.max_round_trip_cost_pct:.0f}%)"
                    )
                    await self._publish_decision(decision)
                    return
                decision.breakdown.notes.append(
                    f"round trip cost {estimate.round_trip_pct:.1f}%"
                )
            except (asyncio.TimeoutError, Exception) as exc:  # noqa: BLE001
                log.debug("cost estimate skipped for %s: %s", candidate.mint, exc)

        decision.action = "buy"
        decision.size_sol = size
        decision.reason = f"score {score:.1f} >= {threshold:.1f}"
        await self._publish_decision(decision)

        position = await self.portfolio.open_position(candidate, size, score, breakdown)
        if position is not None:
            self.entries_taken += 1
            self.risk.record_open(size)

    async def _publish_decision(self, decision: Decision) -> None:
        self.decisions_made += 1
        await self.db.record_decision(decision)
        await self.bus.publish(Topic.DECISION, decision)

    async def _publish_fill(self, fill: Fill) -> None:
        await self.bus.publish(Topic.FILL, fill)

    async def _on_position_closed(self, position: Position) -> None:
        self.risk.record_close(position.total_pnl_sol)

        # Credit the wallets that got us in — or debit them.
        if position.smart_wallets:
            await self.wallets.record_copy_outcome(
                position.smart_wallets, position.total_pnl_sol
            )

        self._blocked_creators = await self.db.blocked_creators()

        if abs(position.pnl_pct) > 0.5:
            await self.bus.publish(
                Topic.ALERT,
                Alert(
                    source="brain",
                    level="info" if position.total_pnl_sol > 0 else "warning",
                    title=f"{'Win' if position.total_pnl_sol > 0 else 'Loss'}: {position.symbol or position.mint[:10]}",
                    message=(
                        f"{position.total_pnl_sol:+.4f} SOL ({position.pnl_pct:+.0%}) "
                        f"in {position.age_s:.0f}s via {position.exit_reason}"
                    ),
                ),
            )

    # -- exit path ----------------------------------------------------------

    async def _exit_loop(self) -> None:
        """Highest-priority loop. Never blocked by scoring, LLM or dashboard."""
        while self._running:
            try:
                if self.portfolio is not None and self.portfolio.open_count:
                    await self.portfolio.mark_to_market()
                    await self.portfolio.process_exits()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                log.exception("exit loop error: %s", exc)
            await asyncio.sleep(1.2)

    # -- market events ------------------------------------------------------

    async def _market_event_loop(self) -> None:
        """Watch for the events that trigger emergency exits."""
        async for _topic, message in self.bus.subscribe(
            Topic.WALLET_TRADE, Topic.TOKEN_TRADE, Topic.MIGRATION
        ):
            if not self._running:
                break
            if self.portfolio is None:
                continue

            try:
                if isinstance(message, MigrationEvent):
                    self.portfolio.note_migration(message.mint)
                    continue

                if not isinstance(message, TradeEvent):
                    continue

                position = self.portfolio.positions.get(message.mint)
                if position is None:
                    continue

                # Price straight from the trade — faster than waiting for the
                # next batched curve read.
                if message.price_sol > 0:
                    from .exits import update_position_price

                    update_position_price(position, message.price_sol)

                if not message.is_buy:
                    if message.trader and message.trader == position.creator:
                        self.portfolio.note_dev_sell(message.mint)
                    elif message.trader in position.smart_wallets:
                        self.portfolio.note_smart_exit(message.mint)
                elif message.trader:
                    # Feed the entity graph. Every observed buy is evidence
                    # about who is coordinating with whom.
                    self.graph.observe_buy(
                        message.trader, message.mint, message.ts, message.sol_amount
                    )
            except Exception as exc:  # noqa: BLE001
                log.debug("market event handling error: %s", exc)

    # -- control ------------------------------------------------------------

    async def _control_loop(self) -> None:
        async for _topic, message in self.bus.subscribe(Topic.CONTROL):
            if not self._running:
                break
            if not isinstance(message, ControlCommand):
                continue
            try:
                await self._handle_control(message)
            except Exception as exc:  # noqa: BLE001
                log.exception("control command failed: %s", exc)

    async def _handle_control(self, command: ControlCommand) -> None:
        assert self.portfolio is not None
        log.warning("control command received: %s %s", command.command, command.mint or "")

        if command.command == "kill":
            await self.bus.set_kill_switch(True, command.value or "console kill switch")
            self._kill_switch = True
            self._kill_reason = command.value or "console kill switch"

        elif command.command == "resume":
            await self.bus.set_kill_switch(False)
            self._kill_switch = False
            self.risk.resume()

        elif command.command == "close_position" and command.mint:
            await self.portfolio.force_close(command.mint, "manual_console")

        elif command.command == "close_all":
            count = await self.portfolio.close_all("manual_close_all")
            log.warning("closed %d position(s) on command", count)

        elif command.command == "request_snapshot":
            await self._publish_snapshot()

        elif command.command == "reload_config":
            log.warning(
                "config reload requested — restart the service to apply. "
                "Open positions are restored automatically."
            )

    # -- snapshots ----------------------------------------------------------

    async def _publish_snapshot(self) -> None:
        if self.portfolio is None:
            return
        bankroll = await self.portfolio.bankroll()
        snapshot = PortfolioSnapshot(
            source="brain",
            mode="live" if self.config.is_live else "paper",
            bankroll_sol=bankroll,
            starting_bankroll_sol=self._starting_bankroll,
            positions=self.portfolio.open_positions,
            realised_pnl_sol=self.portfolio.realised_today,
            unrealised_pnl_sol=self.portfolio.unrealised_pnl_sol,
            daily_pnl_sol=self.portfolio.daily_pnl_sol,
            trades_today=self.portfolio.closed_today,
            wins_today=self.portfolio.wins_today,
            losses_today=self.portfolio.losses_today,
            halted=self.risk.halted or self._kill_switch,
            halt_reason=self._kill_reason or self.risk.halt_reason,
            top_wallets=self.wallets.qualified()[:10],
        )
        await self.bus.publish(Topic.SNAPSHOT, snapshot, journal=False)

    async def _snapshot_loop(self) -> None:
        while self._running:
            try:
                await self._publish_snapshot()
                await self.bus.publish(
                    Topic.HEARTBEAT,
                    Heartbeat(
                        source="brain",
                        service="brain",
                        healthy=True,
                        detail={
                            "candidates": self.candidates_seen,
                            "decisions": self.decisions_made,
                            "entries": self.entries_taken,
                            "open": self.portfolio.open_count if self.portfolio else 0,
                            "halted": self.risk.halted or self._kill_switch,
                            "edge_trades": self.edge.trades_learned,
                            "llm": self.llm.stats(),
                            "intel": self.intel.stats() if self.config.intel.enabled else {},
                            "providers": self.registry.coverage_report()
                            if self.registry.providers
                            else {},
                            "routes": self.router.stats(),
                            "latency": LATENCY.report(),
                            "alerts": self.notifier.stats(),
                        },
                    ),
                    journal=False,
                )
            except Exception as exc:  # noqa: BLE001
                log.debug("snapshot failed: %s", exc)
            await asyncio.sleep(1.0)

    # -- maintenance --------------------------------------------------------

    async def _maintenance_loop(self) -> None:
        while self._running:
            await asyncio.sleep(10.0)
            try:
                await self._check_kill_switch()
                if self.portfolio is not None:
                    self.portfolio.prune_signals()
                if not self.llm.available and self.config.llm.enabled:
                    # Re-probe occasionally so the LLM coming back online is
                    # picked up without a restart.
                    await self.llm.probe()
            except Exception as exc:  # noqa: BLE001
                log.debug("maintenance error: %s", exc)

    # -- entity graph -------------------------------------------------------

    async def _graph_loop(self) -> None:
        """Keep the wallet-cluster graph fresh.

        Funding lineage is what turns co-occurrence heuristics into real
        entity resolution, and it needs a Helius key. Without one the graph
        still clusters from co-buying behaviour, just less precisely.
        """
        while self._running:
            await asyncio.sleep(self.config.intel.funding_resolve_interval_s)
            if not self.config.intel.enabled or not self.config.intel.resolve_funding:
                continue
            try:
                # Prioritise wallets we actually care about: tracked wallets
                # and anyone holding something we hold.
                candidates = [w.address for w in self.wallets.qualified()[:40]]
                if self.portfolio:
                    for position in self.portfolio.open_positions:
                        candidates.extend(position.smart_wallets)

                unresolved = [
                    a for a in dict.fromkeys(candidates)
                    if a not in self.graph.nodes or not self.graph.nodes[a].first_funder
                ]
                if unresolved:
                    await self.intel.resolve_funding(
                        unresolved, limit=self.config.intel.funding_resolve_batch
                    )

                self.graph.rebuild()
                self.graph.prune()
                self.intel.narrative.prune()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                log.debug("graph maintenance failed: %s", exc)

    # -- nightly review -----------------------------------------------------

    async def _review_loop(self) -> None:
        while self._running:
            await asyncio.sleep(300)
            if not self.config.llm.nightly_review or not self.llm.available:
                continue

            now = datetime.now(timezone.utc)
            today = now.strftime("%Y-%m-%d")
            if now.hour != self.config.llm.nightly_review_hour_utc:
                continue
            if self._last_review_day == today:
                continue

            self._last_review_day = today
            try:
                await self.run_review()
            except Exception as exc:  # noqa: BLE001
                log.exception("nightly review failed: %s", exc)

    async def run_review(self) -> dict | None:
        """Have the LLM analyse the trade history and propose changes."""
        performance = await self.db.performance_summary()
        if performance.get("trades", 0) < 10:
            log.info("skipping review — only %d closed trades so far", performance.get("trades", 0))
            return None

        log.info("running nightly review over %d trades", performance["trades"])

        recent = await self.db.closed_positions(limit=40)
        tunable = {
            "scoring.entry_threshold": self.config.scoring.entry_threshold,
            "gates.min_age_s": self.config.gates.min_age_s,
            "gates.max_age_s": self.config.gates.max_age_s,
            "gates.min_sol_in_curve": self.config.gates.min_sol_in_curve,
            "gates.max_sol_in_curve": self.config.gates.max_sol_in_curve,
            "gates.min_unique_buyers": self.config.gates.min_unique_buyers,
            "gates.min_buy_sell_ratio": self.config.gates.min_buy_sell_ratio,
            "exits.stop_loss_pct": self.config.exits.stop_loss_pct,
            "exits.trailing_activate_pct": self.config.exits.trailing_activate_pct,
            "exits.trailing_distance_pct": self.config.exits.trailing_distance_pct,
            "exits.max_hold_s": self.config.exits.max_hold_s,
        }

        result = await self.llm.nightly_review(
            performance, self.edge.report(), recent, tunable
        )
        if not result:
            log.warning("review produced no usable output")
            return None

        summary = str(result.get("summary", ""))
        findings = result.get("findings", []) or []
        proposals = result.get("proposed_changes", []) or []

        # Validate every proposal against the allowlist and its bounds. A
        # confidently wrong suggestion should be rejected here, not applied.
        accepted, rejected = [], []
        for proposal in proposals:
            path = str(proposal.get("parameter", ""))
            value = proposal.get("proposed")
            ok, why = validate_proposed_change(path, value)
            (accepted if ok else rejected).append(
                {**proposal, "rejection_reason": why} if not ok else proposal
            )

        note_text = "\n".join(
            [
                f"SUMMARY: {summary}",
                "",
                "FINDINGS:",
                *[f"  - {f}" for f in findings],
                "",
                "PROPOSED CHANGES:",
                *[
                    f"  - {p.get('parameter')}: {p.get('current')} -> {p.get('proposed')} "
                    f"({p.get('rationale', '')})"
                    for p in accepted
                ],
                *[
                    f"  - REJECTED {p.get('parameter')}: {p.get('rejection_reason')}"
                    for p in rejected
                ],
            ]
        )

        note = LLMNote(
            source="brain",
            kind="review",
            text=note_text,
            data={
                "performance": performance,
                "accepted": accepted,
                "rejected": rejected,
                "auto_applied": False,
            },
        )
        await self.db.record_llm_note(
            note.id, note.ts, "review", "", note_text, note.data
        )
        await self.bus.publish(Topic.LLM_NOTE, note)

        log.warning("NIGHTLY REVIEW\n%s", note_text)

        if accepted and self.config.llm.auto_apply_tuning:
            applied = await self._apply_tuning(accepted)
            note.data["auto_applied"] = applied
            log.warning("auto-applied %d parameter change(s)", len(applied))
        elif accepted:
            log.warning(
                "%d change(s) proposed but auto_apply_tuning is off. Review them and edit "
                "config/config.yaml yourself.",
                len(accepted),
            )

        await self.bus.publish(
            Topic.ALERT,
            Alert(
                source="brain",
                level="info",
                title="Nightly review complete",
                message=summary[:300],
            ),
        )
        return result

    async def _apply_tuning(self, proposals: list[dict]) -> list[dict]:
        """Write accepted changes into the live config object.

        Persisted to the database as an override layer rather than rewriting
        your YAML, so your file stays the source of truth and you can always
        see and undo what the model changed.
        """
        applied = []
        overrides = await self.db.get_kv("llm_overrides", {}) or {}

        for proposal in proposals:
            path = str(proposal["parameter"])
            value = float(proposal["proposed"])
            section, _, field = path.partition(".")
            target = getattr(self.config, section, None)
            if target is None or not hasattr(target, field):
                continue
            old = getattr(target, field)
            # Preserve int-ness where the field is an int.
            setattr(target, field, type(old)(value) if isinstance(old, int) else value)
            overrides[path] = value
            applied.append({"parameter": path, "from": old, "to": value})
            log.warning("tuning applied: %s %s -> %s", path, old, value)

        await self.db.set_kv("llm_overrides", overrides)
        return applied


async def run_brain(config: Config) -> None:
    setup_logging(config.log_level, config.resolve(config.paths.log_dir), "brain")
    brain = Brain(config)

    loop = asyncio.get_running_loop()
    stop_event = asyncio.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        with contextlib.suppress(NotImplementedError):
            loop.add_signal_handler(sig, stop_event.set)

    runner = asyncio.create_task(brain.run())
    waiter = asyncio.create_task(stop_event.wait())
    done, pending = await asyncio.wait({runner, waiter}, return_when=asyncio.FIRST_COMPLETED)

    if waiter in done:
        log.info("shutdown signal received")
        runner.cancel()
    for task in pending:
        task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await runner
