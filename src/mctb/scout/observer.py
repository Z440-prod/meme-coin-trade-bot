"""Rolling per-token statistics.

Everything the scorer needs about a live launch: who is buying, how fast, and
whether the flow is accelerating or dying. Kept in memory with hard caps,
because a Pi has 8GB and pump.fun launches thousands of tokens a day.

The momentum measures use a short recent window rather than lifetime totals.
A token with 400 buys that all happened four minutes ago is dead; a token with
30 buys in the last twenty seconds is alive. Lifetime counters cannot tell
those apart, and that difference is most of the edge.
"""

from __future__ import annotations

import time
from collections import deque
from dataclasses import dataclass, field

from ..models import Features, TradeEvent

# Momentum window, in seconds.
WINDOW_S = 45.0


@dataclass
class TokenObservation:
    mint: str
    name: str = ""
    symbol: str = ""
    creator: str = ""
    first_seen: float = field(default_factory=time.time)
    created_ts: float = field(default_factory=time.time)

    sol_in_curve: float = 0.0
    market_cap_sol: float = 0.0
    last_price: float = 0.0
    peak_price: float = 0.0

    buy_count: int = 0
    sell_count: int = 0
    buyers: set[str] = field(default_factory=set)
    sellers: set[str] = field(default_factory=set)
    net_sol_flow: float = 0.0

    smart_wallets: set[str] = field(default_factory=set)
    smart_wallet_score: float = 0.0
    smart_wallets_exited: set[str] = field(default_factory=set)

    dev_sold: bool = False
    migrated: bool = False
    subscribed: bool = False

    # (ts, signed_sol) — positive for buys, negative for sells.
    recent: deque[tuple[float, float]] = field(default_factory=lambda: deque(maxlen=500))
    # Buys landing in the launch slot, for bundle detection.
    creation_slot: int = 0
    early_slot_tokens: float = 0.0
    total_supply_tokens: float = 0.0

    last_trade_ts: float = 0.0

    @property
    def age_s(self) -> float:
        return time.time() - self.created_ts

    @property
    def buy_sell_ratio(self) -> float:
        if self.sell_count == 0:
            return float(self.buy_count) if self.buy_count else 0.0
        return self.buy_count / self.sell_count

    def window_flow(self, window_s: float = WINDOW_S) -> tuple[float, int, float]:
        """(net SOL, trade count, SOL per second) over the recent window."""
        cutoff = time.time() - window_s
        entries = [(ts, sol) for ts, sol in self.recent if ts >= cutoff]
        if not entries:
            return 0.0, 0, 0.0
        net = sum(sol for _ts, sol in entries)
        span = max(1.0, time.time() - entries[0][0])
        return net, len(entries), net / span

    def apply_trade(self, event: TradeEvent, smart_score: float = 0.0) -> None:
        now = time.time()
        self.last_trade_ts = now

        if event.sol_in_curve > 0:
            self.sol_in_curve = event.sol_in_curve
        if event.market_cap_sol > 0:
            self.market_cap_sol = event.market_cap_sol
        price = event.price_sol
        if price > 0:
            self.last_price = price
            self.peak_price = max(self.peak_price, price)

        if event.is_buy:
            self.buy_count += 1
            if event.trader:
                self.buyers.add(event.trader)
            self.net_sol_flow += event.sol_amount
            self.recent.append((now, event.sol_amount))
            if smart_score > 0 and event.trader:
                self.smart_wallets.add(event.trader)
                # Track the best-scoring wallet in, not the sum, so ten
                # mediocre wallets cannot outweigh one genuinely good one.
                self.smart_wallet_score = max(self.smart_wallet_score, smart_score)
        else:
            self.sell_count += 1
            if event.trader:
                self.sellers.add(event.trader)
            self.net_sol_flow -= event.sol_amount
            self.recent.append((now, -event.sol_amount))
            if event.trader and event.trader == self.creator:
                self.dev_sold = True
            if event.trader in self.smart_wallets:
                self.smart_wallets_exited.add(event.trader)

        # Bundle detection: buys in the launch slot were pre-arranged.
        if self.creation_slot and event.slot and event.is_buy:
            if 0 <= (event.slot - self.creation_slot) <= 1:
                self.early_slot_tokens += event.token_amount

    def to_features(
        self,
        social_mentions: int = 0,
        social_reach: float = 0.0,
        creator_rugs: int = 0,
        creator_wins: int = 0,
        dev_holding_pct: float = 0.0,
        top10_holding_pct: float = 0.0,
    ) -> Features:
        net, _count, velocity = self.window_flow()
        bundle_pct = (
            min(1.0, self.early_slot_tokens / self.total_supply_tokens)
            if self.total_supply_tokens > 0
            else 0.0
        )
        return Features(
            age_s=self.age_s,
            sol_in_curve=self.sol_in_curve,
            market_cap_sol=self.market_cap_sol,
            unique_buyers=len(self.buyers),
            buy_count=self.buy_count,
            sell_count=self.sell_count,
            buy_sell_ratio=self.buy_sell_ratio,
            net_sol_flow=net,
            velocity_sol_per_s=velocity,
            smart_wallet_count=len(self.smart_wallets),
            smart_wallet_score=self.smart_wallet_score,
            social_mentions=social_mentions,
            social_reach=social_reach,
            dev_holding_pct=dev_holding_pct,
            top10_holding_pct=top10_holding_pct,
            bundle_pct=bundle_pct,
            creator_prior_rugs=creator_rugs,
            creator_prior_wins=creator_wins,
            price_sol=self.last_price,
        )


class Watchlist:
    """Bounded collection of tokens currently being observed."""

    def __init__(self, max_size: int = 400, ttl_s: float = 1200.0) -> None:
        self.max_size = max_size
        self.ttl_s = ttl_s
        self._tokens: dict[str, TokenObservation] = {}

    def __len__(self) -> int:
        return len(self._tokens)

    def __contains__(self, mint: str) -> bool:
        return mint in self._tokens

    def get(self, mint: str) -> TokenObservation | None:
        return self._tokens.get(mint)

    def add(self, obs: TokenObservation) -> TokenObservation:
        existing = self._tokens.get(obs.mint)
        if existing:
            return existing
        self._tokens[obs.mint] = obs
        if len(self._tokens) > self.max_size:
            self._evict()
        return obs

    def all(self) -> list[TokenObservation]:
        return list(self._tokens.values())

    def remove(self, mint: str) -> None:
        self._tokens.pop(mint, None)

    def _evict(self) -> None:
        """Drop the least interesting tokens when full.

        Interest = recent activity, with smart-wallet involvement protected.
        A quiet token with a tracked wallet in it is worth more than a noisy
        one without.
        """
        ranked = sorted(
            self._tokens.values(),
            key=lambda o: (
                bool(o.smart_wallets),
                o.last_trade_ts or o.first_seen,
            ),
        )
        for obs in ranked[: max(1, len(self._tokens) - self.max_size)]:
            self._tokens.pop(obs.mint, None)

    def expire(self) -> list[str]:
        """Remove tokens past their TTL. Returns the removed mints so the
        caller can unsubscribe from their metered streams."""
        cutoff = time.time() - self.ttl_s
        dead = [
            mint
            for mint, obs in self._tokens.items()
            if obs.first_seen < cutoff or obs.migrated
        ]
        for mint in dead:
            del self._tokens[mint]
        return dead
