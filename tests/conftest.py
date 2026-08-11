"""Shared test fixtures."""

from __future__ import annotations

import struct
import time

import pytest

from mctb.config import Config
from mctb.models import Candidate, Features, Position, PositionState, TokenSafety
from mctb.solana.curve import CURVE_DISCRIMINATOR


@pytest.fixture
def config() -> Config:
    """Default config with no YAML on disk — exercises the schema defaults."""
    return Config()


@pytest.fixture
def good_features() -> Features:
    """A candidate that should comfortably pass the balanced preset."""
    return Features(
        age_s=45.0,
        sol_in_curve=18.0,
        market_cap_sol=120.0,
        unique_buyers=40,
        buy_count=90,
        sell_count=20,
        buy_sell_ratio=4.5,
        net_sol_flow=8.0,
        velocity_sol_per_s=0.4,
        smart_wallet_count=2,
        smart_wallet_score=0.85,
        social_mentions=3,
        social_reach=12.0,
        dev_holding_pct=0.008,
        top10_holding_pct=0.18,
        bundle_pct=0.02,
        creator_prior_rugs=0,
        creator_prior_wins=1,
        price_sol=1.5e-7,
    )


@pytest.fixture
def good_safety() -> TokenSafety:
    return TokenSafety(
        checked=True,
        mint_authority_revoked=True,
        freeze_authority_revoked=True,
        dev_holding_pct=0.008,
        top10_holding_pct=0.18,
        holder_count=40,
        bundle_pct=0.02,
        passed=True,
    )


@pytest.fixture
def good_candidate(good_features: Features, good_safety: TokenSafety) -> Candidate:
    return Candidate(
        mint="TESTmint1111111111111111111111111111111pump",
        name="Test Coin",
        symbol="TEST",
        creator="Creator111111111111111111111111111111111111",
        features=good_features,
        safety=good_safety,
        smart_wallets=["Wallet1111111111111111111111111111111111111"],
        trigger="smart_wallet",
    )


@pytest.fixture
def open_position() -> Position:
    now = time.time()
    return Position(
        mint="TESTmint1111111111111111111111111111111pump",
        symbol="TEST",
        state=PositionState.OPEN,
        opened_at=now,
        entry_price_sol=1.0e-7,
        current_price_sol=1.0e-7,
        peak_price_sol=1.0e-7,
        last_price_ts=now,
        cost_sol=0.05,
        tokens_bought=500_000.0,
        tokens_held=500_000.0,
        fees_sol=0.0005,
    )


def build_curve_account(
    virtual_token: int = 1_073_000_000_000_000,
    virtual_sol: int = 30_000_000_000,
    real_token: int = 793_100_000_000_000,
    real_sol: int = 0,
    supply: int = 1_000_000_000_000_000,
    complete: bool = False,
    creator: bytes | None = None,
) -> bytes:
    """Construct a byte-accurate bonding curve account for decoding tests."""
    data = bytearray(CURVE_DISCRIMINATOR)
    data += struct.pack("<QQQQQ", virtual_token, virtual_sol, real_token, real_sol, supply)
    data += bytes([1 if complete else 0])
    if creator is not None:
        data += creator.ljust(32, b"\x00")[:32]
    return bytes(data)


def build_mint_account(
    mint_authority: bytes | None = None,
    freeze_authority: bytes | None = None,
    supply: int = 1_000_000_000_000_000,
    decimals: int = 6,
) -> bytes:
    """Construct an SPL Mint account. None authority means revoked."""
    data = bytearray()
    if mint_authority is None:
        data += struct.pack("<I", 0) + bytes(32)
    else:
        data += struct.pack("<I", 1) + mint_authority.ljust(32, b"\x00")[:32]
    data += struct.pack("<Q", supply)
    data += bytes([decimals, 1])
    if freeze_authority is None:
        data += struct.pack("<I", 0) + bytes(32)
    else:
        data += struct.pack("<I", 1) + freeze_authority.ljust(32, b"\x00")[:32]
    return bytes(data)
