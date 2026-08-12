"""Cross-provider fusion — turning many partial answers into one usable view.

This is the part that justifies integrating twenty services rather than one.
A single provider gives you data. Several independent providers give you
something better: **the ability to detect when data is wrong.**

Three rules govern every fused field:

**Blockers are OR, quality is consensus.** If any credible source says the
freeze authority is live, that is a blocker — one source saying "unsafe"
outweighs three saying "fine", because the cost of being wrong is asymmetric.
Graded quality signals work the opposite way and are averaged.

**Disagreement is recorded, not resolved silently.** When GoPlus and Rugcheck
disagree, both answers are kept, the field is marked contested, and confidence
drops. A contested "safe" is not the same as an uncontested one, and the
scorer treats it accordingly.

**Absence is not safety.** A provider that failed to answer contributes
nothing — it never counts as a clean bill of health. Fields nobody could
answer keep their pessimistic defaults.
"""

from __future__ import annotations

import logging
import statistics
import time

from ..providers import Capability, ProviderRegistry, Tier
from ..providers.links import all_token_links
from .types import MarketIntel, Provenance, SecurityIntel

log = logging.getLogger(__name__)


# How much each security provider's opinion is worth. Derived from coverage
# depth and Solana-specificity, not from vendor marketing.
SECURITY_WEIGHTS: dict[str, float] = {
    "rugcheck": 1.0,      # Solana-native, itemised findings, LP lock data
    "goplus": 1.0,        # Solana-native, authoritative on authorities
    "dextools": 0.6,      # decent, but EVM-shaped
    "tokensniffer": 0.4,  # thin Solana coverage
    "gmgn": 0.3,          # unofficial endpoints, low confidence by policy
}


def _agree(values: list[bool], threshold: float = 0.5) -> tuple[bool, bool]:
    """(consensus, contested) for a set of boolean opinions."""
    if not values:
        return False, False
    true_share = sum(values) / len(values)
    contested = 0.0 < true_share < 1.0
    return true_share >= threshold, contested


def _median(values: list[float]) -> float:
    """Median, not mean — one wrong provider should not drag the answer."""
    clean = [v for v in values if v is not None]
    return statistics.median(clean) if clean else 0.0


