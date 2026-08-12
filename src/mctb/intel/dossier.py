"""Dossier assembly — the tiered enrichment engine.

This is where the speed guarantee is enforced. Twenty providers are useless if
consulting them makes you late, so every request declares how long it is
willing to wait and the assembler works within that budget:

    HOT     (~0ms, cache only)   the entry decision path
    WARM    (~300ms budget)      candidate screening
    COLD    (~3s budget)         open positions, terminal lookups
    MANUAL  (unbounded)          explicit deep dives

A HOT assembly performs **no network I/O at all**. It reads whatever the
background enrichment loop has already put in the cache. That is what makes it
possible to have twenty data sources and still decide in under a millisecond:
the data was fetched before you needed it, not while you were waiting.

Anything the hot path could not answer is enqueued for background fetch, so
the *next* decision on that token is better informed — and if the answer turns
out to be bad, an open position can still be exited on it.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections import OrderedDict
from typing import Any

from ..providers import Capability, ProviderRegistry, Tier
from ..providers.links import all_token_links
from .fusion import fuse_market, fuse_security
from .graph import EntityGraph
from .narrative import NarrativeEngine
from .types import MarketIntel, SecurityIntel, TokenDossier, WalletIntel

log = logging.getLogger(__name__)


class DossierStore:
    """Bounded LRU of recently assembled dossiers."""

    def __init__(self, max_entries: int = 2000) -> None:
        self._store: OrderedDict[str, TokenDossier] = OrderedDict()
        self.max_entries = max_entries

    def get(self, mint: str, max_age_s: float = 30.0) -> TokenDossier | None:
        dossier = self._store.get(mint)
        if dossier is None:
            return None
        if dossier.age_s > max_age_s:
            return None
        self._store.move_to_end(mint)
        return dossier

    def put(self, dossier: TokenDossier) -> None:
        self._store[dossier.mint] = dossier
        self._store.move_to_end(dossier.mint)
        while len(self._store) > self.max_entries:
            self._store.popitem(last=False)

    def peek(self, mint: str) -> TokenDossier | None:
        """Any cached dossier regardless of age — used by the HOT path, which
        prefers stale data to no data."""
        return self._store.get(mint)

    def all(self) -> list[TokenDossier]:
        return list(self._store.values())

    def __len__(self) -> int:
        return len(self._store)


class IntelEngine:
    """Assembles dossiers and runs background enrichment."""

    def __init__(
        self,
        registry: ProviderRegistry,
        graph: EntityGraph | None = None,
        narrative: NarrativeEngine | None = None,
    ) -> None:
        self.registry = registry
        self.graph = graph or EntityGraph()
        self.narrative = narrative or NarrativeEngine(registry)
        self.store = DossierStore()

        self._enrich_queue: asyncio.Queue[tuple[str, str]] = asyncio.Queue(maxsize=1000)
        self._enriching: set[str] = set()
        self._running = False

        self.assemblies = 0
        self.hot_hits = 0
        self.hot_misses = 0
        self.background_enrichments = 0
        self.total_assembly_ms = 0.0

    # -- the hot path -------------------------------------------------------

    def assemble_hot(self, mint: str, symbol: str = "") -> TokenDossier:
        """Zero-I/O dossier from cache. Safe on the entry decision path.

        Returns a dossier that may be empty. An empty dossier is not an error
        and never blocks a trade on its own — the deterministic scorer works
        from on-chain features regardless, and this only enriches it.
        """
        started = time.perf_counter()
        cached = self.store.peek(mint)

        if cached is not None:
            self.hot_hits += 1
            dossier = cached
        else:
            self.hot_misses += 1
            dossier = TokenDossier(mint=mint, symbol=symbol)
            # Nothing known yet — ask the background loop to fix that.
            self.request_enrichment(mint, symbol)

        dossier.tier_used = "hot"
        dossier.assembly_ms = (time.perf_counter() - started) * 1000
        return dossier

    def request_enrichment(self, mint: str, symbol: str = "") -> None:
        """Queue a token for background enrichment. Never blocks."""
        if mint in self._enriching:
            return
        try:
            self._enrich_queue.put_nowait((mint, symbol))
            self._enriching.add(mint)
        except asyncio.QueueFull:
            pass

    # -- full assembly ------------------------------------------------------

    async def assemble(
        self,
        mint: str,
        symbol: str = "",
        *,
        tier: Tier = Tier.WARM,
        timeout_s: float | None = None,
        include_narrative: bool = True,
        include_wallets: bool = True,
        creator: str = "",
    ) -> TokenDossier:
        """Full dossier within a time budget."""
        started = time.perf_counter()
        budget = timeout_s if timeout_s is not None else {
            Tier.HOT: 0.05, Tier.WARM: 0.4, Tier.COLD: 3.0, Tier.MANUAL: 15.0
        }[tier]

        dossier = TokenDossier(mint=mint, symbol=symbol, creator=creator)

        tasks: dict[str, asyncio.Task] = {
            "security": asyncio.create_task(
                fuse_security(self.registry, mint, max_tier=tier, timeout_s=budget * 0.8)
            ),
            "market": asyncio.create_task(
                fuse_market(self.registry, mint, max_tier=tier, timeout_s=budget * 0.6)
            ),
        }
        if include_narrative and tier.value >= Tier.COLD.value:
            tasks["narrative"] = asyncio.create_task(
                self.narrative.analyse(mint, symbol, max_tier=tier, timeout_s=budget * 0.5)
            )

        done, pending = await asyncio.wait(tasks.values(), timeout=budget)
        for task in pending:
            task.cancel()

        for key, task in tasks.items():
            if task not in done:
                continue
            try:
                value = task.result()
            except Exception as exc:  # noqa: BLE001
                log.debug("dossier %s component failed: %s", key, exc)
                continue
            if key == "security" and isinstance(value, SecurityIntel):
                dossier.security = value
            elif key == "market" and isinstance(value, MarketIntel):
                dossier.market = value
                dossier.symbol = dossier.symbol or value.symbol
                dossier.name = value.name
            elif key == "narrative":
                dossier.narrative = value

        # Entity analysis is local and fast — no network, so always included.
        if include_wallets:
            buyers = [a for a, _ts in self.graph.token_buyers.get(mint, [])]
            if buyers:
                _unique, warnings = self.graph.deduplicate(buyers)
                dossier.cluster_warnings.extend(warnings)
                fresh = self.graph.fresh_wallet_ratio(buyers)
                if fresh > 0.6:
                    dossier.cluster_warnings.append(
                        f"{fresh:.0%} of buyers are wallets created in the last 24h"
                    )
            if creator:
                insider = self.graph.insider_risk(mint, creator)
                if insider["insider_count"] > 0:
                    dossier.cluster_warnings.append(
                        f"{insider['insider_count']} likely insider wallets "
                        f"({insider['insider_pct']:.0%} of buyers): {insider['reasons'][0] if insider['reasons'] else ''}"
                    )

        dossier.links = all_token_links(mint)
        dossier.tier_used = tier.name.lower()
        dossier.assembly_ms = (time.perf_counter() - started) * 1000

        self.assemblies += 1
        self.total_assembly_ms += dossier.assembly_ms
        self.store.put(dossier)
        return dossier

    # -- background enrichment ---------------------------------------------

    async def _enrich_worker(self, worker_id: int) -> None:
        while self._running:
            try:
                mint, symbol = await asyncio.wait_for(self._enrich_queue.get(), timeout=2.0)
            except asyncio.TimeoutError:
                continue
            except asyncio.CancelledError:
                raise

            try:
                await self.assemble(mint, symbol, tier=Tier.WARM, timeout_s=3.0)
                self.background_enrichments += 1
            except Exception as exc:  # noqa: BLE001
                log.debug("background enrichment of %s failed: %s", mint, exc)
            finally:
                self._enriching.discard(mint)
                self._enrich_queue.task_done()

    async def start(self, workers: int = 3) -> list[asyncio.Task]:
        """Start background enrichment.

        Three workers is a deliberate choice: enough to keep the cache warm
        across a busy watchlist, few enough that they cannot saturate a Pi's
        CPU or blow through provider rate limits.
        """
        self._running = True
        return [
            asyncio.create_task(self._enrich_worker(i), name=f"enrich-{i}")
            for i in range(workers)
        ]

    def stop(self) -> None:
        self._running = False

    # -- wallet intel -------------------------------------------------------

    async def wallet_dossier(
        self, address: str, *, tier: Tier = Tier.COLD, timeout_s: float = 4.0
    ) -> WalletIntel:
        """Everything known about one wallet, local graph plus providers."""
        intel = WalletIntel(address=address)

        node = self.graph.nodes.get(address)
        if node:
            intel.first_funder = node.first_funder
            intel.funded_at = node.funded_at
            intel.is_fresh_wallet = 0 < node.age_s < 86_400
            intel.is_exchange = node.is_exchange
            intel.is_bot = node.is_bot
            intel.labels = list(node.labels)

        cluster = self.graph.cluster_of(address)
        if cluster:
            intel.cluster_id = cluster.id
            intel.cluster_size = cluster.size

        results = await self.registry.query_all(
            Capability.WALLET_PNL, "get_wallet_pnl", address,
            max_tier=tier, timeout_s=timeout_s, limit=2,
        )
        sources = []
        for result in results:
            if not result.usable:
                continue
            sources.append(result.provider)
            d = result.data or {}
            intel.external_pnl_usd = max(intel.external_pnl_usd, float(d.get("realized_pnl_usd") or 0))
            intel.external_winrate = max(intel.external_winrate, float(d.get("winrate") or 0))
            intel.external_tokens_traded = max(
                intel.external_tokens_traded, int(d.get("tokens_traded") or 0)
            )

        label_results = await self.registry.query_all(
            Capability.WALLET_LABELS, "get_wallet_labels", [address],
            max_tier=tier, timeout_s=timeout_s, limit=2,
        )
        for result in label_results:
            if result.usable and address in (result.data or {}):
                label = (result.data or {})[address]
                if label and label not in intel.labels:
                    intel.labels.append(label)
                sources.append(result.provider)

        lowered = " ".join(intel.labels).lower()
        intel.is_exchange = intel.is_exchange or any(
            k in lowered for k in ("exchange", "cex", "binance", "coinbase", "okx")
        )
        intel.is_bot = intel.is_bot or any(k in lowered for k in ("bot", "mev", "arb"))

        intel.provenance = {"sources": sources, "confidence": "medium" if sources else "none"}
        return intel

    async def resolve_funding(self, addresses: list[str], limit: int = 20) -> int:
        """Populate the graph's funding lineage via Helius.

        Called on a slow schedule for wallets we actually care about. This is
        what turns the graph from a co-occurrence heuristic into real entity
        resolution, and it is the main reason a Helius key is worth having.
        """
        helius = self.registry.get("helius")
        if helius is None or not helius.available:
            return 0

        pending = [
            a for a in addresses
            if a not in self.graph.nodes or not self.graph.nodes[a].first_funder
        ][:limit]

        resolved = 0
        for address in pending:
            try:
                result = await helius.get_funding_source(address)  # type: ignore[attr-defined]
            except Exception:  # noqa: BLE001
                continue
            if not result.usable:
                continue
            d = result.data or {}
            self.graph.observe_wallet(
                address,
                first_funder=d.get("first_funder", ""),
                funded_at=float(d.get("first_funded_at") or 0),
            )
            resolved += 1
            # Be gentle: this is a background job, not a race.
            await asyncio.sleep(0.15)

        if resolved:
            log.info("resolved funding lineage for %d wallet(s)", resolved)
        return resolved

    # -- stats --------------------------------------------------------------

    def stats(self) -> dict[str, Any]:
        total_hot = self.hot_hits + self.hot_misses
        return {
            "assemblies": self.assemblies,
            "avg_assembly_ms": round(self.total_assembly_ms / self.assemblies, 2)
            if self.assemblies
            else 0.0,
            "hot_hits": self.hot_hits,
            "hot_misses": self.hot_misses,
            "hot_hit_rate": round(self.hot_hits / total_hot, 3) if total_hot else 0.0,
            "background_enrichments": self.background_enrichments,
            "queue_depth": self._enrich_queue.qsize(),
            "cached_dossiers": len(self.store),
            "graph": self.graph.stats(),
        }
