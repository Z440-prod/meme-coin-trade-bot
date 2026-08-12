"""On-chain data providers: Helius, Solscan, Dune.

**Helius is the one worth having.** Its free tier is generous, it doubles as
your RPC endpoint (removing the public-endpoint rate limiting that otherwise
costs you fills), and its parsed-transaction API turns raw Solana instruction
data into readable swaps — which is what makes the wallet-funding graph in
`intel/graph.py` practical rather than a research project.

Solscan and Dune are useful but optional. Dune in particular is a batch
analytics tool, not a real-time source: queries take seconds to minutes, so it
runs on a slow background schedule and never touches a trading decision.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from .base import Capability, Provider, ProviderResult, Tier
from .http import ProviderHttp

log = logging.getLogger(__name__)


class HeliusProvider(Provider):
    """Helius — parsed transactions, DAS metadata, wallet history."""

    name = "helius"
    display_name = "Helius"
    url = "https://helius.dev"
    capabilities = frozenset(
        {
            Capability.TX_PARSE,
            Capability.TX_HISTORY,
            Capability.WALLET_ACTIVITY,
            Capability.TOKEN_METADATA,
            Capability.TOKEN_HOLDERS,
        }
    )
    tier = Tier.COLD
    requires_key = True
    paid = False  # free tier is genuinely usable
    rate_limit_rps = 8.0
    rate_limit_burst = 20
    cache_ttl_s = 60.0

    def __init__(self, config: Any, api_key: str | None = None) -> None:
        super().__init__(config, api_key)
        self.http = ProviderHttp(
            self.name,
            base_url="https://api.helius.xyz",
            rate_limit_rps=self.rate_limit_rps,
            rate_limit_burst=self.rate_limit_burst,
            timeout_s=12.0,
        )
        self.rpc_http = ProviderHttp(
            f"{self.name}-rpc",
            base_url="https://mainnet.helius-rpc.com",
            rate_limit_rps=self.rate_limit_rps,
            rate_limit_burst=self.rate_limit_burst,
            timeout_s=12.0,
        )

    async def start(self) -> None:
        if self.available:
            await self.http.start()
            await self.rpc_http.start()

    async def close(self) -> None:
        await self.http.close()
        await self.rpc_http.close()

    @property
    def rpc_url(self) -> str:
        """Usable directly as MCTB_RPC_URL — one key covers both."""
        return f"https://mainnet.helius-rpc.com/?api-key={self.api_key}"

    async def probe(self) -> bool:
        if not self.api_key:
            return False
        data, _meta = await self.rpc_http.post_json(
            f"/?api-key={self.api_key}",
            json_body={"jsonrpc": "2.0", "id": 1, "method": "getHealth"},
        )
        return bool(data)

    async def get_wallet_activity(
        self, wallet: str, limit: int = 50
    ) -> ProviderResult[list[dict]]:
        """Parsed transaction history for a wallet.

        The parsing is the value: raw Solana transactions are a soup of
        instructions and inner instructions, and reconstructing "this wallet
        swapped X for Y" yourself is a large amount of fiddly work.
        """
        if not self.api_key:
            return ProviderResult.failure(self.name, "no API key")

        data, meta = await self.http.get_json(
            f"/v0/addresses/{wallet}/transactions",
            params={"api-key": self.api_key, "limit": min(limit, 100)},
            cache_ttl_s=self.cache_ttl_s,
        )
        self.health.record(bool(data), meta.get("latency_ms", 0.0), meta.get("error") or "")
        if not data or not isinstance(data, list):
            return ProviderResult.failure(self.name, meta.get("error") or "no data")

        out = []
        for tx in data:
            transfers = tx.get("tokenTransfers") or []
            native = tx.get("nativeTransfers") or []
            out.append(
                {
                    "signature": tx.get("signature", ""),
                    "timestamp": int(tx.get("timestamp") or 0),
                    "type": tx.get("type", "UNKNOWN"),
                    "source": tx.get("source", ""),
                    "fee": int(tx.get("fee") or 0),
                    "description": (tx.get("description") or "")[:200],
                    "token_transfers": [
                        {
                            "mint": t.get("mint", ""),
                            "amount": float(t.get("tokenAmount") or 0),
                            "from": t.get("fromUserAccount", ""),
                            "to": t.get("toUserAccount", ""),
                        }
                        for t in transfers[:10]
                    ],
                    "native_transfers": [
                        {
                            "amount_sol": int(n.get("amount") or 0) / 1e9,
                            "from": n.get("fromUserAccount", ""),
                            "to": n.get("toUserAccount", ""),
                        }
                        for n in native[:10]
                    ],
                }
            )
        return ProviderResult(
            provider=self.name, ok=True, data=out,
            latency_ms=meta.get("latency_ms", 0.0), cached=meta.get("cached", False),
        )

    async def get_funding_source(self, wallet: str) -> ProviderResult[dict]:
        """Who funded this wallet, and when.

        The single most valuable query for entity resolution. Fresh wallets
        funded by the same source within minutes of each other are almost
        always one actor — which turns "five smart wallets bought" into "one
        person with five wallets bought", a completely different signal.
        """
        if not self.api_key:
            return ProviderResult.failure(self.name, "no API key")

        data, meta = await self.http.get_json(
            f"/v0/addresses/{wallet}/transactions",
            params={"api-key": self.api_key, "limit": 100, "type": "TRANSFER"},
            cache_ttl_s=3600.0,  # funding history is immutable
        )
        if not data or not isinstance(data, list):
            return ProviderResult.failure(self.name, meta.get("error") or "no data")

        # Oldest first: the earliest inbound SOL is the funding event.
        inbound = []
        for tx in sorted(data, key=lambda t: t.get("timestamp") or 0):
            for n in tx.get("nativeTransfers") or []:
                if n.get("toUserAccount") == wallet and int(n.get("amount") or 0) > 0:
                    inbound.append(
                        {
                            "funder": n.get("fromUserAccount", ""),
                            "amount_sol": int(n["amount"]) / 1e9,
                            "timestamp": int(tx.get("timestamp") or 0),
                            "signature": tx.get("signature", ""),
                        }
                    )
        if not inbound:
            return ProviderResult.failure(self.name, "no inbound funding found")

        first = inbound[0]
        return ProviderResult(
            provider=self.name,
            ok=True,
            latency_ms=meta.get("latency_ms", 0.0),
            data={
                "first_funder": first["funder"],
                "first_funded_at": first["timestamp"],
                "first_amount_sol": first["amount_sol"],
                "funder_count": len({f["funder"] for f in inbound}),
                "all_funders": sorted({f["funder"] for f in inbound}),
                "inbound_count": len(inbound),
            },
        )

    async def get_holders(self, mint: str, limit: int = 20) -> ProviderResult[list[dict]]:
        """Token holders via the DAS API."""
        if not self.api_key:
            return ProviderResult.failure(self.name, "no API key")

        data, meta = await self.rpc_http.post_json(
            f"/?api-key={self.api_key}",
            json_body={
                "jsonrpc": "2.0", "id": "holders", "method": "getTokenAccounts",
                "params": {"mint": mint, "limit": min(limit * 3, 1000), "options": {"showZeroBalance": False}},
            },
        )
        if not data or "result" not in data:
            return ProviderResult.failure(self.name, meta.get("error") or "no data")

        accounts = (data["result"] or {}).get("token_accounts") or []
        total = sum(float(a.get("amount") or 0) for a in accounts)
        holders = sorted(accounts, key=lambda a: float(a.get("amount") or 0), reverse=True)
        return ProviderResult(
            provider=self.name, ok=True, latency_ms=meta.get("latency_ms", 0.0),
            data=[
                {
                    "owner": h.get("owner", ""),
                    "amount": float(h.get("amount") or 0),
                    "percentage": (float(h.get("amount") or 0) / total) if total else 0.0,
                }
                for h in holders[:limit]
            ],
        )

    def wallet_url(self, wallet: str) -> str:
        return f"https://xray.helius.xyz/account/{wallet}"


class SolscanProvider(Provider):
    """Solscan Pro — account and token analytics. Paid key required."""

    name = "solscan"
    display_name = "Solscan"
    url = "https://solscan.io"
    capabilities = frozenset(
        {Capability.TOKEN_HOLDERS, Capability.TX_HISTORY, Capability.TOKEN_METADATA}
    )
    tier = Tier.COLD
    requires_key = True
    paid = True
    rate_limit_rps = 3.0
    rate_limit_burst = 8
    cache_ttl_s = 120.0

    def __init__(self, config: Any, api_key: str | None = None) -> None:
        super().__init__(config, api_key)
        self.http = ProviderHttp(
            self.name,
            base_url="https://pro-api.solscan.io/v2.0",
            headers={"token": api_key or ""},
            rate_limit_rps=self.rate_limit_rps,
            rate_limit_burst=self.rate_limit_burst,
            timeout_s=10.0,
        )

    async def start(self) -> None:
        if self.available:
            await self.http.start()

    async def close(self) -> None:
        await self.http.close()

    async def get_holders(self, mint: str, limit: int = 20) -> ProviderResult[list[dict]]:
        if not self.api_key:
            return ProviderResult.failure(self.name, "no API key")
        data, meta = await self.http.get_json(
            "/token/holders",
            params={"address": mint, "page": 1, "page_size": min(limit, 40)},
            cache_ttl_s=self.cache_ttl_s,
        )
        self.health.record(bool(data), meta.get("latency_ms", 0.0), meta.get("error") or "")
        if not data or not data.get("success"):
            return ProviderResult.failure(self.name, meta.get("error") or "no data")

        items = ((data.get("data") or {}).get("items")) or []
        return ProviderResult(
            provider=self.name, ok=True, latency_ms=meta.get("latency_ms", 0.0),
            data=[
                {
                    "owner": h.get("owner", ""),
                    "amount": float(h.get("amount") or 0) / (10 ** int(h.get("decimals") or 6)),
                    "rank": h.get("rank", 0),
                }
                for h in items
            ],
        )

    def token_url(self, mint: str) -> str:
        return f"https://solscan.io/token/{mint}"

    def wallet_url(self, wallet: str) -> str:
        return f"https://solscan.io/account/{wallet}"


class DuneProvider(Provider):
    """Dune Analytics — custom SQL over indexed chain data.

    A batch tool, not a real-time one. Executing a query takes seconds to
    minutes, so this runs on a slow schedule and populates long-horizon
    context (deployer histories, sybil cohorts, meta rotation). It must never
    appear on a trading decision path, and the MANUAL tier enforces that.

    You supply your own saved query IDs — the useful ones are specific to what
    you want to research.
    """

    name = "dune"
    display_name = "Dune"
    url = "https://dune.com"
    capabilities = frozenset({Capability.ANALYTICS_QUERY})
    tier = Tier.MANUAL
    requires_key = True
    paid = False  # free tier allows a limited number of executions
    rate_limit_rps = 0.5
    rate_limit_burst = 2
    cache_ttl_s = 1800.0

    def __init__(self, config: Any, api_key: str | None = None) -> None:
        super().__init__(config, api_key)
        self.http = ProviderHttp(
            self.name,
            base_url="https://api.dune.com/api/v1",
            headers={"X-Dune-API-Key": api_key or ""},
            rate_limit_rps=self.rate_limit_rps,
            rate_limit_burst=self.rate_limit_burst,
            timeout_s=30.0,
        )

    async def start(self) -> None:
        if self.available:
            await self.http.start()

    async def close(self) -> None:
        await self.http.close()

    async def get_cached_results(self, query_id: int, limit: int = 1000) -> ProviderResult[list[dict]]:
        """Latest stored results for a saved query — cheap, no execution."""
        if not self.api_key:
            return ProviderResult.failure(self.name, "no API key")
        data, meta = await self.http.get_json(
            f"/query/{query_id}/results", params={"limit": limit}, cache_ttl_s=self.cache_ttl_s
        )
        self.health.record(bool(data), meta.get("latency_ms", 0.0), meta.get("error") or "")
        if not data:
            return ProviderResult.failure(self.name, meta.get("error") or "no results")
        rows = ((data.get("result") or {}).get("rows")) or []
        return ProviderResult(
            provider=self.name, ok=True, data=rows, latency_ms=meta.get("latency_ms", 0.0)
        )

    async def execute_query(
        self, query_id: int, params: dict[str, Any] | None = None, poll_timeout_s: float = 180.0
    ) -> ProviderResult[list[dict]]:
        """Run a query and wait for it. Minutes, not milliseconds."""
        if not self.api_key:
            return ProviderResult.failure(self.name, "no API key")

        started, meta = await self.http.post_json(
            f"/query/{query_id}/execute",
            json_body={"query_parameters": params or {}},
        )
        if not started or "execution_id" not in started:
            return ProviderResult.failure(self.name, meta.get("error") or "execution failed")

        execution_id = started["execution_id"]
        loop = asyncio.get_event_loop()
        deadline = loop.time() + poll_timeout_s

        while loop.time() < deadline:
            await asyncio.sleep(5.0)
            status, _ = await self.http.get_json(
                f"/execution/{execution_id}/status", cache_ttl_s=0
            )
            state = (status or {}).get("state", "")
            if state == "QUERY_STATE_COMPLETED":
                result, _ = await self.http.get_json(
                    f"/execution/{execution_id}/results", cache_ttl_s=0
                )
                rows = ((result or {}).get("result") or {}).get("rows") or []
                return ProviderResult(provider=self.name, ok=True, data=rows)
            if state in ("QUERY_STATE_FAILED", "QUERY_STATE_CANCELLED"):
                return ProviderResult.failure(self.name, f"query {state}")

        return ProviderResult.failure(self.name, "query timed out")
