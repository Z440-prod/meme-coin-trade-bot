"""Trading frontends with no public API: Photon, BullX, GMGN.

These three are on the requested integration list, so it is worth being
precise about what they actually are.

**Photon and BullX are trading terminals, not data providers.** They are web
frontends. They publish no API, and the only way to "integrate" them would be
to drive a headless browser against a Cloudflare-protected UI — which breaks
whenever they ship a change, risks an IP ban, and plausibly violates their
terms of service. That is not a foundation to put money on.

**GMGN has undocumented internal endpoints** that circulate in bot
communities. They are not a published API: no versioning, no stability
guarantee, aggressive bot protection, and they change without notice. The
adapter below can use them if you explicitly opt in, and defaults to off.

What you actually lose by not scraping them: very little. Their *data* is
DEX Screener, Birdeye and on-chain state, which MCTB already reads directly.
Their value is UI and one-click execution, and for execution MCTB routes
through Jupiter, which sees the same liquidity they do.

So these are exposed as **deep links** — one keystroke from the terminal to
their view of a token. That is the genuinely useful part, and it never breaks.
"""

from __future__ import annotations

import logging
from typing import Any

from .base import Capability, LinkOnlyProvider, Provider, ProviderResult, Tier
from .http import ProviderHttp

log = logging.getLogger(__name__)


class PhotonProvider(LinkOnlyProvider):
    """Photon (Tinyastro) — fast Solana trading frontend. No public API."""

    name = "photon"
    display_name = "Photon"
    url = "https://photon-sol.tinyastro.io"

    def token_url(self, mint: str) -> str:
        return f"https://photon-sol.tinyastro.io/en/lp/{mint}"

    def wallet_url(self, wallet: str) -> str:
        return f"https://photon-sol.tinyastro.io/en/r/{wallet}"


class BullXProvider(LinkOnlyProvider):
    """BullX — trading terminal. No public API; access is invite-gated."""

    name = "bullx"
    display_name = "BullX"
    url = "https://bullx.io"

    def token_url(self, mint: str) -> str:
        return f"https://neo.bullx.io/terminal?chainId=1399811149&address={mint}"


class GmgnProvider(Provider):
    """GMGN — undocumented internal endpoints, opt-in only.

    Disabled by default. Enable with `providers.gmgn.enabled: true` only if
    you accept that:
      - these endpoints are not a supported API and can vanish without notice
      - GMGN uses bot protection, and requests may be blocked or your IP
        rate-limited
      - nothing here should ever be the sole basis for a trade

    Every consumer treats GMGN as a low-weight input, and the fusion layer
    never lets a single unverified provider decide anything on its own.
    """

    name = "gmgn"
    display_name = "GMGN"
    url = "https://gmgn.ai"
    capabilities = frozenset({Capability.TOKEN_MARKET, Capability.WALLET_PNL})
    tier = Tier.MANUAL
    requires_key = False
    paid = False
    rate_limit_rps = 0.5
    rate_limit_burst = 2
    cache_ttl_s = 120.0

    def __init__(self, config: Any, api_key: str | None = None, enabled: bool = False) -> None:
        super().__init__(config, api_key)
        self.opted_in = enabled
        if not enabled:
            from .base import ProviderStatus

            self.set_status(ProviderStatus.DISABLED)

        self.http = ProviderHttp(
            self.name,
            base_url="https://gmgn.ai",
            headers={
                # Without a browser-like UA these endpoints reject outright.
                "User-Agent": (
                    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/122.0 Safari/537.36"
                ),
                "Accept": "application/json, text/plain, */*",
            },
            rate_limit_rps=self.rate_limit_rps,
            rate_limit_burst=self.rate_limit_burst,
            timeout_s=10.0,
            failure_threshold=3,      # give up quickly; this is best-effort
            recovery_s=300.0,
        )

    async def start(self) -> None:
        if self.opted_in:
            await self.http.start()

    async def close(self) -> None:
        await self.http.close()

    async def get_market(self, mint: str) -> ProviderResult[dict]:
        if not self.opted_in:
            return ProviderResult.failure(self.name, "disabled (opt-in required)")

        data, meta = await self.http.get_json(
            f"/defi/quotation/v1/tokens/sol/{mint}", cache_ttl_s=self.cache_ttl_s
        )
        self.health.record(bool(data), meta.get("latency_ms", 0.0), meta.get("error") or "")
        if not data or data.get("code") != 0:
            return ProviderResult.failure(
                self.name, meta.get("error") or "blocked or changed", meta.get("latency_ms", 0.0)
            )

        d = (data.get("data") or {}).get("token") or {}
        return ProviderResult(
            provider=self.name, ok=True, latency_ms=meta.get("latency_ms", 0.0),
            data={
                "price_usd": float(d.get("price") or 0),
                "liquidity_usd": float(d.get("liquidity") or 0),
                "market_cap": float(d.get("market_cap") or 0),
                "holder_count": int(d.get("holder_count") or 0),
                "symbol": d.get("symbol", ""),
                # GMGN's own risk flags — low confidence, but cheap.
                "is_honeypot": bool(d.get("is_honeypot", False)),
                "renounced": bool(d.get("renounced_mint", False)),
            },
        )

    async def get_wallet_pnl(self, wallet: str) -> ProviderResult[dict]:
        if not self.opted_in:
            return ProviderResult.failure(self.name, "disabled (opt-in required)")
        data, meta = await self.http.get_json(
            f"/defi/quotation/v1/smartmoney/sol/walletNew/{wallet}",
            params={"period": "7d"}, cache_ttl_s=600.0,
        )
        if not data or data.get("code") != 0:
            return ProviderResult.failure(self.name, meta.get("error") or "no data")
        d = data.get("data") or {}
        return ProviderResult(
            provider=self.name, ok=True, latency_ms=meta.get("latency_ms", 0.0),
            data={
                "realized_pnl_usd": float(d.get("realized_profit") or 0),
                "winrate": float(d.get("winrate") or 0),
                "tokens_traded": int(d.get("token_num") or 0),
                "buy_count": int(d.get("buy") or 0),
                "sell_count": int(d.get("sell") or 0),
            },
        )

    def token_url(self, mint: str) -> str:
        return f"https://gmgn.ai/sol/token/{mint}"

    def wallet_url(self, wallet: str) -> str:
        return f"https://gmgn.ai/sol/address/{wallet}"


