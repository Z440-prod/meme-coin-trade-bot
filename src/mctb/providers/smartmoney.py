"""Smart-money providers: Nansen, Arkham, Cielo, Bubblemaps.

A caution that applies to this entire category, and that no vendor will tell
you: **a public "smart money" label is a crowded trade.** If a service has
labelled a wallet and sells that label to thousands of subscribers, everyone
copying it arrives at the same time, and the wallet's edge is diluted or
inverted. There are wallets whose business model is building a clean public
record specifically to attract copy-traders they can then dump on.

So these providers are used as *inputs to your own scoring*, never as verdicts.
MCTB's own wallet reputation — built from realised P&L on trades you actually
copied — always outranks a third-party label. See `scout/wallets.py`.

Where they genuinely help is negative labelling: knowing an address is an
exchange, a bridge, a market maker or a known bot is unambiguously useful and
not crowded, because nobody is trying to front-run "this is Binance".
"""

from __future__ import annotations

import logging
from typing import Any

from .base import Capability, LinkOnlyProvider, Provider, ProviderResult, Tier
from .http import ProviderHttp

log = logging.getLogger(__name__)


class CieloProvider(Provider):
    """Cielo — wallet tracking and PnL feeds. Key required, has a free tier.

    The most practical of the smart-money services for this use case: it is
    Solana-first, its feed endpoint gives real-time wallet activity, and its
    pricing is approachable.
    """

    name = "cielo"
    display_name = "Cielo"
    url = "https://cielo.finance"
    capabilities = frozenset(
        {Capability.WALLET_PNL, Capability.WALLET_ACTIVITY, Capability.WALLET_LABELS}
    )
    tier = Tier.COLD
    requires_key = True
    paid = True
    rate_limit_rps = 2.0
    rate_limit_burst = 5
    cache_ttl_s = 60.0

    def __init__(self, config: Any, api_key: str | None = None) -> None:
        super().__init__(config, api_key)
        self.http = ProviderHttp(
            self.name,
            base_url="https://feed-api.cielo.finance/api/v1",
            headers={"X-API-KEY": api_key or ""},
            rate_limit_rps=self.rate_limit_rps,
            rate_limit_burst=self.rate_limit_burst,
            timeout_s=10.0,
        )

    async def start(self) -> None:
        if self.available:
            await self.http.start()

    async def close(self) -> None:
        await self.http.close()

    async def get_wallet_pnl(self, wallet: str) -> ProviderResult[dict]:
        if not self.api_key:
            return ProviderResult.failure(self.name, "no API key")
        data, meta = await self.http.get_json(
            f"/{wallet}/pnl/total-stats", params={"chains": "solana"},
            cache_ttl_s=300.0,
        )
        self.health.record(bool(data), meta.get("latency_ms", 0.0), meta.get("error") or "")
        if not data:
            return ProviderResult.failure(self.name, meta.get("error") or "no data")

        d = (data.get("data") or {}) if isinstance(data, dict) else {}
        return ProviderResult(
            provider=self.name, ok=True, latency_ms=meta.get("latency_ms", 0.0),
            data={
                "realized_pnl_usd": float(d.get("realized_pnl_usd") or 0),
                "realized_roi_pct": float(d.get("realized_roi_percentage") or 0),
                "winrate": float(d.get("winrate") or 0),
                "tokens_traded": int(d.get("tokens_traded") or 0),
                "unrealized_pnl_usd": float(d.get("unrealized_pnl_usd") or 0),
                "combined_pnl_usd": float(d.get("combined_pnl_usd") or 0),
            },
        )

    async def get_wallet_activity(self, wallet: str, limit: int = 50) -> ProviderResult[list[dict]]:
        if not self.api_key:
            return ProviderResult.failure(self.name, "no API key")
        data, meta = await self.http.get_json(
            "/feed", params={"wallet": wallet, "chains": "solana", "limit": min(limit, 100)},
            cache_ttl_s=self.cache_ttl_s,
        )
        if not data:
            return ProviderResult.failure(self.name, meta.get("error") or "no data")
        items = (data.get("data") or {}).get("items") or []
        return ProviderResult(
            provider=self.name, ok=True, latency_ms=meta.get("latency_ms", 0.0),
            data=[
                {
                    "tx_type": i.get("tx_type", ""),
                    "timestamp": int(i.get("timestamp") or 0),
                    "token0_symbol": i.get("token0_symbol", ""),
                    "token1_symbol": i.get("token1_symbol", ""),
                    "token0_address": i.get("token0_address", ""),
                    "token1_address": i.get("token1_address", ""),
                    "value_usd": float(i.get("token0_amount_usd") or 0),
                }
                for i in items
            ],
        )

    def wallet_url(self, wallet: str) -> str:
        return f"https://app.cielo.finance/profile/{wallet}"


