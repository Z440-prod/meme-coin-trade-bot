"""Regression tests for bugs found during end-to-end verification.

Each of these reproduced a real failure. They are kept separate so it is
obvious what they are protecting.
"""

from __future__ import annotations

import time

import pytest

from mctb.brain.executor import PaperExecutor
from mctb.brain.intel_gate import evaluate_intel
from mctb.brain.router import ExecutionRouter, RouteDecision, Venue
from mctb.config import Config
from mctb.intel.graph import EntityGraph
from mctb.intel.types import MarketIntel, SecurityIntel, TokenDossier


class _DeadCurveRPC:
    """A token that has graduated: its bonding curve no longer exists."""

    async def get_bonding_curve(self, mint, commitment="processed"):
        return None


class _DeadOracle:
    async def prices(self, mints):
        return {m: 0.0 for m in mints}

    async def price(self, mint, max_age_s=2.0):
        return 0.0


class _StubRouter:
    """Router that finds a Jupiter route, as it would for a graduated token."""

    def __init__(self, sol_out: float = 0.42, tokens_out: float = 1_234_567.0) -> None:
        self.sol_out = sol_out
        self.tokens_out = tokens_out
        self.sell_calls = 0
        self.buy_calls = 0

    async def choose_sell_venue(self, mint, token_amount):
        self.sell_calls += 1
        return RouteDecision(
            venue=Venue.JUPITER, reason="graduated", expected_out=self.sol_out,
            route_labels=["Raydium"],
        )

    async def choose_buy_venue(self, mint, sol_amount):
        self.buy_calls += 1
        return RouteDecision(
            venue=Venue.JUPITER, reason="graduated", expected_out=self.tokens_out,
            route_labels=["Raydium"],
        )


class _NoRouteRouter(_StubRouter):
    async def choose_sell_venue(self, mint, token_amount):
        self.sell_calls += 1
        return RouteDecision(venue=Venue.UNAVAILABLE, reason="no sell route exists")

    async def choose_buy_venue(self, mint, sol_amount):
        self.buy_calls += 1
        return RouteDecision(venue=Venue.UNAVAILABLE, reason="no buy route exists")


class TestGraduatedTokenIsStillTradeable:
    """BUG: a token that completed its bonding curve was written off as a
    total loss, because only the curve was consulted for pricing.

    Graduation is what happens to tokens that WORKED. Writing those off turns
    the strategy's best outcomes into its worst ones.
    """

    async def test_sell_routes_through_aggregator_when_curve_is_gone(self) -> None:
        config = Config()
        router = _StubRouter(sol_out=0.42)
        executor = PaperExecutor(config, _DeadCurveRPC(), _DeadOracle(), router)

        fill = await executor.sell("GRAD" + "x" * 40, 1_000_000.0, symbol="WINNER")

        assert router.sell_calls == 1
        assert fill.ok
        assert fill.sol_amount > 0, "a graduated token must still be sellable"

    async def test_buy_routes_through_aggregator_when_curve_is_gone(self) -> None:
        config = Config()
        router = _StubRouter(tokens_out=5_000.0)
        executor = PaperExecutor(config, _DeadCurveRPC(), _DeadOracle(), router)

        fill = await executor.buy("GRAD" + "x" * 40, 0.05, symbol="WINNER")

        assert router.buy_calls == 1
        assert fill.ok and fill.token_amount > 0

    async def test_genuinely_unsellable_token_is_still_written_off(self) -> None:
        """The write-off path must survive — it is correct when there really
        is no route anywhere."""
        config = Config()
        router = _NoRouteRouter()
        executor = PaperExecutor(config, _DeadCurveRPC(), _DeadOracle(), router)

        fill = await executor.sell("DEAD" + "x" * 40, 1_000_000.0, symbol="RUG")

        assert fill.sol_amount == 0.0
        assert "liquidity" in (fill.error or "").lower()

    async def test_works_without_a_router(self) -> None:
        """No router configured must degrade, not crash."""
        config = Config()
        executor = PaperExecutor(config, _DeadCurveRPC(), _DeadOracle(), None)
        fill = await executor.sell("DEAD" + "x" * 40, 1_000.0)
        assert fill.sol_amount == 0.0


