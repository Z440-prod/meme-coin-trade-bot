"""Smart-wallet discovery and reputation.

This is the "copy successful traders" half of the system, and it is where the
system stops being generic. A public leaderboard tells you who made money in
general; this tracker learns which wallets make money *for you*, on the tokens
you can actually reach, at the sizes you actually trade.

How discovery works:
  1. Watch which wallets buy new launches early.
  2. Keep watching those launches. When one goes up a lot, the wallets that
     were in before it moved get credit.
  3. A wallet that repeatedly gets credit becomes tracked, and the Scout
     subscribes to its trades so you find out in real time when it buys.
  4. Once you are actually copying it, its score shifts to being driven by
     realised P&L on *your* copies, which is the only measure that pays rent.

The scoring is deliberately conservative. A wallet with three wins is not
proven; it may simply have been lucky, and there are wallets whose entire
business model is looking good on-chain to attract copy-traders they can then
dump on. Sample size discounting is applied throughout.
"""

from __future__ import annotations

import logging
import math
import time
from dataclasses import dataclass, field

from ..db import Database
from ..models import TradeEvent, WalletStat

log = logging.getLogger(__name__)


@dataclass
class _EarlyBuy:
    wallet: str
    mint: str
    ts: float
    sol_in_curve_at_entry: float
    price_at_entry: float


@dataclass
class _TokenTrack:
    """A launch we are watching to see whether its early buyers were right."""

    mint: str
    first_seen: float
    peak_sol_in_curve: float = 0.0
    peak_price: float = 0.0
    entry_price: float = 0.0
    early_buys: list[_EarlyBuy] = field(default_factory=list)
    graded: bool = False


