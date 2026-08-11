"""pump.fun bonding curve: account decoding and pricing.

pump.fun prices tokens with a constant-product curve (the Uniswap V2 formula)
over *virtual* reserves. Virtual reserves are seeded above zero so the curve
has a sane starting price and never divides by zero, while *real* reserves
track what is actually in the account and cap what can be withdrawn.

Being able to compute price locally matters for three reasons:
  - it is one RPC read instead of a third-party price API round trip,
  - it lets us quote a fill before sending it, so we know our own slippage,
  - it keeps working when the data provider is rate limiting us.

Layout (verified against the deployed program):
    0..8    discriminator  = 17 b7 f8 37 60 d8 ac 60
    8..16   virtual_token_reserves  u64 LE
    16..24  virtual_sol_reserves    u64 LE
    24..32  real_token_reserves     u64 LE
    32..40  real_sol_reserves       u64 LE
    40..48  token_total_supply      u64 LE
    48      complete                bool
    49..81  creator                 pubkey  (only on newer accounts)
"""

from __future__ import annotations

import struct
from dataclasses import dataclass

PUMP_PROGRAM_ID = "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P"
PUMP_GLOBAL = "4wTV1YmiEkRvAtNtsSGPtUrqRYQMe5SKy2uB4Jjaxnjf"
PUMP_FEE_RECIPIENT = "CebN5WGQ4jvEPvsVU4EoHEpgzq1VV7AbicfhtW4xC9iM"

CURVE_DISCRIMINATOR = bytes([0x17, 0xB7, 0xF8, 0x37, 0x60, 0xD8, 0xAC, 0x60])

LAMPORTS_PER_SOL = 1_000_000_000
TOKEN_DECIMALS = 6
TOKEN_UNIT = 10**TOKEN_DECIMALS

# pump.fun's trade fee, charged on the SOL side of every buy and sell.
PUMP_FEE_BPS = 100  # 1.00%

# A launch graduates to a DEX at roughly this much SOL in the curve. Past this
# point the bonding curve stops being the venue and the trade changes shape.
GRADUATION_SOL = 85.0


class CurveDecodeError(ValueError):
    """Account data was not a pump.fun bonding curve."""


@dataclass(frozen=True)
class BondingCurve:
    virtual_token_reserves: int
    virtual_sol_reserves: int
    real_token_reserves: int
    real_sol_reserves: int
    token_total_supply: int
    complete: bool
    creator: str | None = None

    # -- pricing ------------------------------------------------------------

    @property
    def price_sol(self) -> float:
        """Spot price in SOL per whole token."""
        if self.virtual_token_reserves == 0:
            return 0.0
        sol = self.virtual_sol_reserves / LAMPORTS_PER_SOL
        tokens = self.virtual_token_reserves / TOKEN_UNIT
        return sol / tokens if tokens else 0.0

    @property
    def sol_in_curve(self) -> float:
        """Real SOL sitting in the curve — the honest measure of how much
        money is actually behind the token."""
        return self.real_sol_reserves / LAMPORTS_PER_SOL

    @property
    def market_cap_sol(self) -> float:
        return self.price_sol * (self.token_total_supply / TOKEN_UNIT)

    @property
    def progress_pct(self) -> float:
        """How far along the curve is toward graduating to a DEX, 0..1."""
        return min(1.0, self.sol_in_curve / GRADUATION_SOL) if GRADUATION_SOL else 0.0

    # -- quoting ------------------------------------------------------------

    def tokens_out_for_sol(self, sol_in: float, apply_fee: bool = True) -> float:
        """How many tokens a buy of `sol_in` SOL yields, at this curve state.

        Constant product: the invariant k = vsol * vtoken is preserved, so
        adding SOL to one side removes tokens from the other.
        """
        if sol_in <= 0 or self.complete:
            return 0.0
        lamports_in = int(sol_in * LAMPORTS_PER_SOL)
        if apply_fee:
            lamports_in -= lamports_in * PUMP_FEE_BPS // 10_000
        if lamports_in <= 0:
            return 0.0

        k = self.virtual_sol_reserves * self.virtual_token_reserves
        new_vsol = self.virtual_sol_reserves + lamports_in
        new_vtokens = k // new_vsol
        tokens_out = self.virtual_token_reserves - new_vtokens

        # You cannot receive more than the curve actually holds.
        tokens_out = min(tokens_out, self.real_token_reserves)
        return max(0.0, tokens_out / TOKEN_UNIT)

    def sol_out_for_tokens(self, tokens_in: float, apply_fee: bool = True) -> float:
        """How much SOL a sell of `tokens_in` tokens yields."""
        if tokens_in <= 0:
            return 0.0
        raw_tokens = int(tokens_in * TOKEN_UNIT)

        k = self.virtual_sol_reserves * self.virtual_token_reserves
        new_vtokens = self.virtual_token_reserves + raw_tokens
        if new_vtokens == 0:
            return 0.0
        new_vsol = k // new_vtokens
        lamports_out = self.virtual_sol_reserves - new_vsol

        # Capped by the real SOL available — this is exactly why a curve that
        # looks liquid on paper can fail to pay you out.
        lamports_out = min(lamports_out, self.real_sol_reserves)
        if apply_fee:
            lamports_out -= lamports_out * PUMP_FEE_BPS // 10_000
        return max(0.0, lamports_out / LAMPORTS_PER_SOL)

    def price_impact_pct(self, sol_in: float) -> float:
        """Percentage worse than spot that a buy of this size actually fills.

        On a thin curve this is the real cost of the trade and it dwarfs fees.
        Size down until this is tolerable.
        """
        if sol_in <= 0:
            return 0.0
        spot = self.price_sol
        if spot <= 0:
            return 0.0
        tokens = self.tokens_out_for_sol(sol_in, apply_fee=False)
        if tokens <= 0:
            return 100.0
        effective = sol_in / tokens
        return max(0.0, (effective - spot) / spot * 100.0)

    def sol_to_move_price(self, target_multiple: float) -> float:
        """SOL required to push price to `target_multiple` x current.

        A sanity check on the upside: if 2x only takes 0.4 SOL of buying, the
        token is thin enough that one seller erases your gain just as fast.
        """
        if target_multiple <= 1.0:
            return 0.0
        # price scales with (vsol/vtok), and k is fixed, so vsol scales with
        # sqrt(multiple).
        new_vsol = self.virtual_sol_reserves * (target_multiple**0.5)
        return (new_vsol - self.virtual_sol_reserves) / LAMPORTS_PER_SOL