class SolscanLink(LinkOnlyProvider):
    """Solscan explorer links, available without a Pro key."""

    name = "solscan_link"
    display_name = "Solscan (explorer)"
    url = "https://solscan.io"

    def token_url(self, mint: str) -> str:
        return f"https://solscan.io/token/{mint}"

    def wallet_url(self, wallet: str) -> str:
        return f"https://solscan.io/account/{wallet}"


class PumpFunLink(LinkOnlyProvider):
    name = "pumpfun_link"
    display_name = "pump.fun"
    url = "https://pump.fun"

    def token_url(self, mint: str) -> str:
        return f"https://pump.fun/coin/{mint}"


def all_token_links(mint: str) -> dict[str, str]:
    """Every external view of a token, for the terminal's link row.

    One keystroke to a chart, a bubble map, a security report or a trading
    frontend is a real workflow improvement, and it costs nothing to maintain.
    """
    return {
        "DEX Screener": f"https://dexscreener.com/solana/{mint}",
        "Birdeye": f"https://birdeye.so/token/{mint}?chain=solana",
        "GMGN": f"https://gmgn.ai/sol/token/{mint}",
        "DEXTools": f"https://www.dextools.io/app/en/solana/pair-explorer/{mint}",
        "Photon": f"https://photon-sol.tinyastro.io/en/lp/{mint}",
        "BullX": f"https://neo.bullx.io/terminal?chainId=1399811149&address={mint}",
        "Rugcheck": f"https://rugcheck.xyz/tokens/{mint}",
        "GoPlus": f"https://gopluslabs.io/token-security/solana/{mint}",
        "Bubblemaps": f"https://app.bubblemaps.io/sol/token/{mint}",
        "Solscan": f"https://solscan.io/token/{mint}",
        "Jupiter": f"https://jup.ag/swap/SOL-{mint}",
        "pump.fun": f"https://pump.fun/coin/{mint}",
    }


def all_wallet_links(wallet: str) -> dict[str, str]:
    return {
        "Solscan": f"https://solscan.io/account/{wallet}",
        "GMGN": f"https://gmgn.ai/sol/address/{wallet}",
        "Cielo": f"https://app.cielo.finance/profile/{wallet}",
        "Arkham": f"https://intel.arkm.com/explorer/address/{wallet}",
        "Nansen": f"https://app.nansen.ai/profiler?address={wallet}&chain=solana",
        "Helius XRAY": f"https://xray.helius.xyz/account/{wallet}",
    }
