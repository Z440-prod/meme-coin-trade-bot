"""Premium market-data providers: Birdeye and DEXTools.

Both require paid keys. Neither is necessary — DEX Screener covers the same
ground for free — but Birdeye in particular adds things DEX Screener does not:
OHLCV candles, per-token trader lists, and top-holder breakdowns, all of which
feed the entity graph.

Recommendation: do not pay for either until the free stack is profitable.
"""

from __future__ import annotations

import logging
from typing import Any

from .base import Capability, Provider, ProviderResult, Tier
from .http import ProviderHttp

log = logging.getLogger(__name__)


class BirdeyeProvider(Provider):
    """Birdeye — market data, OHLCV, holders and trader lists.

    Auth is the `X-API-KEY` header plus `x-chain: solana`. Billing is by
    compute unit rather than request count, so a single expensive endpoint can
    burn a plan far faster than many cheap ones. Cache aggressively.
    """

    name = "birdeye"
    display_name = "Birdeye"
    url = "https://birdeye.so"
    capabilities = frozenset(
        {
            Capability.TOKEN_PRICE,
            Capability.TOKEN_MARKET,
            Capability.TOKEN_METADATA,
            Capability.OHLCV,
            Capability.TRENDING,
            Capability.NEW_PAIRS,
            Capability.TOKEN_HOLDERS,
            Capability.WALLET_ACTIVITY,
        }
    )
    tier = Tier.COLD
    requires_key = True
    paid = True
    rate_limit_rps = 8.0
    rate_limit_burst = 15
    cache_ttl_s = 20.0

    def __init__(self, config: Any, api_key: str | None = None) -> None:
        super().__init__(config, api_key)
        self.http = ProviderHttp(
            self.name,
            base_url="https://public-api.birdeye.so",
            headers={"X-API-KEY": api_key or "", "x-chain": "solana"},
            rate_limit_rps=self.rate_limit_rps,
            rate_limit_burst=self.rate_limit_burst,
            timeout_s=8.0,
        )

    async def start(self) -> None:
        if self.available:
            await self.http.start()

    async def close(self) -> None:
        await self.http.close()

    async def probe(self) -> bool:
        if not self.api_key:
            return False
        data, _meta = await self.http.get_json(
            "/defi/price",
            params={"address": "So11111111111111111111111111111111111111112"},
            cache_ttl_s=0,
        )
        return bool(data and data.get("success"))

    async def get_market(self, mint: str) -> ProviderResult[dict]:
        if not self.api_key:
            return ProviderResult.failure(self.name, "no API key")

        data, meta = await self.http.get_json(
            "/defi/token_overview",
            params={"address": mint},
            cache_ttl_s=self.cache_ttl_s,
            max_stale_s=60.0,
        )
        self.health.record(bool(data), meta.get("latency_ms", 0.0), meta.get("error") or "")
        if not data or not data.get("success"):
            return ProviderResult.failure(
                self.name, meta.get("error") or "no data", meta.get("latency_ms", 0.0)
            )

        d = data.get("data") or {}
        return ProviderResult(
            provider=self.name,
            ok=True,
            latency_ms=meta.get("latency_ms", 0.0),
            cached=meta.get("cached", False),
            data={
                "price_usd": float(d.get("price") or 0),
                "liquidity_usd": float(d.get("liquidity") or 0),
                "market_cap": float(d.get("mc") or d.get("realMc") or 0),
                "fdv": float(d.get("fdv") or 0),
                "volume_1h": float(d.get("v1hUSD") or 0),
                "volume_24h": float(d.get("v24hUSD") or 0),
                "change_1h": float(d.get("priceChange1hPercent") or 0),
                "change_24h": float(d.get("priceChange24hPercent") or 0),
                "holder_count": int(d.get("holder") or 0),
                # Unique traders are far harder to fake than raw volume, which
                # makes this one of Birdeye's most useful fields.
                "unique_wallets_24h": int(d.get("uniqueWallet24h") or 0),
                "unique_wallets_1h": int(d.get("uniqueWallet1h") or 0),
                "trades_24h": int(d.get("trade24h") or 0),
                "buys_24h": int(d.get("buy24h") or 0),
                "sells_24h": int(d.get("sell24h") or 0),
                "symbol": d.get("symbol", ""),
                "name": d.get("name", ""),
                "supply": float(d.get("supply") or 0),
            },
        )

    async def get_holders(self, mint: str, limit: int = 20) -> ProviderResult[list[dict]]:
        """Top holders — a direct input to the entity graph."""
        if not self.api_key:
            return ProviderResult.failure(self.name, "no API key")

        data, meta = await self.http.get_json(
            "/defi/v3/token/holder",
            params={"address": mint, "offset": 0, "limit": min(limit, 100)},
            cache_ttl_s=120.0,
        )
        if not data or not data.get("success"):
            return ProviderResult.failure(self.name, meta.get("error") or "no data")

        items = ((data.get("data") or {}).get("items")) or []
        return ProviderResult(
            provider=self.name,
            ok=True,
            latency_ms=meta.get("latency_ms", 0.0),
            data=[
                {
                    "owner": h.get("owner", ""),
                    "amount": float(h.get("ui_amount") or 0),
                    "percentage": float(h.get("percentage") or 0) / 100.0,
                }
                for h in items
            ],
        )

    async def get_ohlcv(
        self, mint: str, interval: str = "1m", limit: int = 100
    ) -> ProviderResult[list[dict]]:
        if not self.api_key:
            return ProviderResult.failure(self.name, "no API key")
        import time as _time

        seconds = {"1m": 60, "5m": 300, "15m": 900, "1H": 3600}.get(interval, 60)
        now = int(_time.time())
        data, meta = await self.http.get_json(
            "/defi/ohlcv",
            params={
                "address": mint, "type": interval,
                "time_from": now - seconds * limit, "time_to": now,
            },
            cache_ttl_s=30.0,
        )
        if not data or not data.get("success"):
            return ProviderResult.failure(self.name, meta.get("error") or "no data")

        items = ((data.get("data") or {}).get("items")) or []
        return ProviderResult(
            provider=self.name, ok=True, latency_ms=meta.get("latency_ms", 0.0),
            data=[
                {
                    "t": int(c.get("unixTime") or 0), "o": float(c.get("o") or 0),
                    "h": float(c.get("h") or 0), "l": float(c.get("l") or 0),
                    "c": float(c.get("c") or 0), "v": float(c.get("v") or 0),
                }
                for c in items
            ],
        )

    async def get_trending(self, limit: int = 50) -> ProviderResult[list[dict]]:
        if not self.api_key:
            return ProviderResult.failure(self.name, "no API key")
        data, meta = await self.http.get_json(
            "/defi/token_trending",
            params={"sort_by": "rank", "sort_type": "asc", "offset": 0, "limit": min(limit, 20)},
            cache_ttl_s=120.0,
        )
        if not data or not data.get("success"):
            return ProviderResult.failure(self.name, meta.get("error") or "no data")
        tokens = ((data.get("data") or {}).get("tokens")) or []
        return ProviderResult(
            provider=self.name, ok=True, latency_ms=meta.get("latency_ms", 0.0),
            data=[
                {
                    "mint": t.get("address", ""), "symbol": t.get("symbol", ""),
                    "rank": t.get("rank", 0), "price_usd": float(t.get("price") or 0),
                    "volume_24h": float(t.get("volume24hUSD") or 0),
                    "liquidity_usd": float(t.get("liquidity") or 0),
                }
                for t in tokens
            ],
        )

    def token_url(self, mint: str) -> str:
        return f"https://birdeye.so/token/{mint}?chain=solana"


