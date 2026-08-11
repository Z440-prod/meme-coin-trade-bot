"""Local LLM advisor (llama.cpp server).

Where a language model genuinely helps a trading system, and where it does not:

**It does not help on the hot path.** See the note in `scoring.py`. A model
small enough to run on a Pi is too slow to make an entry decision inside the
window where the entry is still available, and a model fast enough would not
be adding judgement anyway.

**It does help with judgement calls that arithmetic cannot express.** Whether
a token's name and metadata look like a coordinated launch or an organic
joke. Whether social chatter is real people or a bot swarm posting the same
sentence. Those are pattern-matching problems over text, which is exactly
what these models are for.

**It helps most with the nightly review.** Reading 200 trades and noticing
"every loss this week entered above 40 SOL of curve depth" is a genuinely
useful thing to have done for you, and there is no latency pressure at 4am.

Every call is bounded by a hard timeout and degrades to "no opinion" — the
bot must trade exactly as well with the LLM switched off as with it on, just
without this one extra input.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from typing import Any

import httpx

from ..config import Config
from ..models import Candidate

log = logging.getLogger(__name__)

CANDIDATE_SYSTEM = """You are a risk analyst for an automated memecoin trading system.
You are shown a token that has ALREADY passed automated on-chain safety checks and
scored above threshold on a quantitative model. Your job is NOT to re-derive the
score. Your job is to catch qualitative red flags the numbers cannot see:

- names/symbols that impersonate an existing well-known token or brand
- metadata suggesting a coordinated or recycled launch (generic filler text,
  a name from a template, a symbol that is a slight misspelling of something big)
- social text that reads like a bot swarm rather than organic interest
- anything that suggests the setup is engineered to attract copy-trading bots

Respond with ONLY a JSON object, no prose, no markdown fence:
{"verdict": "buy" | "neutral" | "avoid",
 "confidence": 0.0-1.0,
 "adjust": <number, negative to penalise, positive to reward>,
 "reasons": ["short phrase", ...],
 "red_flags": ["short phrase", ...]}

Be sparing with "buy" and generous with "avoid". Missing a good trade costs
nothing; taking a rigged one costs the position. If nothing stands out, say
"neutral" with adjust 0."""

REVIEW_SYSTEM = """You are a trading performance analyst reviewing an automated
memecoin bot's own trade history. Be blunt and specific. Do not congratulate.

Look for: which entry conditions preceded losses, whether exits fired too early
or too late, whether position sizing matched conviction, and whether any single
parameter is responsible for a disproportionate share of losses.

Respond with ONLY a JSON object:
{"summary": "2-4 sentences, direct",
 "findings": ["specific observation with the number that supports it", ...],
 "proposed_changes": [{"parameter": "dotted.config.path", "current": <value>,
                       "proposed": <value>, "rationale": "why, citing the data"}],
 "confidence": 0.0-1.0}

