"""Entity resolution — the sybil-collapse logic.

The property that matters: N wallets controlled by one actor must count as
one signal, not N. Everything else in this file supports that.
"""

from __future__ import annotations

import time

import pytest

from mctb.intel.graph import EntityGraph, UnionFind


class TestUnionFind:
    def test_basic_union(self) -> None:
        uf = UnionFind()
        uf.union("a", "b")
        assert uf.find("a") == uf.find("b")
        assert uf.find("a") != uf.find("c")

    def test_transitive(self) -> None:
        uf = UnionFind()
        uf.union("a", "b")
        uf.union("b", "c")
        assert uf.find("a") == uf.find("c")

    def test_clusters(self) -> None:
        uf = UnionFind()
        uf.union("a", "b")
        uf.union("c", "d")
        uf.add("e")
        sizes = sorted(len(m) for m in uf.clusters().values())
        assert sizes == [1, 2, 2]

    def test_evidence_recorded(self) -> None:
        uf = UnionFind()
        uf.union("a", "b", "shared funder")
        assert ("a", "b") in uf.evidence

    def test_repeated_union_is_noop(self) -> None:
        uf = UnionFind()
        assert uf.union("a", "b") is True
        assert uf.union("a", "b") is False


class TestFundingClustering:
    def test_same_funder_close_in_time_clusters(self) -> None:
        graph = EntityGraph()
        now = time.time()
        for i in range(4):
            graph.observe_wallet(f"w{i}", first_funder="funder1", funded_at=now + i * 60)
        graph.rebuild()
        assert graph.cluster_size("w0") == 4

    def test_different_funders_do_not_cluster(self) -> None:
        graph = EntityGraph()
        now = time.time()
        graph.observe_wallet("a", first_funder="f1", funded_at=now)
        graph.observe_wallet("b", first_funder="f2", funded_at=now)
        graph.rebuild()
        assert graph.cluster_size("a") == 1

    def test_exchange_hub_does_not_merge_everyone(self) -> None:
        """The failure that would break this whole feature: linking through a
        high-volume funder would merge half of Solana into one cluster."""
        graph = EntityGraph()
        now = time.time()
        for i in range(60):
            graph.observe_wallet(f"w{i}", first_funder="bighub", funded_at=now + i)
        graph.rebuild()
        assert graph.cluster_size("w0") == 1

    def test_known_hub_seeds_are_excluded(self) -> None:
        graph = EntityGraph()
        now = time.time()
        hub = "5Q544fKrFoe6tsEbD7S8EmxGTJYAKtTVhAW5Q5pge4j1"
        for i in range(3):
            graph.observe_wallet(f"w{i}", first_funder=hub, funded_at=now + i)
        graph.rebuild()
        assert graph.cluster_size("w0") == 1


class TestCooccurrenceClustering:
    def test_repeated_simultaneous_buys_cluster(self) -> None:
        graph = EntityGraph()
        now = time.time()
        for token in range(4):
            graph.observe_buy("a", f"mint{token}", now + token * 600)
            graph.observe_buy("b", f"mint{token}", now + token * 600 + 2)
        graph.rebuild()
        assert graph.cluster_size("a") == 2

    def test_popular_tokens_do_not_cluster(self) -> None:
        """Overlap on a token that everybody bought means nothing."""
        graph = EntityGraph()
        now = time.time()
        for token in range(4):
            for i in range(80):
                graph.observe_buy(f"w{i}", f"mint{token}", now + token * 600 + i * 0.1)
        graph.rebuild()
        assert graph.cluster_size("w0") == 1

    def test_buys_far_apart_do_not_cluster(self) -> None:
        graph = EntityGraph()
        now = time.time()
        for token in range(5):
            graph.observe_buy("a", f"mint{token}", now + token * 600)
            graph.observe_buy("b", f"mint{token}", now + token * 600 + 3600)
        graph.rebuild()
        assert graph.cluster_size("a") == 1

    def test_single_coincidence_insufficient(self) -> None:
        graph = EntityGraph()
        now = time.time()
        graph.observe_buy("a", "mint0", now)
        graph.observe_buy("b", "mint0", now + 1)
        graph.rebuild()
        assert graph.cluster_size("a") == 1


