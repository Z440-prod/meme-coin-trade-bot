"""Provider framework — the contract every external data source implements.

Twenty-odd third-party services, each with its own auth, rate limits, failure
modes and uptime. The framework exists so that none of that leaks into the
trading logic: the Brain asks for "security intel on this mint" and gets an
answer assembled from whoever is currently up and configured.

Three principles, all learned from watching bots die:

**Every provider is optional.** A provider that is unconfigured, rate limited,
down, or slow must degrade to "no opinion" — never to an exception that kills
a trading loop. The system must trade correctly with zero providers enabled.

**Nothing on the hot path blocks on the network.** Entry decisions read from
cache. Cache misses are filled asynchronously and can veto *later*, but they
never make you late. See `TIER` below.

**Provenance is preserved.** Every fused answer records which providers
contributed and which disagreed. When a trade goes wrong you need to know
whether you were told the wrong thing or told the right thing and ignored it.
"""

from __future__ import annotations

import asyncio
import enum
import logging
import time
from abc import ABC
from dataclasses import dataclass, field
from typing import Any, Generic, TypeVar

log = logging.getLogger(__name__)

T = TypeVar("T")


class Capability(str, enum.Enum):
    """What a provider can answer. Used to route requests."""

    # Market data
    TOKEN_PRICE = "token_price"
    TOKEN_MARKET = "token_market"          # price + liquidity + volume + txns
    TOKEN_METADATA = "token_metadata"
    OHLCV = "ohlcv"
    TRENDING = "trending"                  # discovery: what is hot right now
    NEW_PAIRS = "new_pairs"

    # Security
    TOKEN_SECURITY = "token_security"      # authorities, honeypot, tax
    HOLDER_DISTRIBUTION = "holder_distribution"
    LP_STATUS = "lp_status"

    # Smart money
    WALLET_PNL = "wallet_pnl"
    WALLET_ACTIVITY = "wallet_activity"
    WALLET_LABELS = "wallet_labels"        # exchange / bot / whale tagging
    TOKEN_HOLDERS = "token_holders"
    WALLET_CLUSTER = "wallet_cluster"      # which wallets are one actor

    # Social
    SOCIAL_METRICS = "social_metrics"
    NARRATIVE = "narrative"

    # Execution
    SWAP_QUOTE = "swap_quote"
    SWAP_BUILD = "swap_build"
    POOL_INFO = "pool_info"

    # Generic on-chain
    TX_HISTORY = "tx_history"
    TX_PARSE = "tx_parse"
    ANALYTICS_QUERY = "analytics_query"


class Tier(int, enum.Enum):
    """How urgently a provider can be consulted.

    This is the single most important field for speed. The entry decision only
    ever waits on HOT. Everything else enriches around it.
    """

    # Answers from local cache/memory in microseconds. Never a network call.
    HOT = 0
    # Fast, reliable, keyless. Safe to await with a tight timeout during
    # candidate screening.
    WARM = 1
    # Slower or rate-limited. Fetched in the background; results land in cache
    # for the next decision and may trigger an exit on an open position.
    COLD = 2
    # Expensive, heavily rate limited, or paid-per-call. Only on demand
    # (terminal lookup, deep-dive on a position we actually hold).
    MANUAL = 3


class ProviderStatus(str, enum.Enum):
    READY = "ready"
    UNCONFIGURED = "unconfigured"      # no API key supplied
    RATE_LIMITED = "rate_limited"
    CIRCUIT_OPEN = "circuit_open"      # too many recent failures
    DISABLED = "disabled"              # switched off in config
    NO_PUBLIC_API = "no_public_api"    # link-out only; cannot be queried


@dataclass
class ProviderResult(Generic[T]):
    """One provider's answer, with everything needed to judge it."""

    provider: str
    ok: bool
    data: T | None = None
    error: str | None = None
    latency_ms: float = 0.0
    cached: bool = False
    stale: bool = False
    fetched_at: float = field(default_factory=time.time)

    @property
    def usable(self) -> bool:
        return self.ok and self.data is not None

    @classmethod
    def failure(cls, provider: str, error: str, latency_ms: float = 0.0) -> ProviderResult[T]:
        return cls(provider=provider, ok=False, error=error, latency_ms=latency_ms)


