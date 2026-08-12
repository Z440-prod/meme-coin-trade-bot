"""Fusion, astroturf detection, the intel gate, and webhook security."""

from __future__ import annotations

import time

import pytest

from mctb.automation import WebhookConfig, WebhookReceiver, sign_payload
from mctb.brain.intel_gate import evaluate_intel
from mctb.config import Config
from mctb.intel.graph import EntityGraph
from mctb.intel.narrative import Mention, astroturf_score, text_similarity
from mctb.intel.types import (
    Confidence,
    MarketIntel,
    NarrativeIntel,
    Provenance,
    SecurityIntel,
    TokenDossier,
)


class TestProvenance:
    def test_confidence_scales_with_agreement(self) -> None:
        assert Provenance().confidence() == Confidence.NONE
        assert Provenance(sources=["a"]).confidence() == Confidence.MEDIUM
        assert Provenance(sources=["a", "b"]).confidence() == Confidence.HIGH

    def test_disagreement_lowers_confidence(self) -> None:
        prov = Provenance(sources=["a", "b"], disagreed=["mint_authority"])
        assert prov.confidence() == Confidence.LOW
        assert prov.contested


class TestTextSimilarity:
    def test_identical_text(self) -> None:
        assert text_similarity("buy this coin now", "buy this coin now") == 1.0

    def test_unrelated_text(self) -> None:
        assert text_similarity("the weather is nice", "buy this coin now") < 0.2

    def test_near_duplicate_detected(self) -> None:
        a = "this coin is going straight to the moon today"
        b = "this coin is going straight to the moon tomorrow"
        assert text_similarity(a, b) > 0.4

    def test_empty_is_safe(self) -> None:
        assert text_similarity("", "anything") == 0.0


def _mentions(count: int, **kwargs) -> list[Mention]:
    now = time.time()
    return [
        Mention(
            ts=now - i * kwargs.get("gap", 30),
            author=kwargs.get("author", f"user{i}"),
            followers=kwargs.get("followers", 5000 + i * 900),
            text=kwargs.get("text", f"organic thought number {i} about markets"),
            author_age_days=kwargs.get("age_days", 800),
            author_attention=kwargs.get("attention", 5.0),
        )
        for i in range(count)
    ]


class TestAstroturfDetection:
    def test_too_few_mentions_is_neutral(self) -> None:
        score, reasons = astroturf_score(_mentions(2))
        assert score == 0.0 and reasons == []

    def test_organic_chatter_scores_low(self) -> None:
        import random

        now = time.time()
        organic = [
            Mention(
                ts=now - random.uniform(0, 1800),
                author=f"person{i}",
                followers=random.choice([800, 12_000, 340, 95_000, 2_100, 640_000]),
                text=f"some genuinely different opinion {i} about this whole situation",
                author_age_days=random.uniform(200, 3000),
                author_attention=random.uniform(2, 40),
            )
            for i in range(10)
        ]
        score, _reasons = astroturf_score(organic)
        assert score < 0.5

    def test_copy_paste_campaign_detected(self) -> None:
        identical = _mentions(
            10, text="LFG this is the next 100x get in now", gap=8
        )
        score, reasons = astroturf_score(identical)
        assert score > 0.5
        assert any("identical" in r or "minutes" in r for r in reasons)

    def test_burst_detected(self) -> None:
        now = time.time()
        burst = [
            Mention(ts=now - i, author=f"u{i}", followers=1000 + i,
                    text=f"varied text here {i} something", author_age_days=500)
            for i in range(12)
        ]
        score, reasons = astroturf_score(burst)
        assert score > 0.0
        assert any("minutes" in r or "intervals" in r for r in reasons)

    def test_fresh_accounts_detected(self) -> None:
        fresh = _mentions(8, age_days=5, gap=120)
        score, reasons = astroturf_score(fresh)
        assert any("under a month old" in r for r in reasons)
        assert score > 0.0

    def test_single_author_domination(self) -> None:
        now = time.time()
        spam = [
            Mention(ts=now - i * 60, author="oneguy", followers=5000,
                    text=f"different message {i} entirely unique wording", author_age_days=900)
            for i in range(8)
        ]
        score, reasons = astroturf_score(spam)
        assert any("single account" in r for r in reasons)
        assert score > 0.0


