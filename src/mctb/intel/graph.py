"""Entity resolution — working out which wallets are actually one person.

This is the highest-value original analysis in the system, and it addresses a
failure that every naive copy-trading bot has.

**The problem.** Your bot sees "5 tracked smart wallets just bought this
token" and treats it as five independent expert opinions. In reality those
five wallets were all funded from the same address eleven minutes ago, and
they are one person running a sybil. You have not found consensus — you have
found one actor, possibly one actor who knows you are watching and is
manufacturing exactly the signal your bot is looking for.

**The approach.** Cluster wallets into entities using evidence that is
expensive for an adversary to fake:

  *Funding lineage* — wallets funded by the same source are linked. Strong
  evidence, because breaking it means running every wallet through a mixer or
  a CEX, which costs real money and time.

  *Temporal correlation* — wallets funded within a short window of each other
  by the same source are almost certainly one batch.

  *Co-occurrence* — wallets that keep buying the same obscure tokens within
  seconds of each other are coordinated. Two coincidences is chance; six is a
  bot fleet.

  *Behavioural fingerprints* — identical trade sizing and identical hold
  times across wallets indicate one script.

Then: **a cluster votes once.** Five wallets from one entity count as one
signal, not five. That single change removes a whole category of manufactured
signal that most retail bots trade into.

Union-Find keeps this near-linear, so it runs comfortably on a Pi.
"""

from __future__ import annotations

import logging
import time
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any

log = logging.getLogger(__name__)


# --------------------------------------------------------------------------
#  Union-Find
# --------------------------------------------------------------------------


class UnionFind:
    """Disjoint-set with path compression and union by size."""

    def __init__(self) -> None:
        self.parent: dict[str, str] = {}
        self.size: dict[str, int] = {}
        # Why each merge happened — needed to explain a cluster to the user.
        self.evidence: dict[tuple[str, str], str] = {}

    def add(self, item: str) -> None:
        if item not in self.parent:
            self.parent[item] = item
            self.size[item] = 1

    def find(self, item: str) -> str:
        self.add(item)
        root = item
        while self.parent[root] != root:
            root = self.parent[root]
        # Path compression.
        while self.parent[item] != root:
            self.parent[item], item = root, self.parent[item]
        return root

    def union(self, a: str, b: str, reason: str = "") -> bool:
        self.add(a)
        self.add(b)
        root_a, root_b = self.find(a), self.find(b)
        if root_a == root_b:
            return False
        if self.size[root_a] < self.size[root_b]:
            root_a, root_b = root_b, root_a
        self.parent[root_b] = root_a
        self.size[root_a] += self.size[root_b]
        if reason:
            self.evidence[(min(a, b), max(a, b))] = reason
        return True

    def clusters(self) -> dict[str, list[str]]:
        out: dict[str, list[str]] = defaultdict(list)
        for item in self.parent:
            out[self.find(item)].append(item)
        return dict(out)


# --------------------------------------------------------------------------
#  Observations
# --------------------------------------------------------------------------


@dataclass
class WalletNode:
    address: str
    first_funder: str = ""
    funded_at: float = 0.0
    first_seen: float = field(default_factory=time.time)
    labels: list[str] = field(default_factory=list)

    tokens_bought: set[str] = field(default_factory=set)
    trade_sizes_sol: list[float] = field(default_factory=list)
    hold_times_s: list[float] = field(default_factory=list)

    is_exchange: bool = False
    is_bot: bool = False

    @property
    def avg_trade_size(self) -> float:
        return sum(self.trade_sizes_sol) / len(self.trade_sizes_sol) if self.trade_sizes_sol else 0.0

    @property
    def age_s(self) -> float:
        return time.time() - self.funded_at if self.funded_at else 0.0


@dataclass
class Cluster:
    """A set of wallets believed to be one actor."""

    id: str
    members: list[str]
    reasons: list[str] = field(default_factory=list)
    confidence: float = 0.5
    label: str = ""

    @property
    def size(self) -> int:
        return len(self.members)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "size": self.size,
            "members": self.members[:20],
            "reasons": self.reasons[:6],
            "confidence": round(self.confidence, 2),
            "label": self.label,
        }


# Addresses that fund enormous numbers of unrelated wallets. Linking through
# them would merge half of Solana into one cluster, so they are cut points.
# Detected dynamically, with these as known seeds.
KNOWN_HUBS = {
    "5Q544fKrFoe6tsEbD7S8EmxGTJYAKtTVhAW5Q5pge4j1",  # Raydium authority
    "9WzDXwBbmkg8ZTbNMqUxvQRAyrZzDsGYdLVL9zYtAWWM",  # historical CEX hot wallet
    "2ojv9BAiHUrvsm9gxDe7fJSzbNZSJcxZvf8dqmWGHG8S",
    "H8sMJSCQxfKiFTCfDR3DUMLPwcRbM61LGFJ8N4dK3WjS",
}


