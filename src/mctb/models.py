"""Event and state models shared across all three devices.

Every message on the bus is one of these. Because Scout, Brain and Console
are separate processes on separate machines, this module is the contract
between them — change it and you must redeploy all three.
"""

from __future__ import annotations

import time
import uuid
from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, Field

LAMPORTS_PER_SOL = 1_000_000_000
PUMP_TOKEN_DECIMALS = 6


def now_ts() -> float:
    return time.time()


def new_id() -> str:
    return uuid.uuid4().hex[:16]


class Topic(str, Enum):
    """Bus channel names."""

    TOKEN_NEW = "mctb.token.new"
    TOKEN_TRADE = "mctb.token.trade"
    WALLET_TRADE = "mctb.wallet.trade"
    MIGRATION = "mctb.migration"
    SOCIAL = "mctb.social"
    CANDIDATE = "mctb.candidate"
    DECISION = "mctb.decision"
    FILL = "mctb.fill"
    POSITION = "mctb.position"
    SNAPSHOT = "mctb.snapshot"
    CONTROL = "mctb.control"
    HEARTBEAT = "mctb.heartbeat"
    ALERT = "mctb.alert"
    LLM_NOTE = "mctb.llm"


class Envelope(BaseModel):
    """Common header on every bus message."""

    id: str = Field(default_factory=new_id)
    ts: float = Field(default_factory=now_ts)
    source: str = "unknown"


# --------------------------------------------------------------------------
#  Market data events (published by Scout)
# --------------------------------------------------------------------------


class NewTokenEvent(Envelope):
    mint: str
    name: str = ""
    symbol: str = ""
    creator: str = ""
    uri: str = ""
    # SOL the creator put in at launch.
    initial_buy_sol: float = 0.0
    sol_in_curve: float = 0.0
    market_cap_sol: float = 0.0
    signature: str = ""
    pool: str = "pump"
    raw: dict[str, Any] = Field(default_factory=dict)


class TradeEvent(Envelope):
    mint: str
    trader: str
    is_buy: bool
    sol_amount: float = 0.0
    token_amount: float = 0.0
    sol_in_curve: float = 0.0
    market_cap_sol: float = 0.0
    signature: str = ""
    slot: int = 0
    raw: dict[str, Any] = Field(default_factory=dict)

    @property
    def price_sol(self) -> float:
        """Realised price of this specific trade, in SOL per token."""
        if self.token_amount <= 0:
            return 0.0
        return self.sol_amount / self.token_amount


class MigrationEvent(Envelope):
    mint: str
    pool: str = "raydium"
    raw: dict[str, Any] = Field(default_factory=dict)


class SocialEvent(Envelope):
    platform: Literal["x", "telegram", "manual"] = "x"
    author: str = ""
    author_followers: int = 0
    text: str = ""
    url: str = ""
    # Mints or $TICKERs extracted from the text.
    mentioned_mints: list[str] = Field(default_factory=list)
    mentioned_symbols: list[str] = Field(default_factory=list)


# --------------------------------------------------------------------------
#  Analysis (Scout -> Brain -> Console)
# --------------------------------------------------------------------------


class TokenSafety(BaseModel):
    """On-chain checks. `passed` False means do not touch it."""

    checked: bool = False
    mint_authority_revoked: bool = False
    freeze_authority_revoked: bool = False
    dev_holding_pct: float = 1.0
    top10_holding_pct: float = 1.0
    holder_count: int = 0
    bundle_pct: float = 0.0
    lp_burned: bool = False
    passed: bool = False
    failures: list[str] = Field(default_factory=list)
    error: str | None = None


class Features(BaseModel):
    """The numeric fingerprint of a candidate at decision time.

    Stored verbatim with every trade so the personal-edge layer can learn
    which feature values actually made you money.
    """

    age_s: float = 0.0
    sol_in_curve: float = 0.0
    market_cap_sol: float = 0.0
    unique_buyers: int = 0
    buy_count: int = 0
    sell_count: int = 0
    buy_sell_ratio: float = 0.0
    net_sol_flow: float = 0.0
    # SOL/second of net inflow over the recent window — momentum.
    velocity_sol_per_s: float = 0.0
    smart_wallet_count: int = 0
    smart_wallet_score: float = 0.0
    social_mentions: int = 0
    social_reach: float = 0.0
    dev_holding_pct: float = 1.0
    top10_holding_pct: float = 1.0
    bundle_pct: float = 0.0
    creator_prior_rugs: int = 0
    creator_prior_wins: int = 0
    price_sol: float = 0.0


class Candidate(Envelope):
    """A token Scout thinks is worth a decision."""

    mint: str
    name: str = ""
    symbol: str = ""
    creator: str = ""
    features: Features = Field(default_factory=Features)
    safety: TokenSafety = Field(default_factory=TokenSafety)
    smart_wallets: list[str] = Field(default_factory=list)
    trigger: Literal["new_token", "smart_wallet", "social", "momentum"] = "new_token"


class ScoreBreakdown(BaseModel):
    """Explains exactly why a score is what it is — no black boxes."""

    base_score: float = 0.0
    components: dict[str, float] = Field(default_factory=dict)
    personal_edge_adjust: float = 0.0
    llm_adjust: float = 0.0
    final_score: float = 0.0
    gate_failures: list[str] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)


class Decision(Envelope):
    mint: str
    symbol: str = ""
    action: Literal["buy", "skip"] = "skip"
    score: float = 0.0
    size_sol: float = 0.0
    breakdown: ScoreBreakdown = Field(default_factory=ScoreBreakdown)
    reason: str = ""
    llm_reasoning: str = ""