class DexToolsProvider(Provider):
    """DEXTools — paid API, no meaningful free tier.

    Its main differentiator is a proprietary "DEXTscore" audit metric. The
    plan tier is part of the URL path, which is unusual and easy to get wrong.
    """

    name = "dextools"
    display_name = "DEXTools"
    url = "https://dextools.io"
    capabilities = frozenset({Capability.TOKEN_MARKET, Capability.TOKEN_SECURITY})
    tier = Tier.MANUAL
    requires_key = True
    paid = True
    rate_limit_rps = 1.0
    rate_limit_burst = 3
    cache_ttl_s = 120.0

    def __init__(self, config: Any, api_key: str | None = None, plan: str = "trial") -> None:
        super().__init__(config, api_key)
        self.plan = plan
        self.http = ProviderHttp(
            self.name,
            base_url=f"https://public-api.dextools.io/{plan}/v2",
            headers={"X-API-Key": api_key or ""},
            rate_limit_rps=self.rate_limit_rps,
            rate_limit_burst=self.rate_limit_burst,
            timeout_s=10.0,
        )

    async def start(self) -> None:
        if self.available:
            await self.http.start()

    async def close(self) -> None:
        await self.http.close()

    async def get_security(self, mint: str) -> ProviderResult[dict]:
        if not self.api_key:
            return ProviderResult.failure(self.name, "no API key")
        data, meta = await self.http.get_json(
            f"/token/solana/{mint}/audit", cache_ttl_s=self.cache_ttl_s
        )
        self.health.record(bool(data), meta.get("latency_ms", 0.0), meta.get("error") or "")
        if not data:
            return ProviderResult.failure(self.name, meta.get("error") or "no data")

        d = data.get("data") or {}
        return ProviderResult(
            provider=self.name, ok=True, latency_ms=meta.get("latency_ms", 0.0),
            data={
                "is_open_source": d.get("isOpenSource") == "yes",
                "is_honeypot": d.get("isHoneypot") == "yes",
                "is_mintable": d.get("isMintable") == "yes",
                "is_proxy": d.get("isProxy") == "yes",
                "slippage_modifiable": d.get("slippageModifiable") == "yes",
                "is_blacklisted": d.get("isBlacklisted") == "yes",
                "buy_tax": float(d.get("buyTax", {}).get("max") or 0),
                "sell_tax": float(d.get("sellTax", {}).get("max") or 0),
            },
        )

    async def get_market(self, mint: str) -> ProviderResult[dict]:
        if not self.api_key:
            return ProviderResult.failure(self.name, "no API key")
        data, meta = await self.http.get_json(
            f"/token/solana/{mint}/info", cache_ttl_s=self.cache_ttl_s
        )
        if not data:
            return ProviderResult.failure(self.name, meta.get("error") or "no data")
        d = data.get("data") or {}
        return ProviderResult(
            provider=self.name, ok=True, latency_ms=meta.get("latency_ms", 0.0),
            data={
                "market_cap": float(d.get("mcap") or 0),
                "fdv": float(d.get("fdv") or 0),
                "holder_count": int(d.get("holders") or 0),
                "total_supply": float(d.get("totalSupply") or 0),
            },
        )

    def token_url(self, mint: str) -> str:
        return f"https://www.dextools.io/app/en/solana/pair-explorer/{mint}"
