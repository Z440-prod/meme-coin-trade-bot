"""On-chain safety screening — the checks that keep you out of unsellable tokens.

Most money lost to memecoin bots is not lost to bad price predictions. It is
lost to buying tokens that were never sellable in the first place. These
checks are cheap (two or three RPC reads) and they are the highest-value code
in this repository.

What we screen for:

  **Mint authority still live** — the deployer can mint unlimited new supply
  and dilute your position to nothing at will.

  **Freeze authority still live** — the deployer can freeze your token
  account. You keep the tokens forever and can never sell them at any price.
  This is the classic honeypot and it is completely invisible from a chart.

  **Concentrated holders** — if ten wallets hold most of the supply, your exit
  depends entirely on their patience.

  **Bundled launch** — buys landing in the same slot as creation mean insiders
  were positioned before the launch was public. You are their exit liquidity.

None of this makes a token *good*. It only means the token is not obviously
rigged, which is a much lower bar than it sounds.
"""

from __future__ import annotations

import logging
import struct
from dataclasses import dataclass, field

from ..models import TokenSafety
from .curve import BondingCurve, _b58encode
from .rpc import SolanaRPC

log = logging.getLogger(__name__)

# SPL Token Mint account layout, 82 bytes:
#   0..4    COption tag for mint_authority (u32 LE; 0 = None, 1 = Some)
#   4..36   mint_authority pubkey
#   36..44  supply u64
#   44      decimals u8
#   45      is_initialized bool
#   46..50  COption tag for freeze_authority
#   50..82  freeze_authority pubkey
MINT_ACCOUNT_LEN = 82


@dataclass
class MintInfo:
    supply: int = 0
    decimals: int = 6
    mint_authority: str | None = None
    freeze_authority: str | None = None
    initialized: bool = False

    @property
    def mint_authority_revoked(self) -> bool:
        return self.mint_authority is None

    @property
    def freeze_authority_revoked(self) -> bool:
        return self.freeze_authority is None


def decode_mint_account(data: bytes) -> MintInfo:
    if len(data) < MINT_ACCOUNT_LEN:
        raise ValueError(f"mint account is {len(data)} bytes, expected >= {MINT_ACCOUNT_LEN}")

    (mint_auth_tag,) = struct.unpack_from("<I", data, 0)
    mint_authority = _b58encode(data[4:36]) if mint_auth_tag == 1 else None

    (supply,) = struct.unpack_from("<Q", data, 36)
    decimals = data[44]
    initialized = bool(data[45])

    (freeze_auth_tag,) = struct.unpack_from("<I", data, 46)
    freeze_authority = _b58encode(data[50:82]) if freeze_auth_tag == 1 else None

    return MintInfo(
        supply=supply,
        decimals=decimals,
        mint_authority=mint_authority,
        freeze_authority=freeze_authority,
        initialized=initialized,
    )


@dataclass
class SafetyThresholds:
    require_mint_authority_revoked: bool = True
    require_freeze_authority_revoked: bool = True
    max_dev_holding_pct: float = 0.05
    max_top10_holding_pct: float = 0.35
    max_bundle_pct: float = 0.20
    name_blocklist: list[str] = field(default_factory=list)