@dataclass
class ProviderHealth:
    """Rolling health, surfaced on the terminal's provider panel."""

    calls: int = 0
    failures: int = 0
    rate_limits: int = 0
    total_latency_ms: float = 0.0
    last_ok_ts: float = 0.0
    last_error: str = ""
    last_error_ts: float = 0.0
    consecutive_failures: int = 0

    @property
    def avg_latency_ms(self) -> float:
        return self.total_latency_ms / self.calls if self.calls else 0.0

    @property
    def success_rate(self) -> float:
        return (self.calls - self.failures) / self.calls if self.calls else 1.0

    def record(self, ok: bool, latency_ms: float, error: str = "") -> None:
        self.calls += 1
        self.total_latency_ms += latency_ms
        if ok:
            self.last_ok_ts = time.time()
            self.consecutive_failures = 0
        else:
            self.failures += 1
            self.consecutive_failures += 1
            self.last_error = error[:200]
            self.last_error_ts = time.time()
            if "rate" in error.lower() or "429" in error:
                self.rate_limits += 1

    def snapshot(self) -> dict[str, Any]:
        return {
            "calls": self.calls,
            "failures": self.failures,
            "rate_limits": self.rate_limits,
            "success_rate": round(self.success_rate, 3),
            "avg_latency_ms": round(self.avg_latency_ms, 1),
            "consecutive_failures": self.consecutive_failures,
            "last_error": self.last_error,
            "age_s": round(time.time() - self.last_ok_ts, 1) if self.last_ok_ts else None,
        }