class TestRouterUnitsAreConsistent:
    """BUG: `choose_buy_venue` returned Jupiter's raw base-unit amount while
    the bonding-curve branch returned whole tokens. A 6-decimal token would
    have been recorded as a million times larger than it was, corrupting
    every downstream P&L calculation.

    Caught by noticing 0.01 SOL "buying" 751,659 USDC in a demo run.
    """

    class _FakeRPC:
        """Mint account for a token with the given decimals."""

        def __init__(self, decimals: int) -> None:
            self.decimals = decimals

        async def get_bonding_curve(self, mint, commitment="processed"):
            return None  # graduated

        async def get_account_data(self, address, commitment="confirmed"):
            from .conftest import build_mint_account

            return build_mint_account(decimals=self.decimals)

    class _FakeJupiter:
        name = "jupiter"
        available = True

        def __init__(self, out_amount: int) -> None:
            self.out_amount = out_amount

        async def get_quote(self, inp, outp, amount, slippage_bps=300, **kw):
            from mctb.providers.base import ProviderResult

            return ProviderResult(
                provider="jupiter", ok=True,
                data={
                    "in_amount": amount, "out_amount": self.out_amount,
                    "min_out_amount": self.out_amount, "price_impact_pct": 0.1,
                    "slippage_bps": slippage_bps, "route_hops": 1,
                    "route_labels": ["Raydium"], "raw_quote": {},
                },
            )

    def _router(self, decimals: int, out_amount: int):
        from mctb.providers import ProviderRegistry

        config = Config()
        registry = ProviderRegistry(config)
        registry.providers = {"jupiter": self._FakeJupiter(out_amount)}  # type: ignore[dict-item]
        return ExecutionRouter(config, registry, self._FakeRPC(decimals))

    async def test_buy_returns_whole_tokens_not_base_units(self) -> None:
        # 1,500,000 base units of a 6-decimal token == 1.5 whole tokens.
        router = self._router(decimals=6, out_amount=1_500_000)
        decision = await router.choose_buy_venue("M" * 44, 0.01)
        assert decision.venue == Venue.JUPITER
        assert decision.expected_out == pytest.approx(1.5)

    async def test_respects_non_default_decimals(self) -> None:
        """Hardcoding 6 decimals would report this 1000x wrong."""
        router = self._router(decimals=9, out_amount=1_500_000_000)
        decision = await router.choose_buy_venue("M" * 44, 0.01)
        assert decision.expected_out == pytest.approx(1.5)

    async def test_decimals_are_cached(self) -> None:
        router = self._router(decimals=6, out_amount=1_000_000)
        assert await router.token_decimals("M" * 44) == 6
        assert router._decimals["M" * 44] == 6

    async def test_decimals_fallback_is_safe(self) -> None:
        """An unreadable mint must fall back to 6, not crash a trade."""
        from mctb.providers import ProviderRegistry

        class _BrokenRPC:
            async def get_account_data(self, *a, **k):
                raise RuntimeError("rpc down")

        router = ExecutionRouter(Config(), ProviderRegistry(Config()), _BrokenRPC())
        assert await router.token_decimals("M" * 44) == 6


