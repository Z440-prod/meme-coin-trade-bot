"""Execution venues: Jupiter, Raydium, Meteora.

**Jupiter matters more than any other integration in this file.** It is the
Solana swap aggregator: it searches every venue — Raydium, Meteora, Orca,
PumpSwap, Whirlpool and dozens more — and returns the best route with a real
price-impact number attached.

Two concrete advantages over routing everything through pump.fun directly:

1. **Post-graduation coverage.** Once a token completes its bonding curve, the
   pump.fun curve is gone and a curve-only bot simply cannot trade it. Jupiter
   routes to wherever the liquidity actually moved.
2. **A quote is a free honeypot test.** If Jupiter cannot construct a sell
   route for a token you are considering buying, you have just learned
   something extremely important before committing money. `check_sellable()`
   below does exactly this, and it catches things that authority checks miss.

Verified live: `lite-api.jup.ag` is the current keyless host. The older
`quote-api.jup.ag` host is dead and returns nothing.
"""

from __future__ import annotations

import logging
from typing import Any

from .base import Capability, Provider, ProviderResult, Tier
from .http import ProviderHttp

log = logging.getLogger(__name__)

SOL_MINT = "So11111111111111111111111111111111111111112"
USDC_MINT = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
LAMPORTS_PER_SOL = 1_000_000_000