class Provider(ABC):  # noqa: B024 - see class docstring
    """Base class for every external data source.

    Subclasses declare what they can do and implement the fetch methods for
    those capabilities. Anything not implemented simply is not routed to them —
    which is why none of the capability methods are abstract: a provider that
    serves only market data should not be forced to stub out wallet labelling.
    """

    #: Short stable identifier, used in config, logs and provenance.
    name: str = "unnamed"
    #: Human-readable name for the UI.
    display_name: str = "Unnamed"
    #: What this provider can answer.
    capabilities: frozenset[Capability] = frozenset()
    #: Default urgency tier.
    tier: Tier = Tier.COLD
    #: Whether an API key is mandatory.
    requires_key: bool = False
    #: Whether the service costs money beyond a free tier.
    paid: bool = False
    #: Requests per second this provider tolerates. Enforced locally.
    rate_limit_rps: float = 5.0
    #: Burst allowance for the token bucket.
    rate_limit_burst: int = 10
    #: How long answers stay fresh, per capability defaults.
    cache_ttl_s: float = 30.0
    #: Homepage, shown in the terminal.
    url: str = ""

    def __init__(self, config: Any, api_key: str | None = None) -> None:
        self.config = config
        self.api_key = api_key or None
        self.health = ProviderHealth()
        self._status = (
            ProviderStatus.UNCONFIGURED
            if (self.requires_key and not self.api_key)
            else ProviderStatus.READY
        )

    # -- status -------------------------------------------------------------

    @property
    def status(self) -> ProviderStatus:
        return self._status

    def set_status(self, status: ProviderStatus) -> None:
        if status != self._status:
            log.info("provider %s: %s -> %s", self.name, self._status.value, status.value)
        self._status = status

    @property
    def available(self) -> bool:
        return self._status == ProviderStatus.READY

    def supports(self, capability: Capability) -> bool:
        return capability in self.capabilities and self.available

    # -- lifecycle ----------------------------------------------------------

    async def start(self) -> None:
        return None

    async def close(self) -> None:
        return None

    async def probe(self) -> bool:
        """Cheap liveness check used by preflight and the terminal."""
        return self.available

    # -- capability methods -------------------------------------------------
    # Subclasses override whichever apply. The registry only calls a method on
    # a provider that declares the matching capability, so these defaults are
    # never hit in normal operation.

    async def get_market(self, mint: str) -> ProviderResult[dict]:
        return ProviderResult.failure(self.name, "not implemented")

    async def get_markets(self, mints: list[str]) -> ProviderResult[dict[str, dict]]:
        """Batch form. Providers with a real batch endpoint override this; the
        default fans out, which the registry avoids for large lists."""
        out: dict[str, dict] = {}
        for mint in mints:
            result = await self.get_market(mint)
            if result.usable:
                out[mint] = result.data  # type: ignore[assignment]
        return ProviderResult(provider=self.name, ok=True, data=out)

    async def get_security(self, mint: str) -> ProviderResult[dict]:
        return ProviderResult.failure(self.name, "not implemented")

    async def get_holders(self, mint: str, limit: int = 20) -> ProviderResult[list[dict]]:
        return ProviderResult.failure(self.name, "not implemented")

    async def get_trending(self, limit: int = 50) -> ProviderResult[list[dict]]:
        return ProviderResult.failure(self.name, "not implemented")

    async def get_new_pairs(self, limit: int = 50) -> ProviderResult[list[dict]]:
        return ProviderResult.failure(self.name, "not implemented")

    async def get_wallet_pnl(self, wallet: str) -> ProviderResult[dict]:
        return ProviderResult.failure(self.name, "not implemented")

    async def get_wallet_activity(self, wallet: str, limit: int = 50) -> ProviderResult[list[dict]]:
        return ProviderResult.failure(self.name, "not implemented")

    async def get_wallet_labels(self, wallets: list[str]) -> ProviderResult[dict[str, str]]:
        return ProviderResult.failure(self.name, "not implemented")

    async def get_social(self, symbol: str, mint: str = "") -> ProviderResult[dict]:
        return ProviderResult.failure(self.name, "not implemented")

    async def get_quote(
        self, input_mint: str, output_mint: str, amount: int, slippage_bps: int = 300
    ) -> ProviderResult[dict]:
        return ProviderResult.failure(self.name, "not implemented")

    async def get_pool_info(self, mint: str) -> ProviderResult[dict]:
        return ProviderResult.failure(self.name, "not implemented")

    # -- links --------------------------------------------------------------

    def token_url(self, mint: str) -> str | None:
        """Deep link to this provider's page for a token.

        Providers with no public API still earn their place here — a one-click
        jump to a chart or a bubble map is genuinely useful even when the data
        cannot be pulled programmatically.
        """
        return None

    def wallet_url(self, wallet: str) -> str | None:
        return None

    def describe(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "display_name": self.display_name,
            "status": self.status.value,
            "tier": self.tier.name,
            "paid": self.paid,
            "requires_key": self.requires_key,
            "has_key": bool(self.api_key),
            "capabilities": sorted(c.value for c in self.capabilities),
            "url": self.url,
            "health": self.health.snapshot(),
        }


class LinkOnlyProvider(Provider):
    """A service with no usable public API.

    Photon, BullX and friends are trading frontends, not data sources. Claiming
    otherwise would mean writing a scraper against a Cloudflare-protected UI
    that breaks weekly and may violate their terms. Instead we expose what they
    genuinely offer: a fast, one-click jump to their view of a token.
    """

    tier = Tier.MANUAL
    capabilities = frozenset()

    def __init__(self, config: Any, api_key: str | None = None) -> None:
        super().__init__(config, api_key)
        self.set_status(ProviderStatus.NO_PUBLIC_API)

    @property
    def available(self) -> bool:
        return False


async def gather_providers(
    coros: list[asyncio.Future | Any], timeout_s: float
) -> list[ProviderResult]:
    """Await several provider calls with one shared deadline.

    Anything that has not answered by the deadline is abandoned rather than
    allowed to hold up the caller. This is what keeps a single slow provider
    from adding its latency to every decision.
    """
    if not coros:
        return []
    tasks = [asyncio.ensure_future(c) for c in coros]
    done, pending = await asyncio.wait(tasks, timeout=timeout_s)

    for task in pending:
        task.cancel()

    results: list[ProviderResult] = []
    for task in tasks:
        if task in done:
            try:
                value = task.result()
                if isinstance(value, ProviderResult):
                    results.append(value)
            except Exception as exc:  # noqa: BLE001
                log.debug("provider call raised: %s", exc)
    return results
