"""Social signal collection.

A correction to the source specification: **X API v2 has no WebSocket.** It
offers a chunked-HTTP "filtered stream" and a polled "recent search", and
which of those you can reach depends on your access tier. So this module
polls recent search, which works on the widest range of plans, and degrades
to doing nothing at all if no token is configured.

Treat social as the weakest input in the system. It is trivially gamed —
paid-for "calls", bot swarms and copy-paste hype accounts all show up as
signal — so it is capped at a small share of the score and can never trigger
a trade on its own.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from collections.abc import Callable
from typing import Any

import httpx

from ..models import SocialEvent

log = logging.getLogger(__name__)

X_API_BASE = "https://api.x.com/2"

# Solana addresses are base58, 32-44 chars. pump.fun mints conventionally end
# in "pump", which makes them cheap to spot in free text.
MINT_RE = re.compile(r"\b[1-9A-HJ-NP-Za-km-z]{32,44}\b")
TICKER_RE = re.compile(r"\$([A-Za-z][A-Za-z0-9]{1,14})\b")


def extract_mentions(text: str) -> tuple[list[str], list[str]]:
    """Pull candidate mints and $TICKERs out of a post."""
    mints = [m for m in MINT_RE.findall(text) if len(m) >= 32]
    # Prefer pump.fun-style mints when several base58 strings are present.
    pump_mints = [m for m in mints if m.endswith("pump")]
    tickers = [t.upper() for t in TICKER_RE.findall(text)]
    return (pump_mints or mints)[:5], tickers[:5]


class SocialMonitor:
    """Polls X recent search. Disabled and harmless without a bearer token."""

    def __init__(
        self,
        bearer_token: str | None,
        accounts: list[str],
        queries: list[str],
        poll_interval_s: float = 30.0,
        min_followers: int = 5_000,
        on_event: Callable[[SocialEvent], Any] | None = None,
        enabled: bool = True,
    ) -> None:
        self.bearer_token = bearer_token
        self.accounts = [a.lstrip("@") for a in accounts]
        self.queries = queries
        self.poll_interval_s = poll_interval_s
        self.min_followers = min_followers
        self.on_event = on_event
        self.enabled = enabled and bool(bearer_token)

        self._client: httpx.AsyncClient | None = None
        self._since_id: str | None = None
        self._running = False
        self._author_cache: dict[str, int] = {}

        self.posts_seen = 0
        self.last_poll_ts = 0.0
        self.rate_limited_until = 0.0

    @property
    def query(self) -> str:
        """Build one query string. One request per poll costs far less than
        one request per account, and X bills per request."""
        parts: list[str] = []
        if self.accounts:
            handles = " OR ".join(f"from:{a}" for a in self.accounts[:20])
            parts.append(f"({handles})")
        for q in self.queries:
            parts.append(f"({q})")
        combined = " OR ".join(parts) if parts else "pump.fun"
        return f"({combined}) -is:retweet"

    async def run(self) -> None:
        if not self.enabled:
            log.info(
                "social monitoring disabled (no MCTB_X_BEARER_TOKEN, or social.enabled=false). "
                "The bot works fine without it — social is one optional feature."
            )
            return

        self._client = httpx.AsyncClient(
            timeout=httpx.Timeout(15.0),
            headers={"Authorization": f"Bearer {self.bearer_token}"},
        )
        self._running = True
        log.info("social monitor polling every %.0fs", self.poll_interval_s)

        try:
            while self._running:
                try:
                    if time.time() >= self.rate_limited_until:
                        await self._poll()
                except asyncio.CancelledError:
                    raise
                except Exception as exc:  # noqa: BLE001
                    log.warning("social poll failed: %s", exc)
                await asyncio.sleep(self.poll_interval_s)
        finally:
            if self._client:
                await self._client.aclose()
                self._client = None

    async def _poll(self) -> None:
        assert self._client is not None
        params: dict[str, Any] = {
            "query": self.query,
            "max_results": 25,
            "tweet.fields": "created_at,public_metrics,author_id,text",
            "expansions": "author_id",
            "user.fields": "public_metrics,username",
        }
        if self._since_id:
            params["since_id"] = self._since_id

        resp = await self._client.get(f"{X_API_BASE}/tweets/search/recent", params=params)

        if resp.status_code == 429:
            # Respect the reset header rather than hammering and getting banned.
            reset = resp.headers.get("x-rate-limit-reset")
            wait_until = float(reset) if reset and reset.isdigit() else time.time() + 900
            self.rate_limited_until = wait_until
            log.warning(
                "X API rate limited; pausing social polling for %.0fs. "
                "Raise social.poll_interval_s if this keeps happening.",
                wait_until - time.time(),
            )
            return
        if resp.status_code in (401, 403):
            log.error(
                "X API rejected the token (HTTP %d). Your plan may not include "
                "recent search. Disabling social monitoring for this run.",
                resp.status_code,
            )
            self._running = False
            return
        resp.raise_for_status()

        payload = resp.json()
        self.last_poll_ts = time.time()

        users = {
            u["id"]: u for u in payload.get("includes", {}).get("users", [])
        }
        posts = payload.get("data", []) or []
        if posts:
            self._since_id = posts[0]["id"]

        for post in posts:
            author = users.get(post.get("author_id", ""), {})
            followers = (
                author.get("public_metrics", {}).get("followers_count", 0) if author else 0
            )
            username = author.get("username", "")
            if followers and followers < self.min_followers:
                continue

            text = post.get("text", "")
            mints, tickers = extract_mentions(text)
            if not mints and not tickers:
                continue

            self.posts_seen += 1
            event = SocialEvent(
                source="scout.social",
                platform="x",
                author=username,
                author_followers=followers,
                text=text[:500],
                url=f"https://x.com/{username}/status/{post['id']}" if username else "",
                mentioned_mints=mints,
                mentioned_symbols=tickers,
            )
            if self.on_event:
                result = self.on_event(event)
                if asyncio.iscoroutine(result):
                    await result

    async def stop(self) -> None:
        self._running = False


class SocialIndex:
    """In-memory rolling index of recent mentions.

    Mentions expire, because a coin mentioned twenty minutes ago is not a
    live signal — and letting stale hype keep scoring is how you end up
    buying the top of something that already ran.
    """

    def __init__(self, ttl_s: float = 900.0) -> None:
        self.ttl_s = ttl_s
        self._by_mint: dict[str, list[tuple[float, float]]] = {}
        self._by_symbol: dict[str, list[tuple[float, float]]] = {}

    def add(self, event: SocialEvent) -> None:
        now = time.time()
        # Reach is log-scaled: a 5M-follower account is influential, but not
        # a thousand times more so than a 5k account.
        import math

        reach = math.log10(max(event.author_followers, 10))
        for mint in event.mentioned_mints:
            self._by_mint.setdefault(mint, []).append((now, reach))
        for symbol in event.mentioned_symbols:
            self._by_symbol.setdefault(symbol.upper(), []).append((now, reach))

    def _live(self, entries: list[tuple[float, float]]) -> list[tuple[float, float]]:
        cutoff = time.time() - self.ttl_s
        return [e for e in entries if e[0] > cutoff]

    def lookup(self, mint: str, symbol: str = "") -> tuple[int, float]:
        """(mention count, summed reach) for a token, mints and ticker combined."""
        entries = self._live(self._by_mint.get(mint, []))
        if symbol:
            entries = entries + self._live(self._by_symbol.get(symbol.upper(), []))
        return len(entries), sum(reach for _ts, reach in entries)

    def prune(self) -> None:
        for store in (self._by_mint, self._by_symbol):
            for key in list(store.keys()):
                live = self._live(store[key])
                if live:
                    store[key] = live
                else:
                    del store[key]
