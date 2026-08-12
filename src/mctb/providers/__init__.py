"""External data providers.

Every third-party service the system talks to lives here behind one interface,
so the trading logic never depends on any particular vendor being up, paid for,
or even in existence.

See `base.py` for the contract and `registry.py` for routing.
"""

from .base import (
    Capability,
    LinkOnlyProvider,
    Provider,
    ProviderHealth,
    ProviderResult,
    ProviderStatus,
    Tier,
)
from .links import all_token_links, all_wallet_links
from .registry import PROVIDER_CLASSES, ProviderRegistry

__all__ = [
    "PROVIDER_CLASSES",
    "Capability",
    "LinkOnlyProvider",
    "Provider",
    "ProviderHealth",
    "ProviderRegistry",
    "ProviderResult",
    "ProviderStatus",
    "Tier",
    "all_token_links",
    "all_wallet_links",
]