class EntityGraph:
    """Builds and queries the wallet-cluster graph."""

    # A funding hub linking more than this many wallets is infrastructure
    # (exchange, bridge, deployer service), not one person's sybil.
    HUB_THRESHOLD = 25
    # Wallets funded by the same source within this window are one batch.
    BATCH_WINDOW_S = 1800.0
    # Co-buying this many of the same tokens implies coordination.
    COOCCURRENCE_THRESHOLD = 3
    # ...but only for tokens with few enough buyers that overlap is meaningful.
    COOCCURRENCE_MAX_BUYERS = 60

    def __init__(self) -> None:
        self.nodes: dict[str, WalletNode] = {}
        self.token_buyers: dict[str, list[tuple[str, float]]] = defaultdict(list)
        self._uf = UnionFind()
        self._clusters: dict[str, Cluster] = {}
        self._member_to_cluster: dict[str, str] = {}
        self._dirty = True
        self._hubs: set[str] = set(KNOWN_HUBS)

    # -- ingestion ----------------------------------------------------------

    def observe_wallet(
        self,
        address: str,
        *,
        first_funder: str = "",
        funded_at: float = 0.0,
        labels: list[str] | None = None,
    ) -> WalletNode:
        node = self.nodes.get(address)
        if node is None:
            node = WalletNode(address=address)
            self.nodes[address] = node
            self._dirty = True

        if first_funder and not node.first_funder:
            node.first_funder = first_funder
            node.funded_at = funded_at
            self._dirty = True
        if labels:
            for label in labels:
                if label not in node.labels:
                    node.labels.append(label)
            lowered = " ".join(node.labels).lower()
            node.is_exchange = any(
                k in lowered for k in ("exchange", "cex", "binance", "coinbase", "okx", "bybit")
            )
            node.is_bot = any(k in lowered for k in ("bot", "mev", "arbitrage", "sandwich"))
        return node

    def observe_buy(
        self, address: str, mint: str, ts: float, size_sol: float = 0.0
    ) -> None:
        node = self.observe_wallet(address)
        node.tokens_bought.add(mint)
        if size_sol > 0:
            node.trade_sizes_sol.append(size_sol)
            if len(node.trade_sizes_sol) > 50:
                node.trade_sizes_sol.pop(0)
        self.token_buyers[mint].append((address, ts))
        self._dirty = True

    def observe_hold_time(self, address: str, seconds: float) -> None:
        node = self.nodes.get(address)
        if node:
            node.hold_times_s.append(seconds)
            if len(node.hold_times_s) > 50:
                node.hold_times_s.pop(0)

    # -- clustering ---------------------------------------------------------

    def _detect_hubs(self) -> set[str]:
        """Find funding addresses that serve too many wallets to be one person."""
        counts: dict[str, int] = defaultdict(int)
        for node in self.nodes.values():
            if node.first_funder:
                counts[node.first_funder] += 1
        detected = {addr for addr, n in counts.items() if n >= self.HUB_THRESHOLD}
        return detected | set(KNOWN_HUBS)

    def rebuild(self) -> None:
        """Recompute clusters. Cheap enough to run every few minutes."""
        if not self._dirty:
            return

        self._uf = UnionFind()
        self._hubs = self._detect_hubs()

        for address in self.nodes:
            self._uf.add(address)

        # --- Link 1: shared funding source ---------------------------------
        by_funder: dict[str, list[WalletNode]] = defaultdict(list)
        for node in self.nodes.values():
            if node.first_funder and node.first_funder not in self._hubs:
                by_funder[node.first_funder].append(node)

        for funder, group in by_funder.items():
            if len(group) < 2:
                continue
            group.sort(key=lambda n: n.funded_at)
            for i in range(len(group) - 1):
                a, b = group[i], group[i + 1]
                gap = abs(b.funded_at - a.funded_at) if (a.funded_at and b.funded_at) else 1e9
                if gap <= self.BATCH_WINDOW_S:
                    self._uf.union(
                        a.address, b.address,
                        f"funded by {funder[:8]}… within {gap / 60:.0f} min of each other",
                    )
                else:
                    # Same funder but far apart in time — weaker, still linked,
                    # because a shared non-hub funder is itself unusual.
                    self._uf.union(a.address, b.address, f"shared funder {funder[:8]}…")

        # --- Link 2: repeated co-occurrence on obscure tokens --------------
        pair_counts: dict[tuple[str, str], int] = defaultdict(int)
        for buyers in self.token_buyers.values():
            # Overlap on a token everyone bought means nothing.
            if len(buyers) > self.COOCCURRENCE_MAX_BUYERS or len(buyers) < 2:
                continue
            ordered = sorted(buyers, key=lambda b: b[1])
            for i in range(len(ordered)):
                addr_a, ts_a = ordered[i]
                for j in range(i + 1, len(ordered)):
                    addr_b, ts_b = ordered[j]
                    # Only near-simultaneous buys count as coordination.
                    if ts_b - ts_a > 120:
                        break
                    if addr_a != addr_b:
                        pair_counts[(min(addr_a, addr_b), max(addr_a, addr_b))] += 1

        for (a, b), count in pair_counts.items():
            if count >= self.COOCCURRENCE_THRESHOLD:
                self._uf.union(a, b, f"co-bought {count} obscure tokens within seconds")

        # --- Link 3: identical behavioural fingerprint ---------------------
        # Only applied to wallets already sharing a funder-adjacent signal
        # would be safer, but identical sizing across many trades is itself
        # strong: humans vary, scripts do not.
        sized = [n for n in self.nodes.values() if len(n.trade_sizes_sol) >= 5]
        for i, node_a in enumerate(sized):
            for node_b in sized[i + 1 :]:
                if self._uf.find(node_a.address) == self._uf.find(node_b.address):
                    continue
                avg_a, avg_b = node_a.avg_trade_size, node_b.avg_trade_size
                if avg_a <= 0 or avg_b <= 0:
                    continue
                if abs(avg_a - avg_b) / max(avg_a, avg_b) < 0.02:
                    shared = node_a.tokens_bought & node_b.tokens_bought
                    if len(shared) >= 2:
                        self._uf.union(
                            node_a.address, node_b.address,
                            f"identical average trade size ({avg_a:.4f} SOL) on {len(shared)} shared tokens",
                        )

        # --- Materialise clusters ------------------------------------------
        self._clusters = {}
        self._member_to_cluster = {}
        for root, members in self._uf.clusters().items():
            if len(members) < 2:
                continue
            reasons: list[str] = []
            for (a, b), reason in self._uf.evidence.items():
                if a in members and b in members and reason not in reasons:
                    reasons.append(reason)

            # Confidence rises with corroborating evidence, but a huge cluster
            # is more likely to be over-merged than to be one giant sybil.
            confidence = min(0.95, 0.45 + 0.12 * len(reasons))
            if len(members) > 25:
                confidence *= 0.6

            cluster = Cluster(
                id=f"C{root[:8]}",
                members=sorted(members),
                reasons=reasons,
                confidence=confidence,
            )
            self._clusters[cluster.id] = cluster
            for member in members:
                self._member_to_cluster[member] = cluster.id

        self._dirty = False
        if self._clusters:
            log.info(
                "entity graph: %d wallets, %d clusters (largest %d)",
                len(self.nodes),
                len(self._clusters),
                max(c.size for c in self._clusters.values()),
            )

    # -- queries ------------------------------------------------------------

    def cluster_of(self, address: str) -> Cluster | None:
        self.rebuild()
        cluster_id = self._member_to_cluster.get(address)
        return self._clusters.get(cluster_id) if cluster_id else None

    def cluster_size(self, address: str) -> int:
        cluster = self.cluster_of(address)
        return cluster.size if cluster else 1

    def deduplicate(self, addresses: list[str]) -> tuple[list[str], list[str]]:
        """Collapse a list of wallets to one representative per entity.

        **This is the function that changes trading behaviour.** Feed it the
        five wallets that just bought and it returns however many distinct
        actors those really are, plus a human-readable warning for each
        collapse. The scorer counts distinct actors, not addresses.
        """
        self.rebuild()
        seen_clusters: set[str] = set()
        unique: list[str] = []
        warnings: list[str] = []

        for address in addresses:
            cluster_id = self._member_to_cluster.get(address)
            if cluster_id is None:
                unique.append(address)
                continue
            if cluster_id in seen_clusters:
                continue
            seen_clusters.add(cluster_id)
            unique.append(address)

            cluster = self._clusters[cluster_id]
            overlap = [a for a in addresses if self._member_to_cluster.get(a) == cluster_id]
            if len(overlap) > 1:
                reason = cluster.reasons[0] if cluster.reasons else "linked on-chain"
                warnings.append(
                    f"{len(overlap)} of these wallets are one entity ({cluster.id}): {reason}"
                )

        return unique, warnings

    def sybil_risk(self, addresses: list[str]) -> float:
        """0..1 — how much of this apparent consensus is really one actor."""
        if len(addresses) <= 1:
            return 0.0
        unique, _warnings = self.deduplicate(addresses)
        return 1.0 - (len(unique) / len(addresses))

    def insider_risk(self, mint: str, creator: str, window_s: float = 3600.0) -> dict[str, Any]:
        """Did wallets funded by the deployer's own funder buy this launch?

        The clearest insider pattern there is: a deployer funds several fresh
        wallets, launches a token, and those wallets buy it immediately. Every
        step is visible on chain, and almost nobody checks.
        """
        self.rebuild()
        creator_node = self.nodes.get(creator)
        creator_funder = creator_node.first_funder if creator_node else ""
        creator_cluster = self._member_to_cluster.get(creator, "")

        buyers = self.token_buyers.get(mint, [])
        if not buyers:
            return {"insider_count": 0, "insider_pct": 0.0, "wallets": [], "reasons": []}

        first_buy_ts = min(ts for _a, ts in buyers)
        insiders: list[str] = []
        reasons: list[str] = []

        for address, ts in buyers:
            node = self.nodes.get(address)
            if node is None:
                continue

            if creator_cluster and self._member_to_cluster.get(address) == creator_cluster:
                insiders.append(address)
                reasons.append(f"{address[:8]}… is in the deployer's own cluster")
                continue

            if creator_funder and node.first_funder == creator_funder:
                insiders.append(address)
                reasons.append(f"{address[:8]}… shares the deployer's funding source")
                continue

            # A wallet created shortly before the launch that immediately buys
            # it is not a coincidence.
            if node.funded_at and 0 < (first_buy_ts - node.funded_at) < window_s:
                if ts - first_buy_ts < 60:
                    insiders.append(address)
                    reasons.append(
                        f"{address[:8]}… was funded "
                        f"{(first_buy_ts - node.funded_at) / 60:.0f} min before launch and bought instantly"
                    )

        unique_insiders = sorted(set(insiders))
        return {
            "insider_count": len(unique_insiders),
            "insider_pct": len(unique_insiders) / len(buyers) if buyers else 0.0,
            "wallets": unique_insiders[:20],
            "reasons": reasons[:8],
        }

    def fresh_wallet_ratio(self, addresses: list[str], max_age_s: float = 86_400.0) -> float:
        """Share of these wallets created within the last day.

        A cohort of brand-new wallets buying one token is manufactured demand
        far more often than it is organic discovery.
        """
        if not addresses:
            return 0.0
        fresh = 0
        known = 0
        for address in addresses:
            node = self.nodes.get(address)
            if node is None or not node.funded_at:
                continue
            known += 1
            if node.age_s < max_age_s:
                fresh += 1
        return fresh / known if known else 0.0

    def stats(self) -> dict[str, Any]:
        self.rebuild()
        sizes = [c.size for c in self._clusters.values()]
        return {
            "wallets": len(self.nodes),
            "clusters": len(self._clusters),
            "clustered_wallets": sum(sizes),
            "largest_cluster": max(sizes) if sizes else 0,
            "hubs_detected": len(self._hubs),
            "tokens_tracked": len(self.token_buyers),
        }

    def top_clusters(self, limit: int = 10) -> list[dict[str, Any]]:
        self.rebuild()
        clusters = sorted(self._clusters.values(), key=lambda c: c.size, reverse=True)
        return [c.to_dict() for c in clusters[:limit]]

    def prune(self, max_tokens: int = 5000, max_wallets: int = 50_000) -> None:
        """Bound memory. A Pi has 8GB and this runs for weeks."""
        if len(self.token_buyers) > max_tokens:
            ordered = sorted(
                self.token_buyers.items(),
                key=lambda kv: max((ts for _a, ts in kv[1]), default=0),
            )
            for mint, _ in ordered[: len(self.token_buyers) - max_tokens]:
                del self.token_buyers[mint]
            self._dirty = True

        if len(self.nodes) > max_wallets:
            ordered_nodes = sorted(self.nodes.values(), key=lambda n: n.first_seen)
            for node in ordered_nodes[: len(self.nodes) - max_wallets]:
                # Never evict a wallet we have real evidence about.
                if not node.first_funder and len(node.tokens_bought) < 2:
                    del self.nodes[node.address]
            self._dirty = True