async def fuse_security(
    registry: ProviderRegistry,
    mint: str,
    *,
    max_tier: Tier = Tier.WARM,
    timeout_s: float = 3.0,
    check_sellable: bool = True,
    sell_test_tokens: int = 1_000_000,
) -> SecurityIntel:
    """Combine every security provider into one verdict.

    The `check_sellable` step is the one that catches what contract scans
    cannot: asking Jupiter to quote a sell is an end-to-end test of whether an
    exit route exists at all. A token can pass every authority check and still
    be untradeable because no pool will take it.
    """
    started = time.perf_counter()
    results = await registry.query_all(
        Capability.TOKEN_SECURITY,
        "get_security",
        mint,
        max_tier=max_tier,
        timeout_s=timeout_s,
        limit=4,
    )

    usable = [r for r in results if r.usable]
    prov = Provenance(
        sources=[r.provider for r in usable],
        missing=[r.provider for r in results if not r.usable],
        latency_ms=(time.perf_counter() - started) * 1000,
    )

    intel = SecurityIntel(mint=mint)

    if not usable:
        # Nothing answered. Stay pessimistic — an unscreened token is not a
        # safe token, it is an unknown one, and unknown does not get bought.
        intel.blockers.append("no security provider could screen this token")
        intel.risk_score = 100.0
        intel.provenance = prov.to_dict()
        return intel

    # --- Blockers: any credible source flagging them wins ------------------
    mint_live: list[bool] = []
    freeze_live: list[bool] = []
    honeypot: list[bool] = []

    for result in usable:
        d = result.data or {}
        if "mint_authority_revoked" in d:
            mint_live.append(not d["mint_authority_revoked"])
        if "freeze_authority_revoked" in d:
            freeze_live.append(not d["freeze_authority_revoked"])
        if "is_honeypot" in d:
            honeypot.append(bool(d["is_honeypot"]))

        # Token-2022 extensions that can silently prevent or tax selling.
        if d.get("non_transferable"):
            intel.non_transferable = True
        if d.get("transfer_fee"):
            intel.has_transfer_fee = True
        if d.get("permanent_delegate"):
            intel.has_permanent_delegate = True
        if d.get("balance_mutable"):
            intel.balance_mutable = True

        for name in d.get("risk_names") or []:
            if name and name not in intel.risk_flags:
                intel.risk_flags.append(name)

    # `any()` rather than majority: the downside is unbounded.
    intel.mint_authority_live = any(mint_live)
    intel.freeze_authority_live = any(freeze_live)
    intel.is_honeypot = any(honeypot)

    _, mint_contested = _agree(mint_live)
    _, freeze_contested = _agree(freeze_live)
    if mint_contested:
        prov.disagreed.append("mint_authority")
    if freeze_contested:
        prov.disagreed.append("freeze_authority")

    # --- Graded fields: median across providers ----------------------------
    def collect(key: str) -> list[float]:
        return [
            float((r.data or {})[key])
            for r in usable
            if (r.data or {}).get(key) is not None
        ]

    intel.top10_holding_pct = _median(collect("top10_holding_pct"))
    intel.holder_count = int(_median(collect("holder_count")) or _median(collect("total_holders")))
    intel.lp_locked_pct = _median(collect("lp_locked_pct"))
    intel.lp_burned = any((r.data or {}).get("lp_burned") for r in usable)

    creator_balance = _median(collect("creator_balance"))
    supply = _median(collect("supply")) or _median(collect("total_supply"))
    if supply > 0 and creator_balance > 0:
        intel.dev_holding_pct = creator_balance / supply

    # Weighted risk score across providers that produce one.
    weighted: list[tuple[float, float]] = []
    for result in usable:
        score = (result.data or {}).get("risk_score")
        if score is not None:
            weighted.append((float(score), SECURITY_WEIGHTS.get(result.provider, 0.5)))
    if weighted:
        total_weight = sum(w for _s, w in weighted)
        intel.risk_score = sum(s * w for s, w in weighted) / total_weight
    else:
        intel.risk_score = 30.0 if not intel.risk_flags else 60.0

    # --- The live sellability test -----------------------------------------
    if check_sellable:
        jupiter = registry.get("jupiter")
        if jupiter is not None and jupiter.available:
            try:
                check = await jupiter.check_sellable(mint, sell_test_tokens)  # type: ignore[attr-defined]
                if check.usable:
                    d = check.data or {}
                    intel.sellable = bool(d.get("sellable", True))
                    intel.sell_impact_pct = float(d.get("price_impact_pct") or 0)
                    prov.sources.append("jupiter")
                    if not intel.sellable:
                        intel.blockers.append(
                            f"no sell route exists ({d.get('reason', 'unknown')}) — "
                            "you could buy this and be unable to exit"
                        )
            except Exception as exc:  # noqa: BLE001
                log.debug("sellability check failed for %s: %s", mint, exc)

    # --- Assemble blockers --------------------------------------------------
    if intel.mint_authority_live:
        intel.blockers.append("mint authority live — supply can be inflated against you")
    if intel.freeze_authority_live:
        intel.blockers.append("freeze authority live — your tokens can be frozen (honeypot)")
    if intel.is_honeypot:
        intel.blockers.append("flagged as a honeypot")
    if intel.non_transferable:
        intel.blockers.append("token is non-transferable")
    if intel.has_permanent_delegate:
        intel.blockers.append("permanent delegate set — a third party can move your tokens")
    if intel.balance_mutable:
        intel.blockers.append("balance is mutable — your holdings can be edited")
    if intel.has_transfer_fee:
        # Not a hard blocker: some legitimate tokens use fees. Flagged so the
        # scorer can price it in.
        intel.risk_flags.append("transfer_fee")

    intel.provenance = prov.to_dict()
    return intel


