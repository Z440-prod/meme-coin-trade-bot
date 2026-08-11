"""Configuration schema.

Everything tunable lives here and is validated at startup, so a typo in YAML
fails immediately and loudly instead of silently trading with a default you
did not intend.

Load order (later overrides earlier):
    1. defaults defined in this file
    2. config/config.yaml
    3. the strategy preset named by `strategy_preset`
    4. environment variables (secrets only)
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, Field, model_validator

REPO_ROOT = Path(__file__).resolve().parents[2]


class BusConfig(BaseModel):
    redis_url: str = "redis://127.0.0.1:6379/0"
    redis_password: str | None = None
    # Durable audit log of every event, replayable after a crash.
    journal_maxlen: int = 200_000


class RpcConfig(BaseModel):
    url: str = "https://api.mainnet-beta.solana.com"
    ws_url: str = "wss://api.mainnet-beta.solana.com"
    fallback_url: str | None = None
    timeout_s: float = 10.0
    max_retries: int = 3
    # Commitment for reads. "processed" is fastest and what you want for
    # sniping; "confirmed" is safer for accounting.
    read_commitment: Literal["processed", "confirmed", "finalized"] = "confirmed"
    price_commitment: Literal["processed", "confirmed", "finalized"] = "processed"


class SocialConfig(BaseModel):
    enabled: bool = False
    # X/Twitter accounts whose posts count as signal. Handles, no '@'.
    x_accounts: list[str] = Field(default_factory=list)
    # Free-text queries for X recent-search polling.
    x_queries: list[str] = Field(default_factory=list)
    # Seconds between polls. X bills per request on the current pay-per-use
    # plans, so do not set this aggressively low without checking your quota.
    poll_interval_s: float = 30.0
    # A mention only counts toward scoring for this long.
    mention_ttl_s: float = 900.0
    # Ignore accounts below this follower count when they are discovered
    # indirectly (retweets, quotes).
    min_followers: int = 5_000


class LLMConfig(BaseModel):
    enabled: bool = True
    base_url: str = "http://127.0.0.1:8080"
    model: str = "local"
    # HARD timeout. The LLM is advisory; if it is slow we trade without it.
    timeout_s: float = 6.0
    max_tokens: int = 400
    temperature: float = 0.2
    # Only ask the LLM about candidates that already cleared this deterministic
    # score. Keeps the Pi from melting and keeps latency sane.
    min_score_to_consult: float = 55.0
    # How much the LLM is allowed to move the final score, in points.
    # It can veto (large negative) but it cannot manufacture conviction.
    max_bonus: float = 12.0
    max_penalty: float = 40.0
    # Nightly self-review of your own trade history.
    nightly_review: bool = True
    nightly_review_hour_utc: int = 4
    # If true, bounded parameter tweaks proposed by the review are applied
    # automatically. Off by default — read the report yourself first.
    auto_apply_tuning: bool = False


class GatesConfig(BaseModel):
    """Hard pass/fail filters. Any failure vetoes the trade outright.

    These are not scored — they are binary. A token that fails a gate is
    never bought no matter how good everything else looks.
    """

    # Token must be at least this old (seconds). Blocks the very first blocks
    # where bundled snipers dominate and you are exit liquidity.
    min_age_s: float = 8.0
    # ...and no older than this. Stale launches are someone else's trade.
    max_age_s: float = 900.0

    min_sol_in_curve: float = 3.0
    max_sol_in_curve: float = 85.0

    min_unique_buyers: int = 8
    min_buy_sell_ratio: float = 1.5

    # Authorities must be revoked — otherwise the deployer can mint infinite
    # supply or freeze your tokens so you can never sell.
    require_mint_authority_revoked: bool = True
    require_freeze_authority_revoked: bool = True

    # Creator/dev holdings as a fraction of supply.
    max_dev_holding_pct: float = 0.05
    # Concentration among the largest holders.
    max_top10_holding_pct: float = 0.35
    # Fraction of supply bought in the same slot as creation — the signature
    # of a bundled launch where insiders are already positioned above you.
    max_bundle_pct: float = 0.20

    # Reject creators who have previously rugged you (learned locally).
    block_known_rug_creators: bool = True
    # Reject tokens whose name/symbol match your blocklist patterns.
    name_blocklist: list[str] = Field(default_factory=list)

    # Require at least one tracked smart wallet to have bought.
    require_smart_wallet: bool = False


class ScoringConfig(BaseModel):
    """Weights for the deterministic fast-path scorer.

    Each feature produces a normalised 0..1 sub-score; the weighted sum is
    rescaled to 0..100. Weights need not sum to anything in particular.
    """

    w_smart_wallets: float = 30.0
    w_buy_pressure: float = 18.0
    w_unique_buyers: float = 12.0
    w_curve_progress: float = 10.0
    w_holder_distribution: float = 12.0
    w_social: float = 10.0
    w_velocity: float = 15.0
    w_creator_history: float = 8.0

    # Minimum final score required to open a position.
    entry_threshold: float = 62.0

    # Let the locally-learned personal edge adjust the score. This is the
    # layer that makes the bot yours rather than generic.
    personal_edge_enabled: bool = True
    # Cap on how many points the learned edge may add or remove, so a small
    # or unlucky sample can never fully hijack your strategy.
    personal_edge_max_adjust: float = 15.0
    # Below this many observations a feature bucket is ignored entirely.
    personal_edge_min_samples: int = 12


class RiskConfig(BaseModel):
    # Absolute ceiling on a single position, in SOL.
    max_position_sol: float = 0.05
    # Position size as a fraction of current bankroll (whichever is smaller
    # of this and max_position_sol wins).
    position_pct_of_bankroll: float = 0.02
    # Never let the wallet drop below this — you need SOL for fees, and a
    # wallet at zero cannot even sell its way out.
    min_sol_reserve: float = 0.05

    max_concurrent_positions: int = 3
    max_positions_per_hour: int = 12
    max_sol_deployed_per_hour: float = 0.4

    # Halt trading for the rest of the UTC day after losing this much SOL.
    daily_loss_limit_sol: float = 0.25
    # Halt after this many consecutive losers...
    consecutive_loss_limit: int = 5
    # ...and stay halted for this long.
    cooldown_after_halt_s: float = 3600.0

    # Refuse to trade the same mint twice within this window.
    same_mint_cooldown_s: float = 3600.0

    # Kill switch file — `touch` it from anywhere to stop all trading now.
    kill_switch_file: str = "./KILL_SWITCH"


class ExitConfig(BaseModel):
    """Exit rules. This is where profit is actually realised or given back."""

    # Hard stop, as a fraction below entry. Memecoins gap hard; treat this as
    # a target, not a guarantee.
    stop_loss_pct: float = 0.35

    # Take-profit ladder: sell `fraction` of the ORIGINAL position once the
    # price reaches `multiple` x entry.
    take_profit_ladder: list[dict[str, float]] = Field(
        default_factory=lambda: [
            {"multiple": 1.6, "fraction": 0.34},
            {"multiple": 2.5, "fraction": 0.33},
            {"multiple": 5.0, "fraction": 0.20},
        ]
    )

    # Trailing stop arms once the position is up this much...
    trailing_activate_pct: float = 0.45
    # ...and then exits if price falls this far from the peak.
    trailing_distance_pct: float = 0.25

    # Bail out if the position has not reached `stagnant_min_gain_pct` after
    # this many seconds. Capital stuck in a flat coin is capital not working.
    max_hold_s: float = 900.0
    stagnant_after_s: float = 240.0
    stagnant_min_gain_pct: float = 0.10

    # Emergency exit triggers — these bypass the ladder and dump immediately.
    exit_on_dev_sell: bool = True
    exit_on_migration: bool = False
    # If the tracked smart wallets that got you in start selling, follow them.
    exit_on_smart_wallet_exit: bool = True
    # If price data goes stale for this long, assume something is wrong and out.
    stale_price_exit_s: float = 90.0


class ExecutionConfig(BaseModel):
    mode: Literal["paper", "live"] = "paper"

    # Second, independent safety latch. `mode: live` alone is NOT enough —
    # this must also be true. Two switches, so one careless edit cannot arm
    # real money.
    live_trading_enabled: bool = False

    # Slippage tolerance in percent, as PumpPortal expects it.
    buy_slippage_pct: float = 15.0
    sell_slippage_pct: float = 25.0

    # Priority fee in SOL. On a busy chain, too low means your buy lands late
    # and your sell does not land at all.
    priority_fee_sol: float = 0.0008
    # Sells get a larger fee — getting out matters more than getting in.
    exit_priority_fee_sol: float = 0.002

    pool: Literal["pump", "raydium", "pump-amm", "auto"] = "auto"

    # Retry a failed exit this many times, escalating the fee each attempt.
    exit_max_retries: int = 4
    exit_retry_fee_multiplier: float = 1.8

    # --- Paper-trading realism knobs ---------------------------------------
    # Simulated round-trip latency between decision and fill.
    paper_latency_ms: float = 400.0
    # Simulated adverse price movement on entry and exit.
    paper_slippage_pct: float = 4.0
    # pump.fun's trade fee.
    paper_fee_pct: float = 1.0
    paper_starting_bankroll_sol: float = 1.0


class WalletTrackingConfig(BaseModel):
    """Copy-trading. The 'follow successful traders' half of the system."""

    enabled: bool = True
    # Wallets you explicitly trust, highest weight.
    seed_wallets: list[str] = Field(default_factory=list)
    # Automatically discover wallets that bought tokens which later pumped.
    auto_discover: bool = True
    # A discovered wallet must have been early on this many winners...
    discovery_min_wins: int = 3
    # ...where a "win" is a token that did at least this multiple.
    discovery_win_multiple: float = 3.0
    # ...and it must have been in before this market cap.
    discovery_max_entry_sol: float = 20.0
    # Drop a tracked wallet whose score decays below this.
    prune_below_score: float = 0.25
    max_tracked_wallets: int = 150
    # Metered PumpPortal subscriptions cost SOL — cap how many we subscribe to.
    max_subscribed_wallets: int = 40


class ScoutConfig(BaseModel):
    """RPi 1 behaviour."""

    # Independent RPC log watcher, so PumpPortal is not a single point of
    # failure. Costs RPC quota — turn it off if you are on a free endpoint.
    enable_rpc_watcher: bool = True
    # Tokens held in the active watchlist and re-evaluated each pass.
    watchlist_size: int = 400
    # How often the watchlist is scored and candidates emitted.
    evaluate_interval_s: float = 2.0
    # Per-token trade streams are metered. Only the most promising tokens get
    # a live stream; the rest are tracked from curve reads alone.
    max_subscribed_tokens: int = 60
    # A token leaves the watchlist after this long without qualifying.
    watchlist_ttl_s: float = 1200.0
    # How long to watch a launch before grading its early buyers.
    wallet_grade_after_s: float = 1800.0
    # Skip the on-chain safety screen below this preliminary score. Saves RPC
    # quota on tokens that were never going to qualify anyway.
    min_prelim_score_for_safety_check: float = 30.0


class ConsoleConfig(BaseModel):
    host: str = "0.0.0.0"
    port: int = 8787
    # Open the trade page in a browser automatically when a position fills.
    auto_open_browser: bool = True
    browser_url_template: str = "https://pump.fun/coin/{mint}"
    # Supervisory watchdog: if the Brain stops publishing for this long, shout.
    brain_heartbeat_timeout_s: float = 20.0
    # Console-side backstop. Slightly wider than the Brain's own stop so it
    # only fires if the Brain has genuinely failed to act.
    supervisor_stop_loss_pct: float = 0.50
    supervisor_enabled: bool = True
    desktop_notifications: bool = True
    telegram_alerts: bool = False


class PathsConfig(BaseModel):
    data_dir: str = "./data"
    log_dir: str = "./logs"
    db_file: str = "./data/mctb.db"
    keystore_path: str = "./secrets/trading.keystore"


class Config(BaseModel):
    # Which device this process is. Set via --role or MCTB_ROLE.
    role: Literal["scout", "brain", "console"] = "scout"
    strategy_preset: str | None = "balanced"
    log_level: str = "INFO"

    bus: BusConfig = Field(default_factory=BusConfig)
    rpc: RpcConfig = Field(default_factory=RpcConfig)
    social: SocialConfig = Field(default_factory=SocialConfig)
    llm: LLMConfig = Field(default_factory=LLMConfig)
    gates: GatesConfig = Field(default_factory=GatesConfig)
    scoring: ScoringConfig = Field(default_factory=ScoringConfig)
    risk: RiskConfig = Field(default_factory=RiskConfig)
    exits: ExitConfig = Field(default_factory=ExitConfig)
    execution: ExecutionConfig = Field(default_factory=ExecutionConfig)
    wallets: WalletTrackingConfig = Field(default_factory=WalletTrackingConfig)
    scout: ScoutConfig = Field(default_factory=ScoutConfig)
    console: ConsoleConfig = Field(default_factory=ConsoleConfig)
    paths: PathsConfig = Field(default_factory=PathsConfig)

    # --- Secrets, populated from the environment only ------------------------
    pumpportal_api_key: str | None = None
    x_bearer_token: str | None = None
    telegram_bot_token: str | None = None
    telegram_chat_id: str | None = None
    keystore_passphrase: str | None = None

    @model_validator(mode="after")
    def _validate_coherence(self) -> Config:
        tp = self.exits.take_profit_ladder
        total = sum(step.get("fraction", 0.0) for step in tp)
        if total > 1.0 + 1e-9:
            raise ValueError(
                f"take_profit_ladder fractions sum to {total:.2f}, which is more than the "
                "whole position. Reduce them so they sum to <= 1.0."
            )
        for step in tp:
            if "multiple" not in step or "fraction" not in step:
                raise ValueError(
                    "each take_profit_ladder entry needs both 'multiple' and 'fraction'"
                )
            if step["multiple"] <= 1.0:
                raise ValueError(
                    f"take-profit multiple {step['multiple']} is <= 1.0, which would sell at "
                    "a loss the instant you buy. Multiples are absolute (2.0 == 2x entry)."
                )
        if not 0 < self.exits.stop_loss_pct < 1:
            raise ValueError("exits.stop_loss_pct must be a fraction strictly between 0 and 1")
        if self.risk.max_position_sol <= 0:
            raise ValueError("risk.max_position_sol must be positive")
        if self.execution.mode == "live" and not self.execution.live_trading_enabled:
            # Not an error — this is the safety latch doing its job. The Brain
            # logs loudly and falls back to paper.
            pass
        return self

    # --- Convenience ---------------------------------------------------------
    @property
    def is_live(self) -> bool:
        """True only when BOTH safety latches are set."""
        return self.execution.mode == "live" and self.execution.live_trading_enabled

    def resolve(self, path_str: str) -> Path:
        """Resolve a possibly-relative config path against the repo root."""
        p = Path(path_str).expanduser()
        return p if p.is_absolute() else (REPO_ROOT / p).resolve()


def _deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    out = dict(base)
    for key, value in override.items():
        if key in out and isinstance(out[key], dict) and isinstance(value, dict):
            out[key] = _deep_merge(out[key], value)
        else:
            out[key] = value
    return out


def _load_yaml(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    with path.open() as fh:
        return yaml.safe_load(fh) or {}


def _env_overrides() -> dict[str, Any]:
    """Secrets and per-machine endpoints come from the environment.

    Config files get committed and shared; environment files do not. So
    anything sensitive or machine-specific is read here, never from YAML.
    """
    env: dict[str, Any] = {}

    def put(path: tuple[str, ...], value: Any) -> None:
        if value in (None, ""):
            return
        cursor = env
        for part in path[:-1]:
            cursor = cursor.setdefault(part, {})
        cursor[path[-1]] = value

    put(("bus", "redis_url"), os.getenv("MCTB_REDIS_URL"))
    put(("bus", "redis_password"), os.getenv("MCTB_REDIS_PASSWORD"))
    put(("rpc", "url"), os.getenv("MCTB_RPC_URL"))
    put(("rpc", "ws_url"), os.getenv("MCTB_RPC_WS_URL"))
    put(("rpc", "fallback_url"), os.getenv("MCTB_RPC_URL_FALLBACK"))
    put(("llm", "base_url"), os.getenv("MCTB_LLM_BASE_URL"))
    put(("paths", "keystore_path"), os.getenv("MCTB_KEYSTORE_PATH"))
    put(("pumpportal_api_key",), os.getenv("MCTB_PUMPPORTAL_API_KEY"))
    put(("x_bearer_token",), os.getenv("MCTB_X_BEARER_TOKEN"))
    put(("telegram_bot_token",), os.getenv("MCTB_TELEGRAM_BOT_TOKEN"))
    put(("telegram_chat_id",), os.getenv("MCTB_TELEGRAM_CHAT_ID"))
    put(("keystore_passphrase",), os.getenv("MCTB_KEYSTORE_PASSPHRASE"))
    put(("role",), os.getenv("MCTB_ROLE"))
    return env


def load_env_file(path: Path | None = None) -> None:
    """Minimal .env loader so we do not need python-dotenv on a Pi.

    Existing environment variables always win, so `MCTB_ROLE=brain mctb run`
    overrides whatever the file says.
    """
    path = path or (REPO_ROOT / ".env")
    if not path.exists():
        return
    for raw in path.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key, value = key.strip(), value.strip().strip("'\"")
        os.environ.setdefault(key, value)


def load_config(
    config_path: Path | None = None,
    role: str | None = None,
    overrides: dict[str, Any] | None = None,
) -> Config:
    """Build the effective config from files + environment + explicit overrides."""
    load_env_file()

    config_path = config_path or (REPO_ROOT / "config" / "config.yaml")
    if not config_path.exists():
        example = REPO_ROOT / "config" / "config.example.yaml"
        raise FileNotFoundError(
            f"No config at {config_path}.\n"
            f"Create one with:  cp {example} {config_path}"
        )

    data = _load_yaml(config_path)

    preset_name = data.get("strategy_preset")
    if preset_name:
        preset_path = REPO_ROOT / "config" / "strategies" / f"{preset_name}.yaml"
        if not preset_path.exists():
            available = sorted(
                p.stem for p in (REPO_ROOT / "config" / "strategies").glob("*.yaml")
            )
            raise FileNotFoundError(
                f"strategy_preset '{preset_name}' not found at {preset_path}. "
                f"Available presets: {', '.join(available) or '(none)'}"
            )
        # The preset provides the base; config.yaml overrides it, so your
        # personal edits always beat the preset.
        data = _deep_merge(_load_yaml(preset_path), data)

    data = _deep_merge(data, _env_overrides())
    if role:
        data["role"] = role
    if overrides:
        data = _deep_merge(data, overrides)

    return Config.model_validate(data)
