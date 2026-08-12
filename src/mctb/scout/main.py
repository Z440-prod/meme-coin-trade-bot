"""Scout service — RPi 1.

Ingests every data source, keeps a bounded watchlist of live launches, screens
them on chain, and publishes `Candidate` events for the Brain to act on.

The Scout deliberately does not decide anything about money. It gathers
evidence and hands it over. That separation means you can restart, redeploy or
break the Scout without any risk to an open position.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import signal
import time

from ..bus import Bus
from ..config import Config
from ..db import Database
from ..logging_setup import setup_logging
from ..models import (
    Alert,
    Candidate,
    Heartbeat,
    MigrationEvent,
    NewTokenEvent,
    SocialEvent,
    Topic,
    TradeEvent,
)
from ..solana.checks import SafetyChecker, SafetyThresholds
from ..solana.rpc import SolanaRPC
from .observer import TokenObservation, Watchlist
from .pumpportal import PumpPortalFeed
from .rpc_watcher import PriceOracle, RpcWatcher
from .social import SocialIndex, SocialMonitor
from .wallets import WalletTracker

log = logging.getLogger(__name__)


class Scout:
    def __init__(self, config: Config) -> None:
        self.config = config
        self.bus = Bus(
            config.bus.redis_url,
            config.bus.redis_password,
            source="scout",
            journal_maxlen=config.bus.journal_maxlen,
        )
        self.db = Database(config.resolve(config.paths.db_file))
        self.rpc = SolanaRPC(
            config.rpc.url,
            config.rpc.fallback_url,
            config.rpc.timeout_s,
            config.rpc.max_retries,
        )
        self.safety = SafetyChecker(self.rpc)
        self.oracle = PriceOracle(self.rpc, config.rpc.price_commitment)

        self.watchlist = Watchlist(config.scout.watchlist_size, config.scout.watchlist_ttl_s)
        self.social_index = SocialIndex(config.social.mention_ttl_s)

        self.wallets = WalletTracker(
            self.db,
            seed_wallets=config.wallets.seed_wallets,
            auto_discover=config.wallets.auto_discover,
            min_wins=config.wallets.discovery_min_wins,
            win_multiple=config.wallets.discovery_win_multiple,
            max_entry_sol=config.wallets.discovery_max_entry_sol,
            max_tracked=config.wallets.max_tracked_wallets,
            prune_below_score=config.wallets.prune_below_score,
            grade_after_s=config.scout.wallet_grade_after_s,
        )

        self.feed = PumpPortalFeed(
            api_key=config.pumpportal_api_key,
            on_new_token=self._on_new_token,
            on_trade=self._on_trade,
            on_wallet_trade=self._on_wallet_trade,
            on_migration=self._on_migration,
        )
        self.rpc_watcher = RpcWatcher(
            config.rpc.ws_url,
            self.rpc,
            on_new_token=self._on_new_token,
            # Trades decoded straight from program logs. Without a PumpPortal
            # API key this is the ONLY source of trade data, and without trade
            # data nothing can ever score highly enough to become a candidate.
            on_trade=self._on_chain_trade,
            enabled=config.scout.enable_rpc_watcher,
        )
        self.social = SocialMonitor(
            bearer_token=config.x_bearer_token,
            accounts=config.social.x_accounts,
            queries=config.social.x_queries,
            poll_interval_s=config.social.poll_interval_s,
            min_followers=config.social.min_followers,
            on_event=self._on_social,
            enabled=config.social.enabled,
        )

        self._running = False
        self._blocked_creators: set[str] = set()
        self._emitted: dict[str, float] = {}
        self.candidates_emitted = 0
        self.tokens_seen = 0

    # -- lifecycle ----------------------------------------------------------

    async def start(self) -> None:
        await self.bus.connect()
        await self.db.connect()
        await self.rpc.connect()
        await self.wallets.load()
        self._blocked_creators = await self.db.blocked_creators()

        health = await self.rpc.get_health()
        if "unhealthy" in health:
            log.warning("RPC endpoint reports: %s", health)
        else:
            log.info("RPC endpoint healthy at %s", self.rpc.url.split("?")[0])

        self._running = True
        log.info("scout starting — watchlist cap %d", self.config.scout.watchlist_size)

    async def run(self) -> None:
        await self.start()
        tasks = [
            asyncio.create_task(self.feed.run(), name="feed"),
            asyncio.create_task(self.rpc_watcher.run(), name="rpc_watcher"),
            asyncio.create_task(self.social.run(), name="social"),
            asyncio.create_task(self._evaluate_loop(), name="evaluate"),
            asyncio.create_task(self._maintenance_loop(), name="maintenance"),
            asyncio.create_task(self._heartbeat_loop(), name="heartbeat"),
        ]
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
        with contextlib.suppress(Exception):
            await self.feed.stop()
        with contextlib.suppress(Exception):
            await self.rpc_watcher.stop()
        with contextlib.suppress(Exception):
            await self.social.stop()
        await self.rpc.close()
        await self.db.close()
        await self.bus.close()
        log.info("scout stopped")

    # -- feed callbacks -----------------------------------------------------

    async def _on_new_token(self, event: NewTokenEvent) -> None:
        if event.mint in self.watchlist:
            return

        self.tokens_seen += 1
        obs = TokenObservation(
            mint=event.mint,
            name=event.name,
            symbol=event.symbol,
            creator=event.creator,
            sol_in_curve=event.sol_in_curve,
            market_cap_sol=event.market_cap_sol,
        )
        self.watchlist.add(obs)
        self.wallets.observe_new_token(event.mint, 0.0, event.sol_in_curve)

        # Subscribe to this token's trades immediately.
        #
        # This is not optional — it breaks a deadlock. A brand-new token has
        # no buyers, no buy/sell ratio and no velocity, so its triage score is
        # near zero and it would never qualify for the on-chain safety screen.
        # But the screen is what used to trigger the subscription, so the token
        # could never accumulate the very data needed to judge it. Without
        # this, the Scout emits zero candidates for ever unless a seeded smart
        # wallet happens to trade.
        #
        # Subscriptions are metered, so this is capped and the oldest quiet
        # token is evicted to make room for newer launches.
        await self._maybe_subscribe_token(obs)

        await self.db.record_token(
            event.mint, event.name, event.symbol, event.creator, event.initial_buy_sol
        )
        await self.bus.publish(Topic.TOKEN_NEW, event, journal=False)

    async def _on_trade(self, event: TradeEvent) -> None:
        obs = self.watchlist.get(event.mint)
        if obs is None:
            return
        smart = self.wallets.score_for(event.trader) if self.wallets.is_tracked(event.trader) else 0.0
        obs.apply_trade(event, smart_score=smart)
        self.wallets.observe_trade(event)

        # Relay trades only for tokens we deliberately subscribed to. Those
        # are the ones the Brain may be holding, and it needs to see a dev
        # sell the moment it happens rather than on the next price poll.
        # Relaying the whole firehose would swamp the bus for no benefit.
        if obs.subscribed:
            await self.bus.publish(Topic.TOKEN_TRADE, event, journal=False)

    async def _on_chain_trade(self, event: TradeEvent) -> None:
        """A trade decoded from pump.fun program logs.

        This is the firehose — every trade on the whole program — so it is
        filtered to the watchlist immediately. Tokens we are not tracking are
        dropped without further work.
        """
        obs = self.watchlist.get(event.mint)
        if obs is None:
            return

        smart = (
            self.wallets.score_for(event.trader)
            if self.wallets.is_tracked(event.trader)
            else 0.0
        )
        obs.apply_trade(event, smart_score=smart)
        self.wallets.observe_trade(event)

        # Relay to the Brain so its entity graph and exit logic see it.
        await self.bus.publish(Topic.TOKEN_TRADE, event, journal=False)

        if smart > 0 and event.is_buy:
            await self.bus.publish(Topic.WALLET_TRADE, event)

    async def _on_wallet_trade(self, event: TradeEvent) -> None:
        """A wallet we deliberately follow just traded — the strongest signal
        we get, and the one worth reacting to immediately rather than waiting
        for the next evaluation pass."""
        score = self.wallets.score_for(event.trader)
        obs = self.watchlist.get(event.mint)

        if obs is None:
            # They bought something we were not even watching. Start now.
            obs = TokenObservation(mint=event.mint, sol_in_curve=event.sol_in_curve)
            self.watchlist.add(obs)

        obs.apply_trade(event, smart_score=score)
        self.wallets.observe_trade(event)
        await self.bus.publish(Topic.WALLET_TRADE, event)

        log.info(
            "tracked wallet %s… %s %s (score %.2f, %.3f SOL)",
            event.trader[:8],
            "BOUGHT" if event.is_buy else "SOLD",
            (obs.symbol or event.mint)[:12],
            score,
            event.sol_amount,
        )

        if event.is_buy:
            await self._evaluate_token(obs, trigger="smart_wallet")

    async def _on_migration(self, event: MigrationEvent) -> None:
        obs = self.watchlist.get(event.mint)
        if obs:
            obs.migrated = True
        await self.bus.publish(Topic.MIGRATION, event)

    async def _on_social(self, event: SocialEvent) -> None:
        self.social_index.add(event)
        await self.bus.publish(Topic.SOCIAL, event)

        # A big account naming a specific mint is worth evaluating right away.
        for mint in event.mentioned_mints:
            obs = self.watchlist.get(mint)
            if obs is not None:
                await self._evaluate_token(obs, trigger="social")

    # -- evaluation ---------------------------------------------------------

    def _prelim_score(self, obs: TokenObservation) -> float:
        """Cheap 0-100 triage before spending RPC calls on the safety screen.

        Deliberately crude. Its only job is to keep us from running three
        on-chain reads for every one of the thousands of tokens launched daily.
        """
        score = 0.0
        gates = self.config.gates

        if obs.smart_wallets:
            score += 40.0 * min(1.0, obs.smart_wallet_score + 0.3)
        if obs.buy_sell_ratio >= gates.min_buy_sell_ratio:
            score += 15.0
        if len(obs.buyers) >= gates.min_unique_buyers:
            score += 15.0
        if gates.min_sol_in_curve <= obs.sol_in_curve <= gates.max_sol_in_curve:
            score += 15.0
        _net, _count, velocity = obs.window_flow()
        if velocity > 0.05:
            score += min(15.0, velocity * 60.0)
        return score

    async def _evaluate_token(self, obs: TokenObservation, trigger: str = "momentum") -> None:
        """Screen one token and publish a Candidate if it survives."""
        gates = self.config.gates
        now = time.time()

        # Do not spam the Brain with the same token every pass.
        last = self._emitted.get(obs.mint, 0.0)
        if now - last < 20.0:
            return

        age = obs.age_s
        if age < gates.min_age_s or age > gates.max_age_s:
            return
        if obs.migrated:
            return
        if gates.block_known_rug_creators and obs.creator in self._blocked_creators:
            return
        if gates.require_smart_wallet and not obs.smart_wallets:
            return

        prelim = self._prelim_score(obs)
        if prelim < self.config.scout.min_prelim_score_for_safety_check:
            return

        # Refresh curve state from chain — the feed's numbers can lag, and
        # everything downstream keys off these.
        try:
            curve = await self.rpc.get_bonding_curve(
                obs.mint, commitment=self.config.rpc.price_commitment
            )
        except Exception as exc:  # noqa: BLE001
            log.debug("curve read failed for %s: %s", obs.mint, exc)
            return

        if curve is None:
            return
        if curve.complete:
            obs.migrated = True
            return

        obs.sol_in_curve = curve.sol_in_curve
        obs.market_cap_sol = curve.market_cap_sol
        obs.last_price = curve.price_sol
        obs.peak_price = max(obs.peak_price, curve.price_sol)
        obs.total_supply_tokens = curve.token_total_supply / 10**6
        if curve.creator and not obs.creator:
            obs.creator = curve.creator

        if not (gates.min_sol_in_curve <= obs.sol_in_curve <= gates.max_sol_in_curve):
            return

        # On-chain safety screen — the expensive part, hence the gating above.
        thresholds = SafetyThresholds(
            require_mint_authority_revoked=gates.require_mint_authority_revoked,
            require_freeze_authority_revoked=gates.require_freeze_authority_revoked,
            max_dev_holding_pct=gates.max_dev_holding_pct,
            max_top10_holding_pct=gates.max_top10_holding_pct,
            max_bundle_pct=gates.max_bundle_pct,
            name_blocklist=gates.name_blocklist,
        )
        safety = await self.safety.check(
            obs.mint,
            thresholds,
            creator=obs.creator,
            curve=curve,
            name=obs.name,
            symbol=obs.symbol,
        )

        creator_rep = (
            await self.db.creator_reputation(obs.creator) if obs.creator else {"rugs": 0, "wins": 0}
        )

        mentions, reach = self.social_index.lookup(obs.mint, obs.symbol)
        features = obs.to_features(
            social_mentions=mentions,
            social_reach=reach,
            creator_rugs=creator_rep.get("rugs", 0),
            creator_wins=creator_rep.get("wins", 0),
            dev_holding_pct=safety.dev_holding_pct,
            top10_holding_pct=safety.top10_holding_pct,
        )

        candidate = Candidate(
            source="scout",
            mint=obs.mint,
            name=obs.name,
            symbol=obs.symbol,
            creator=obs.creator,
            features=features,
            safety=safety,
            smart_wallets=sorted(obs.smart_wallets),
            trigger=trigger,  # type: ignore[arg-type]
        )

        self._emitted[obs.mint] = now
        self.candidates_emitted += 1
        await self.db.record_candidate(candidate)
        await self.bus.publish(Topic.CANDIDATE, candidate)

        log.info(
            "candidate %s (%s) age=%.0fs curve=%.2f SOL buyers=%d smart=%d safety=%s",
            obs.symbol or obs.mint[:10],
            trigger,
            age,
            obs.sol_in_curve,
            len(obs.buyers),
            len(obs.smart_wallets),
            "PASS" if safety.passed else f"FAIL({len(safety.failures)})",
        )

        # Promising tokens get a live trade stream so we see the exit as it
        # happens rather than on the next poll.
        if safety.passed and not obs.subscribed:
            await self._maybe_subscribe_token(obs)

    async def _maybe_subscribe_token(self, obs: TokenObservation) -> None:
        """Subscribe to a token's trade stream, within the metered budget.

        When the budget is full, the oldest token with no traction is dropped
        to make room. A launch that has attracted nothing in several minutes
        is not going to, and the slot is worth more spent on a fresh one.
        """
        if obs.subscribed or obs.migrated:
            return

        cap = self.config.scout.max_subscribed_tokens
        subscribed = [o for o in self.watchlist.all() if o.subscribed]

        if len(subscribed) >= cap:
            # Evict the least interesting: no tracked wallet involved, oldest,
            # fewest buyers. Never evict something a smart wallet is in.
            evictable = [o for o in subscribed if not o.smart_wallets]
            if not evictable:
                return
            victim = min(evictable, key=lambda o: (len(o.buyers), -o.age_s))
            # Only evict for a genuinely newer token.
            if victim.age_s < obs.age_s:
                return
            victim.subscribed = False
            await self.feed.unsubscribe_tokens([victim.mint])

        obs.subscribed = True
        await self.feed.subscribe_tokens([obs.mint])

    async def _evaluate_loop(self) -> None:
        while self._running:
            try:
                # Newest first: a launch's window of opportunity is short.
                for obs in sorted(
                    self.watchlist.all(), key=lambda o: o.created_ts, reverse=True
                ):
                    if not self._running:
                        break
                    await self._evaluate_token(obs, trigger="momentum")
                    # Yield between tokens so the feed callbacks stay responsive.
                    await asyncio.sleep(0)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                log.exception("evaluation pass failed: %s", exc)
            await asyncio.sleep(self.config.scout.evaluate_interval_s)

    # -- housekeeping -------------------------------------------------------

    async def _maintenance_loop(self) -> None:
        while self._running:
            await asyncio.sleep(30.0)
            try:
                expired = self.watchlist.expire()
                if expired:
                    stale = [m for m in expired if m in self.feed._tracked_tokens]
                    if stale:
                        await self.feed.unsubscribe_tokens(stale)

                self.social_index.prune()
                await self.wallets.grade_matured()

                # Keep metered subscriptions pointed at the best wallets.
                if self.config.wallets.enabled:
                    want = set(
                        self.wallets.to_subscribe(self.config.wallets.max_subscribed_wallets)
                    )
                    have = set(self.feed._tracked_wallets)
                    if want - have:
                        await self.feed.subscribe_wallets(sorted(want - have))
                    if have - want:
                        await self.feed.unsubscribe_wallets(sorted(have - want))

                await self.wallets.prune()
                self._blocked_creators = await self.db.blocked_creators()

                cutoff = time.time() - 300
                self._emitted = {m: t for m, t in self._emitted.items() if t > cutoff}
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                log.exception("maintenance failed: %s", exc)

    async def _heartbeat_loop(self) -> None:
        last_warning = 0.0
        while self._running:
            await asyncio.sleep(5.0)
            feed_age = time.time() - (self.feed.last_message_ts or 0)
            healthy = self.feed.connected and feed_age < 120

            await self.bus.publish(
                Topic.HEARTBEAT,
                Heartbeat(
                    source="scout",
                    service="scout",
                    healthy=healthy,
                    detail={
                        "watchlist": len(self.watchlist),
                        "tokens_seen": self.tokens_seen,
                        "candidates": self.candidates_emitted,
                        "feed_connected": self.feed.connected,
                        "feed_messages": self.feed.messages_received,
                        "feed_silent_s": round(feed_age, 1),
                        "rpc_watcher": self.rpc_watcher.connected,
                        "rpc_logs": self.rpc_watcher.logs_received,
                        "tracked_wallets": len(self.wallets.stats),
                        "social_posts": self.social.posts_seen,
                    },
                ),
                journal=False,
            )

            # Both feeds down at once means we are blind, and a blind bot with
            # open positions is the dangerous case.
            if not healthy and not self.rpc_watcher.connected:
                if time.time() - last_warning > 120:
                    last_warning = time.time()
                    await self.bus.publish(
                        Topic.ALERT,
                        Alert(
                            source="scout",
                            level="critical",
                            title="Scout is blind",
                            message=(
                                "Both the PumpPortal feed and the RPC watcher are down. "
                                "No new market data is arriving."
                            ),
                        ),
                    )


async def run_scout(config: Config) -> None:
    setup_logging(config.log_level, config.resolve(config.paths.log_dir), "scout")
    scout = Scout(config)

    loop = asyncio.get_running_loop()
    stop_event = asyncio.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        with contextlib.suppress(NotImplementedError):
            loop.add_signal_handler(sig, stop_event.set)

    runner = asyncio.create_task(scout.run())
    waiter = asyncio.create_task(stop_event.wait())
    done, pending = await asyncio.wait({runner, waiter}, return_when=asyncio.FIRST_COMPLETED)

    if waiter in done:
        log.info("shutdown signal received")
        runner.cancel()
    for task in pending:
        task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await runner