class TestBondingCurveLiquidityFallback:
    """BUG: DEX aggregators report zero liquidity for tokens still on a
    bonding curve — correctly, since there is no DEX pair yet. But every
    liquidity-derived measure then silently produced nothing, including
    wash-trade suspicion, on exactly the token class this system targets.

    Observed live: a token with $31k of 24h volume showed liquidity $0 and
    wash suspicion 0%. With the curve's real reserves substituted it showed
    $1,074 liquidity, 32x volume/liquidity and 60% suspicion.
    """

    class _CurveRPC:
        def __init__(self, sol_in_curve: float) -> None:
            self.sol_in_curve = sol_in_curve

        async def get_bonding_curve(self, mint, commitment="processed"):
            class _Curve:
                complete = False

            curve = _Curve()
            curve.sol_in_curve = self.sol_in_curve  # type: ignore[attr-defined]
            return curve

    class _MarketProvider:
        """DEX provider reporting real volume but zero liquidity."""

        name = "dexscreener"
        display_name = "DEX Screener"
        tier = None
        available = True

        from mctb.providers import Capability as _C

        capabilities = frozenset({_C.TOKEN_MARKET})

        def __init__(self) -> None:
            from mctb.providers.base import ProviderHealth
            from mctb.providers.base import Tier as _T

            self.health = ProviderHealth()
            self.tier = _T.WARM

        def supports(self, capability) -> bool:
            return capability in self.capabilities

        async def get_market(self, mint):
            from mctb.providers.base import ProviderResult

            return ProviderResult(
                provider="dexscreener", ok=True,
                data={"liquidity_usd": 0.0, "volume_24h": 31_000.0,
                      "price_usd": 0.0000077, "symbol": "CURVE"},
            )

    def _registry(self):
        from mctb.providers import ProviderRegistry

        registry = ProviderRegistry(Config())
        registry.providers = {"dexscreener": self._MarketProvider()}  # type: ignore[dict-item]
        registry._reindex()
        return registry

    async def test_zero_liquidity_without_the_fallback(self) -> None:
        from mctb.intel.fusion import fuse_market

        intel = await fuse_market(self._registry(), "M" * 44, rpc=None)
        assert intel.liquidity_usd == 0.0
        assert intel.wash_trade_suspicion == 0.0, "detection is silently disabled"

    async def test_curve_reserves_restore_liquidity_and_detection(self) -> None:
        import mctb.intel.fusion as fusion

        # Pin the SOL price so the test does not depend on the live market.
        fusion._SOL_PRICE = (time.time(), 100.0)

        intel = await fusion.fuse_market(
            self._registry(), "M" * 44, rpc=self._CurveRPC(sol_in_curve=10.0)
        )
        assert intel.liquidity_usd == pytest.approx(1000.0)
        assert intel.volume_to_liquidity == pytest.approx(31.0)
        # Volume/liquidity alone contributes 0.4. Reaching the blocking
        # threshold needs corroborating signals (few unique traders, or a
        # suspiciously even buy/sell split) — each signal is deliberately weak
        # on its own. The live token that exposed this bug scored 0.6 because
        # it had those too.
        assert intel.wash_trade_suspicion == pytest.approx(0.4)
        assert "bonding_curve" in intel.provenance["sources"]

    async def test_corroborating_signals_reach_the_blocking_threshold(self) -> None:
        import mctb.intel.fusion as fusion
        from mctb.providers import ProviderRegistry
        from mctb.providers.base import ProviderResult

        fusion._SOL_PRICE = (time.time(), 100.0)

        class _Washy(self._MarketProvider):
            async def get_market(self, mint):
                return ProviderResult(
                    provider="dexscreener", ok=True,
                    data={
                        "liquidity_usd": 0.0, "volume_24h": 200_000.0,
                        "volume_1h": 40_000.0, "unique_wallets_1h": 3,
                        "buys_5m": 60, "sells_5m": 59,
                    },
                )

        registry = ProviderRegistry(Config())
        registry.providers = {"dexscreener": _Washy()}  # type: ignore[dict-item]
        registry._reindex()

        intel = await fusion.fuse_market(
            registry, "M" * 44, rpc=self._CurveRPC(sol_in_curve=10.0)
        )
        assert intel.wash_trade_suspicion > Config().intel.max_wash_suspicion

    async def test_dex_liquidity_is_preferred_when_present(self) -> None:
        """The fallback must not override a real DEX figure."""
        import mctb.intel.fusion as fusion

        class _WithLiquidity(self._MarketProvider):
            async def get_market(self, mint):
                from mctb.providers.base import ProviderResult

                return ProviderResult(
                    provider="dexscreener", ok=True,
                    data={"liquidity_usd": 50_000.0, "volume_24h": 31_000.0},
                )

        from mctb.providers import ProviderRegistry

        registry = ProviderRegistry(Config())
        registry.providers = {"dexscreener": _WithLiquidity()}  # type: ignore[dict-item]
        registry._reindex()

        intel = await fusion.fuse_market(
            registry, "M" * 44, rpc=self._CurveRPC(sol_in_curve=10.0)
        )
        assert intel.liquidity_usd == pytest.approx(50_000.0)
        assert "bonding_curve" not in intel.provenance["sources"]