Only propose a change you can justify from the data shown. If the sample is too
small to conclude anything, say so and propose nothing — an empty
proposed_changes list is a valid and often correct answer."""


def _extract_json(text: str) -> dict[str, Any] | None:
    """Pull a JSON object out of a model response.

    Small models wrap JSON in prose or markdown fences no matter how firmly
    you ask them not to, so we go looking rather than failing.
    """
    text = text.strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass

    fence = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
    if fence:
        try:
            return json.loads(fence.group(1))
        except json.JSONDecodeError:
            pass

    # First balanced brace group.
    start = text.find("{")
    if start == -1:
        return None
    depth = 0
    for idx in range(start, len(text)):
        if text[idx] == "{":
            depth += 1
        elif text[idx] == "}":
            depth -= 1
            if depth == 0:
                try:
                    return json.loads(text[start : idx + 1])
                except json.JSONDecodeError:
                    return None
    return None


class LLMAdvisor:
    def __init__(self, config: Config) -> None:
        self.config = config
        self.cfg = config.llm
        self._client: httpx.AsyncClient | None = None
        self.available = False
        self.calls = 0
        self.timeouts = 0
        self.parse_failures = 0
        self.total_latency_s = 0.0
        self._cache: dict[str, tuple[float, dict[str, Any]]] = {}

    async def connect(self) -> None:
        if not self.cfg.enabled:
            log.info("LLM advisor disabled in config")
            return

        self._client = httpx.AsyncClient(
            base_url=self.cfg.base_url, timeout=httpx.Timeout(self.cfg.timeout_s)
        )
        await self.probe()

    async def probe(self) -> bool:
        """Check the llama.cpp server is up. Non-fatal if it is not."""
        if self._client is None:
            return False
        for path in ("/health", "/v1/models"):
            try:
                resp = await self._client.get(path, timeout=5.0)
                if resp.status_code < 400:
                    self.available = True
                    log.info("LLM server reachable at %s", self.cfg.base_url)
                    return True
            except Exception:  # noqa: BLE001
                continue

        self.available = False
        log.warning(
            "LLM server not reachable at %s — the bot will trade without it. "
            "Start it with: ./scripts/setup_llm.sh serve",
            self.cfg.base_url,
        )
        return False

    async def close(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    # -- core call ----------------------------------------------------------

    async def _chat(
        self, system: str, user: str, max_tokens: int | None = None, timeout_s: float | None = None
    ) -> dict[str, Any] | None:
        """One completion, JSON-parsed. Returns None on any failure.

        Returning None rather than raising is deliberate: every caller must
        treat "the LLM had nothing to say" as normal operation.
        """
        if self._client is None or not self.available:
            return None

        started = time.time()
        try:
            resp = await self._client.post(
                "/v1/chat/completions",
                json={
                    "model": self.cfg.model,
                    "messages": [
                        {"role": "system", "content": system},
                        {"role": "user", "content": user},
                    ],
                    "temperature": self.cfg.temperature,
                    "max_tokens": max_tokens or self.cfg.max_tokens,
                    "stream": False,
                    # llama.cpp honours this and it markedly improves the
                    # odds of getting parseable output from a small model.
                    "response_format": {"type": "json_object"},
                },
                timeout=timeout_s or self.cfg.timeout_s,
            )
            resp.raise_for_status()
            content = resp.json()["choices"][0]["message"]["content"]
        except (httpx.TimeoutException, asyncio.TimeoutError):
            self.timeouts += 1
            log.debug("LLM timed out after %.1fs", timeout_s or self.cfg.timeout_s)
            return None
        except Exception as exc:  # noqa: BLE001
            log.debug("LLM call failed: %s", exc)
            # A server that has gone away should stop being probed on every
            # candidate; the periodic re-probe will notice when it returns.
            if isinstance(exc, httpx.ConnectError):
                self.available = False
            return None
        finally:
            self.calls += 1
            self.total_latency_s += time.time() - started

        parsed = _extract_json(content)
        if parsed is None:
            self.parse_failures += 1
            log.debug("LLM returned unparseable output: %s", content[:200])
        return parsed

    # -- candidate review ---------------------------------------------------

    async def evaluate_candidate(
        self, candidate: Candidate, base_score: float, social_context: str = ""
    ) -> tuple[float, str, dict[str, Any]]:
        """Advisory adjustment for one candidate.

        Returns (points, human-readable reasoning, raw dict). Always returns
        promptly — if the model is slow or absent the adjustment is zero.
        """
        if not self.cfg.enabled or not self.available:
            return 0.0, "", {}
        if base_score < self.cfg.min_score_to_consult:
            return 0.0, "", {}

        cached = self._cache.get(candidate.mint)
        if cached and (time.time() - cached[0]) < 120:
            data = cached[1]
            return (
                float(data.get("_applied", 0.0)),
                str(data.get("_text", "")),
                data,
            )

        f = candidate.features
        prompt = f"""Token: {candidate.name or "(no name)"} (${candidate.symbol or "?"})
Mint: {candidate.mint}
Age: {f.age_s:.0f}s since launch
Liquidity: {f.sol_in_curve:.2f} SOL in bonding curve
Market cap: {f.market_cap_sol:.1f} SOL
Buyers: {f.unique_buyers} unique, {f.buy_count} buys vs {f.sell_count} sells
Momentum: {f.velocity_sol_per_s:.3f} SOL/sec net inflow
Tracked wallets in: {f.smart_wallet_count} (best reputation {f.smart_wallet_score:.2f})
Creator: {candidate.creator[:12] or "unknown"} — {f.creator_prior_rugs} prior rugs, {f.creator_prior_wins} prior winners
Holders: top-10 hold {f.top10_holding_pct:.0%}, creator holds {f.dev_holding_pct:.1%}
Launch-slot buys: {f.bundle_pct:.0%} of supply
Social: {f.social_mentions} mentions
Quantitative score: {base_score:.1f}/100
{social_context}