class SafetyChecker:
    """Runs the screen. Caches results because a token's authorities do not
    change often and the RPC budget is finite."""

    def __init__(self, rpc: SolanaRPC, cache_ttl_s: float = 60.0) -> None:
        self.rpc = rpc
        self.cache_ttl_s = cache_ttl_s
        self._cache: dict[str, tuple[float, TokenSafety]] = {}

    async def check(
        self,
        mint: str,
        thresholds: SafetyThresholds,
        creator: str = "",
        curve: BondingCurve | None = None,
        name: str = "",
        symbol: str = "",
    ) -> TokenSafety:
        import time

        cached = self._cache.get(mint)
        if cached and (time.time() - cached[0]) < self.cache_ttl_s:
            return cached[1]

        safety = TokenSafety(checked=True)
        failures: list[str] = []

        # --- Name / symbol blocklist (free, do it first) --------------------
        haystack = f"{name} {symbol}".lower()
        for pattern in thresholds.name_blocklist:
            if pattern.lower() in haystack:
                failures.append(f"name matches blocklist pattern '{pattern}'")

        # --- Mint account: the two authority checks -------------------------
        try:
            data = await self.rpc.get_account_data(mint, commitment="confirmed")
            if data is None:
                safety.error = "mint account not found"
                safety.failures = ["mint account not found on chain"]
                safety.passed = False
                self._cache[mint] = (time.time(), safety)
                return safety

            info = decode_mint_account(data)
            safety.mint_authority_revoked = info.mint_authority_revoked
            safety.freeze_authority_revoked = info.freeze_authority_revoked

            if thresholds.require_mint_authority_revoked and not info.mint_authority_revoked:
                failures.append(
                    f"mint authority NOT revoked (held by {info.mint_authority[:8]}…) "
                    "— supply can be inflated against you"
                )
            if thresholds.require_freeze_authority_revoked and not info.freeze_authority_revoked:
                failures.append(
                    f"freeze authority NOT revoked (held by {info.freeze_authority[:8]}…) "
                    "— your tokens can be frozen so you can never sell"
                )
        except Exception as exc:  # noqa: BLE001
            log.warning("mint check failed for %s: %s", mint, exc)
            safety.error = str(exc)
            # An unreadable mint is not a token we bet money on.
            failures.append(f"could not read mint account: {exc}")

        # --- Holder concentration -------------------------------------------
        try:
            holders = await self.rpc.get_token_largest_accounts(mint)
            if holders:
                supply_resp = await self.rpc.get_token_supply(mint)
                total = float(supply_resp.get("uiAmount") or 0.0)
                if total > 0:
                    amounts = [float(h.get("uiAmount") or 0.0) for h in holders]
                    # The bonding curve itself holds the unsold supply; it is
                    # not a "holder" in the sense we care about, so drop the
                    # largest balance when it dominates.
                    if amounts and amounts[0] / total > 0.5:
                        amounts = amounts[1:]

                    safety.holder_count = len([a for a in amounts if a > 0])
                    top10 = sum(sorted(amounts, reverse=True)[:10])
                    safety.top10_holding_pct = top10 / total

                    if safety.top10_holding_pct > thresholds.max_top10_holding_pct:
                        failures.append(
                            f"top 10 holders control {safety.top10_holding_pct:.0%} of supply "
                            f"(limit {thresholds.max_top10_holding_pct:.0%})"
                        )
        except Exception as exc:  # noqa: BLE001
            # Concentration is a soft signal; a read failure downgrades our
            # confidence but does not by itself veto the trade.
            log.debug("holder check failed for %s: %s", mint, exc)
            safety.top10_holding_pct = 0.0

        # --- Creator holdings ------------------------------------------------
        if creator:
            try:
                accounts = await self.rpc.get_token_accounts_by_owner(creator, mint)
                dev_tokens = 0.0
                for acct in accounts:
                    parsed = acct.get("account", {}).get("data", {}).get("parsed", {})
                    amount = parsed.get("info", {}).get("tokenAmount", {})
                    dev_tokens += float(amount.get("uiAmount") or 0.0)

                supply_resp = await self.rpc.get_token_supply(mint)
                total = float(supply_resp.get("uiAmount") or 0.0)
                if total > 0:
                    safety.dev_holding_pct = dev_tokens / total
                    if safety.dev_holding_pct > thresholds.max_dev_holding_pct:
                        failures.append(
                            f"creator holds {safety.dev_holding_pct:.1%} of supply "
                            f"(limit {thresholds.max_dev_holding_pct:.1%})"
                        )
            except Exception as exc:  # noqa: BLE001
                log.debug("creator holding check failed for %s: %s", mint, exc)
                safety.dev_holding_pct = 0.0

        # --- Curve sanity ----------------------------------------------------
        if curve is not None:
            if curve.complete:
                failures.append("bonding curve already complete — token has migrated")
            if curve.real_sol_reserves <= 0:
                failures.append("curve holds no real SOL — nothing backing an exit")

        safety.failures = failures
        safety.passed = not failures
        self._cache[mint] = (time.time(), safety)
        return safety

    def invalidate(self, mint: str) -> None:
        self._cache.pop(mint, None)


def estimate_bundle_pct(
    creation_slot: int, early_trades: list[tuple[int, float]], total_supply_tokens: float
) -> float:
    """Fraction of supply bought in the same or next slot as creation.

    `early_trades` is [(slot, token_amount), ...]. Buys landing in the launch
    slot were not organic discovery — they were pre-arranged, which means
    those wallets are above you in the queue with a lower cost basis.
    """
    if total_supply_tokens <= 0 or not early_trades:
        return 0.0
    bundled = sum(
        amount for slot, amount in early_trades if 0 <= (slot - creation_slot) <= 1
    )
    return min(1.0, bundled / total_supply_tokens)
