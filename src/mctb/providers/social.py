"""Social and narrative providers: LunarCrush, Kaito.

Both are key-gated. Both measure attention, which is a real driver of memecoin
price — and also the single easiest thing in this entire system to fake.

Treat everything here as weak evidence. `intel/narrative.py` caps social's
total contribution and, more importantly, looks at the *shape* of attention
rather than its level: a mention count that goes from 0 to 400 in ten minutes
across accounts that all registered last week is a manufactured campaign, and
that pattern is more informative than the raw number ever is.
"""

from __future__ import annotations

import logging
from typing import Any

from .base import Capability, Provider, ProviderResult, Tier
from .http import ProviderHttp

log = logging.getLogger(__name__)


class LunarCrushProvider(Provider):
    """LunarCrush — social volume, sentiment and engagement metrics."""

    name = "lunarcrush"
    display_name = "LunarCrush"
    url = "https://lunarcrush.com"
    capabilities = frozenset({Capability.SOCIAL_METRICS, Capability.NARRATIVE})
    tier = Tier.COLD
    requires_key = True
    paid = True
    rate_limit_rps = 1.0
    rate_limit_burst = 3
    cache_ttl_s = 300.0

    def __init__(self, config: Any, api_key: str | None = None) -> None:
        super().__init__(config, api_key)
        self.http = ProviderHttp(
            self.name,
            base_url="https://lunarcrush.com/api4/public",
            headers={"Authorization": f"Bearer {api_key}"} if api_key else {},
            rate_limit_rps=self.rate_limit_rps,
            rate_limit_burst=self.rate_limit_burst,
            timeout_s=10.0,
        )

    async def start(self) -> None:
        if self.available:
            await self.http.start()

    async def close(self) -> None:
        await self.http.close()

    async def get_social(self, symbol: str, mint: str = "") -> ProviderResult[dict]:
        if not self.api_key:
            return ProviderResult.failure(self.name, "no API key")
        if not symbol:
            return ProviderResult.failure(self.name, "symbol required")

        data, meta = await self.http.get_json(
            f"/coins/{symbol}/v1", cache_ttl_s=self.cache_ttl_s, max_stale_s=600.0
        )
        self.health.record(bool(data), meta.get("latency_ms", 0.0), meta.get("error") or "")
        if not data:
            return ProviderResult.failure(self.name, meta.get("error") or "no data")

        d = data.get("data") or {}
        return ProviderResult(
            provider=self.name, ok=True, latency_ms=meta.get("latency_ms", 0.0),
            cached=meta.get("cached", False),
            data={
                "galaxy_score": float(d.get("galaxy_score") or 0),
                "alt_rank": int(d.get("alt_rank") or 0),
                "social_volume_24h": int(d.get("social_volume_24h") or 0),
                "social_dominance": float(d.get("social_dominance") or 0),
                "interactions_24h": int(d.get("interactions_24h") or 0),
                "sentiment": float(d.get("sentiment") or 0),
                "contributors_active": int(d.get("num_contributors") or 0),
                "posts_active": int(d.get("num_posts") or 0),
            },
        )

    def token_url(self, mint: str) -> str:
        return f"https://lunarcrush.com/discover/{mint}"


class KaitoProvider(Provider):
    """Kaito — attention/mindshare analytics for crypto narratives.

    Its public surface is the Yaps attention score for X accounts. That is
    genuinely useful for one specific job: **grading the accounts that shill a
    token.** Ten mentions from accounts with no attention history is noise;
    two from accounts with real, sustained mindshare is a signal.
    """

    name = "kaito"
    display_name = "Kaito"
    url = "https://kaito.ai"
    capabilities = frozenset({Capability.NARRATIVE, Capability.SOCIAL_METRICS})
    tier = Tier.MANUAL
    requires_key = True
    paid = True
    rate_limit_rps = 0.5
    rate_limit_burst = 2
    cache_ttl_s = 3600.0

    def __init__(self, config: Any, api_key: str | None = None) -> None:
        super().__init__(config, api_key)
        self.http = ProviderHttp(
            self.name,
            base_url="https://api.kaito.ai",
            headers={"Authorization": f"Bearer {api_key}"} if api_key else {},
            rate_limit_rps=self.rate_limit_rps,
            rate_limit_burst=self.rate_limit_burst,
            timeout_s=12.0,
        )

    async def start(self) -> None:
        if self.available:
            await self.http.start()

    async def close(self) -> None:
        await self.http.close()

    async def get_account_attention(self, username: str) -> ProviderResult[dict]:
        """Attention score for one X account — how much real mindshare it has."""
        if not self.api_key:
            return ProviderResult.failure(self.name, "no API key")

        data, meta = await self.http.get_json(
            "/api/v1/yaps", params={"username": username.lstrip("@")},
            cache_ttl_s=self.cache_ttl_s,
        )
        self.health.record(bool(data), meta.get("latency_ms", 0.0), meta.get("error") or "")
        if not data:
            return ProviderResult.failure(self.name, meta.get("error") or "no data")

        return ProviderResult(
            provider=self.name, ok=True, latency_ms=meta.get("latency_ms", 0.0),
            data={
                "username": data.get("username", username),
                "yaps_all": float(data.get("yaps_all") or 0),
                "yaps_l7d": float(data.get("yaps_l7d") or 0),
                "yaps_l30d": float(data.get("yaps_l30d") or 0),
            },
        )

    async def get_social(self, symbol: str, mint: str = "") -> ProviderResult[dict]:
        return ProviderResult.failure(self.name, "use get_account_attention for account grading")
