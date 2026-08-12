"""Unified intel models.

Providers return whatever shape their vendor chose. These are the shapes the
rest of the system actually consumes — one vocabulary, with provenance and a
confidence attached to every fused field.

The provenance is not decoration. When a trade goes wrong the first question
is always "did we know?", and the answer has to be recoverable months later
from the stored record.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from pydantic import BaseModel, Field


class Confidence(str, Enum):
    """How much to trust a fused field."""

    NONE = "none"           # nobody answered
    LOW = "low"             # one source, or sources disagree
    MEDIUM = "medium"       # one trusted source, or several weak agreeing ones
    HIGH = "high"           # multiple independent sources agree


@dataclass
class Provenance:
    """Who said what, and who disagreed."""

    sources: list[str] = field(default_factory=list)
    disagreed: list[str] = field(default_factory=list)
    missing: list[str] = field(default_factory=list)
    fetched_at: float = field(default_factory=time.time)
    latency_ms: float = 0.0

    @property
    def source_count(self) -> int:
        return len(self.sources)

    @property
    def contested(self) -> bool:
        return bool(self.disagreed)

    def confidence(self) -> Confidence:
        if not self.sources:
            return Confidence.NONE
        if self.disagreed:
            return Confidence.LOW
        if len(self.sources) >= 2:
            return Confidence.HIGH
        return Confidence.MEDIUM

    def to_dict(self) -> dict[str, Any]:
        return {
            "sources": self.sources,
            "disagreed": self.disagreed,
            "missing": self.missing,
            "confidence": self.confidence().value,
            "age_s": round(time.time() - self.fetched_at, 1),
            "latency_ms": round(self.latency_ms, 1),
        }


class SecurityIntel(BaseModel):
    """Fused verdict on whether a token can be safely bought and sold."""

    mint: str = ""

    # Hard blockers — any True means do not touch it.
    mint_authority_live: bool = False
    freeze_authority_live: bool = False
    is_honeypot: bool = False
    non_transferable: bool = False
    has_transfer_fee: bool = False
    has_permanent_delegate: bool = False
    balance_mutable: bool = False

    # Graded risk.
    risk_score: float = 50.0            # 0 = clean, 100 = certain rug
    top10_holding_pct: float = 0.0
    dev_holding_pct: float = 0.0
    holder_count: int = 0
    lp_burned: bool = False
    lp_locked_pct: float = 0.0

    # Market-derived: can a sell route actually be built right now?
    sellable: bool = True
    sell_impact_pct: float = 0.0
    round_trip_cost_pct: float = 0.0

    risk_flags: list[str] = Field(default_factory=list)
    blockers: list[str] = Field(default_factory=list)
    provenance: dict[str, Any] = Field(default_factory=dict)

    @property
    def safe(self) -> bool:
        return not self.blockers

    @property
    def confidence(self) -> str:
        return self.provenance.get("confidence", "none")


class MarketIntel(BaseModel):
    """Fused price, liquidity and flow."""

    mint: str = ""
    symbol: str = ""
    name: str = ""

    price_usd: float = 0.0
    price_sol: float = 0.0
    liquidity_usd: float = 0.0
    market_cap: float = 0.0
    fdv: float = 0.0

    volume_5m: float = 0.0
    volume_1h: float = 0.0
    volume_24h: float = 0.0

    buys_5m: int = 0
    sells_5m: int = 0
    buys_1h: int = 0
    sells_1h: int = 0

    change_5m: float = 0.0
    change_1h: float = 0.0
    change_24h: float = 0.0

    unique_wallets_1h: int = 0
    holder_count: int = 0
    age_s: float = 0.0
    dex: str = ""
    boosted: int = 0

    # Derived quality measures — see market.py for why these matter.
    volume_to_liquidity: float = 0.0
    wash_trade_suspicion: float = 0.0

    provenance: dict[str, Any] = Field(default_factory=dict)

    @property
    def buy_sell_ratio_5m(self) -> float:
        return self.buys_5m / self.sells_5m if self.sells_5m else float(self.buys_5m)


class WalletIntel(BaseModel):
    """What we know about one wallet, from all sources plus our own history."""

    address: str = ""
    labels: list[str] = Field(default_factory=list)

    # Third-party performance claims — treat sceptically, see smartmoney.py.
    external_pnl_usd: float = 0.0
    external_winrate: float = 0.0
    external_tokens_traded: int = 0

    # Our own record, which always outranks the above.
    local_score: float = 0.5
    local_copies: int = 0
    local_pnl_sol: float = 0.0

    # Entity resolution.
    cluster_id: str = ""
    cluster_size: int = 1
    first_funder: str = ""
    funded_at: float = 0.0
    is_fresh_wallet: bool = False

    # Classification.
    is_exchange: bool = False
    is_bot: bool = False
    is_insider: bool = False

    provenance: dict[str, Any] = Field(default_factory=dict)

    @property
    def trustworthy(self) -> bool:
        """Worth acting on: our own record is good and it is not a bot,
        an exchange, or one node of a sybil cluster."""
        return (
            self.local_score >= 0.6
            and not self.is_exchange
            and not self.is_bot
            and self.cluster_size <= 3
        )


class NarrativeIntel(BaseModel):
    """Social attention, and whether it looks organic."""

    mint: str = ""
    symbol: str = ""

    mention_count: int = 0
    reach_score: float = 0.0
    sentiment: float = 0.0
    galaxy_score: float = 0.0
    social_volume_24h: int = 0
    contributors: int = 0

    # Shape, which matters more than level.
    velocity: float = 0.0               # mentions per minute, recent
    acceleration: float = 0.0           # is it still accelerating
    astroturf_score: float = 0.0        # 0..1, higher = more manufactured
    quality_accounts: int = 0           # accounts with real attention history

    themes: list[str] = Field(default_factory=list)
    provenance: dict[str, Any] = Field(default_factory=dict)

    @property
    def organic(self) -> bool:
        return self.astroturf_score < 0.5


class TokenDossier(BaseModel):
    """Everything known about one token — the terminal's main object."""

    mint: str
    symbol: str = ""
    name: str = ""
    creator: str = ""

    security: SecurityIntel = Field(default_factory=SecurityIntel)
    market: MarketIntel = Field(default_factory=MarketIntel)
    narrative: NarrativeIntel = Field(default_factory=NarrativeIntel)

    smart_wallets: list[WalletIntel] = Field(default_factory=list)
    cluster_warnings: list[str] = Field(default_factory=list)

    # Structured entity findings. The gate reads these numbers directly —
    # `cluster_warnings` is prose for humans and must never be parsed.
    insider_pct: float = 0.0
    insider_count: int = 0
    fresh_wallet_pct: float = 0.0

    links: dict[str, str] = Field(default_factory=dict)
    assembled_at: float = Field(default_factory=time.time)
    assembly_ms: float = 0.0
    tier_used: str = "warm"

    @property
    def age_s(self) -> float:
        return time.time() - self.assembled_at

    def summary_line(self) -> str:
        return (
            f"{self.symbol or self.mint[:10]} "
            f"${self.market.price_usd:.8f} "
            f"liq ${self.market.liquidity_usd:,.0f} "
            f"risk {self.security.risk_score:.0f} "
            f"{'SAFE' if self.security.safe else 'BLOCKED'}"
        )