class JupiterProvider(Provider):
    """Jupiter aggregator — quotes, routing, and swap transaction building."""

    name = "jupiter"
    display_name = "Jupiter"
    url = "https://jup.ag"
    capabilities = frozenset(
        {Capability.SWAP_QUOTE, Capability.SWAP_BUILD, Capability.TOKEN_PRICE}
    )
    tier = Tier.WARM
    requires_key = False
    paid = False
    # The keyless lite host is rate limited; a paid key raises this a lot.
    rate_limit_rps = 3.0
    rate_limit_burst = 8
    # Quotes go stale almost immediately — this is a very short cache purely
    # to coalesce duplicate asks within the same decision.
    cache_ttl_s = 2.0

    LITE_HOST = "https://lite-api.jup.ag"
    PRO_HOST = "https://api.jup.ag"

    def __init__(self, config: Any, api_key: str | None = None) -> None:
        super().__init__(config, api_key)
        # A paid key unlocks the higher-throughput host.
        host = self.PRO_HOST if api_key else self.LITE_HOST
        headers = {"x-api-key": api_key} if api_key else {}
        if api_key:
            self.rate_limit_rps = 20.0
            self.rate_limit_burst = 40

        self.http = ProviderHttp(
            self.name,
            base_url=host,
            headers=headers,
            rate_limit_rps=self.rate_limit_rps,
            rate_limit_burst=self.rate_limit_burst,
            timeout_s=6.0,
            max_connections=30,
        )

    async def start(self) -> None:
        await self.http.start()
        # Warm the TLS connection now so the first real quote does not pay for
        # a handshake at the moment it matters.
        await self.http.warm("/swap/v1/quote")

    async def close(self) -> None:
        await self.http.close()

    async def probe(self) -> bool:
        result = await self.get_quote(SOL_MINT, USDC_MINT, LAMPORTS_PER_SOL // 10)
        return result.usable

    # -- quoting ------------------------------------------------------------

    async def get_quote(
        self,
        input_mint: str,
        output_mint: str,
        amount: int,
        slippage_bps: int = 300,
        *,
        only_direct: bool = False,
        max_accounts: int | None = None,
        cache_ttl_s: float | None = None,
    ) -> ProviderResult[dict]:
        """Best route for an exact-in swap. `amount` is in the input mint's
        smallest unit (lamports for SOL)."""
        params: dict[str, Any] = {
            "inputMint": input_mint,
            "outputMint": output_mint,
            "amount": int(amount),
            "slippageBps": int(slippage_bps),
        }
        if only_direct:
            # Direct routes are simpler and land more reliably under
            # congestion, at the cost of a possibly worse price.
            params["onlyDirectRoutes"] = "true"
        if max_accounts:
            # Smaller transactions are cheaper and less likely to exceed limits.
            params["maxAccounts"] = max_accounts

        data, meta = await self.http.get_json(
            "/swap/v1/quote",
            params=params,
            cache_ttl_s=self.cache_ttl_s if cache_ttl_s is None else cache_ttl_s,
        )
        self.health.record(bool(data), meta.get("latency_ms", 0.0), meta.get("error") or "")

        if not data or "outAmount" not in data:
            return ProviderResult.failure(
                self.name,
                meta.get("error") or "no route found",
                meta.get("latency_ms", 0.0),
            )

        route_plan = data.get("routePlan") or []
        return ProviderResult(
            provider=self.name,
            ok=True,
            latency_ms=meta.get("latency_ms", 0.0),
            cached=meta.get("cached", False),
            data={
                "in_amount": int(data.get("inAmount") or 0),
                "out_amount": int(data.get("outAmount") or 0),
                "min_out_amount": int(data.get("otherAmountThreshold") or 0),
                "price_impact_pct": float(data.get("priceImpactPct") or 0) * 100.0,
                "slippage_bps": int(data.get("slippageBps") or slippage_bps),
                "route_hops": len(route_plan),
                "route_labels": [
                    (hop.get("swapInfo") or {}).get("label", "?") for hop in route_plan
                ],
                "raw_quote": data,  # needed verbatim by the swap builder
            },
        )

    async def check_sellable(
        self, mint: str, token_amount: int, slippage_bps: int = 1000
    ) -> ProviderResult[dict]:
        """Can this token actually be sold right now, and for how much?

        A pure honeypot check that costs one quote and no gas. Authority flags
        tell you whether the *deployer* can stop you selling; this tells you
        whether the *market* can absorb it. Both fail modes lose you the
        position, and this one is invisible to a contract scan.
        """
        result = await self.get_quote(mint, SOL_MINT, token_amount, slippage_bps)
        if not result.usable:
            return ProviderResult(
                provider=self.name,
                ok=True,
                data={
                    "sellable": False,
                    "reason": result.error or "no sell route exists",
                    "sol_out": 0.0,
                    "price_impact_pct": 100.0,
                },
                latency_ms=result.latency_ms,
            )

        quote = result.data or {}
        return ProviderResult(
            provider=self.name,
            ok=True,
            latency_ms=result.latency_ms,
            data={
                "sellable": True,
                "reason": "",
                "sol_out": quote["out_amount"] / LAMPORTS_PER_SOL,
                "price_impact_pct": quote["price_impact_pct"],
                "route_hops": quote["route_hops"],
                "route_labels": quote["route_labels"],
            },
        )

    async def round_trip_cost_pct(self, mint: str, sol_amount: float) -> ProviderResult[dict]:
        """What buying and immediately selling would cost, as a percentage.

        This is the true cost of entering a position — fees plus price impact
        in both directions. On a thin token it can exceed 20%, which means the
        trade needs a 20% move just to break even. Worth knowing *before*
        committing, not after.
        """
        lamports = int(sol_amount * LAMPORTS_PER_SOL)
        buy = await self.get_quote(SOL_MINT, mint, lamports)
        if not buy.usable:
            return ProviderResult.failure(self.name, f"no buy route: {buy.error}")

        tokens_out = (buy.data or {})["out_amount"]
        sell = await self.get_quote(mint, SOL_MINT, tokens_out)
        if not sell.usable:
            return ProviderResult(
                provider=self.name,
                ok=True,
                data={
                    "round_trip_cost_pct": 100.0,
                    "sellable": False,
                    "reason": "buy route exists but sell route does not — likely honeypot",
                },
            )

        sol_back = (sell.data or {})["out_amount"] / LAMPORTS_PER_SOL
        cost_pct = (sol_amount - sol_back) / sol_amount * 100.0 if sol_amount else 100.0

        return ProviderResult(
            provider=self.name,
            ok=True,
            data={
                "round_trip_cost_pct": cost_pct,
                "sellable": True,
                "sol_in": sol_amount,
                "sol_back": sol_back,
                "buy_impact_pct": (buy.data or {})["price_impact_pct"],
                "sell_impact_pct": (sell.data or {})["price_impact_pct"],
                "buy_route": (buy.data or {})["route_labels"],
                "sell_route": (sell.data or {})["route_labels"],
            },
        )

    async def build_swap(
        self,
        raw_quote: dict,
        user_public_key: str,
        *,
        priority_fee_lamports: int = 100_000,
        wrap_unwrap_sol: bool = True,
        dynamic_slippage: bool = False,
    ) -> ProviderResult[dict]:
        """Turn a quote into an unsigned transaction.

        Non-custodial: Jupiter returns base64 transaction bytes which we sign
        locally. The key never leaves the Brain.
        """
        body: dict[str, Any] = {
            "quoteResponse": raw_quote,
            "userPublicKey": user_public_key,
            "wrapAndUnwrapSol": wrap_unwrap_sol,
            "dynamicComputeUnitLimit": True,
            "prioritizationFeeLamports": {
                "priorityLevelWithMaxLamports": {
                    "maxLamports": int(priority_fee_lamports),
                    "priorityLevel": "high",
                }
            },
        }
        if dynamic_slippage:
            body["dynamicSlippage"] = True

        data, meta = await self.http.post_json("/swap/v1/swap", json_body=body)
        if not data or "swapTransaction" not in data:
            return ProviderResult.failure(
                self.name, meta.get("error") or "swap build failed", meta.get("latency_ms", 0.0)
            )

        return ProviderResult(
            provider=self.name,
            ok=True,
            latency_ms=meta.get("latency_ms", 0.0),
            data={
                "swap_transaction": data["swapTransaction"],
                "last_valid_block_height": data.get("lastValidBlockHeight"),
                "priority_fee_lamports": data.get("prioritizationFeeLamports"),
            },
        )

    async def get_prices(self, mints: list[str]) -> ProviderResult[dict[str, float]]:
        """Batch USD prices. Cheap and useful for watchlist marking."""
        if not mints:
            return ProviderResult(provider=self.name, ok=True, data={})
        out: dict[str, float] = {}
        for i in range(0, len(mints), 100):
            chunk = mints[i : i + 100]
            data, _meta = await self.http.get_json(
                "/price/v3", params={"ids": ",".join(chunk)}, cache_ttl_s=10.0
            )
            for mint, row in ((data or {}).items() if isinstance(data, dict) else []):
                if isinstance(row, dict) and row.get("usdPrice") is not None:
                    out[mint] = float(row["usdPrice"])
        return ProviderResult(provider=self.name, ok=True, data=out)

    def token_url(self, mint: str) -> str:
        return f"https://jup.ag/swap/SOL-{mint}"


class RaydiumProvider(Provider):
    """Raydium — pool metadata and token info. Free, keyless."""

    name = "raydium"
    display_name = "Raydium"
    url = "https://raydium.io"
    capabilities = frozenset({Capability.POOL_INFO, Capability.TOKEN_METADATA})
    tier = Tier.COLD
    requires_key = False
    paid = False
    rate_limit_rps = 2.0
    rate_limit_burst = 5
    cache_ttl_s = 60.0

    def __init__(self, config: Any, api_key: str | None = None) -> None:
        super().__init__(config, api_key)
        self.http = ProviderHttp(
            self.name,
            base_url="https://api-v3.raydium.io",
            rate_limit_rps=self.rate_limit_rps,
            rate_limit_burst=self.rate_limit_burst,
            timeout_s=8.0,
        )

    async def start(self) -> None:
        await self.http.start()

    async def close(self) -> None:
        await self.http.close()

    async def get_pool_info(self, mint: str) -> ProviderResult[dict]:
        data, meta = await self.http.get_json(
            "/pools/info/mint",
            params={"mint1": mint, "poolType": "all", "poolSortField": "liquidity",
                    "sortType": "desc", "pageSize": 10, "page": 1},
            cache_ttl_s=self.cache_ttl_s,
        )
        self.health.record(bool(data), meta.get("latency_ms", 0.0), meta.get("error") or "")
        if not data or not data.get("success"):
            return ProviderResult.failure(self.name, meta.get("error") or "no pools")

        pools = ((data.get("data") or {}).get("data")) or []
        if not pools:
            return ProviderResult.failure(self.name, "no pools for mint")

        best = max(pools, key=lambda p: float(p.get("tvl") or 0))
        return ProviderResult(
            provider=self.name,
            ok=True,
            latency_ms=meta.get("latency_ms", 0.0),
            data={
                "pool_id": best.get("id", ""),
                "pool_type": best.get("type", ""),
                "tvl_usd": float(best.get("tvl") or 0),
                "volume_24h": float((best.get("day") or {}).get("volume") or 0),
                "fee_rate": float(best.get("feeRate") or 0),
                "pool_count": len(pools),
            },
        )

    async def get_token_metadata(self, mints: list[str]) -> ProviderResult[dict[str, dict]]:
        data, meta = await self.http.get_json(
            "/mint/ids", params={"mints": ",".join(mints[:50])}, cache_ttl_s=600.0
        )
        if not data or not data.get("success"):
            return ProviderResult.failure(self.name, meta.get("error") or "no data")
        out = {
            row["address"]: {
                "symbol": row.get("symbol", ""),
                "name": row.get("name", ""),
                "decimals": row.get("decimals", 6),
                "logo": row.get("logoURI", ""),
                "tags": row.get("tags", []),
            }
            for row in (data.get("data") or [])
            if row and row.get("address")
        }
        return ProviderResult(provider=self.name, ok=True, data=out,
                              latency_ms=meta.get("latency_ms", 0.0))

    def token_url(self, mint: str) -> str:
        return f"https://raydium.io/swap/?inputMint=sol&outputMint={mint}"


class MeteoraProvider(Provider):
    """Meteora DLMM — pool data. Free, keyless.

    Verified host is `dlmm.datapi.meteora.ag`; the older `dlmm-api.meteora.ag`
    paths now 404.
    """

    name = "meteora"
    display_name = "Meteora"
    url = "https://meteora.ag"
    capabilities = frozenset({Capability.POOL_INFO})
    tier = Tier.COLD
    requires_key = False
    paid = False
    rate_limit_rps = 3.0
    rate_limit_burst = 8
    cache_ttl_s = 60.0

    def __init__(self, config: Any, api_key: str | None = None) -> None:
        super().__init__(config, api_key)
        self.http = ProviderHttp(
            self.name,
            base_url="https://dlmm.datapi.meteora.ag",
            rate_limit_rps=self.rate_limit_rps,
            rate_limit_burst=self.rate_limit_burst,
            timeout_s=8.0,
        )

    async def start(self) -> None:
        await self.http.start()

    async def close(self) -> None:
        await self.http.close()

    async def get_pool_info(self, mint: str) -> ProviderResult[dict]:
        data, meta = await self.http.get_json(
            "/pools", params={"search_term": mint, "limit": 10}, cache_ttl_s=self.cache_ttl_s
        )
        self.health.record(bool(data), meta.get("latency_ms", 0.0), meta.get("error") or "")
        if not data:
            return ProviderResult.failure(self.name, meta.get("error") or "no data")

        pools = data.get("data") or []
        relevant = [
            p for p in pools
            if mint in ((p.get("token_x") or {}).get("address", ""),
                        (p.get("token_y") or {}).get("address", ""))
        ]
        if not relevant:
            return ProviderResult.failure(self.name, "no DLMM pool for mint")

        best = max(relevant, key=lambda p: float(p.get("liquidity") or 0))
        return ProviderResult(
            provider=self.name,
            ok=True,
            latency_ms=meta.get("latency_ms", 0.0),
            data={
                "pool_address": best.get("address", ""),
                "name": best.get("name", ""),
                "liquidity_usd": float(best.get("liquidity") or 0),
                "volume_24h": float(best.get("volume_24h") or 0),
                "fees_24h": float(best.get("fees_24h") or 0),
                "bin_step": best.get("bin_step"),
                "pool_count": len(relevant),
            },
        )

    async def protocol_metrics(self) -> ProviderResult[dict]:
        data, meta = await self.http.get_json("/stats/protocol_metrics", cache_ttl_s=300.0)
        if not data:
            return ProviderResult.failure(self.name, meta.get("error") or "no data")
        return ProviderResult(provider=self.name, ok=True, data=data,
                              latency_ms=meta.get("latency_ms", 0.0))

    def token_url(self, mint: str) -> str:
        return f"https://app.meteora.ag/dlmm?token={mint}"
