"""Outbound alerts: Telegram, Discord, and generic webhooks.

Telegram and Discord both take a webhook or bot token and nothing else — no
OAuth, no app review. That makes them the practical way to get alerts onto
your phone from a Pi in your house.

Everything here is rate limited and deduplicated, because an alert channel
that floods is an alert channel you mute, and a muted channel is worse than
none: you will miss the one message that mattered.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Any

import httpx

log = logging.getLogger(__name__)


@dataclass
class AlertRoute:
    """Where a class of alert goes, and how loudly."""

    name: str
    min_level: str = "info"          # info | warning | critical
    telegram: bool = True
    discord: bool = True
    webhooks: list[str] = field(default_factory=list)


LEVEL_ORDER = {"info": 0, "warning": 1, "critical": 2}


class OutboundNotifier:
    """Fan-out to every configured channel."""

    def __init__(
        self,
        *,
        telegram_token: str | None = None,
        telegram_chat_id: str | None = None,
        discord_webhook: str | None = None,
        generic_webhooks: list[str] | None = None,
        min_interval_s: float = 1.0,
        dedupe_window_s: float = 60.0,
    ) -> None:
        self.telegram_token = telegram_token
        self.telegram_chat_id = telegram_chat_id
        self.discord_webhook = discord_webhook
        self.generic_webhooks = generic_webhooks or []
        self.min_interval_s = min_interval_s
        self.dedupe_window_s = dedupe_window_s

        self._client: httpx.AsyncClient | None = None
        self._last_sent: dict[str, float] = {}
        self._last_any = 0.0
        self.sent = 0
        self.suppressed = 0
        self.errors = 0

    @property
    def enabled(self) -> bool:
        return bool(
            (self.telegram_token and self.telegram_chat_id)
            or self.discord_webhook
            or self.generic_webhooks
        )

    async def start(self) -> None:
        if self.enabled:
            self._client = httpx.AsyncClient(timeout=httpx.Timeout(10.0))
            channels = []
            if self.telegram_token and self.telegram_chat_id:
                channels.append("telegram")
            if self.discord_webhook:
                channels.append("discord")
            if self.generic_webhooks:
                channels.append(f"{len(self.generic_webhooks)} webhook(s)")
            log.info("outbound alerts: %s", ", ".join(channels))

    async def close(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    def _should_send(self, key: str) -> bool:
        now = time.time()
        if now - self._last_sent.get(key, 0.0) < self.dedupe_window_s:
            self.suppressed += 1
            return False
        if now - self._last_any < self.min_interval_s:
            self.suppressed += 1
            return False
        self._last_sent[key] = now
        self._last_any = now
        return True

    async def send(
        self,
        title: str,
        message: str,
        level: str = "info",
        *,
        dedupe_key: str | None = None,
        links: dict[str, str] | None = None,
        fields: dict[str, Any] | None = None,
    ) -> bool:
        if not self.enabled or self._client is None:
            return False
        if not self._should_send(dedupe_key or title):
            return False

        tasks = []
        if self.telegram_token and self.telegram_chat_id:
            tasks.append(self._telegram(title, message, level, links, fields))
        if self.discord_webhook:
            tasks.append(self._discord(title, message, level, links, fields))
        for url in self.generic_webhooks:
            tasks.append(self._generic(url, title, message, level, fields))

        results = await asyncio.gather(*tasks, return_exceptions=True)
        ok = any(r is True for r in results)
        if ok:
            self.sent += 1
        else:
            self.errors += 1
        return ok

    # -- channels -----------------------------------------------------------

    async def _telegram(
        self, title: str, message: str, level: str,
        links: dict[str, str] | None, fields: dict[str, Any] | None,
    ) -> bool:
        icon = {"critical": "\U0001f6a8", "warning": "⚠️"}.get(level, "ℹ️")
        lines = [f"{icon} *{self._md(title)}*", "", self._md(message)]
        if fields:
            lines.append("")
            lines.extend(f"`{k}`: {self._md(str(v))}" for k, v in list(fields.items())[:8])
        if links:
            lines.append("")
            lines.append(" | ".join(f"[{k}]({v})" for k, v in list(links.items())[:6]))

        try:
            resp = await self._client.post(  # type: ignore[union-attr]
                f"https://api.telegram.org/bot{self.telegram_token}/sendMessage",
                json={
                    "chat_id": self.telegram_chat_id,
                    "text": "\n".join(lines)[:4000],
                    "parse_mode": "Markdown",
                    "disable_web_page_preview": True,
                },
            )
            if resp.status_code >= 400:
                log.warning("telegram rejected the message: %s", resp.text[:160])
                return False
            return True
        except Exception as exc:  # noqa: BLE001
            log.debug("telegram send failed: %s", exc)
            return False

    async def _discord(
        self, title: str, message: str, level: str,
        links: dict[str, str] | None, fields: dict[str, Any] | None,
    ) -> bool:
        colour = {"critical": 0xEF5350, "warning": 0xFFA726}.get(level, 0x42A5F5)
        embed: dict[str, Any] = {
            "title": title[:250],
            "description": message[:4000],
            "color": colour,
            "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime()),
        }
        if fields:
            embed["fields"] = [
                {"name": str(k)[:250], "value": str(v)[:1000], "inline": True}
                for k, v in list(fields.items())[:10]
            ]
        if links:
            embed.setdefault("fields", []).append(
                {
                    "name": "Links",
                    "value": " · ".join(f"[{k}]({v})" for k, v in list(links.items())[:6])[:1000],
                    "inline": False,
                }
            )
        try:
            resp = await self._client.post(  # type: ignore[union-attr]
                self.discord_webhook, json={"embeds": [embed]}  # type: ignore[arg-type]
            )
            return resp.status_code < 400
        except Exception as exc:  # noqa: BLE001
            log.debug("discord send failed: %s", exc)
            return False

    async def _generic(
        self, url: str, title: str, message: str, level: str, fields: dict[str, Any] | None
    ) -> bool:
        """Plain JSON POST — what Zapier, n8n, Make and IFTTT all consume."""
        try:
            resp = await self._client.post(  # type: ignore[union-attr]
                url,
                json={
                    "source": "mctb",
                    "title": title,
                    "message": message,
                    "level": level,
                    "timestamp": time.time(),
                    "data": fields or {},
                },
            )
            return resp.status_code < 400
        except Exception as exc:  # noqa: BLE001
            log.debug("webhook %s failed: %s", url[:40], exc)
            return False

    @staticmethod
    def _md(text: str) -> str:
        """Escape Telegram Markdown so a token symbol cannot break formatting."""
        for char in ("_", "*", "[", "]", "`"):
            text = text.replace(char, f"\\{char}")
        return text

    def stats(self) -> dict[str, Any]:
        return {
            "enabled": self.enabled,
            "sent": self.sent,
            "suppressed": self.suppressed,
            "errors": self.errors,
            "channels": {
                "telegram": bool(self.telegram_token and self.telegram_chat_id),
                "discord": bool(self.discord_webhook),
                "webhooks": len(self.generic_webhooks),
            },
        }