class TestIntelGate:
    @pytest.fixture
    def config(self) -> Config:
        return Config()

    @pytest.fixture
    def clean_dossier(self) -> TokenDossier:
        return TokenDossier(
            mint="M" * 44,
            security=SecurityIntel(
                mint="M" * 44, risk_score=15.0, sellable=True,
                provenance={"sources": ["goplus", "rugcheck"], "confidence": "high"},
            ),
            market=MarketIntel(
                mint="M" * 44, liquidity_usd=30_000, volume_24h=90_000,
                volume_to_liquidity=3.0, unique_wallets_1h=60,
            ),
        )

    def test_clean_token_passes_with_bonus(self, clean_dossier, config: Config) -> None:
        verdict = evaluate_intel(clean_dossier, config)
        assert verdict.passed
        assert verdict.adjustment > 0

    def test_security_blocker_propagates(self, clean_dossier, config: Config) -> None:
        clean_dossier.security.blockers = ["freeze authority live"]
        verdict = evaluate_intel(clean_dossier, config)
        assert not verdict.passed

    def test_unsellable_blocks(self, clean_dossier, config: Config) -> None:
        clean_dossier.security.sellable = False
        verdict = evaluate_intel(clean_dossier, config)
        assert not verdict.passed
        assert any("sell route" in b for b in verdict.blockers)

    def test_wash_trading_blocks(self, clean_dossier, config: Config) -> None:
        clean_dossier.market.wash_trade_suspicion = 0.9
        clean_dossier.market.volume_to_liquidity = 80
        verdict = evaluate_intel(clean_dossier, config)
        assert not verdict.passed
        assert any("manufactured" in b for b in verdict.blockers)

    def test_expensive_round_trip_blocks(self, clean_dossier, config: Config) -> None:
        verdict = evaluate_intel(clean_dossier, config, round_trip_cost_pct=40.0)
        assert not verdict.passed
        assert any("round trip" in b for b in verdict.blockers)

    def test_sybil_consensus_blocks(self, clean_dossier, config: Config) -> None:
        """The headline behaviour, end to end through the gate."""
        graph = EntityGraph()
        now = time.time()
        wallets = [f"sybil{i}" for i in range(4)]
        for i, wallet in enumerate(wallets):
            graph.observe_wallet(wallet, first_funder="attacker", funded_at=now + i * 20)

        verdict = evaluate_intel(clean_dossier, config, graph=graph, smart_wallets=wallets)
        assert not verdict.passed
        assert verdict.effective_smart_wallets == 1
        assert verdict.sybil_ratio == pytest.approx(0.75)

    def test_independent_wallets_pass(self, clean_dossier, config: Config) -> None:
        graph = EntityGraph()
        now = time.time()
        wallets = [f"indep{i}" for i in range(3)]
        for i, wallet in enumerate(wallets):
            # Established wallets, distinct funders. Deliberately old: a
            # cohort of brand-new wallets is itself a blocker, tested below.
            graph.observe_wallet(wallet, first_funder=f"f{i}", funded_at=now - 86400 * 45)
        graph.rebuild()

        verdict = evaluate_intel(clean_dossier, config, graph=graph, smart_wallets=wallets)
        assert verdict.passed, verdict.blockers
        assert verdict.effective_smart_wallets == 3
        assert verdict.sybil_ratio == 0.0

    def test_fresh_wallet_cohort_blocks(self, clean_dossier, config: Config) -> None:
        """A cohort of wallets all created hours ago is manufactured demand,
        even when they are genuinely independent of each other."""
        graph = EntityGraph()
        now = time.time()
        wallets = [f"fresh{i}" for i in range(4)]
        for i, wallet in enumerate(wallets):
            graph.observe_wallet(wallet, first_funder=f"f{i}", funded_at=now - 3600)
        graph.rebuild()

        verdict = evaluate_intel(clean_dossier, config, graph=graph, smart_wallets=wallets)
        assert not verdict.passed
        assert any("last 24h" in b for b in verdict.blockers)

    def test_astroturf_penalised(self, clean_dossier, config: Config) -> None:
        clean_dossier.narrative = NarrativeIntel(
            mint="M" * 44, mention_count=20, astroturf_score=0.9,
            themes=["copy-paste campaign"],
        )
        astro = evaluate_intel(clean_dossier, config).adjustment

        clean_dossier.narrative = NarrativeIntel(
            mint="M" * 44, mention_count=20, astroturf_score=0.05, quality_accounts=3
        )
        organic = evaluate_intel(clean_dossier, config).adjustment
        assert organic > astro

    def test_adjustment_is_bounded(self, clean_dossier, config: Config) -> None:
        clean_dossier.security.risk_score = 0.0
        clean_dossier.security.lp_burned = True
        clean_dossier.market.unique_wallets_1h = 5000
        clean_dossier.narrative = NarrativeIntel(
            mint="M" * 44, mention_count=500, quality_accounts=100, astroturf_score=0.0
        )
        verdict = evaluate_intel(clean_dossier, config)
        assert verdict.adjustment <= config.intel.max_intel_bonus

    def test_no_providers_means_no_opinion(self, config: Config) -> None:
        """With nothing configured, intel must be silent rather than blocking."""
        empty = TokenDossier(mint="M" * 44)
        verdict = evaluate_intel(empty, config)
        assert verdict.passed
        assert verdict.adjustment == 0.0

    def test_disabled_intel_is_inert(self, clean_dossier, config: Config) -> None:
        config.intel.enabled = False
        clean_dossier.security.blockers = ["freeze authority live"]
        verdict = evaluate_intel(clean_dossier, config)
        assert verdict.passed
        assert verdict.adjustment == 0.0

    def test_paid_promotion_is_penalised(self, clean_dossier, config: Config) -> None:
        baseline = evaluate_intel(clean_dossier, config).adjustment
        clean_dossier.market.boosted = 5
        assert evaluate_intel(clean_dossier, config).adjustment < baseline


