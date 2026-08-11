"""Bonding curve decoding and pricing.

These are the calculations every trade is priced against, so they get the
most thorough tests in the suite. A bug here does not produce an error — it
produces a wrong position size or a wrong stop loss, silently.
"""

from __future__ import annotations

import pytest

from mctb.solana.curve import (
    LAMPORTS_PER_SOL,
    TOKEN_UNIT,
    BondingCurve,
    CurveDecodeError,
    _b58decode,
    _b58encode,
    decode_curve,
)

from .conftest import build_curve_account


class TestDecoding:
    def test_decodes_known_layout(self) -> None:
        curve = decode_curve(build_curve_account())
        assert curve.virtual_token_reserves == 1_073_000_000_000_000
        assert curve.virtual_sol_reserves == 30_000_000_000
        assert curve.real_token_reserves == 793_100_000_000_000
        assert curve.complete is False

    def test_rejects_wrong_discriminator(self) -> None:
        data = bytearray(build_curve_account())
        data[0] = 0xFF
        with pytest.raises(CurveDecodeError, match="discriminator"):
            decode_curve(bytes(data))

    def test_rejects_short_data(self) -> None:
        with pytest.raises(CurveDecodeError, match="too short"):
            decode_curve(b"\x00" * 10)

    def test_reads_creator_when_present(self) -> None:
        curve = decode_curve(build_curve_account(creator=b"\x01" * 32))
        assert curve.creator is not None

    def test_creator_is_none_when_absent(self) -> None:
        assert decode_curve(build_curve_account()).creator is None

    def test_all_zero_creator_treated_as_unset(self) -> None:
        assert decode_curve(build_curve_account(creator=bytes(32))).creator is None

    def test_complete_flag(self) -> None:
        assert decode_curve(build_curve_account(complete=True)).complete is True


class TestPricing:
    def test_spot_price(self) -> None:
        # 30 SOL virtual against 1.073e9 virtual tokens.
        curve = decode_curve(build_curve_account())
        expected = (30_000_000_000 / LAMPORTS_PER_SOL) / (1_073_000_000_000_000 / TOKEN_UNIT)
        assert curve.price_sol == pytest.approx(expected, rel=1e-9)

    def test_price_rises_as_sol_enters(self) -> None:
        low = decode_curve(build_curve_account(virtual_sol=30_000_000_000))
        high = decode_curve(build_curve_account(virtual_sol=60_000_000_000))
        assert high.price_sol > low.price_sol

    def test_zero_token_reserves_does_not_divide_by_zero(self) -> None:
        assert decode_curve(build_curve_account(virtual_token=0)).price_sol == 0.0

    def test_sol_in_curve_uses_real_reserves(self) -> None:
        curve = decode_curve(build_curve_account(real_sol=5_000_000_000))
        assert curve.sol_in_curve == pytest.approx(5.0)


class TestQuoting:
    @pytest.fixture
    def curve(self) -> BondingCurve:
        return decode_curve(build_curve_account(real_sol=10_000_000_000))

    def test_buy_returns_tokens(self, curve: BondingCurve) -> None:
        assert curve.tokens_out_for_sol(1.0) > 0

    def test_larger_buy_gets_worse_average_price(self, curve: BondingCurve) -> None:
        """Price impact must be monotonic — this is what makes sizing matter."""
        small = curve.tokens_out_for_sol(0.1, apply_fee=False)
        large = curve.tokens_out_for_sol(10.0, apply_fee=False)
        price_small = 0.1 / small
        price_large = 10.0 / large
        assert price_large > price_small

    def test_fee_reduces_tokens_received(self, curve: BondingCurve) -> None:
        with_fee = curve.tokens_out_for_sol(1.0, apply_fee=True)
        without_fee = curve.tokens_out_for_sol(1.0, apply_fee=False)
        assert with_fee < without_fee
        # ~1% fee.
        assert with_fee / without_fee == pytest.approx(0.99, abs=0.01)

    def test_buy_capped_by_real_token_reserves(self) -> None:
        curve = decode_curve(build_curve_account(real_token=1_000_000))
        assert curve.tokens_out_for_sol(1000.0) <= 1_000_000 / TOKEN_UNIT

    def test_no_tokens_from_completed_curve(self) -> None:
        curve = decode_curve(build_curve_account(complete=True))
        assert curve.tokens_out_for_sol(1.0) == 0.0

    def test_sell_returns_sol(self, curve: BondingCurve) -> None:
        assert curve.sol_out_for_tokens(1_000_000.0) > 0

    def test_sell_capped_by_real_sol_reserves(self) -> None:
        """The critical one: a curve holding 0.5 SOL cannot pay out more than
        0.5 SOL no matter how large the position looks on paper."""
        curve = decode_curve(build_curve_account(real_sol=500_000_000))
        assert curve.sol_out_for_tokens(10**12) <= 0.5

    def test_round_trip_loses_to_fees_and_impact(self, curve: BondingCurve) -> None:
        """Buying then immediately selling must lose money. If this ever
        passes at break-even, the fee or impact model is wrong and every
        backtest built on it is optimistic."""
        tokens = curve.tokens_out_for_sol(1.0)
        assert curve.sol_out_for_tokens(tokens) < 1.0

    def test_negative_and_zero_inputs(self, curve: BondingCurve) -> None:
        assert curve.tokens_out_for_sol(0) == 0.0
        assert curve.tokens_out_for_sol(-5) == 0.0
        assert curve.sol_out_for_tokens(0) == 0.0
        assert curve.sol_out_for_tokens(-5) == 0.0


class TestPriceImpact:
    def test_impact_grows_with_size(self) -> None:
        curve = decode_curve(build_curve_account(real_sol=10_000_000_000))
        assert curve.price_impact_pct(10.0) > curve.price_impact_pct(0.1)

    def test_tiny_order_has_negligible_impact(self) -> None:
        curve = decode_curve(build_curve_account(real_sol=10_000_000_000))
        assert curve.price_impact_pct(0.001) < 1.0

    def test_sol_to_move_price(self) -> None:
        curve = decode_curve(build_curve_account())
        needed = curve.sol_to_move_price(2.0)
        assert needed > 0
        # Doubling the price should take more buying than a 50% move.
        assert needed > curve.sol_to_move_price(1.5)


class TestBase58:
    @pytest.mark.parametrize(
        "raw",
        [b"\x00" * 32, b"\xff" * 32, bytes(range(32)), b"\x00\x01\x02", b"hello world"],
    )
    def test_round_trip(self, raw: bytes) -> None:
        assert _b58decode(_b58encode(raw)) == raw

    def test_leading_zeros_preserved(self) -> None:
        raw = b"\x00\x00\x01\x02"
        assert _b58encode(raw).startswith("11")
        assert _b58decode(_b58encode(raw)) == raw

    def test_known_vector(self) -> None:
        # The all-zero 32-byte pubkey is the Solana system program address.
        assert _b58encode(bytes(32)) == "1" * 32

    def test_invalid_character_rejected(self) -> None:
        with pytest.raises(ValueError, match="invalid base58"):
            _b58decode("0OIl")