class Fill(Envelope):
    """A completed (or failed) execution."""

    mint: str
    symbol: str = ""
    side: Literal["buy", "sell"]
    ok: bool = True
    sol_amount: float = 0.0
    token_amount: float = 0.0
    price_sol: float = 0.0
    fee_sol: float = 0.0
    signature: str = ""
    paper: bool = True
    latency_ms: float = 0.0
    error: str | None = None
    # For sells: which rule fired.
    exit_reason: str = ""


class PositionState(str, Enum):
    OPENING = "opening"
    OPEN = "open"
    CLOSING = "closing"
    CLOSED = "closed"
    FAILED = "failed"


class Position(BaseModel):
    id: str = Field(default_factory=new_id)
    mint: str
    symbol: str = ""
    name: str = ""
    state: PositionState = PositionState.OPENING
    opened_at: float = Field(default_factory=now_ts)
    closed_at: float | None = None

    entry_price_sol: float = 0.0
    cost_sol: float = 0.0
    tokens_held: float = 0.0
    tokens_bought: float = 0.0

    current_price_sol: float = 0.0
    last_price_ts: float = 0.0
    peak_price_sol: float = 0.0

    realised_sol: float = 0.0
    fees_sol: float = 0.0

    # Which ladder rungs have already been sold.
    ladder_filled: list[int] = Field(default_factory=list)
    trailing_armed: bool = False

    entry_score: float = 0.0
    entry_features: Features = Field(default_factory=Features)
    smart_wallets: list[str] = Field(default_factory=list)
    creator: str = ""
    exit_reason: str = ""
    paper: bool = True

    # --- Derived -----------------------------------------------------------
    @property
    def value_sol(self) -> float:
        """Mark-to-market value of tokens still held."""
        return self.tokens_held * self.current_price_sol

    @property
    def unrealised_pnl_sol(self) -> float:
        cost_remaining = (
            self.cost_sol * (self.tokens_held / self.tokens_bought)
            if self.tokens_bought > 0
            else 0.0
        )
        return self.value_sol - cost_remaining

    @property
    def total_pnl_sol(self) -> float:
        if self.state == PositionState.CLOSED:
            return self.realised_sol - self.cost_sol - self.fees_sol
        return self.realised_sol + self.value_sol - self.cost_sol - self.fees_sol

    @property
    def pnl_pct(self) -> float:
        if self.cost_sol <= 0:
            return 0.0
        return self.total_pnl_sol / self.cost_sol

    @property
    def multiple(self) -> float:
        """Current price as a multiple of entry price."""
        if self.entry_price_sol <= 0:
            return 1.0
        return self.current_price_sol / self.entry_price_sol

    @property
    def age_s(self) -> float:
        end = self.closed_at or now_ts()
        return end - self.opened_at


class WalletStat(BaseModel):
    """Reputation of a wallet we might copy.

    `score` is what actually drives sizing, and it is learned from how that
    wallet's calls performed *for you*, not from a public leaderboard.
    """

    address: str
    label: str = ""
    seeded: bool = False
    tokens_bought: int = 0
    wins: int = 0
    losses: int = 0
    copied_trades: int = 0
    copied_pnl_sol: float = 0.0
    avg_entry_sol_in_curve: float = 0.0
    last_seen: float = Field(default_factory=now_ts)
    score: float = 0.5
    subscribed: bool = False

    @property
    def win_rate(self) -> float:
        total = self.wins + self.losses
        return self.wins / total if total else 0.0


class PortfolioSnapshot(Envelope):
    """Full Brain state, published for the Console to mirror."""

    mode: str = "paper"
    bankroll_sol: float = 0.0
    starting_bankroll_sol: float = 0.0
    positions: list[Position] = Field(default_factory=list)
    realised_pnl_sol: float = 0.0
    unrealised_pnl_sol: float = 0.0
    daily_pnl_sol: float = 0.0
    trades_today: int = 0
    wins_today: int = 0
    losses_today: int = 0
    halted: bool = False
    halt_reason: str = ""
    top_wallets: list[WalletStat] = Field(default_factory=list)


class ControlCommand(Envelope):
    """Console -> Brain commands."""

    command: Literal[
        "kill",
        "resume",
        "close_position",
        "close_all",
        "request_snapshot",
        "set_mode",
        "reload_config",
    ]
    mint: str | None = None
    value: str | None = None


class Heartbeat(Envelope):
    service: str
    healthy: bool = True
    detail: dict[str, Any] = Field(default_factory=dict)


class Alert(Envelope):
    level: Literal["info", "warning", "critical"] = "info"
    title: str = ""
    message: str = ""


class LLMNote(Envelope):
    kind: Literal["candidate", "digest", "review"] = "candidate"
    mint: str = ""
    text: str = ""
    data: dict[str, Any] = Field(default_factory=dict)


# Topic -> model, so the bus can deserialise into the right type.
TOPIC_MODELS: dict[str, type[BaseModel]] = {
    Topic.TOKEN_NEW.value: NewTokenEvent,
    Topic.TOKEN_TRADE.value: TradeEvent,
    Topic.WALLET_TRADE.value: TradeEvent,
    Topic.MIGRATION.value: MigrationEvent,
    Topic.SOCIAL.value: SocialEvent,
    Topic.CANDIDATE.value: Candidate,
    Topic.DECISION.value: Decision,
    Topic.FILL.value: Fill,
    Topic.SNAPSHOT.value: PortfolioSnapshot,
    Topic.CONTROL.value: ControlCommand,
    Topic.HEARTBEAT.value: Heartbeat,
    Topic.ALERT.value: Alert,
    Topic.LLM_NOTE.value: LLMNote,
}