def decode_curve(data: bytes) -> BondingCurve:
    """Parse raw account data into a BondingCurve."""
    if len(data) < 49:
        raise CurveDecodeError(
            f"account data is {len(data)} bytes, too short to be a bonding curve"
        )
    if data[:8] != CURVE_DISCRIMINATOR:
        raise CurveDecodeError(
            f"bad discriminator {data[:8].hex()}; expected {CURVE_DISCRIMINATOR.hex()}. "
            "This account is not a pump.fun bonding curve — the token may have "
            "already migrated to a DEX."
        )

    vtok, vsol, rtok, rsol, supply = struct.unpack_from("<QQQQQ", data, 8)
    complete = bool(data[48])

    creator = None
    if len(data) >= 81:
        creator_bytes = data[49:81]
        if any(creator_bytes):  # all-zero means the field is unset
            creator = _b58encode(creator_bytes)

    return BondingCurve(
        virtual_token_reserves=vtok,
        virtual_sol_reserves=vsol,
        real_token_reserves=rtok,
        real_sol_reserves=rsol,
        token_total_supply=supply,
        complete=complete,
        creator=creator,
    )


_B58_ALPHABET = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"


def _b58encode(raw: bytes) -> str:
    """Base58 encode, so this module has no hard dependency on `base58`."""
    num = int.from_bytes(raw, "big")
    out = ""
    while num > 0:
        num, rem = divmod(num, 58)
        out = _B58_ALPHABET[rem] + out
    # Leading zero bytes are encoded as leading '1's.
    for byte in raw:
        if byte != 0:
            break
        out = "1" + out
    return out or "1"


def _b58decode(text: str) -> bytes:
    num = 0
    for char in text:
        idx = _B58_ALPHABET.find(char)
        if idx == -1:
            raise ValueError(f"invalid base58 character {char!r}")
        num = num * 58 + idx
    raw = num.to_bytes((num.bit_length() + 7) // 8, "big") if num else b""
    pad = len(text) - len(text.lstrip("1"))
    return b"\x00" * pad + raw


def derive_bonding_curve_address(mint: str) -> str:
    """PDA of the bonding curve account for a mint: seeds ["bonding-curve", mint]."""
    try:
        from solders.pubkey import Pubkey
    except ImportError as exc:  # pragma: no cover
        raise RuntimeError(
            "solders is required to derive PDAs.\nInstall with: pip install -e '.[trade]'"
        ) from exc

    mint_key = Pubkey.from_string(mint)
    program = Pubkey.from_string(PUMP_PROGRAM_ID)
    address, _bump = Pubkey.find_program_address([b"bonding-curve", bytes(mint_key)], program)
    return str(address)


def derive_associated_token_address(owner: str, mint: str) -> str:
    """Standard SPL associated token account address."""
    try:
        from solders.pubkey import Pubkey
    except ImportError as exc:  # pragma: no cover
        raise RuntimeError("solders is required.\nInstall with: pip install -e '.[trade]'") from exc

    token_program = Pubkey.from_string("TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA")
    ata_program = Pubkey.from_string("ATokenGPvbdGVxr1b2hvZbsiqW5xWH25efTNsLJA8knL")
    address, _bump = Pubkey.find_program_address(
        [
            bytes(Pubkey.from_string(owner)),
            bytes(token_program),
            bytes(Pubkey.from_string(mint)),
        ],
        ata_program,
    )
    return str(address)
