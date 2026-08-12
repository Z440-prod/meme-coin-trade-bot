"""Security providers: GoPlus, Rugcheck, Token Sniffer.

Two of these three are free and keyless, and together they are the highest
value-per-line integration in the whole system. Most money lost to memecoin
bots is lost to tokens that were never sellable — not to bad price
predictions — and these services exist specifically to detect that.

They are deliberately kept as separate providers rather than merged, because
**disagreement between them is itself a signal**. When GoPlus says a mint is
clean and Rugcheck flags it, that is worth knowing; `intel/security.py` fuses
them and records which source said what.
"""

from __future__ import annotations

import logging
from typing import Any

from .base import Capability, Provider, ProviderResult, Tier
from .http import ProviderHttp

log = logging.getLogger(__name__)


class GoPlusProvider(Provider):
    """GoPlus Security — free, keyless, no registration.

    Returns the full Solana token-security object: mint/freeze/close
    authorities, metadata mutability, transfer hooks, DEX liquidity and
    holder breakdown. Verified live against the public endpoint.
    """

    name = "goplus"
    display_name = "GoPlus Security"
    url = "https://gopluslabs.io"
    capabilities = frozenset(
        {Capability.TOKEN_SECURITY, Capability.HOLDER_DISTRIBUTION, Capability.LP_STATUS}
    )
    tier = Tier.WARM
    requires_key = False
    paid = False
    rate_limit_rps = 2.0
    rate_limit_burst = 6
    # Authorities rarely change, so this can be cached hard.
    cache_ttl_s = 300.0

    def __init__(self, config: Any, api_key: str | None = None) -> None:
        super().__init__(config, api_key)
        self.http = ProviderHttp(
            self.name,
            base_url="https://api.gopluslabs.io",
            rate_limit_rps=self.rate_limit_rps,
            rate_limit_burst=self.rate_limit_burst,
            timeout_s=8.0,
        )

    async def start(self) -> None:
        await self.http.start()

    async def close(self) -> None:
        await self.http.close()

    async def get_security(self, mint: str) -> ProviderResult[dict]:
        data, meta = await self.http.get_json(
            "/api/v1/solana/token_security",
            params={"contract_addresses": mint},
            cache_ttl_s=self.cache_ttl_s,
            max_stale_s=600.0,
        )
        self.health.record(bool(data), meta.get("latency_ms", 0.0), meta.get("error") or "")

        if not data or data.get("code") != 1:
            return ProviderResult.failure(
                self.name,
                meta.get("error") or f"code={data.get('code') if data else 'none'}",
                meta.get("latency_ms", 0.0),
            )

        raw = (data.get("result") or {}).get(mint)
        if not raw:
            return ProviderResult.failure(self.name, "mint not in response")

        def _status_clean(field: str) -> bool:
            """GoPlus encodes authority presence as status "0" (absent/safe)
            or "1" (present/risky). Absent means revoked."""
            node = raw.get(field) or {}
            return str(node.get("status", "0")) == "0"

        holders = raw.get("holders") or []
        top10 = sum(float(h.get("percent") or 0) for h in holders[:10])

        dex = raw.get("dex") or []
        total_tvl = sum(float(d.get("tvl") or 0) for d in dex)
        # burn_percent can legitimately be null; only treat an explicit high
        # value as burned.
        lp_burned = any(
            (d.get("burn_percent") is not None and float(d["burn_percent"]) >= 90)
            for d in dex
        )

        return ProviderResult(
            provider=self.name,
            ok=True,
            latency_ms=meta.get("latency_ms", 0.0),
            cached=meta.get("cached", False),
            data={
                "mint_authority_revoked": _status_clean("mintable"),
                "freeze_authority_revoked": _status_clean("freezable"),
                # Solana-specific extras that EVM-oriented tools miss:
                "closable": not _status_clean("closable"),
                "balance_mutable": not _status_clean("balance_mutable_authority"),
                "metadata_mutable": not _status_clean("metadata_mutable"),
                "transfer_hook_upgradable": not _status_clean("transfer_hook_upgrade"),
                "non_transferable": str(raw.get("non_transferable", "0")) == "1",
                "default_account_state_frozen": str(raw.get("default_account_state", "0")) == "2",
                "top10_holding_pct": top10 / 100.0 if top10 > 1 else top10,
                "holder_count": int(raw.get("holder_count") or 0) or len(holders),
                "total_supply": float(raw.get("total_supply") or 0),
                "lp_burned": lp_burned,
                "dex_count": len(dex),
                "total_tvl_usd": total_tvl,
                "creators": raw.get("creators") or [],
                "trusted_token": str(raw.get("trusted_token", "0")) == "1",
            },
        )

    def token_url(self, mint: str) -> str:
        return f"https://gopluslabs.io/token-security/solana/{mint}"


