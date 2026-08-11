"""SPL mint account decoding — the honeypot detection primitive."""

from __future__ import annotations

import pytest

from mctb.solana.checks import decode_mint_account, estimate_bundle_pct

from .conftest import build_mint_account


class TestMintDecoding:
    def test_fully_revoked_authorities(self) -> None:
        info = decode_mint_account(build_mint_account())
        assert info.mint_authority is None
        assert info.freeze_authority is None
        assert info.mint_authority_revoked is True
        assert info.freeze_authority_revoked is True

    def test_live_mint_authority_detected(self) -> None:
        info = decode_mint_account(build_mint_account(mint_authority=b"\x01" * 32))
        assert info.mint_authority is not None
        assert info.mint_authority_revoked is False

    def test_live_freeze_authority_detected(self) -> None:
        """The honeypot signature: freeze authority still held means your
        tokens can be frozen and you can never sell."""
        info = decode_mint_account(build_mint_account(freeze_authority=b"\x02" * 32))
        assert info.freeze_authority is not None
        assert info.freeze_authority_revoked is False

    def test_both_live(self) -> None:
        info = decode_mint_account(
            build_mint_account(mint_authority=b"\x01" * 32, freeze_authority=b"\x02" * 32)
        )
        assert not info.mint_authority_revoked
        assert not info.freeze_authority_revoked
        assert info.mint_authority != info.freeze_authority

    def test_supply_and_decimals(self) -> None:
        info = decode_mint_account(build_mint_account(supply=123_456_789, decimals=9))
        assert info.supply == 123_456_789
        assert info.decimals == 9
        assert info.initialized is True

    def test_short_account_rejected(self) -> None:
        with pytest.raises(ValueError, match="expected >="):
            decode_mint_account(b"\x00" * 40)

    def test_extra_trailing_bytes_tolerated(self) -> None:
        """Token-2022 mints carry extensions after the base layout; the base
        fields must still decode."""
        info = decode_mint_account(build_mint_account() + b"\xff" * 200)
        assert info.mint_authority_revoked is True


class TestBundleDetection:
    def test_same_slot_buys_counted(self) -> None:
        pct = estimate_bundle_pct(
            creation_slot=1000,
            early_trades=[(1000, 200_000.0), (1001, 100_000.0), (1050, 500_000.0)],
            total_supply_tokens=1_000_000.0,
        )
        # 200k + 100k of 1M — the slot-1050 buy is organic and excluded.
        assert pct == pytest.approx(0.3)

    def test_no_early_trades(self) -> None:
        assert estimate_bundle_pct(1000, [], 1_000_000.0) == 0.0

    def test_zero_supply_does_not_divide_by_zero(self) -> None:
        assert estimate_bundle_pct(1000, [(1000, 5.0)], 0.0) == 0.0

    def test_capped_at_one(self) -> None:
        assert estimate_bundle_pct(1000, [(1000, 5_000_000.0)], 1_000_000.0) == 1.0

    def test_later_slots_excluded(self) -> None:
        assert estimate_bundle_pct(1000, [(1005, 900_000.0)], 1_000_000.0) == 0.0