class NansenProvider(Provider):
    """Nansen — wallet labelling and smart-money flows. Expensive.

    Best-in-class labelling, priced accordingly. Its real value here is the
    label taxonomy (exchange, fund, bot, bridge), not the "smart money"
    designation — see the module docstring on why crowded labels decay.
    """

    name = "nansen"
    display_name = "Nansen"
    url = "https://nansen.ai"
    capabilities = frozenset(
        {Capability.WALLET_LABELS, Capability.WALLET_PNL, Capability.TOKEN_HOLDERS}
    )
    tier = Tier.MANUAL
    requires_key = True
    paid = True
    rate_limit_rps = 1.0
    rate_limit_burst = 3
    cache_ttl_s = 600.0

    def __init__(self, config: Any, api_key: str | None = None) -> None:
        super().__init__(config, api_key)
        self.http = ProviderHttp(
            self.name,
            base_url="https://api.nansen.ai/api/beta",
            headers={"apiKey": api_key or "", "Content-Type": "application/json"},
            rate_limit_rps=self.rate_limit_rps,
            rate_limit_burst=self.rate_limit_burst,
            timeout_s=15.0,
        )

    async def start(self) -> None:
        if self.available:
            await self.http.start()

    async def close(self) -> None:
        await self.http.close()

    async def get_wallet_labels(self, wallets: list[str]) -> ProviderResult[dict[str, str]]:
        if not self.api_key:
            return ProviderResult.failure(self.name, "no API key")
        data, meta = await self.http.post_json(
            "/profiler/address/labels",
            json_body={"parameters": {"addresses": wallets[:50], "chain": "solana"}},
        )
        self.health.record(bool(data), meta.get("latency_ms", 0.0), meta.get("error") or "")
        if not data:
            return ProviderResult.failure(self.name, meta.get("error") or "no data")

        out: dict[str, str] = {}
        rows = data if isinstance(data, list) else (data.get("data") or [])
        for row in rows:
            address = row.get("address") or row.get("wallet_address")
            labels = row.get("labels") or row.get("label") or []
            if address:
                out[address] = ", ".join(labels) if isinstance(labels, list) else str(labels)
        return ProviderResult(
            provider=self.name, ok=True, data=out, latency_ms=meta.get("latency_ms", 0.0)
        )

    def wallet_url(self, wallet: str) -> str:
        return f"https://app.nansen.ai/profiler?address={wallet}&chain=solana"


class ArkhamProvider(Provider):
    """Arkham Intelligence — entity attribution. Key required."""

    name = "arkham"
    display_name = "Arkham"
    url = "https://arkhamintelligence.com"
    capabilities = frozenset({Capability.WALLET_LABELS, Capability.WALLET_CLUSTER})
    tier = Tier.MANUAL
    requires_key = True
    paid = True
    rate_limit_rps = 1.0
    rate_limit_burst = 3
    cache_ttl_s = 600.0

    def __init__(self, config: Any, api_key: str | None = None) -> None:
        super().__init__(config, api_key)
        self.http = ProviderHttp(
            self.name,
            base_url="https://api.arkm.com",
            headers={"API-Key": api_key or ""},
            rate_limit_rps=self.rate_limit_rps,
            rate_limit_burst=self.rate_limit_burst,
            timeout_s=15.0,
        )

    async def start(self) -> None:
        if self.available:
            await self.http.start()

    async def close(self) -> None:
        await self.http.close()

    async def get_wallet_labels(self, wallets: list[str]) -> ProviderResult[dict[str, str]]:
        """Arkham's entity attribution — which real-world entity owns an address.

        This is genuinely complementary to our own clustering: we infer
        clusters from on-chain funding behaviour, Arkham adds off-chain
        attribution we could never derive ourselves.
        """
        if not self.api_key:
            return ProviderResult.failure(self.name, "no API key")

        out: dict[str, str] = {}
        latency = 0.0
        for wallet in wallets[:20]:
            data, meta = await self.http.get_json(
                f"/intelligence/address/{wallet}",
                params={"chain": "solana"},
                cache_ttl_s=self.cache_ttl_s,
            )
            latency += meta.get("latency_ms", 0.0)
            if not data:
                continue
            entity = data.get("arkhamEntity") or {}
            label = data.get("arkhamLabel") or {}
            name = entity.get("name") or label.get("name")
            if name:
                out[wallet] = f"{name} ({entity.get('type', 'entity')})"

        self.health.record(bool(out), latency)
        return ProviderResult(provider=self.name, ok=True, data=out, latency_ms=latency)

    def wallet_url(self, wallet: str) -> str:
        return f"https://intel.arkm.com/explorer/address/{wallet}"


class BubblemapsProvider(LinkOnlyProvider):
    """Bubblemaps — holder cluster visualisation.

    Its public data endpoint was unreachable when this was written (HTTP 500),
    and its documented programmatic access is enterprise-tier. Rather than
    ship a fragile scraper, this is a link-out.

    **You are not losing much.** Bubblemaps visualises wallet clustering, and
    `intel/graph.py` computes clustering from funding trails directly — which
    is the same insight, computed locally, with no rate limit and no
    dependency. The link is here because the visual is genuinely good for a
    human sanity check before a large manual buy.
    """

    name = "bubblemaps"
    display_name = "Bubblemaps"
    url = "https://bubblemaps.io"

    def token_url(self, mint: str) -> str:
        return f"https://app.bubblemaps.io/sol/token/{mint}"