class WalletTracker:
    def __init__(
        self,
        db: Database,
        seed_wallets: list[str],
        auto_discover: bool = True,
        min_wins: int = 3,
        win_multiple: float = 3.0,
        max_entry_sol: float = 20.0,
        max_tracked: int = 150,
        prune_below_score: float = 0.25,
        grade_after_s: float = 1800.0,
    ) -> None:
        self.db = db
        self.auto_discover = auto_discover
        self.min_wins = min_wins
        self.win_multiple = win_multiple
        self.max_entry_sol = max_entry_sol
        self.max_tracked = max_tracked
        self.prune_below_score = prune_below_score
        # How long to watch a launch before deciding whether its early buyers
        # were smart. Too short and you grade noise; too long and the wallet
        # has moved on.
        self.grade_after_s = grade_after_s

        self.seed_wallets = [w for w in seed_wallets if w]
        self.stats: dict[str, WalletStat] = {}
        self._tracking: dict[str, _TokenTrack] = {}

    async def load(self) -> None:
        """Restore reputation from disk, then merge in any new seed wallets."""
        for wallet in await self.db.wallets(limit=self.max_tracked * 2):
            self.stats[wallet.address] = wallet

        for address in self.seed_wallets:
            existing = self.stats.get(address)
            if existing:
                existing.seeded = True
                # Seeds start trusted, but not blindly — if your own results
                # say otherwise, the learned score still moves.
                existing.score = max(existing.score, 0.75)
            else:
                self.stats[address] = WalletStat(
                    address=address, seeded=True, label="seed", score=0.8
                )
            await self.db.upsert_wallet(self.stats[address])

        log.info(
            "wallet tracker loaded: %d known, %d seeded",
            len(self.stats),
            sum(1 for w in self.stats.values() if w.seeded),
        )

    # -- observation --------------------------------------------------------

    def observe_new_token(self, mint: str, price: float, sol_in_curve: float) -> None:
        if not self.auto_discover:
            return
        if mint not in self._tracking:
            self._tracking[mint] = _TokenTrack(
                mint=mint,
                first_seen=time.time(),
                entry_price=price,
                peak_price=price,
                peak_sol_in_curve=sol_in_curve,
            )

    def observe_trade(self, event: TradeEvent) -> None:
        """Record a trade for later grading."""
        track = self._tracking.get(event.mint)
        if track is None:
            if not self.auto_discover:
                return
            track = _TokenTrack(
                mint=event.mint,
                first_seen=time.time(),
                entry_price=event.price_sol,
                peak_price=event.price_sol,
            )
            self._tracking[event.mint] = track

        track.peak_sol_in_curve = max(track.peak_sol_in_curve, event.sol_in_curve)
        if event.price_sol > 0:
            track.peak_price = max(track.peak_price, event.price_sol)
            if track.entry_price <= 0:
                track.entry_price = event.price_sol

        # Only *early* buys count toward discovery. Buying a coin that has
        # already run is not skill, and crediting it would teach the tracker
        # to follow momentum chasers.
        if (
            event.is_buy
            and self.auto_discover
            and event.sol_in_curve <= self.max_entry_sol
            and event.trader
        ):
            already = any(b.wallet == event.trader for b in track.early_buys)
            if not already and len(track.early_buys) < 200:
                track.early_buys.append(
                    _EarlyBuy(
                        wallet=event.trader,
                        mint=event.mint,
                        ts=time.time(),
                        sol_in_curve_at_entry=event.sol_in_curve,
                        price_at_entry=event.price_sol or track.entry_price,
                    )
                )

    # -- grading ------------------------------------------------------------

    async def grade_matured(self) -> int:
        """Score early buyers of launches that have had time to prove out.

        Called periodically by the Scout. Returns how many tokens were graded.
        """
        now = time.time()
        graded = 0

        for mint, track in list(self._tracking.items()):
            age = now - track.first_seen
            if track.graded or age < self.grade_after_s:
                continue

            track.graded = True
            graded += 1

            achieved = (
                track.peak_price / track.entry_price if track.entry_price > 0 else 1.0
            )
            was_winner = achieved >= self.win_multiple

            for buy in track.early_buys:
                stat = self.stats.get(buy.wallet)
                if stat is None:
                    stat = WalletStat(address=buy.wallet)
                    self.stats[buy.wallet] = stat

                stat.tokens_bought += 1
                stat.last_seen = now
                # Running mean of how early this wallet tends to get in.
                n = stat.tokens_bought
                stat.avg_entry_sol_in_curve = (
                    stat.avg_entry_sol_in_curve * (n - 1) + buy.sol_in_curve_at_entry
                ) / n

                # Credit only if they were in *before* the move, not during it.
                if was_winner and buy.price_at_entry > 0:
                    from_entry = track.peak_price / buy.price_at_entry
                    if from_entry >= self.win_multiple:
                        stat.wins += 1
                    else:
                        stat.losses += 1
                else:
                    stat.losses += 1

                stat.score = self._compute_score(stat)
                await self.db.upsert_wallet(stat)

            del self._tracking[mint]

        # Drop stale trackers so memory does not grow without bound.
        for mint, track in list(self._tracking.items()):
            if now - track.first_seen > self.grade_after_s * 4:
                del self._tracking[mint]

        if graded:
            log.info("graded %d matured launches; %d wallets known", graded, len(self.stats))
        return graded

    def _compute_score(self, stat: WalletStat) -> float:
        """Reputation in 0..1.

        Three ingredients, in increasing order of how much they matter:
          - hit rate on discovered launches (weak — public information)
          - how early the wallet typically enters (moderate — hard to fake)
          - realised P&L on trades you actually copied (strong — this is the
            only evidence that survives contact with your own execution)

        All of it is shrunk toward 0.5 by sample size, so a wallet that is
        3-for-3 does not outrank one that is 40-for-60.
        """
        attempts = stat.wins + stat.losses
        if attempts == 0 and stat.copied_trades == 0:
            return 0.5 if not stat.seeded else 0.75

        # Laplace-smoothed hit rate.
        hit_rate = (stat.wins + 1) / (attempts + 2) if attempts else 0.5
        # Confidence grows with the square root of sample size, saturating
        # around 30 observations.
        confidence = min(1.0, math.sqrt(attempts / 30.0)) if attempts else 0.0
        discovery = 0.5 + (hit_rate - 0.5) * confidence

        # Earliness: entering under ~5 SOL of curve is genuinely early.
        earliness = 0.5
        if stat.avg_entry_sol_in_curve > 0:
            earliness = max(0.0, min(1.0, 1.0 - (stat.avg_entry_sol_in_curve / 30.0)))

        score = 0.55 * discovery + 0.20 * earliness + 0.25 * 0.5

        # Realised P&L on your own copies overrides everything once there is
        # enough of it to mean something.
        if stat.copied_trades >= 3:
            avg_pnl = stat.copied_pnl_sol / stat.copied_trades
            # ±0.05 SOL average per copy maps to roughly the full range.
            pnl_component = max(0.0, min(1.0, 0.5 + avg_pnl * 10.0))
            weight = min(0.7, stat.copied_trades / 20.0)
            score = score * (1 - weight) + pnl_component * weight

        if stat.seeded:
            score = max(score, 0.6)

        return round(max(0.0, min(1.0, score)), 4)

    # -- feedback from our own trading --------------------------------------

    async def record_copy_outcome(self, wallets: list[str], pnl_sol: float) -> None:
        """Feed a closed position's result back to the wallets that triggered it.

        This closes the loop. A wallet whose calls you keep losing money on
        loses influence over your sizing regardless of how good its public
        record looks.
        """
        for address in wallets:
            stat = self.stats.get(address)
            if stat is None:
                continue
            stat.copied_trades += 1
            stat.copied_pnl_sol += pnl_sol
            stat.score = self._compute_score(stat)
            await self.db.upsert_wallet(stat)
            log.info(
                "wallet %s… now %d copies, %+.4f SOL total, score %.2f",
                address[:8],
                stat.copied_trades,
                stat.copied_pnl_sol,
                stat.score,
            )

    # -- selection ----------------------------------------------------------

    def qualified(self) -> list[WalletStat]:
        """Wallets good enough to act on."""
        out = [
            w
            for w in self.stats.values()
            if w.seeded or (w.wins >= self.min_wins and w.score >= 0.5)
        ]
        out.sort(key=lambda w: w.score, reverse=True)
        return out[: self.max_tracked]

    def to_subscribe(self, limit: int) -> list[str]:
        """Best wallets to spend metered subscription budget on."""
        return [w.address for w in self.qualified()[:limit]]

    def score_for(self, address: str) -> float:
        stat = self.stats.get(address)
        return stat.score if stat else 0.0

    def is_tracked(self, address: str) -> bool:
        stat = self.stats.get(address)
        return bool(stat and (stat.seeded or stat.wins >= self.min_wins))

    async def prune(self) -> int:
        """Forget wallets that proved out badly, freeing subscription budget."""
        removed = 0
        for address, stat in list(self.stats.items()):
            if stat.seeded:
                continue
            attempts = stat.wins + stat.losses
            stale = (time.time() - stat.last_seen) > 86_400 * 7
            if (attempts >= 10 and stat.score < self.prune_below_score) or (
                stale and attempts < 3
            ):
                del self.stats[address]
                removed += 1
        if removed:
            log.info("pruned %d underperforming wallets", removed)
        return removed