class TestGraphSeesCandidateBuyers:
    """BUG: the entity graph only observed buys on tokens already held, so it
    had no data at the moment a decision was being made — which is the only
    moment sybil detection matters.
    """

    def test_sybil_detected_on_a_token_we_do_not_hold(self) -> None:
        graph = EntityGraph()
        now = time.time()

        # Four wallets, one funder, all buying a candidate we have no position in.
        wallets = [f"sybil{i}" for i in range(4)]
        for i, wallet in enumerate(wallets):
            graph.observe_wallet(wallet, first_funder="attacker", funded_at=now - 86400 * 10 + i * 30)
            graph.observe_buy(wallet, "CANDIDATE_MINT", now + i)

        unique, warnings = graph.deduplicate(wallets)
        assert len(unique) == 1, "four sybil wallets must collapse to one actor"
        assert warnings

    def test_graph_ingests_buys_regardless_of_position(self) -> None:
        """Mirrors the ordering fix in Brain._market_event_loop: observe first,
        filter by position afterwards."""
        graph = EntityGraph()
        now = time.time()
        for i, mint in enumerate(["HELD", "CANDIDATE_A", "CANDIDATE_B"]):
            graph.observe_buy(f"w{i}", mint, now + i, 0.1)

        assert len(graph.nodes) == 3
        assert len(graph.token_buyers) == 3


