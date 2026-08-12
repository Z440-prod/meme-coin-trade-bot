"""Narrative and attention analysis, with astroturf detection.

Social attention genuinely moves memecoin prices. It is also the cheapest
thing in this market to manufacture: a few hundred dollars buys a coordinated
posting campaign that looks, to a naive mention counter, exactly like organic
discovery.

So this module deliberately ignores the *level* of attention and looks at its
**shape**:

  - Organic interest builds unevenly, from accounts with unrelated histories,
    with varied wording, and it decays gradually.
  - Manufactured interest arrives in a burst, from accounts created recently
    or with no attention history, often with near-identical phrasing, and it
    stops as abruptly as it started.

The output is an `astroturf_score`. A high mention count with a high astroturf
score scores *worse* than no mentions at all — because it means somebody is
spending money to get you to buy, and you are the intended buyer.
"""

from __future__ import annotations

import logging
import math
import re
import time
from collections import defaultdict
from dataclasses import dataclass, field

from ..providers import Capability, ProviderRegistry, Tier
from .types import NarrativeIntel, Provenance

log = logging.getLogger(__name__)

_WORD_RE = re.compile(r"[a-z0-9$#]+")
_STOPWORDS = {
    "the", "a", "an", "is", "to", "and", "of", "in", "on", "for", "it", "this",
    "that", "with", "at", "by", "be", "are", "was", "you", "i", "we", "my",
}


@dataclass
class Mention:
    ts: float
    author: str
    followers: int
    text: str
    author_age_days: float = 0.0
    author_attention: float = 0.0   # from Kaito, when available


@dataclass
class TokenBuzz:
    """Rolling mention history for one token."""

    mentions: list[Mention] = field(default_factory=list)
    first_seen: float = field(default_factory=time.time)

    def add(self, mention: Mention) -> None:
        self.mentions.append(mention)
        if len(self.mentions) > 500:
            self.mentions.pop(0)

    def window(self, seconds: float) -> list[Mention]:
        cutoff = time.time() - seconds
        return [m for m in self.mentions if m.ts >= cutoff]


def _shingles(text: str, size: int = 3) -> set[str]:
    """Word n-grams, for near-duplicate detection."""
    words = [w for w in _WORD_RE.findall(text.lower()) if w not in _STOPWORDS]
    if len(words) < size:
        return {" ".join(words)} if words else set()
    return {" ".join(words[i : i + size]) for i in range(len(words) - size + 1)}


def text_similarity(a: str, b: str) -> float:
    """Jaccard similarity over word trigrams."""
    sa, sb = _shingles(a), _shingles(b)
    if not sa or not sb:
        return 0.0
    return len(sa & sb) / len(sa | sb)