Assess for qualitative red flags."""

        data = await self._chat(CANDIDATE_SYSTEM, prompt)
        if not data:
            return 0.0, "", {}

        try:
            raw_adjust = float(data.get("adjust", 0.0))
        except (TypeError, ValueError):
            raw_adjust = 0.0

        verdict = str(data.get("verdict", "neutral")).lower()
        confidence = float(data.get("confidence", 0.5) or 0.5)

        # An explicit "avoid" is honoured even if the model forgot to fill in
        # a number, because a veto is the most valuable thing it produces.
        if verdict == "avoid" and raw_adjust >= 0:
            raw_adjust = -self.cfg.max_penalty * max(0.5, confidence)

        adjust = max(-self.cfg.max_penalty, min(self.cfg.max_bonus, raw_adjust))

        reasons = data.get("reasons") or []
        red_flags = data.get("red_flags") or []
        parts: list[str] = []
        if red_flags:
            parts.append("RED FLAGS: " + "; ".join(str(r) for r in red_flags[:4]))
        if reasons:
            parts.append("; ".join(str(r) for r in reasons[:4]))
        text = f"[{verdict}] " + " | ".join(parts) if parts else f"[{verdict}]"

        data["_applied"] = adjust
        data["_text"] = text
        self._cache[candidate.mint] = (time.time(), data)
        self._prune_cache()

        return adjust, text, data

    def _prune_cache(self, max_entries: int = 500) -> None:
        if len(self._cache) <= max_entries:
            return
        cutoff = time.time() - 300
        self._cache = {k: v for k, v in self._cache.items() if v[0] > cutoff}

    # -- nightly review -----------------------------------------------------

    async def nightly_review(
        self,
        performance: dict[str, Any],
        edge_table: list[dict],
        recent_trades: list[dict],
        current_config: dict[str, Any],
    ) -> dict[str, Any] | None:
        """Analyse your own history and propose bounded parameter changes.

        Given a generous timeout because nothing is waiting on it.
        """
        if not self.cfg.enabled or not self.available:
            return None

        trades_text = "\n".join(
            f"  {i + 1}. {t.get('symbol') or t.get('mint', '')[:10]}: "
            f"{t.get('pnl_pct', 0) * 100:+.1f}% ({t.get('pnl_sol', 0):+.4f} SOL), "
            f"held {(t.get('closed_at', 0) or 0) - (t.get('opened_at', 0) or 0):.0f}s, "
            f"entry score {t.get('entry_score', 0):.0f}, exit: {t.get('exit_reason', '?')}"
            for i, t in enumerate(recent_trades[:40])
        )

        edge_text = "\n".join(
            f"  {r['feature']}={r['bucket']}: {r['trades']} trades, "
            f"{r['win_rate']:.0%} win rate, {r['avg_pnl_pct']:+.1f}% average"
            for r in edge_table[:25]
        )

        prompt = f"""PERFORMANCE ({performance.get('trades', 0)} closed trades)
Win rate: {performance.get('win_rate', 0):.1%}
Total P&L: {performance.get('total_pnl_sol', 0):+.4f} SOL
Profit factor: {performance.get('profit_factor', 0):.2f}
Max drawdown: {performance.get('max_drawdown_sol', 0):.4f} SOL
Best: {performance.get('best_sol', 0):+.4f} | Worst: {performance.get('worst_sol', 0):+.4f}
Average hold: {performance.get('avg_hold_s', 0):.0f}s
Fees paid: {performance.get('total_fees_sol', 0):.4f} SOL
Exit reason counts: {json.dumps(performance.get('exit_reasons', {}))}

LEARNED FEATURE BUCKETS (from this bot's own trades)
{edge_text or "  (not enough data yet)"}

RECENT TRADES
{trades_text or "  (none)"}

CURRENT TUNABLE PARAMETERS
{json.dumps(current_config, indent=2)}

Review this performance and propose changes only where the data supports them."""

        # 90 seconds: a small model on a Pi needs it, and nothing is blocked.
        return await self._chat(
            REVIEW_SYSTEM, prompt, max_tokens=1200, timeout_s=90.0
        )

    # -- health -------------------------------------------------------------

    @property
    def avg_latency_s(self) -> float:
        return self.total_latency_s / self.calls if self.calls else 0.0

    def stats(self) -> dict[str, Any]:
        return {
            "available": self.available,
            "calls": self.calls,
            "timeouts": self.timeouts,
            "parse_failures": self.parse_failures,
            "avg_latency_s": round(self.avg_latency_s, 2),
        }


# Parameters the nightly review is allowed to touch, with hard bounds.
# The model can suggest anything; only these are ever applied, and only inside
# these ranges. This is what stops a confidently wrong suggestion from setting
# your position size to 10 SOL.
TUNABLE_BOUNDS: dict[str, tuple[float, float]] = {
    "scoring.entry_threshold": (40.0, 90.0),
    "gates.min_age_s": (2.0, 120.0),
    "gates.max_age_s": (120.0, 3600.0),
    "gates.min_sol_in_curve": (0.5, 30.0),
    "gates.max_sol_in_curve": (20.0, 85.0),
    "gates.min_unique_buyers": (2.0, 100.0),
    "gates.min_buy_sell_ratio": (1.0, 5.0),
    "gates.max_dev_holding_pct": (0.0, 0.15),
    "gates.max_top10_holding_pct": (0.10, 0.60),
    "exits.stop_loss_pct": (0.15, 0.60),
    "exits.trailing_activate_pct": (0.15, 1.50),
    "exits.trailing_distance_pct": (0.10, 0.50),
    "exits.max_hold_s": (120.0, 3600.0),
    "exits.stagnant_after_s": (60.0, 900.0),
}


def validate_proposed_change(path: str, value: Any) -> tuple[bool, str]:
    """Is this parameter change allowed, and inside its bounds?"""
    if path not in TUNABLE_BOUNDS:
        return False, f"{path} is not in the tunable allowlist"
    try:
        numeric = float(value)
    except (TypeError, ValueError):
        return False, f"{value!r} is not numeric"
    low, high = TUNABLE_BOUNDS[path]
    if not (low <= numeric <= high):
        return False, f"{numeric} is outside the allowed range {low}..{high}"
    return True, ""
