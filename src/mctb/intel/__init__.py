"""Intelligence layer — fusion, entity resolution and dossier assembly.

Providers return raw vendor data. This package turns it into decisions:
cross-checked, provenance-tracked, and assembled within a latency budget.
"""

from .dossier import DossierStore, IntelEngine
from .fusion import fuse_market, fuse_security
from .graph import Cluster, EntityGraph, UnionFind, WalletNode
from .narrative import NarrativeEngine, astroturf_score, text_similarity
from .types import (
    Confidence,
    MarketIntel,
    NarrativeIntel,
    Provenance,
    SecurityIntel,
    TokenDossier,
    WalletIntel,
)

__all__ = [
    "Cluster",
    "Confidence",
    "DossierStore",
    "EntityGraph",
    "IntelEngine",
    "MarketIntel",
    "NarrativeEngine",
    "NarrativeIntel",
    "Provenance",
    "SecurityIntel",
    "TokenDossier",
    "UnionFind",
    "WalletIntel",
    "WalletNode",
    "astroturf_score",
    "fuse_market",
    "fuse_security",
    "text_similarity",
]