def astroturf_score(mentions: list[Mention]) -> tuple[float, list[str]]:
    """0..1 — how manufactured this attention looks, plus the reasons why.

    Each signal is independently weak and jointly quite strong. None of them
    is conclusive on its own, which is why they are summed and capped rather
    than used as individual gates.
    """
    if len(mentions) < 3:
        return 0.0, []

    score = 0.0
    reasons: list[str] = []

    # --- 1. Burstiness -----------------------------------------------------
    # Organic attention arrives unevenly over time. A campaign fires at once.
    times = sorted(m.ts for m in mentions)
    span = times[-1] - times[0]
    if span > 0:
        gaps = [times[i + 1] - times[i] for i in range(len(times) - 1)]
        mean_gap = sum(gaps) / len(gaps)
        if mean_gap > 0:
            variance = sum((g - mean_gap) ** 2 for g in gaps) / len(gaps)
            # Coefficient of variation: low means suspiciously regular,
            # which is a scheduler rather than a crowd.
            cv = math.sqrt(variance) / mean_gap
            if cv < 0.35 and len(mentions) >= 6:
                score += 0.25
                reasons.append(f"posts arrive at near-constant intervals (cv={cv:.2f})")
    if span < 300 and len(mentions) >= 8:
        score += 0.2
        reasons.append(f"{len(mentions)} mentions inside {span / 60:.1f} minutes")

    # --- 2. Text duplication ----------------------------------------------
    texts = [m.text for m in mentions if m.text]
    if len(texts) >= 3:
        pairs = 0
        similar = 0
        for i in range(min(len(texts), 25)):
            for j in range(i + 1, min(len(texts), 25)):
                pairs += 1
                if text_similarity(texts[i], texts[j]) > 0.55:
                    similar += 1
        if pairs and similar / pairs > 0.3:
            score += 0.3
            reasons.append(f"{similar}/{pairs} message pairs are near-identical copy")

    # --- 3. Account quality ------------------------------------------------
    with_age = [m for m in mentions if m.author_age_days > 0]
    if with_age:
        fresh = sum(1 for m in with_age if m.author_age_days < 30)
        if fresh / len(with_age) > 0.5:
            score += 0.25
            reasons.append(f"{fresh}/{len(with_age)} posting accounts are under a month old")

    with_attention = [m for m in mentions if m.author_attention > 0]
    if len(with_attention) >= 3:
        no_history = sum(1 for m in with_attention if m.author_attention < 1.0)
        if no_history / len(with_attention) > 0.7:
            score += 0.2
            reasons.append("most posting accounts have no sustained attention history")

    # --- 4. Follower distribution -----------------------------------------
    # A real crowd has a long tail of follower counts. A purchased one is
    # often a cluster of similarly-sized accounts bought from one seller.
    followers = sorted(m.followers for m in mentions if m.followers > 0)
    if len(followers) >= 6:
        median = followers[len(followers) // 2]
        if median > 0:
            near_median = sum(1 for f in followers if 0.7 * median <= f <= 1.3 * median)
            if near_median / len(followers) > 0.6:
                score += 0.15
                reasons.append("posting accounts have suspiciously uniform follower counts")

    # --- 5. Single-author domination --------------------------------------
    by_author: dict[str, int] = defaultdict(int)
    for mention in mentions:
        by_author[mention.author] += 1
    if by_author:
        top = max(by_author.values())
        if top / len(mentions) > 0.5 and len(mentions) >= 5:
            score += 0.15
            reasons.append("over half the mentions come from a single account")

    return min(1.0, score), reasons


class NarrativeEngine:
    """Tracks buzz per token and produces fused narrative intel."""

    def __init__(self, registry: ProviderRegistry, ttl_s: float = 1800.0) -> None:
        self.registry = registry
        self.ttl_s = ttl_s
        self._buzz: dict[str, TokenBuzz] = {}
        self._by_symbol: dict[str, TokenBuzz] = {}
        self._account_attention: dict[str, float] = {}

    # -- ingestion ----------------------------------------------------------

    def observe(
        self,
        *,
        mints: list[str],
        symbols: list[str],
        author: str,
        followers: int,
        text: str,
        author_age_days: float = 0.0,
    ) -> None:
        mention = Mention(
            ts=time.time(),
            author=author,
            followers=followers,
            text=text,
            author_age_days=author_age_days,
            author_attention=self._account_attention.get(author.lower(), 0.0),
        )
        for mint in mints:
            self._buzz.setdefault(mint, TokenBuzz()).add(mention)
        for symbol in symbols:
            self._by_symbol.setdefault(symbol.upper(), TokenBuzz()).add(mention)

    async def grade_account(self, username: str) -> float:
        """Look up an account's real attention history via Kaito, cached."""
        key = username.lower().lstrip("@")
        if key in self._account_attention:
            return self._account_attention[key]

        kaito = self.registry.get("kaito")
        if kaito is None or not kaito.available:
            return 0.0
        try:
            result = await kaito.get_account_attention(key)  # type: ignore[attr-defined]
            score = float((result.data or {}).get("yaps_l30d") or 0) if result.usable else 0.0
        except Exception:  # noqa: BLE001
            score = 0.0
        self._account_attention[key] = score
        return score

    # -- analysis -----------------------------------------------------------

    async def analyse(
        self, mint: str, symbol: str = "", *, max_tier: Tier = Tier.COLD, timeout_s: float = 3.0
    ) -> NarrativeIntel:
        started = time.perf_counter()
        intel = NarrativeIntel(mint=mint, symbol=symbol)
        prov = Provenance()

        buzz = self._buzz.get(mint)
        symbol_buzz = self._by_symbol.get(symbol.upper()) if symbol else None

        mentions: list[Mention] = []
        if buzz:
            mentions.extend(buzz.window(self.ttl_s))
        if symbol_buzz:
            # A ticker match is weaker evidence than a mint match — many
            # tokens share a symbol, often deliberately.
            mentions.extend(symbol_buzz.window(self.ttl_s))

        if mentions:
            prov.sources.append("local_social")
            intel.mention_count = len(mentions)
            intel.reach_score = sum(math.log10(max(m.followers, 10)) for m in mentions)
            intel.quality_accounts = sum(1 for m in mentions if m.author_attention >= 1.0)

            recent = [m for m in mentions if m.ts > time.time() - 300]
            intel.velocity = len(recent) / 5.0
            older = [m for m in mentions if time.time() - 600 < m.ts <= time.time() - 300]
            prior_velocity = len(older) / 5.0
            intel.acceleration = intel.velocity - prior_velocity

            intel.astroturf_score, reasons = astroturf_score(mentions)
            intel.themes = reasons

        # --- External social providers -----------------------------------
        if symbol:
            results = await self.registry.query_all(
                Capability.SOCIAL_METRICS,
                "get_social",
                symbol,
                mint,
                max_tier=max_tier,
                timeout_s=timeout_s,
                limit=2,
            )
            for result in results:
                if not result.usable:
                    prov.missing.append(result.provider)
                    continue
                prov.sources.append(result.provider)
                d = result.data or {}
                intel.galaxy_score = max(intel.galaxy_score, float(d.get("galaxy_score") or 0))
                intel.social_volume_24h = max(
                    intel.social_volume_24h, int(d.get("social_volume_24h") or 0)
                )
                intel.contributors = max(intel.contributors, int(d.get("contributors_active") or 0))
                if d.get("sentiment") is not None:
                    intel.sentiment = float(d["sentiment"])

        prov.latency_ms = (time.perf_counter() - started) * 1000
        intel.provenance = prov.to_dict()
        return intel

    def prune(self) -> None:
        cutoff = time.time() - self.ttl_s * 2
        for store in (self._buzz, self._by_symbol):
            for key in list(store.keys()):
                buzz = store[key]
                buzz.mentions = [m for m in buzz.mentions if m.ts > cutoff]
                if not buzz.mentions:
                    del store[key]

    def trending_themes(self, top_n: int = 10, window_s: float = 3600.0) -> list[tuple[str, int]]:
        """What the market is talking about right now.

        Detecting the current meta early is worth more than any single token
        call: memecoin attention rotates in themes, and being in the right
        theme matters more than picking the right token within it.
        """
        counts: dict[str, int] = defaultdict(int)
        cutoff = time.time() - window_s
        for buzz in self._buzz.values():
            for mention in buzz.mentions:
                if mention.ts < cutoff:
                    continue
                for word in _WORD_RE.findall(mention.text.lower()):
                    if len(word) > 3 and word not in _STOPWORDS:
                        counts[word] += 1
        return sorted(counts.items(), key=lambda kv: kv[1], reverse=True)[:top_n]
