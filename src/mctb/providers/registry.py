"""Provider registry — construction, routing and lifecycle.

The rest of the system never names a provider. It asks the registry for a
capability and gets back whoever is configured, healthy and fast enough for
the urgency of the request. That indirection is what makes the whole stack
optional: unplug every API key and the trading logic is unchanged, it just has
less to go on.

Routing rules, in order:
  1. Only providers that declare the capability and are `available`.
  2. Only providers at or below the requested tier (an urgent request never
     waits on a MANUAL provider).
  3. Ordered by tier, then by observed success rate, then by latency.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from .base import Capability, Provider, ProviderResult, ProviderStatus, Tier, gather_providers
from .dexscreener import DexScreenerProvider
from .execution import JupiterProvider, MeteoraProvider, RaydiumProvider
from .links import BullXProvider, GmgnProvider, PhotonProvider, PumpFunLink, SolscanLink
from .market import BirdeyeProvider, DexToolsProvider
from .onchain import DuneProvider, HeliusProvider, SolscanProvider
from .security import GoPlusProvider, RugcheckProvider, TokenSnifferProvider
from .smartmoney import ArkhamProvider, BubblemapsProvider, CieloProvider, NansenProvider
from .social import KaitoProvider, LunarCrushProvider

log = logging.getLogger(__name__)


#: Every provider class, keyed by the name used in config and `.env`.
PROVIDER_CLASSES: dict[str, type[Provider]] = {
    # Free, keyless — the backbone.
    "dexscreener": DexScreenerProvider,
    "goplus": GoPlusProvider,
    "rugcheck": RugcheckProvider,
    "jupiter": JupiterProvider,
    "raydium": RaydiumProvider,
    "meteora": MeteoraProvider,
    # Key required.
    "birdeye": BirdeyeProvider,
    "helius": HeliusProvider,
    "solscan": SolscanProvider,
    "dune": DuneProvider,
    "cielo": CieloProvider,
    "nansen": NansenProvider,
    "arkham": ArkhamProvider,
    "lunarcrush": LunarCrushProvider,
    "kaito": KaitoProvider,
    "dextools": DexToolsProvider,
    "tokensniffer": TokenSnifferProvider,
    # Opt-in / link-only.
    "gmgn": GmgnProvider,
    "bubblemaps": BubblemapsProvider,
    "photon": PhotonProvider,
    "bullx": BullXProvider,
    "solscan_link": SolscanLink,
    "pumpfun_link": PumpFunLink,
}


class ProviderRegistry:
    """Owns every provider instance and routes capability requests."""

    def __init__(self, config: Any) -> None:
        self.config = config
        self.providers: dict[str, Provider] = {}
        self._by_capability: dict[Capability, list[Provider]] = {}
        self._started = False

    # -- construction -------------------------------------------------------

    def build(self) -> None:
        """Instantiate providers according to config and available keys."""
        provider_cfg = getattr(self.config, "providers", None)
        keys = getattr(provider_cfg, "keys", {}) if provider_cfg else {}
        enabled = getattr(provider_cfg, "enabled", None) if provider_cfg else None
        disabled = set(getattr(provider_cfg, "disabled", []) or []) if provider_cfg else set()

        for name, cls in PROVIDER_CLASSES.items():
            if name in disabled:
                continue
            if enabled and name not in enabled:
                continue

            api_key = keys.get(name) or None
            try:
                if name == "gmgn":
                    opt_in = bool(getattr(provider_cfg, "gmgn_opt_in", False))
                    provider = cls(self.config, api_key, enabled=opt_in)  # type: ignore[call-arg]
                elif name == "dextools":
                    plan = getattr(provider_cfg, "dextools_plan", "trial")
                    provider = cls(self.config, api_key, plan=plan)  # type: ignore[call-arg]
                else:
                    provider = cls(self.config, api_key)
            except Exception as exc:  # noqa: BLE001
                log.error("could not construct provider %s: %s", name, exc)
                continue

            self.providers[name] = provider

        self._reindex()

        ready = [p.name for p in self.providers.values() if p.available]
        unconfigured = [
            p.name for p in self.providers.values()
            if p.status == ProviderStatus.UNCONFIGURED
        ]
        log.info(
            "providers: %d ready (%s)%s",
            len(ready),
            ", ".join(sorted(ready)) or "none",
            f"; {len(unconfigured)} awaiting keys" if unconfigured else "",
        )

    def _reindex(self) -> None:
        self._by_capability = {}
        for provider in self.providers.values():
            for capability in provider.capabilities:
                self._by_capability.setdefault(capability, []).append(provider)

    async def start(self) -> None:
        if self._started:
            return
        # Start in parallel — twenty sequential TLS handshakes is a slow boot.
        await asyncio.gather(
            *(self._safe_start(p) for p in self.providers.values()), return_exceptions=True
        )
        self._started = True

    @staticmethod
    async def _safe_start(provider: Provider) -> None:
        try:
            await provider.start()
        except Exception as exc:  # noqa: BLE001
            log.warning("provider %s failed to start: %s", provider.name, exc)

    async def close(self) -> None:
        await asyncio.gather(
            *(p.close() for p in self.providers.values()), return_exceptions=True
        )
        self._started = False

    # -- routing ------------------------------------------------------------

    def get(self, name: str) -> Provider | None:
        return self.providers.get(name)

    def for_capability(
        self, capability: Capability, max_tier: Tier = Tier.COLD
    ) -> list[Provider]:
        """Providers that can serve this capability, best first."""
        candidates = [
            p
            for p in self._by_capability.get(capability, [])
            if p.available and p.tier.value <= max_tier.value
        ]
        candidates.sort(
            key=lambda p: (
                p.tier.value,
                -p.health.success_rate,
                p.health.avg_latency_ms or 0.0,
            )
        )
        return candidates

    def has(self, capability: Capability, max_tier: Tier = Tier.COLD) -> bool:
        return bool(self.for_capability(capability, max_tier))

    # -- fan-out helpers ----------------------------------------------------

    async def query_all(
        self,
        capability: Capability,
        method: str,
        *args: Any,
        max_tier: Tier = Tier.COLD,
        timeout_s: float = 3.0,
        limit: int = 4,
        **kwargs: Any,
    ) -> list[ProviderResult]:
        """Ask every capable provider at once, under one shared deadline.

        This is the core fusion primitive: several independent answers to the
        same question, so `intel/` can cross-check them. Slow providers are
        abandoned at the deadline rather than delaying the result.
        """
        providers = self.for_capability(capability, max_tier)[:limit]
        if not providers:
            return []

        coros = []
        for provider in providers:
            fn = getattr(provider, method, None)
            if fn is None:
                continue
            coros.append(self._guard(provider, fn, *args, **kwargs))

        return await gather_providers(coros, timeout_s)

    async def query_first(
        self,
        capability: Capability,
        method: str,
        *args: Any,
        max_tier: Tier = Tier.COLD,
        timeout_s: float = 3.0,
        **kwargs: Any,
    ) -> ProviderResult | None:
        """Try providers in preference order, return the first usable answer.

        Used where one good answer is enough and extra calls would only burn
        rate limit — market data, for instance, where DEX Screener alone is
        normally sufficient.
        """
        for provider in self.for_capability(capability, max_tier):
            fn = getattr(provider, method, None)
            if fn is None:
                continue
            try:
                result = await asyncio.wait_for(
                    self._guard(provider, fn, *args, **kwargs), timeout=timeout_s
                )
            except (asyncio.TimeoutError, Exception):  # noqa: BLE001
                continue
            if result and result.usable:
                return result
        return None

    @staticmethod
    async def _guard(provider: Provider, fn: Any, *args: Any, **kwargs: Any) -> ProviderResult:
        """Never let a provider exception escape into a trading loop."""
        try:
            return await fn(*args, **kwargs)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            log.debug("provider %s raised: %s", provider.name, exc)
            provider.health.record(False, 0.0, str(exc))
            return ProviderResult.failure(provider.name, f"{type(exc).__name__}: {exc}")

    # -- health / introspection --------------------------------------------

    async def probe_all(self, timeout_s: float = 12.0) -> dict[str, bool]:
        """Liveness check across everything. Used by preflight and the terminal."""
        names = [n for n, p in self.providers.items() if p.available]

        async def _probe(name: str) -> tuple[str, bool]:
            try:
                return name, await asyncio.wait_for(
                    self.providers[name].probe(), timeout=timeout_s
                )
            except Exception:  # noqa: BLE001
                return name, False

        results = await asyncio.gather(*(_probe(n) for n in names), return_exceptions=True)
        return {
            r[0]: r[1] for r in results if isinstance(r, tuple)
        }

    def describe(self) -> list[dict[str, Any]]:
        rows = [p.describe() for p in self.providers.values()]
        rows.sort(key=lambda r: (r["status"] != "ready", r["tier"], r["name"]))
        return rows

    def http_stats(self) -> dict[str, Any]:
        out: dict[str, Any] = {}
        for name, provider in self.providers.items():
            http = getattr(provider, "http", None)
            if http is not None and hasattr(http, "snapshot"):
                out[name] = http.snapshot()
        return out

    def capability_matrix(self) -> dict[str, list[str]]:
        """Which providers can currently answer each capability.

        Rendered on the terminal so you can see at a glance what your key
        configuration actually buys you — and what a missing key costs.
        """
        matrix: dict[str, list[str]] = {}
        for capability in Capability:
            available = [p.name for p in self.for_capability(capability, Tier.MANUAL)]
            matrix[capability.value] = available
        return matrix

    def coverage_report(self) -> dict[str, Any]:
        """Honest summary of what is and is not covered."""
        critical = [
            Capability.TOKEN_MARKET,
            Capability.TOKEN_SECURITY,
            Capability.SWAP_QUOTE,
        ]
        gaps = [c.value for c in critical if not self.has(c)]
        return {
            "total": len(self.providers),
            "ready": sum(1 for p in self.providers.values() if p.available),
            "unconfigured": sum(
                1 for p in self.providers.values() if p.status == ProviderStatus.UNCONFIGURED
            ),
            "link_only": sum(
                1 for p in self.providers.values() if p.status == ProviderStatus.NO_PUBLIC_API
            ),
            "critical_gaps": gaps,
            # True when every critical capability has at least one provider.
            "healthy": not gaps,
        }
