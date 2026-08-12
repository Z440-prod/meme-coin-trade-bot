"""Provider framework: rate limiting, circuit breaking, caching, coalescing.

These are the mechanisms that keep twenty external services from making the
bot slow or unreliable, so they are tested against their failure modes rather
than just their happy paths.
"""

from __future__ import annotations

import asyncio
import time

import pytest

from mctb.config import Config
from mctb.providers import Capability, ProviderRegistry, ProviderStatus, Tier
from mctb.providers.base import Provider, ProviderResult, gather_providers
from mctb.providers.http import CircuitBreaker, ResponseCache, TokenBucket


class TestTokenBucket:
    async def test_allows_burst_then_throttles(self) -> None:
        bucket = TokenBucket(rate_per_s=10, burst=3)
        for _ in range(3):
            assert await bucket.acquire(timeout_s=0.1)
        # Fourth needs a refill, which at 10/s takes ~100ms.
        assert not await bucket.acquire(timeout_s=0.01)

    async def test_refills_over_time(self) -> None:
        bucket = TokenBucket(rate_per_s=50, burst=1)
        assert await bucket.acquire(timeout_s=0.1)
        await asyncio.sleep(0.05)
        assert await bucket.acquire(timeout_s=0.2)

    async def test_waits_rather_than_failing_when_it_can(self) -> None:
        bucket = TokenBucket(rate_per_s=20, burst=1)
        await bucket.acquire()
        started = time.monotonic()
        assert await bucket.acquire(timeout_s=1.0)
        assert time.monotonic() - started >= 0.03


class TestCircuitBreaker:
    def test_opens_after_threshold(self) -> None:
        breaker = CircuitBreaker(failure_threshold=3, recovery_s=60)
        for _ in range(2):
            breaker.record_failure()
        assert not breaker.is_open
        breaker.record_failure()
        assert breaker.is_open

    def test_success_resets(self) -> None:
        breaker = CircuitBreaker(failure_threshold=2, recovery_s=60)
        breaker.record_failure()
        breaker.record_success()
        breaker.record_failure()
        assert not breaker.is_open

    def test_half_opens_after_recovery(self) -> None:
        breaker = CircuitBreaker(failure_threshold=2, recovery_s=0.05)
        breaker.record_failure()
        breaker.record_failure()
        assert breaker.is_open
        time.sleep(0.06)
        # One probe is allowed through.
        assert not breaker.is_open

    def test_failed_probe_reopens(self) -> None:
        breaker = CircuitBreaker(failure_threshold=2, recovery_s=0.05)
        breaker.record_failure()
        breaker.record_failure()
        time.sleep(0.06)
        assert not breaker.is_open   # probe allowed
        breaker.record_failure()     # probe failed
        assert breaker.is_open       # closed again immediately


class TestResponseCache:
    def test_hit_and_expiry(self) -> None:
        cache = ResponseCache()
        cache.put("k", {"v": 1}, ttl_s=10)
        hit = cache.get("k")
        assert hit is not None and hit[0] == {"v": 1} and hit[1] is False

        cache.put("k2", {"v": 2}, ttl_s=0.01)
        time.sleep(0.02)
        assert cache.get("k2") is None

    def test_stale_while_revalidate(self) -> None:
        """The mechanism that keeps the hot path off the network."""
        cache = ResponseCache()
        cache.put("k", {"v": 1}, ttl_s=0.01)
        time.sleep(0.02)
        assert cache.get("k") is None                      # too stale by default
        cache.put("k", {"v": 1}, ttl_s=0.01)
        time.sleep(0.02)
        hit = cache.get("k", max_stale_s=5.0)
        assert hit is not None and hit[1] is True          # served, marked stale

    def test_negative_caching(self) -> None:
        """A known-bad lookup must not be retried on every candidate."""
        cache = ResponseCache()
        cache.put("bad", None, ttl_s=10, negative=True)
        hit = cache.get("bad")
        assert hit is not None and hit[0] is None

    def test_eviction_bounds_memory(self) -> None:
        cache = ResponseCache(max_entries=50)
        for i in range(200):
            cache.put(f"k{i}", i, ttl_s=300)
        assert len(cache._entries) <= 50

    def test_stats(self) -> None:
        cache = ResponseCache()
        cache.put("a", 1, ttl_s=10)
        cache.get("a")
        cache.get("missing")
        stats = cache.stats()
        assert stats["hits"] == 1 and stats["misses"] == 1


