"""Shared HTTP machinery for every provider.

This module is where most of the system's speed lives. Five mechanisms, each
addressing a specific way that talking to twenty external services makes a
trading bot slow or unreliable:

**Connection reuse (HTTP/2 + keepalive).** A cold TLS handshake to a provider
costs 100-300ms from a home connection. Reusing a warm pooled connection costs
the round trip alone. Over a trading session this is the single largest
latency saving available, and it is free.

**Token-bucket rate limiting.** Every provider bans you if you exceed its
limits, and a ban costs far more than the requests you saved. We shape traffic
locally so we never find out where the ceiling is.

**Circuit breaking.** When a provider starts failing, continuing to call it
burns time on a guaranteed timeout. After N consecutive failures the circuit
opens and calls fail instantly, with a single probe periodically testing
recovery. A dead provider costs microseconds instead of seconds.

**Single-flight coalescing.** When the Scout screens a token and the Brain
scores it in the same instant, both ask for the same security report. Without
coalescing that is two identical requests against your rate limit. With it,
the second caller awaits the first one's result.

**Stale-while-revalidate.** The hot path returns cached data immediately —
even slightly stale data — and kicks off a background refresh. Decisions never
wait on a network call. This is what makes it possible to consult twenty
providers without any of them being on the critical path.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Any

import httpx
import orjson

log = logging.getLogger(__name__)


# --------------------------------------------------------------------------
#  Rate limiting
# --------------------------------------------------------------------------


class TokenBucket:
    """Classic token bucket. Smooths bursts without serialising everything."""

    def __init__(self, rate_per_s: float, burst: int) -> None:
        self.rate = max(0.01, rate_per_s)
        self.burst = max(1, burst)
        self._tokens = float(self.burst)
        self._last = time.monotonic()
        self._lock = asyncio.Lock()

    async def acquire(self, timeout_s: float = 5.0) -> bool:
        """Take one token. False if none became available within the timeout."""
        deadline = time.monotonic() + timeout_s
        while True:
            async with self._lock:
                now = time.monotonic()
                self._tokens = min(
                    float(self.burst), self._tokens + (now - self._last) * self.rate
                )
                self._last = now
                if self._tokens >= 1.0:
                    self._tokens -= 1.0
                    return True
                shortfall = (1.0 - self._tokens) / self.rate

            if time.monotonic() + shortfall > deadline:
                return False
            await asyncio.sleep(min(shortfall, 0.25))

    @property
    def available(self) -> float:
        now = time.monotonic()
        return min(float(self.burst), self._tokens + (now - self._last) * self.rate)


# --------------------------------------------------------------------------
#  Circuit breaker
# --------------------------------------------------------------------------


class CircuitBreaker:
    """Fails fast while a provider is down, probes periodically for recovery."""

    def __init__(self, failure_threshold: int = 5, recovery_s: float = 60.0) -> None:
        self.failure_threshold = failure_threshold
        self.recovery_s = recovery_s
        self.failures = 0
        self.opened_at = 0.0
        self._half_open = False

    @property
    def is_open(self) -> bool:
        if self.failures < self.failure_threshold:
            return False
        if time.time() - self.opened_at >= self.recovery_s:
            # Let exactly one request through to test the water.
            self._half_open = True
            return False
        return True

    def record_success(self) -> None:
        if self.failures:
            log.info("circuit closed after recovery")
        self.failures = 0
        self._half_open = False

    def record_failure(self) -> None:
        self.failures += 1
        if self._half_open:
            # The probe failed; restart the recovery clock rather than letting
            # a flood through.
            self.opened_at = time.time()
            self._half_open = False
        elif self.failures == self.failure_threshold:
            self.opened_at = time.time()
            log.warning("circuit opened after %d consecutive failures", self.failures)


# --------------------------------------------------------------------------
#  Cache
# --------------------------------------------------------------------------


@dataclass
class CacheEntry:
    value: Any
    fetched_at: float
    ttl_s: float
    # Negative entries remember failures so we do not retry a known-bad lookup
    # on every candidate.
    negative: bool = False

    @property
    def age_s(self) -> float:
        return time.time() - self.fetched_at

    @property
    def fresh(self) -> bool:
        return self.age_s < self.ttl_s

    def stale_but_usable(self, max_stale_s: float) -> bool:
        return self.age_s < (self.ttl_s + max_stale_s)


class ResponseCache:
    """TTL cache with negative caching and stale-while-revalidate support."""

    def __init__(self, max_entries: int = 20_000) -> None:
        self._entries: dict[str, CacheEntry] = {}
        self.max_entries = max_entries
        self.hits = 0
        self.misses = 0
        self.stale_hits = 0

    def get(self, key: str, max_stale_s: float = 0.0) -> tuple[Any, bool] | None:
        """Returns (value, is_stale) or None. Negative entries return (None, False)."""
        entry = self._entries.get(key)
        if entry is None:
            self.misses += 1
            return None
        if entry.fresh:
            self.hits += 1
            return (None if entry.negative else entry.value), False
        if max_stale_s > 0 and entry.stale_but_usable(max_stale_s) and not entry.negative:
            self.stale_hits += 1
            return entry.value, True
        del self._entries[key]
        self.misses += 1
        return None

    def put(self, key: str, value: Any, ttl_s: float, negative: bool = False) -> None:
        if len(self._entries) >= self.max_entries:
            self._evict()
        self._entries[key] = CacheEntry(value, time.time(), ttl_s, negative)

    def _evict(self) -> None:
        # Drop expired first; if that frees nothing, drop the oldest quarter.
        now = time.time()
        expired = [k for k, e in self._entries.items() if (now - e.fetched_at) > e.ttl_s * 3]
        for key in expired:
            del self._entries[key]
        if len(self._entries) >= self.max_entries:
            ordered = sorted(self._entries.items(), key=lambda kv: kv[1].fetched_at)
            for key, _ in ordered[: max(1, len(ordered) // 4)]:
                del self._entries[key]

    def invalidate(self, prefix: str = "") -> int:
        keys = [k for k in self._entries if not prefix or k.startswith(prefix)]
        for key in keys:
            del self._entries[key]
        return len(keys)

    def stats(self) -> dict[str, Any]:
        total = self.hits + self.misses
        return {
            "entries": len(self._entries),
            "hits": self.hits,
            "misses": self.misses,
            "stale_hits": self.stale_hits,
            "hit_rate": round(self.hits / total, 3) if total else 0.0,
        }


# --------------------------------------------------------------------------
#  The client
# --------------------------------------------------------------------------


@dataclass
class HttpStats:
    requests: int = 0
    errors: int = 0
    rate_limited: int = 0
    coalesced: int = 0
    circuit_rejections: int = 0
    total_latency_ms: float = 0.0
    by_host: dict[str, int] = field(default_factory=dict)


class ProviderHttp:
    """One of these per provider. Owns its pool, limiter, breaker and cache."""

    def __init__(
        self,
        name: str,
        base_url: str = "",
        headers: dict[str, str] | None = None,
        rate_limit_rps: float = 5.0,
        rate_limit_burst: int = 10,
        timeout_s: float = 8.0,
        max_connections: int = 20,
        failure_threshold: int = 5,
        recovery_s: float = 60.0,
        http2: bool = True,
    ) -> None:
        self.name = name
        self.base_url = base_url.rstrip("/")
        self.default_headers = headers or {}
        self.timeout_s = timeout_s
        self.http2 = http2
        self.max_connections = max_connections

        self.bucket = TokenBucket(rate_limit_rps, rate_limit_burst)
        self.breaker = CircuitBreaker(failure_threshold, recovery_s)
        self.cache = ResponseCache()
        self.stats = HttpStats()

        self._client: httpx.AsyncClient | None = None
        self._inflight: dict[str, asyncio.Future] = {}
        self._refreshing: set[str] = set()

    async def start(self) -> None:
        if self._client is not None:
            return
        try:
            self._client = httpx.AsyncClient(
                base_url=self.base_url,
                headers={
                    "User-Agent": "mctb/2.0",
                    "Accept": "application/json",
                    **self.default_headers,
                },
                timeout=httpx.Timeout(self.timeout_s, connect=min(4.0, self.timeout_s)),
                limits=httpx.Limits(
                    max_connections=self.max_connections,
                    max_keepalive_connections=self.max_connections,
                    # Long keepalive: a warm pool is the whole point.
                    keepalive_expiry=120.0,
                ),
                http2=self.http2,
                follow_redirects=True,
            )
        except ImportError:
            # h2 not installed — fall back rather than failing to start.
            self.http2 = False
            self._client = httpx.AsyncClient(
                base_url=self.base_url,
                headers={"User-Agent": "mctb/2.0", **self.default_headers},
                timeout=httpx.Timeout(self.timeout_s),
                limits=httpx.Limits(max_connections=self.max_connections),
                follow_redirects=True,
            )

    async def close(self) -> None:
        if self._client is not None:
            try:
                await self._client.aclose()
            except Exception:  # noqa: BLE001
                pass
            self._client = None

    async def warm(self, path: str = "/") -> None:
        """Open a connection ahead of time so the first real call is not
        paying for a TLS handshake at the worst possible moment."""
        if self._client is None:
            await self.start()
        try:
            await self._client.head(path, timeout=3.0)  # type: ignore[union-attr]
        except Exception:  # noqa: BLE001
            pass

    # -- the main entry point ----------------------------------------------

    async def get_json(
        self,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
        cache_ttl_s: float = 30.0,
        max_stale_s: float = 0.0,
        cache_key: str | None = None,
        timeout_s: float | None = None,
        negative_ttl_s: float = 20.0,
    ) -> tuple[Any, dict[str, Any]]:
        """GET and parse JSON, with the full cache/coalesce/limit/break stack.

        Returns (data, meta) where meta carries `cached`, `stale`, `latency_ms`
        and `error`. Data is None on failure — callers must handle that, and
        every provider adapter does.
        """
        key = cache_key or self._key(path, params)
        meta: dict[str, Any] = {"cached": False, "stale": False, "latency_ms": 0.0, "error": None}

        # 1. Cache, including stale-while-revalidate.
        if cache_ttl_s > 0:
            hit = self.cache.get(key, max_stale_s)
            if hit is not None:
                value, is_stale = hit
                meta["cached"] = True
                meta["stale"] = is_stale
                if is_stale:
                    # Serve immediately, refresh behind the caller's back.
                    self._schedule_refresh(
                        key, path, params, headers, cache_ttl_s, timeout_s, negative_ttl_s
                    )
                if value is None:
                    meta["error"] = "negative cache"
                return value, meta

        # 2. Coalesce identical concurrent requests.
        existing = self._inflight.get(key)
        if existing is not None:
            self.stats.coalesced += 1
            try:
                value = await existing
                meta["cached"] = True
                return value, meta
            except Exception as exc:  # noqa: BLE001
                meta["error"] = str(exc)
                return None, meta

        future: asyncio.Future = asyncio.get_event_loop().create_future()
        self._inflight[key] = future

        try:
            value, fetch_meta = await self._fetch(
                path, params, headers, timeout_s, cache_ttl_s, negative_ttl_s, key
            )
            meta.update(fetch_meta)
            if not future.done():
                future.set_result(value)
            return value, meta
        except Exception as exc:  # noqa: BLE001
            if not future.done():
                future.set_exception(exc)
            meta["error"] = str(exc)
            return None, meta
        finally:
            self._inflight.pop(key, None)

    async def _fetch(
        self,
        path: str,
        params: dict[str, Any] | None,
        headers: dict[str, str] | None,
        timeout_s: float | None,
        cache_ttl_s: float,
        negative_ttl_s: float,
        key: str,
    ) -> tuple[Any, dict[str, Any]]:
        meta: dict[str, Any] = {"cached": False, "stale": False, "latency_ms": 0.0, "error": None}

        # 3. Circuit breaker — fail in microseconds while the provider is down.
        if self.breaker.is_open:
            self.stats.circuit_rejections += 1
            meta["error"] = "circuit open"
            return None, meta

        # 4. Rate limit.
        if not await self.bucket.acquire(timeout_s=2.0):
            self.stats.rate_limited += 1
            meta["error"] = "local rate limit"
            return None, meta

        if self._client is None:
            await self.start()

        started = time.perf_counter()
        try:
            resp = await self._client.get(  # type: ignore[union-attr]
                path,
                params=params,
                headers=headers,
                timeout=timeout_s or self.timeout_s,
            )
            latency_ms = (time.perf_counter() - started) * 1000
            meta["latency_ms"] = latency_ms
            self.stats.requests += 1
            self.stats.total_latency_ms += latency_ms

            if resp.status_code == 429:
                self.stats.rate_limited += 1
                self.breaker.record_failure()
                meta["error"] = "rate limited (429)"
                # Negative-cache briefly so a hot loop does not keep hammering.
                self.cache.put(key, None, min(negative_ttl_s, 10.0), negative=True)
                return None, meta

            if resp.status_code in (401, 403):
                self.breaker.record_failure()
                meta["error"] = f"auth rejected ({resp.status_code})"
                # Long negative cache: a bad key will not fix itself.
                self.cache.put(key, None, 300.0, negative=True)
                return None, meta

            if resp.status_code >= 500:
                self.breaker.record_failure()
                meta["error"] = f"server error ({resp.status_code})"
                return None, meta

            if resp.status_code >= 400:
                # 404 is a legitimate answer meaning "no data for this token",
                # so it is cached but does not count against the circuit.
                meta["error"] = f"http {resp.status_code}"
                self.cache.put(key, None, negative_ttl_s, negative=True)
                return None, meta

            data = orjson.loads(resp.content)
            self.breaker.record_success()
            if cache_ttl_s > 0:
                self.cache.put(key, data, cache_ttl_s)
            return data, meta

        except (httpx.TimeoutException, asyncio.TimeoutError):
            meta["latency_ms"] = (time.perf_counter() - started) * 1000
            self.stats.errors += 1
            self.breaker.record_failure()
            meta["error"] = "timeout"
            return None, meta
        except Exception as exc:  # noqa: BLE001
            meta["latency_ms"] = (time.perf_counter() - started) * 1000
            self.stats.errors += 1
            self.breaker.record_failure()
            meta["error"] = f"{type(exc).__name__}: {exc}"
            return None, meta

    async def post_json(
        self,
        path: str,
        *,
        json_body: dict[str, Any] | None = None,
        data: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
        timeout_s: float | None = None,
    ) -> tuple[Any, dict[str, Any]]:
        """POST. Never cached and never coalesced — POSTs have side effects."""
        meta: dict[str, Any] = {"cached": False, "stale": False, "latency_ms": 0.0, "error": None}

        if self.breaker.is_open:
            self.stats.circuit_rejections += 1
            meta["error"] = "circuit open"
            return None, meta
        if not await self.bucket.acquire(timeout_s=2.0):
            meta["error"] = "local rate limit"
            return None, meta
        if self._client is None:
            await self.start()

        started = time.perf_counter()
        try:
            resp = await self._client.post(  # type: ignore[union-attr]
                path, json=json_body, data=data, headers=headers,
                timeout=timeout_s or self.timeout_s,
            )
            meta["latency_ms"] = (time.perf_counter() - started) * 1000
            self.stats.requests += 1
            self.stats.total_latency_ms += meta["latency_ms"]

            if resp.status_code >= 400:
                if resp.status_code >= 500 or resp.status_code == 429:
                    self.breaker.record_failure()
                meta["error"] = f"http {resp.status_code}: {resp.text[:160]}"
                return None, meta

            self.breaker.record_success()
            return orjson.loads(resp.content), meta
        except Exception as exc:  # noqa: BLE001
            meta["latency_ms"] = (time.perf_counter() - started) * 1000
            self.stats.errors += 1
            self.breaker.record_failure()
            meta["error"] = f"{type(exc).__name__}: {exc}"
            return None, meta

    # -- background refresh -------------------------------------------------

    def _schedule_refresh(
        self,
        key: str,
        path: str,
        params: dict[str, Any] | None,
        headers: dict[str, str] | None,
        cache_ttl_s: float,
        timeout_s: float | None,
        negative_ttl_s: float,
    ) -> None:
        if key in self._refreshing:
            return
        self._refreshing.add(key)

        async def _refresh() -> None:
            try:
                await self._fetch(
                    path, params, headers, timeout_s, cache_ttl_s, negative_ttl_s, key
                )
            except Exception:  # noqa: BLE001
                pass
            finally:
                self._refreshing.discard(key)

        try:
            asyncio.get_running_loop().create_task(_refresh())
        except RuntimeError:
            self._refreshing.discard(key)

    @staticmethod
    def _key(path: str, params: dict[str, Any] | None) -> str:
        if not params:
            return path
        parts = "&".join(f"{k}={v}" for k, v in sorted(params.items()) if v is not None)
        return f"{path}?{parts}"

    def snapshot(self) -> dict[str, Any]:
        return {
            "requests": self.stats.requests,
            "errors": self.stats.errors,
            "rate_limited": self.stats.rate_limited,
            "coalesced": self.stats.coalesced,
            "circuit_rejections": self.stats.circuit_rejections,
            "circuit_open": self.breaker.is_open,
            "avg_latency_ms": round(
                self.stats.total_latency_ms / self.stats.requests, 1
            )
            if self.stats.requests
            else 0.0,
            "tokens_available": round(self.bucket.available, 1),
            "http2": self.http2,
            "cache": self.cache.stats(),
        }