async def fuse_market(
    registry: ProviderRegistry,
    mint: str,
    *,
    max_tier: Tier = Tier.WARM,
    timeout_s: float = 2.5,
    sol_price_usd: float = 0.0,
) -> MarketIntel:
    """Combine market-data providers into one price/liquidity view."""
    started = time.perf_counter()
    results = await registry.query_all(
        Capability.TOKEN_MARKET,
        "get_market",
        mint,
        max_tier=max_tier,
        timeout_s=timeout_s,
        limit=3,
    )
    usable = [r for r in results if r.usable]

    prov = Provenance(
        sources=[r.provider for r in usable],
        missing=[r.provider for r in results if not r.usable],
        latency_ms=(time.perf_counter() - started) * 1000,
    )

    intel = MarketIntel(mint=mint)
    if not usable:
        intel.provenance = prov.to_dict()
        return intel

    def pick(key: str, default: float = 0.0) -> float:
        values = [
            float((r.data or {})[key])
            for r in usable
            if (r.data or {}).get(key) is not None
        ]
        return _median(values) if values else default

    def first_str(key: str) -> str:
        for result in usable:
            value = (result.data or {}).get(key)
            if value:
                return str(value)
        return ""

    intel.symbol = first_str("symbol")
    intel.name = first_str("name")
    intel.dex = first_str("dex")

    intel.price_usd = pick("price_usd")
    intel.liquidity_usd = pick("liquidity_usd")
    intel.market_cap = pick("market_cap")
    intel.fdv = pick("fdv")
    intel.volume_5m = pick("volume_5m")
    intel.volume_1h = pick("volume_1h")
    intel.volume_24h = pick("volume_24h")
    intel.change_5m = pick("change_5m")
    intel.change_1h = pick("change_1h")
    intel.change_24h = pick("change_24h")
    intel.buys_5m = int(pick("buys_5m"))
    intel.sells_5m = int(pick("sells_5m"))
    intel.buys_1h = int(pick("buys_1h"))
    intel.sells_1h = int(pick("sells_1h"))
    intel.unique_wallets_1h = int(pick("unique_wallets_1h"))
    intel.holder_count = int(pick("holder_count"))
    intel.age_s = pick("age_s")
    intel.boosted = int(pick("boosted"))

    if sol_price_usd > 0 and intel.price_usd > 0:
        intel.price_sol = intel.price_usd / sol_price_usd

    # Price disagreement between independent sources is a genuine warning: it
    # usually means one is stale, and stale prices are what blow through stop
    # losses without firing them.
    prices = [
        float((r.data or {}).get("price_usd") or 0)
        for r in usable
        if (r.data or {}).get("price_usd")
    ]
    if len(prices) >= 2:
        spread = (max(prices) - min(prices)) / max(prices) if max(prices) else 0.0
        if spread > 0.15:
            prov.disagreed.append(f"price_spread_{spread:.0%}")

    # --- Derived quality measures ------------------------------------------
    if intel.liquidity_usd > 0:
        intel.volume_to_liquidity = intel.volume_24h / intel.liquidity_usd

        # Wash-trading heuristic. Enormous volume against thin liquidity with
        # very few unique wallets means the same coins cycling between the same
        # hands — the classic way to manufacture a chart that looks alive.
        # This is one of the few genuinely cheap edges available: the data is
        # free and most retail bots never compute it.
        suspicion = 0.0
        if intel.volume_to_liquidity > 20:
            suspicion += 0.4
        if intel.volume_to_liquidity > 60:
            suspicion += 0.3
        if intel.unique_wallets_1h and intel.volume_1h > 0:
            per_wallet = intel.volume_1h / max(1, intel.unique_wallets_1h)
            if per_wallet > intel.liquidity_usd * 0.5:
                suspicion += 0.3
        # A near-perfect buy/sell balance at high volume is a bot cycling.
        total_5m = intel.buys_5m + intel.sells_5m
        if total_5m > 40 and abs(intel.buys_5m - intel.sells_5m) / total_5m < 0.05:
            suspicion += 0.2
        intel.wash_trade_suspicion = min(1.0, suspicion)

    intel.provenance = prov.to_dict()
    return intel


async def fuse_all(
    registry: ProviderRegistry,
    mint: str,
    *,
    max_tier: Tier = Tier.WARM,
    timeout_s: float = 4.0,
) -> tuple[SecurityIntel, MarketIntel]:
    """Security and market together, in parallel under one deadline."""
    import asyncio

    security_task = asyncio.create_task(
        fuse_security(registry, mint, max_tier=max_tier, timeout_s=timeout_s * 0.75)
    )
    market_task = asyncio.create_task(
        fuse_market(registry, mint, max_tier=max_tier, timeout_s=timeout_s * 0.6)
    )
    security, market = await asyncio.gather(security_task, market_task)
    return security, market


def token_links(mint: str) -> dict[str, str]:
    return all_token_links(mint)