class _StubProvider(Provider):
    name = "stub"
    display_name = "Stub"
    capabilities = frozenset({Capability.TOKEN_MARKET})
    tier = Tier.WARM

    def __init__(self, config, api_key=None, *, delay=0.0, fail=False) -> None:
        super().__init__(config, api_key)
        self.delay = delay
        self.fail = fail
        self.calls = 0

    async def get_market(self, mint: str) -> ProviderResult[dict]:
        self.calls += 1
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.fail:
            raise RuntimeError("stub failure")
        return ProviderResult(provider=self.name, ok=True, data={"price_usd": 1.0})


class TestGather:
    async def test_slow_provider_abandoned_at_deadline(self) -> None:
        """One slow provider must not add its latency to every decision."""
        fast = _StubProvider(None, delay=0.01)
        slow = _StubProvider(None, delay=5.0)
        started = time.perf_counter()
        results = await gather_providers(
            [fast.get_market("m"), slow.get_market("m")], timeout_s=0.2
        )
        elapsed = time.perf_counter() - started
        assert elapsed < 1.0
        assert len(results) == 1

    async def test_empty_input(self) -> None:
        assert await gather_providers([], timeout_s=1.0) == []


class TestRegistry:
    @pytest.fixture
    def registry(self) -> ProviderRegistry:
        registry = ProviderRegistry(Config())
        registry.build()
        return registry

    def test_free_providers_ready_without_keys(self, registry: ProviderRegistry) -> None:
        """The whole system must work with zero API keys."""
        ready = {p.name for p in registry.providers.values() if p.available}
        for expected in ("dexscreener", "goplus", "rugcheck", "jupiter"):
            assert expected in ready, f"{expected} should work without a key"

    def test_key_required_providers_are_unconfigured(self, registry: ProviderRegistry) -> None:
        birdeye = registry.get("birdeye")
        assert birdeye is not None
        assert birdeye.status == ProviderStatus.UNCONFIGURED
        assert not birdeye.available

    def test_link_only_providers_never_queried(self, registry: ProviderRegistry) -> None:
        for name in ("photon", "bullx", "bubblemaps"):
            provider = registry.get(name)
            assert provider is not None
            assert provider.status == ProviderStatus.NO_PUBLIC_API
            assert not provider.available
            # They still provide their real value: a working deep link.
            assert provider.token_url("M" * 44).startswith("http")

    def test_gmgn_disabled_by_default(self, registry: ProviderRegistry) -> None:
        gmgn = registry.get("gmgn")
        assert gmgn is not None and not gmgn.available

    def test_critical_capabilities_covered_with_no_keys(
        self, registry: ProviderRegistry
    ) -> None:
        coverage = registry.coverage_report()
        assert coverage["critical_gaps"] == []

    def test_routing_respects_tier(self, registry: ProviderRegistry) -> None:
        """An urgent request must never be routed to a MANUAL provider."""
        warm = registry.for_capability(Capability.TOKEN_MARKET, Tier.WARM)
        assert all(p.tier.value <= Tier.WARM.value for p in warm)

    def test_routing_prefers_healthy_providers(self, registry: ProviderRegistry) -> None:
        providers = registry.for_capability(Capability.TOKEN_SECURITY, Tier.WARM)
        assert providers
        providers[0].health.record(False, 100.0, "boom")
        providers[0].health.record(False, 100.0, "boom")
        reordered = registry.for_capability(Capability.TOKEN_SECURITY, Tier.WARM)
        if len(reordered) > 1:
            assert reordered[0].name != providers[0].name

    def test_disabled_providers_excluded(self) -> None:
        config = Config()
        config.providers.disabled = ["dexscreener"]
        registry = ProviderRegistry(config)
        registry.build()
        assert registry.get("dexscreener") is None

    def test_enabled_allowlist(self) -> None:
        config = Config()
        config.providers.enabled = ["jupiter", "goplus"]
        registry = ProviderRegistry(config)
        registry.build()
        assert set(registry.providers) == {"jupiter", "goplus"}

    async def test_provider_exception_never_escapes(self, registry: ProviderRegistry) -> None:
        """A misbehaving provider must never kill a trading loop."""
        broken = _StubProvider(Config(), fail=True)
        registry.providers["stub"] = broken
        registry._reindex()
        results = await registry.query_all(
            Capability.TOKEN_MARKET, "get_market", "mint", timeout_s=1.0
        )
        assert all(isinstance(r, ProviderResult) for r in results)
        assert any(not r.ok for r in results)

    def test_capability_matrix(self, registry: ProviderRegistry) -> None:
        matrix = registry.capability_matrix()
        assert "token_security" in matrix
        assert "goplus" in matrix["token_security"]