class TestWebhookSecurity:
    @pytest.fixture
    def receiver(self) -> WebhookReceiver:
        return WebhookReceiver(WebhookConfig(secret="s" * 40, allow_trading=False))

    def test_valid_signature_accepted(self, receiver: WebhookReceiver) -> None:
        body = b'{"action":"signal"}'
        headers = sign_payload("s" * 40, body)
        ok, why = receiver.verify_signature(
            body, headers["X-MCTB-Signature"], headers["X-MCTB-Timestamp"]
        )
        assert ok, why

    def test_tampered_body_rejected(self, receiver: WebhookReceiver) -> None:
        headers = sign_payload("s" * 40, b'{"action":"signal"}')
        ok, why = receiver.verify_signature(
            b'{"action":"buy"}', headers["X-MCTB-Signature"], headers["X-MCTB-Timestamp"]
        )
        assert not ok and "mismatch" in why

    def test_wrong_secret_rejected(self, receiver: WebhookReceiver) -> None:
        body = b'{"action":"signal"}'
        headers = sign_payload("wrong-secret", body)
        ok, _why = receiver.verify_signature(
            body, headers["X-MCTB-Signature"], headers["X-MCTB-Timestamp"]
        )
        assert not ok

    def test_stale_signature_rejected(self, receiver: WebhookReceiver) -> None:
        body = b'{"action":"signal"}'
        headers = sign_payload("s" * 40, body, timestamp=time.time() - 10_000)
        ok, why = receiver.verify_signature(
            body, headers["X-MCTB-Signature"], headers["X-MCTB-Timestamp"]
        )
        assert not ok and "old" in why

    def test_replay_rejected(self, receiver: WebhookReceiver) -> None:
        body = b'{"action":"signal"}'
        headers = sign_payload("s" * 40, body)
        first, _ = receiver.verify_signature(
            body, headers["X-MCTB-Signature"], headers["X-MCTB-Timestamp"]
        )
        second, why = receiver.verify_signature(
            body, headers["X-MCTB-Signature"], headers["X-MCTB-Timestamp"]
        )
        assert first and not second and "replay" in why

    def test_trading_blocked_by_default(self, receiver: WebhookReceiver) -> None:
        signal = receiver.parse(b'{"action":"buy","mint":"M"}')
        assert signal is not None
        allowed, why = receiver.authorise(signal)
        assert not allowed and "allow_trading" in why

    def test_kill_allowed_by_default(self, receiver: WebhookReceiver) -> None:
        signal = receiver.parse(b'{"action":"kill"}')
        assert signal is not None and receiver.authorise(signal)[0]

    def test_parses_tradingview_plain_text(self, receiver: WebhookReceiver) -> None:
        signal = receiver.parse(b"buy 8LXPAS3MR5oSeEC1LqGZyHVpgs1Qw6m9My6iDKTPpump 0.05")
        assert signal is not None
        assert signal.action == "buy"
        assert signal.mint.endswith("pump")
        assert signal.size_sol == 0.05

    def test_maps_tradingview_strategy_words(self, receiver: WebhookReceiver) -> None:
        assert receiver.parse(b'{"action":"long"}').action == "buy"
        assert receiver.parse(b'{"action":"exit"}').action == "sell"

    def test_unknown_action_becomes_signal(self, receiver: WebhookReceiver) -> None:
        assert receiver.parse(b'{"action":"nonsense"}').action == "signal"

    def test_config_refuses_trading_without_secret(self) -> None:
        """An endpoint that can spend money with no authentication must not
        be constructible."""
        with pytest.raises(ValueError, match="open door"):
            Config.model_validate(
                {"webhooks": {"enabled": True, "allow_trading": True, "secret": ""}}
            )