class TestDeduplication:
    def test_collapses_sybil_to_one_actor(self) -> None:
        """The headline behaviour: five wallets, one funder, one signal."""
        graph = EntityGraph()
        now = time.time()
        wallets = [f"sybil{i}" for i in range(5)]
        for i, wallet in enumerate(wallets):
            graph.observe_wallet(wallet, first_funder="attacker", funded_at=now + i * 30)

        unique, warnings = graph.deduplicate(wallets)
        assert len(unique) == 1
        assert warnings and "one entity" in warnings[0]

    def test_independent_wallets_survive(self) -> None:
        graph = EntityGraph()
        now = time.time()
        wallets = [f"indep{i}" for i in range(4)]
        for i, wallet in enumerate(wallets):
            graph.observe_wallet(wallet, first_funder=f"funder{i}", funded_at=now)
        graph.rebuild()
        unique, warnings = graph.deduplicate(wallets)
        assert len(unique) == 4
        assert not warnings

    def test_mixed_sybil_and_independent(self) -> None:
        graph = EntityGraph()
        now = time.time()
        sybils = [f"s{i}" for i in range(3)]
        for i, wallet in enumerate(sybils):
            graph.observe_wallet(wallet, first_funder="attacker", funded_at=now + i * 10)
        graph.observe_wallet("real1", first_funder="honest1", funded_at=now)
        graph.observe_wallet("real2", first_funder="honest2", funded_at=now)

        unique, _warnings = graph.deduplicate(sybils + ["real1", "real2"])
        assert len(unique) == 3   # one entity + two independents

    def test_sybil_risk_score(self) -> None:
        graph = EntityGraph()
        now = time.time()
        wallets = [f"w{i}" for i in range(4)]
        for i, wallet in enumerate(wallets):
            graph.observe_wallet(wallet, first_funder="one", funded_at=now + i * 10)
        assert graph.sybil_risk(wallets) == pytest.approx(0.75)

    def test_sybil_risk_zero_for_singleton(self) -> None:
        graph = EntityGraph()
        assert graph.sybil_risk(["only"]) == 0.0
        assert graph.sybil_risk([]) == 0.0


class TestInsiderDetection:
    def test_detects_deployer_funded_buyers(self) -> None:
        graph = EntityGraph()
        now = time.time()
        graph.observe_wallet("deployer", first_funder="masterfunder", funded_at=now - 7200)
        for i in range(3):
            graph.observe_wallet(f"insider{i}", first_funder="masterfunder", funded_at=now - 1800)
            graph.observe_buy(f"insider{i}", "newtoken", now)
        graph.observe_wallet("outsider", first_funder="unrelated", funded_at=now - 86400 * 30)
        graph.observe_buy("outsider", "newtoken", now + 30)

        result = graph.insider_risk("newtoken", "deployer")
        assert result["insider_count"] == 3
        assert result["insider_pct"] == pytest.approx(0.75)
        assert result["reasons"]

    def test_no_insiders_when_unrelated(self) -> None:
        graph = EntityGraph()
        now = time.time()
        graph.observe_wallet("deployer", first_funder="f1", funded_at=now - 86400 * 60)
        for i in range(3):
            graph.observe_wallet(f"buyer{i}", first_funder=f"other{i}", funded_at=now - 86400 * 30)
            graph.observe_buy(f"buyer{i}", "token", now)
        assert graph.insider_risk("token", "deployer")["insider_count"] == 0

    def test_no_buyers_is_safe(self) -> None:
        graph = EntityGraph()
        assert graph.insider_risk("unknown", "creator")["insider_count"] == 0


class TestFreshWallets:
    def test_detects_fresh_cohort(self) -> None:
        graph = EntityGraph()
        now = time.time()
        for i in range(4):
            graph.observe_wallet(f"fresh{i}", first_funder=f"f{i}", funded_at=now - 3600)
        assert graph.fresh_wallet_ratio([f"fresh{i}" for i in range(4)]) == 1.0

    def test_old_wallets_not_fresh(self) -> None:
        graph = EntityGraph()
        now = time.time()
        for i in range(3):
            graph.observe_wallet(f"old{i}", first_funder=f"f{i}", funded_at=now - 86400 * 90)
        assert graph.fresh_wallet_ratio([f"old{i}" for i in range(3)]) == 0.0

    def test_unknown_wallets_ignored(self) -> None:
        graph = EntityGraph()
        assert graph.fresh_wallet_ratio(["never-seen"]) == 0.0


class TestMaintenance:
    def test_prune_bounds_memory(self) -> None:
        graph = EntityGraph()
        now = time.time()
        for i in range(200):
            graph.observe_buy(f"w{i}", f"mint{i}", now + i)
        graph.prune(max_tokens=50, max_wallets=50)
        assert len(graph.token_buyers) <= 50

    def test_prune_keeps_evidenced_wallets(self) -> None:
        graph = EntityGraph()
        now = time.time()
        graph.observe_wallet("important", first_funder="known", funded_at=now)
        for i in range(200):
            graph.observe_wallet(f"noise{i}")
        graph.prune(max_wallets=10)
        assert "important" in graph.nodes

    def test_stats(self) -> None:
        graph = EntityGraph()
        now = time.time()
        for i in range(3):
            graph.observe_wallet(f"w{i}", first_funder="f", funded_at=now + i)
        stats = graph.stats()
        assert stats["wallets"] == 3
        assert stats["clusters"] >= 1