class TestScoutSubscribesNewTokens:
    """BUG (most serious found): a deadlock that made the pipeline dead on
    arrival.

    A brand-new token has no buyers, no buy/sell ratio and no velocity, so its
    triage score was near zero. The on-chain safety screen only ran above a
    threshold that score could never reach — and passing that screen was what
    used to trigger subscribing to the token's trade stream. So the token could
    never accumulate the data needed to qualify.

    Observed live: 13 tokens watchlisted, 0 candidates emitted, both services
    reporting healthy.
    """

    def _observation(self, mint: str, *, age_s: float = 0.0, buyers: int = 0,
                     smart: bool = False):
        from mctb.scout.observer import TokenObservation

        obs = TokenObservation(mint=mint)
        obs.created_ts = time.time() - age_s
        for i in range(buyers):
            obs.buyers.add(f"b{i}")
        if smart:
            obs.smart_wallets.add("smartwallet")
        return obs

    def test_brand_new_token_scores_below_triage_threshold(self) -> None:
        """Documents the condition that caused the deadlock — a fresh token
        genuinely cannot score its way in, so subscription must not depend on
        the score."""
        from mctb.config import Config as C
        from mctb.scout.main import Scout

        config = C()
        scout = Scout.__new__(Scout)          # no I/O
        scout.config = config

        fresh = self._observation("M" * 44)
        assert Scout._prelim_score(scout, fresh) < config.scout.min_prelim_score_for_safety_check

    async def test_new_token_is_subscribed_immediately(self) -> None:
        from mctb.config import Config as C
        from mctb.scout.main import Scout
        from mctb.scout.observer import Watchlist

        class _Feed:
            def __init__(self) -> None:
                self.subscribed: list[str] = []
                self.unsubscribed: list[str] = []

            async def subscribe_tokens(self, mints):
                self.subscribed.extend(mints)

            async def unsubscribe_tokens(self, mints):
                self.unsubscribed.extend(mints)

        scout = Scout.__new__(Scout)
        scout.config = C()
        scout.watchlist = Watchlist()
        scout.feed = _Feed()

        obs = self._observation("M" * 44)
        scout.watchlist.add(obs)
        await Scout._maybe_subscribe_token(scout, obs)

        assert obs.subscribed
        assert scout.feed.subscribed == ["M" * 44]

    async def test_budget_evicts_the_stalest_token(self) -> None:
        from mctb.config import Config as C
        from mctb.scout.main import Scout
        from mctb.scout.observer import Watchlist

        class _Feed:
            def __init__(self) -> None:
                self.subscribed: list[str] = []
                self.unsubscribed: list[str] = []

            async def subscribe_tokens(self, mints):
                self.subscribed.extend(mints)

            async def unsubscribe_tokens(self, mints):
                self.unsubscribed.extend(mints)

        config = C()
        config.scout.max_subscribed_tokens = 2
        scout = Scout.__new__(Scout)
        scout.config = config
        scout.watchlist = Watchlist()
        scout.feed = _Feed()

        stale = self._observation("A" * 44, age_s=600, buyers=0)
        busy = self._observation("B" * 44, age_s=600, buyers=40)
        for obs in (stale, busy):
            scout.watchlist.add(obs)
            await Scout._maybe_subscribe_token(scout, obs)

        fresh = self._observation("C" * 44, age_s=1)
        scout.watchlist.add(fresh)
        await Scout._maybe_subscribe_token(scout, fresh)

        assert fresh.subscribed, "a new launch must get a slot"
        assert not stale.subscribed, "the quiet old token should be evicted"
        assert busy.subscribed, "the busy token must be kept"

    async def test_smart_wallet_tokens_are_never_evicted(self) -> None:
        from mctb.config import Config as C
        from mctb.scout.main import Scout
        from mctb.scout.observer import Watchlist

        class _Feed:
            async def subscribe_tokens(self, mints): ...
            async def unsubscribe_tokens(self, mints): ...

        config = C()
        config.scout.max_subscribed_tokens = 1
        scout = Scout.__new__(Scout)
        scout.config = config
        scout.watchlist = Watchlist()
        scout.feed = _Feed()

        tracked = self._observation("A" * 44, age_s=900, smart=True)
        scout.watchlist.add(tracked)
        await Scout._maybe_subscribe_token(scout, tracked)

        fresh = self._observation("C" * 44, age_s=1)
        scout.watchlist.add(fresh)
        await Scout._maybe_subscribe_token(scout, fresh)

        assert tracked.subscribed, "never drop a token a tracked wallet is in"


