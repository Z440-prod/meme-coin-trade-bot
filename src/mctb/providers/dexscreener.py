"""DEX Screener — free, keyless, and the best general market data available.

No API key, generous limits, and a batch endpoint that takes up to 30 mints in
one request. That combination makes it the default market-data provider: one
call prices an entire watchlist.

Endpoints used (all verified against the live API):
    /latest/dex/tokens/{addresses}   up to 30 comma-separated mints
    /latest/dex/search?q=            free-text search
    /token-profiles/latest/v1        newly profiled tokens (discovery)
    /token-boosts/top/v1             paid promotion — see the note below

Rate limits are documented as 300 req/min for the token endpoints and 60
req/min for the profile endpoints; we shape to well under both.
"""

from __future__ import annotations

import logging
import time
from typing import Any

from .base import Capability, Provider, ProviderResult, Tier
from .http import ProviderHttp

log = logging.getLogger(__name__)


class DexScreenerProvider(Provider):
    name = "dexscreener"
    display_name = "DEX Screener"
    url = "https://dexscreener.com"
    capabilities = frozenset(
        {
            Capability.TOKEN_MARKET,
            Capability.TOKEN_PRICE,
            Capability.TOKEN_METADATA,
            Capability.TRENDING,
            Capability.NEW_PAIRS,
            Capability.LP_STATUS,
        }
    )
    tier = Tier.WARM
    requires_key = False
    paid = False
    # Documented 300/min on token endpoints; 4/s leaves plenty of headroom.
    rate_limit_rps = 4.0
    rate_limit_burst = 12
    cache_ttl_s = 15.0

    def __init__(self, config: Any, api_key: str | None = None) -> None:
        super().__init__(config, api_key)
        self.http = ProviderHttp(
            self.name,
            base_url="https://api.dexscreener.com",
            rate_limit_rps=self.rate_limit_rps,
            rate_limit_burst=self.rate_limit_burst,
            timeout_s=6.0,
        )

    async def start(self) -> None:
        await self.http.start()
        await self.http.warm("/latest/dex/tokens/So11111111111111111111111111111111111111112")

    async def close(self) -> None:
        await self.http.close()

    async def probe(self) -> bool:
        data, _meta = await self.http.get_json(
            "/latest/dex/tokens/So11111111111111111111111111111111111111112", cache_ttl_s=0
        )
        return bool(data)

    # -- market -------------------------------------------------------------

    @staticmethod
    def _best_pair(pairs: list[dict]) -> dict | None:
        """Pick the pair that actually represents the token.

        A mint can appear in many pairs across many chains — including, as the
        live API shows, chains you have never heard of. Filter to Solana, then
        take the deepest liquidity, because that is the one you would actually
        trade against and the one whose price is hardest to manipulate.
        """
        solana = [p for p in pairs if p.get("chainId") == "solana"]
        if not solana:
            return None
        return max(solana, key=lambda p: float((p.get("liquidity") or {}).get("usd") or 0))

    @classmethod
    def _normalise(cls, pair: dict) -> dict:
        liquidity = pair.get("liquidity") or {}
        txns = pair.get("txns") or {}
        volume = pair.get("volume") or {}
        change = pair.get("priceChange") or {}
        m5 = txns.get("m5") or {}
        h1 = txns.get("h1") or {}
        h24 = txns.get("h24") or {}

        created_ms = pair.get("pairCreatedAt") or 0
        age_s = (time.time() - created_ms / 1000) if created_ms else 0.0

        return {
            "price_usd": float(pair.get("priceUsd") or 0),
            "price_native": float(pair.get("priceNative") or 0),
            "liquidity_usd": float(liquidity.get("usd") or 0),
            "liquidity_base": float(liquidity.get("base") or 0),
            "liquidity_quote": float(liquidity.get("quote") or 0),
            "fdv": float(pair.get("fdv") or 0),
            "market_cap": float(pair.get("marketCap") or 0),
            "volume_5m": float(volume.get("m5") or 0),
            "volume_1h": float(volume.get("h1") or 0),
            "volume_24h": float(volume.get("h24") or 0),
            "buys_5m": int(m5.get("buys") or 0),
            "sells_5m": int(m5.get("sells") or 0),
            "buys_1h": int(h1.get("buys") or 0),
            "sells_1h": int(h1.get("sells") or 0),
            "buys_24h": int(h24.get("buys") or 0),
            "sells_24h": int(h24.get("sells") or 0),
            "change_5m": float(change.get("m5") or 0),
            "change_1h": float(change.get("h1") or 0),
            "change_24h": float(change.get("h24") or 0),
            "dex": pair.get("dexId", ""),
            "pair_address": pair.get("pairAddress", ""),
            "symbol": (pair.get("baseToken") or {}).get("symbol", ""),
            "name": (pair.get("baseToken") or {}).get("name", ""),
            "age_s": age_s,
            "url": pair.get("url", ""),
            "socials": (pair.get("info") or {}).get("socials", []),
            "websites": (pair.get("info") or {}).get("websites", []),
            # Paid promotion. Not a quality signal — if anything the opposite,
            # since buying visibility is exactly what a launch with no organic
            # interest does. Surfaced so the scorer can treat it sceptically.
            "boosted": int((pair.get("boosts") or {}).get("active") or 0),
        }

    async def get_market(self, mint: str) -> ProviderResult[dict]:
        result = await self.get_markets([mint])
        if not result.usable:
            return ProviderResult.failure(self.name, result.error or "no data")
        data = (result.data or {}).get(mint)
        if not data:
            return ProviderResult.failure(self.name, "no solana pair found")
        return ProviderResult(
            provider=self.name, ok=True, data=data,
            latency_ms=result.latency_ms, cached=result.cached,
        )

    async def get_markets(self, mints: list[str]) -> ProviderResult[dict[str, dict]]:
        """Batch: up to 30 mints per request.

        This is the endpoint that makes watchlist pricing cheap — 30 tokens in
        one round trip instead of 30 round trips.
        """
        if not mints:
            return ProviderResult(provider=self.name, ok=True, data={})

        out: dict[str, dict] = {}
        total_latency = 0.0
        any_cached = False
        errors: list[str] = []

        for i in range(0, len(mints), 30):
            chunk = mints[i : i + 30]
            data, meta = await self.http.get_json(
                f"/latest/dex/tokens/{','.join(chunk)}",
                cache_ttl_s=self.cache_ttl_s,
                max_stale_s=45.0,
            )
            total_latency += meta.get("latency_ms", 0.0)
            any_cached = any_cached or meta.get("cached", False)
            if meta.get("error"):
                errors.append(meta["error"])
            if not data:
                continue

            by_mint: dict[str, list[dict]] = {}
            for pair in data.get("pairs") or []:
                address = (pair.get("baseToken") or {}).get("address")
                if address:
                    by_mint.setdefault(address, []).append(pair)

            for mint, pairs in by_mint.items():
                best = self._best_pair(pairs)
                if best:
                    out[mint] = self._normalise(best)

        self.health.record(bool(out), total_latency, "; ".join(errors[:2]))
        if not out and errors:
            return ProviderResult.failure(self.name, errors[0], total_latency)
        return ProviderResult(
            provider=self.name, ok=True, data=out,
            latency_ms=total_latency, cached=any_cached,
        )

    # -- discovery ----------------------------------------------------------

    async def get_new_pairs(self, limit: int = 50) -> ProviderResult[list[dict]]:
        """Recently profiled tokens. Solana only."""
        data, meta = await self.http.get_json(
            "/token-profiles/latest/v1", cache_ttl_s=60.0, max_stale_s=120.0
        )
        self.health.record(bool(data), meta.get("latency_ms", 0.0), meta.get("error") or "")
        if not data:
            return ProviderResult.failure(self.name, meta.get("error") or "no data")

        out = [
            {
                "mint": item.get("tokenAddress", ""),
                "url": item.get("url", ""),
                "icon": item.get("icon", ""),
                "description": (item.get("description") or "")[:400],
                "links": item.get("links", []),
            }
            for item in data
            if item.get("chainId") == "solana" and item.get("tokenAddress")
        ]
        return ProviderResult(
            provider=self.name, ok=True, data=out[:limit],
            latency_ms=meta.get("latency_ms", 0.0), cached=meta.get("cached", False),
        )

    async def get_trending(self, limit: int = 50) -> ProviderResult[list[dict]]:
        """Top boosted tokens.

        Read this as "who is paying for attention", not "what is good". It is
        genuinely useful as a discovery surface and genuinely dangerous as a
        buy signal; the scorer treats a boost as mild evidence *against*.
        """
        data, meta = await self.http.get_json(
            "/token-boosts/top/v1", cache_ttl_s=120.0, max_stale_s=300.0
        )
        self.health.record(bool(data), meta.get("latency_ms", 0.0), meta.get("error") or "")
        if not data:
            return ProviderResult.failure(self.name, meta.get("error") or "no data")

        out = [
            {
                "mint": item.get("tokenAddress", ""),
                "boost_amount": float(item.get("totalAmount") or 0),
                "url": item.get("url", ""),
                "description": (item.get("description") or "")[:200],
            }
            for item in data
            if item.get("chainId") == "solana" and item.get("tokenAddress")
        ]
        out.sort(key=lambda r: r["boost_amount"], reverse=True)
        return ProviderResult(
            provider=self.name, ok=True, data=out[:limit],
            latency_ms=meta.get("latency_ms", 0.0), cached=meta.get("cached", False),
        )

    async def search(self, query: str, limit: int = 20) -> ProviderResult[list[dict]]:
        """Free-text search — powers the terminal's lookup bar."""
        data, meta = await self.http.get_json(
            "/latest/dex/search", params={"q": query}, cache_ttl_s=30.0
        )
        if not data:
            return ProviderResult.failure(self.name, meta.get("error") or "no data")

        out = []
        for pair in (data.get("pairs") or [])[: limit * 3]:
            if pair.get("chainId") != "solana":
                continue
            row = self._normalise(pair)
            row["mint"] = (pair.get("baseToken") or {}).get("address", "")
            out.append(row)
            if len(out) >= limit:
                break
        return ProviderResult(
            provider=self.name, ok=True, data=out, latency_ms=meta.get("latency_ms", 0.0)
        )

    def token_url(self, mint: str) -> str:
        return f"https://dexscreener.com/solana/{mint}"