class RugcheckProvider(Provider):
    """Rugcheck — free tier, keyless, Solana-native.

    Produces a numeric risk score plus itemised risk findings, and crucially
    reports LP lock percentage and insider/holder analysis that GoPlus does
    not. Verified live.
    """

    name = "rugcheck"
    display_name = "Rugcheck"
    url = "https://rugcheck.xyz"
    capabilities = frozenset(
        {Capability.TOKEN_SECURITY, Capability.HOLDER_DISTRIBUTION, Capability.LP_STATUS}
    )
    tier = Tier.WARM
    requires_key = False
    paid = False
    rate_limit_rps = 2.0
    rate_limit_burst = 5
    cache_ttl_s = 180.0

    def __init__(self, config: Any, api_key: str | None = None) -> None:
        super().__init__(config, api_key)
        headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
        self.http = ProviderHttp(
            self.name,
            base_url="https://api.rugcheck.xyz",
            headers=headers,
            rate_limit_rps=self.rate_limit_rps,
            rate_limit_burst=self.rate_limit_burst,
            timeout_s=8.0,
        )

    async def start(self) -> None:
        await self.http.start()

    async def close(self) -> None:
        await self.http.close()

    async def get_security(self, mint: str) -> ProviderResult[dict]:
        data, meta = await self.http.get_json(
            f"/v1/tokens/{mint}/report",
            cache_ttl_s=self.cache_ttl_s,
            max_stale_s=600.0,
        )
        self.health.record(bool(data), meta.get("latency_ms", 0.0), meta.get("error") or "")
        if not data:
            return ProviderResult.failure(
                self.name, meta.get("error") or "no report", meta.get("latency_ms", 0.0)
            )

        token = data.get("token") or {}
        risks = data.get("risks") or []

        # Rugcheck's raw `score` is inverted (higher = riskier) and unbounded.
        # `score_normalised` is 0-100. Prefer it, fall back to a clamp.
        normalised = data.get("score_normalised")
        risk_score = (
            float(normalised)
            if normalised is not None
            else min(100.0, float(data.get("score") or 0) / 10.0)
        )

        markets = data.get("markets") or []
        lp_locked_pct = float(data.get("lpLockedPct") or 0)

        top_holders = data.get("topHolders") or []
        top10 = sum(float(h.get("pct") or 0) for h in top_holders[:10])

        return ProviderResult(
            provider=self.name,
            ok=True,
            latency_ms=meta.get("latency_ms", 0.0),
            cached=meta.get("cached", False),
            data={
                "risk_score": risk_score,
                "risks": [
                    {
                        "name": r.get("name", ""),
                        "description": (r.get("description") or "")[:200],
                        "level": r.get("level", ""),
                        "score": r.get("score", 0),
                    }
                    for r in risks
                ],
                "risk_names": [r.get("name", "") for r in risks],
                "danger_count": sum(1 for r in risks if r.get("level") == "danger"),
                "warn_count": sum(1 for r in risks if r.get("level") == "warn"),
                "mint_authority_revoked": token.get("mintAuthority") is None,
                "freeze_authority_revoked": token.get("freezeAuthority") is None,
                "creator": data.get("creator", ""),
                "creator_balance": float(data.get("creatorBalance") or 0),
                "supply": float(token.get("supply") or 0),
                "decimals": int(token.get("decimals") or 6),
                "lp_locked_pct": lp_locked_pct / 100.0 if lp_locked_pct > 1 else lp_locked_pct,
                "top10_holding_pct": top10 / 100.0 if top10 > 1 else top10,
                "total_holders": int(data.get("totalHolders") or 0),
                "market_count": len(markets),
                "rugged": bool(data.get("rugged", False)),
                # Token-2022 extensions that can silently break selling.
                "transfer_fee": (data.get("token_extensions") or {}).get("transferFeeConfig")
                is not None,
                "permanent_delegate": (data.get("token_extensions") or {}).get(
                    "permanentDelegate"
                )
                is not None,
                "verified": bool((data.get("verification") or {}).get("jup_verified", False)),
            },
        )

    def token_url(self, mint: str) -> str:
        return f"https://rugcheck.xyz/tokens/{mint}"


class TokenSnifferProvider(Provider):
    """Token Sniffer — API key required, primarily EVM-focused.

    Included for completeness. Its Solana coverage is thinner than GoPlus or
    Rugcheck, so it is weighted low in fusion and is not worth paying for
    unless you also trade EVM chains.
    """

    name = "tokensniffer"
    display_name = "Token Sniffer"
    url = "https://tokensniffer.com"
    capabilities = frozenset({Capability.TOKEN_SECURITY})
    tier = Tier.COLD
    requires_key = True
    paid = True
    rate_limit_rps = 1.0
    rate_limit_burst = 3
    cache_ttl_s = 600.0

    def __init__(self, config: Any, api_key: str | None = None) -> None:
        super().__init__(config, api_key)
        self.http = ProviderHttp(
            self.name,
            base_url="https://tokensniffer.com",
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
            f"/api/v2/tokens/solana/{mint}",
            params={"apikey": self.api_key, "include_metrics": "true"},
            cache_ttl_s=self.cache_ttl_s,
        )
        self.health.record(bool(data), meta.get("latency_ms", 0.0), meta.get("error") or "")
        if not data:
            return ProviderResult.failure(
                self.name, meta.get("error") or "no data", meta.get("latency_ms", 0.0)
            )

        # Token Sniffer's score runs 0-100 with HIGHER meaning safer, the
        # opposite of Rugcheck. Invert so every security provider in this
        # module reports risk in the same direction.
        raw_score = float(data.get("score") or 0)
        return ProviderResult(
            provider=self.name,
            ok=True,
            latency_ms=meta.get("latency_ms", 0.0),
            data={
                "risk_score": max(0.0, 100.0 - raw_score),
                "safety_score": raw_score,
                "is_flagged": bool(data.get("is_flagged", False)),
                "risk_names": [t.get("id", "") for t in (data.get("tests") or []) if not t.get("result", True)],
                "swap_simulation_ok": bool((data.get("swap_simulation") or {}).get("is_sellable", True)),
            },
        )

    def token_url(self, mint: str) -> str:
        return f"https://tokensniffer.com/token/solana/{mint}"