class TestFreeTierTradeData:
    """BUG (root cause of the zero-candidate failure): PumpPortal's per-token
    trade stream is metered. Without an API key the server replies
    "'subscribeTokenTrade' ... only available when connecting with an API key"
    and silently delivers nothing.

    That left the Scout with new-token events and no trades — so no buyers, no
    buy/sell ratio, no velocity, and therefore no candidate could ever score
    highly enough to be evaluated. The system reported healthy and did nothing.

    Fix: decode trades from the pump.fun program logs we already subscribe to
    for free. Verified live: 1042 trades parsed from 1159 logs in 35 seconds.
    """

    @staticmethod
    def _payload(*, sol_lamports: int = 500_000_000, tokens: int = 1_000_000_000,
                 is_buy: bool = True, virtual_sol: int = 30_000_000_000,
                 virtual_tokens: int = 1_073_000_000_000_000) -> bytes:
        import struct

        from mctb.scout.rpc_watcher import TRADE_EVENT_DISCRIMINATOR

        body = bytearray()
        body += b"\x01" * 32                                   # mint
        body += struct.pack("<QQ", sol_lamports, tokens)
        body += bytes([1 if is_buy else 0])
        body += b"\x02" * 32                                   # user
        body += struct.pack("<q", 1_700_000_000)               # timestamp
        body += struct.pack("<QQ", virtual_sol, virtual_tokens)
        return TRADE_EVENT_DISCRIMINATOR + bytes(body)

    def test_decodes_a_buy(self) -> None:
        from mctb.scout.rpc_watcher import parse_trade_event

        event = parse_trade_event(self._payload())
        assert event is not None
        assert event.is_buy
        assert event.sol_amount == pytest.approx(0.5)
        assert event.token_amount == pytest.approx(1000.0)
        assert event.sol_in_curve == pytest.approx(30.0)
        assert event.trader and event.mint

    def test_decodes_a_sell(self) -> None:
        from mctb.scout.rpc_watcher import parse_trade_event

        event = parse_trade_event(self._payload(is_buy=False))
        assert event is not None and not event.is_buy

    def test_rejects_wrong_discriminator(self) -> None:
        from mctb.scout.rpc_watcher import parse_trade_event

        assert parse_trade_event(b"\x00" * 8 + b"\x01" * 105) is None

    def test_rejects_short_payload(self) -> None:
        from mctb.scout.rpc_watcher import TRADE_EVENT_DISCRIMINATOR, parse_trade_event

        assert parse_trade_event(TRADE_EVENT_DISCRIMINATOR + b"\x00" * 20) is None

    def test_rejects_absurd_amounts(self) -> None:
        """A mis-parse must be dropped, not acted on.

        10**18 lamports is a billion SOL — it fits in a u64, so the struct
        unpack succeeds and only the sanity check stands between it and the
        trading logic.
        """
        from mctb.scout.rpc_watcher import parse_trade_event

        assert parse_trade_event(self._payload(sol_lamports=10**18)) is None

    def test_rejects_zero_virtual_reserves(self) -> None:
        from mctb.scout.rpc_watcher import parse_trade_event

        assert parse_trade_event(self._payload(virtual_tokens=0)) is None

    def test_tolerates_newer_program_versions(self) -> None:
        """Later program versions append fields; the prefix must still parse."""
        from mctb.scout.rpc_watcher import parse_trade_event

        event = parse_trade_event(self._payload() + b"\xAB" * 64)
        assert event is not None and event.sol_amount == pytest.approx(0.5)


class TestStructuredIntelFindings:
    """BUG: the intel gate parsed insider percentages out of a prose warning
    string. It happened to work, and would have broken silently the first time
    the wording changed.
    """

    @pytest.fixture
    def dossier(self) -> TokenDossier:
        return TokenDossier(
            mint="M" * 44,
            security=SecurityIntel(
                mint="M" * 44, risk_score=10.0, sellable=True,
                provenance={"sources": ["goplus"], "confidence": "medium"},
            ),
            market=MarketIntel(mint="M" * 44, liquidity_usd=50_000),
        )

    def test_insider_ratio_blocks_from_structured_field(self, dossier) -> None:
        config = Config()
        dossier.insider_count = 3
        dossier.insider_pct = 0.60
        verdict = evaluate_intel(dossier, config)
        assert not verdict.passed
        assert any("insider" in b for b in verdict.blockers)

    def test_below_threshold_does_not_block(self, dossier) -> None:
        config = Config()
        dossier.insider_count = 1
        dossier.insider_pct = 0.10
        assert evaluate_intel(dossier, config).passed

    def test_reworded_prose_cannot_change_the_decision(self, dossier) -> None:
        """The whole point of the fix: prose is decoration, numbers decide."""
        config = Config()
        dossier.insider_count = 3
        dossier.insider_pct = 0.60
        dossier.cluster_warnings = ["completely different wording with no numbers"]
        assert not evaluate_intel(dossier, config).passed

    def test_fresh_wallet_ratio_blocks_from_structured_field(self, dossier) -> None:
        config = Config()
        dossier.fresh_wallet_pct = 0.95
        verdict = evaluate_intel(dossier, config)
        assert not verdict.passed
        assert any("last 24h" in b for b in verdict.blockers)

    def test_warnings_still_surface_as_notes(self, dossier) -> None:
        config = Config()
        dossier.cluster_warnings = ["3 of these wallets are one entity (C1234): shared funder"]
        verdict = evaluate_intel(dossier, config)
        assert any("one entity" in n for n in verdict.notes)
