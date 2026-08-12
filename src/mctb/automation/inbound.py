"""Inbound webhooks: TradingView, Zapier, n8n and anything else that can POST.

This turns MCTB into something external systems can drive. A TradingView alert
on a chart condition, an n8n workflow, a Zapier trigger — any of them can send
a signal, request a dossier, or pull the kill switch.

**Security is not optional here.** This endpoint can move money. Three layers:

  1. **A shared secret** in the URL path, so the endpoint is not guessable.
  2. **HMAC-SHA256 signatures** with constant-time comparison, so a leaked URL
     alone is not enough.
  3. **A capability allowlist** — `webhooks.allow_trading` defaults to false,
     so an inbound message cannot open a position unless you have explicitly
     decided it may. Informational and kill commands work regardless.

Replay protection is included: a signed message is valid for a short window
and each nonce is accepted once.

Do not expose this to the public internet. Put it behind Tailscale or a
reverse proxy with TLS. TradingView needs a reachable URL, so if you use it,
terminate TLS properly and keep the secret long.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

log = logging.getLogger(__name__)


@dataclass
class WebhookConfig:
    secret: str = ""
    #: Whether inbound messages may open or close positions.
    allow_trading: bool = False
    #: Whether they may engage the kill switch. Safe to allow — it only stops.
    allow_kill: bool = True
    #: Signature freshness window.
    max_age_s: float = 300.0
    #: Source IPs allowed, empty means any. TradingView publishes its ranges.
    allowed_ips: list[str] = field(default_factory=list)


@dataclass
class InboundSignal:
    """A normalised inbound message."""

    source: str
    action: str
    mint: str = ""
    symbol: str = ""
    size_sol: float = 0.0
    reason: str = ""
    raw: dict[str, Any] = field(default_factory=dict)
    received_at: float = field(default_factory=time.time)


#: Actions an inbound webhook may request.
ACTIONS = {
    # Informational — always permitted.
    "signal": "treat this token as a candidate and score it normally",
    "watch": "add to the watchlist",
    "unwatch": "remove from the watchlist",
    "dossier": "assemble and return a dossier",
    "note": "record a note against a token",
    # Control — permitted unless explicitly disabled.
    "kill": "engage the kill switch",
    "resume": "clear the kill switch",
    # Trading — requires allow_trading.
    "buy": "open a position",
    "sell": "close a position",
    "close_all": "close every position",
}

TRADING_ACTIONS = {"buy", "sell", "close_all"}
KILL_ACTIONS = {"kill", "resume"}


class WebhookReceiver:
    """Verifies, normalises and dispatches inbound webhooks."""

    def __init__(self, config: WebhookConfig) -> None:
        self.config = config
        self._seen_nonces: dict[str, float] = {}
        self.handlers: dict[str, Callable[[InboundSignal], Any]] = {}

        self.received = 0
        self.rejected = 0
        self.dispatched = 0

    def on(self, action: str, handler: Callable[[InboundSignal], Any]) -> None:
        self.handlers[action] = handler

    # -- verification -------------------------------------------------------

    def verify_signature(self, body: bytes, signature: str, timestamp: str) -> tuple[bool, str]:
        """HMAC-SHA256 over `timestamp.body`, compared in constant time."""
        if not self.config.secret:
            return False, "no webhook secret configured"
        if not signature:
            return False, "missing signature"

        try:
            ts = float(timestamp)
        except (TypeError, ValueError):
            return False, "missing or invalid timestamp"

        age = abs(time.time() - ts)
        if age > self.config.max_age_s:
            return False, f"signature is {age:.0f}s old (limit {self.config.max_age_s:.0f}s)"

        expected = hmac.new(
            self.config.secret.encode(),
            f"{timestamp}.".encode() + body,
            hashlib.sha256,
        ).hexdigest()

        # Constant-time: never leak how much of the signature was correct.
        if not hmac.compare_digest(expected, signature.strip().lower()):
            return False, "signature mismatch"

        nonce = signature[:32]
        self._prune_nonces()
        if nonce in self._seen_nonces:
            return False, "replayed signature"
        self._seen_nonces[nonce] = time.time()

        return True, ""

    def _prune_nonces(self) -> None:
        cutoff = time.time() - self.config.max_age_s * 2
        self._seen_nonces = {n: t for n, t in self._seen_nonces.items() if t > cutoff}

    def check_ip(self, ip: str) -> bool:
        if not self.config.allowed_ips:
            return True
        return any(ip.startswith(prefix) for prefix in self.config.allowed_ips)

    # -- parsing ------------------------------------------------------------

    def parse(self, body: bytes, source: str = "webhook") -> InboundSignal | None:
        """Normalise a payload into an InboundSignal.

        Accepts both JSON and TradingView's plain-text alert format, since
        TradingView alerts are often configured as bare text.
        """
        text = body.decode("utf-8", errors="replace").strip()

        data: dict[str, Any]
        try:
            parsed = json.loads(text)
            data = parsed if isinstance(parsed, dict) else {"message": text}
        except json.JSONDecodeError:
            # TradingView plain text: "buy SOL_MINT 0.02" or "kill".
            parts = text.split()
            if not parts:
                return None
            data = {"action": parts[0].lower()}
            if len(parts) > 1:
                data["mint"] = parts[1]
            if len(parts) > 2:
                try:
                    data["size_sol"] = float(parts[2])
                except ValueError:
                    pass

        action = str(data.get("action") or data.get("event") or "signal").lower().strip()
        if action not in ACTIONS:
            # TradingView strategy alerts use these words.
            if action in ("long", "entry", "buy_signal"):
                action = "buy"
            elif action in ("short", "exit", "sell_signal", "close"):
                action = "sell"
            else:
                action = "signal"

        return InboundSignal(
            source=source,
            action=action,
            mint=str(data.get("mint") or data.get("token") or data.get("ticker") or "").strip(),
            symbol=str(data.get("symbol") or "").strip(),
            size_sol=float(data.get("size_sol") or data.get("size") or 0),
            reason=str(data.get("reason") or data.get("comment") or "")[:200],
            raw=data,
        )

    # -- dispatch -----------------------------------------------------------

    def authorise(self, signal: InboundSignal) -> tuple[bool, str]:
        if signal.action in TRADING_ACTIONS and not self.config.allow_trading:
            return False, (
                f"'{signal.action}' requires webhooks.allow_trading: true. "
                "It is off by default so an inbound message cannot spend money."
            )
        if signal.action in KILL_ACTIONS and not self.config.allow_kill:
            return False, f"'{signal.action}' is disabled by config"
        return True, ""

    async def dispatch(self, signal: InboundSignal) -> dict[str, Any]:
        self.received += 1

        allowed, why = self.authorise(signal)
        if not allowed:
            self.rejected += 1
            log.warning("inbound %s from %s rejected: %s", signal.action, signal.source, why)
            return {"ok": False, "error": why}

        handler = self.handlers.get(signal.action)
        if handler is None:
            return {"ok": False, "error": f"no handler registered for '{signal.action}'"}

        log.warning(
            "inbound %s from %s: %s %s",
            signal.action, signal.source,
            signal.mint[:12] or signal.symbol, signal.reason,
        )
        try:
            result = handler(signal)
            if hasattr(result, "__await__"):
                result = await result
            self.dispatched += 1
            return {"ok": True, "action": signal.action, "result": result}
        except Exception as exc:  # noqa: BLE001
            log.exception("inbound handler for %s failed: %s", signal.action, exc)
            return {"ok": False, "error": str(exc)}

    def stats(self) -> dict[str, Any]:
        return {
            "received": self.received,
            "rejected": self.rejected,
            "dispatched": self.dispatched,
            "handlers": sorted(self.handlers),
            "trading_allowed": self.config.allow_trading,
            "secret_set": bool(self.config.secret),
        }


def sign_payload(secret: str, body: bytes, timestamp: float | None = None) -> dict[str, str]:
    """Produce headers for a signed request.

    Useful for testing, and for anything you write yourself that needs to call
    the receiver. n8n and Zapier can both compute this in a code step.
    """
    ts = str(timestamp or time.time())
    signature = hmac.new(
        secret.encode(), f"{ts}.".encode() + body, hashlib.sha256
    ).hexdigest()
    return {"X-MCTB-Timestamp": ts, "X-MCTB-Signature": signature}
