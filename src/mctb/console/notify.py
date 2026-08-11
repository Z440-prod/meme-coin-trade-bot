"""Alerts and browser automation on the host PC.

Desktop notifications work on Linux (notify-send), macOS (osascript) and
Windows (PowerShell toast) without installing anything extra. Telegram is
optional and is what you actually want when you are not at the machine.
"""

from __future__ import annotations

import asyncio
import logging
import platform
import shutil
import subprocess
import time
import webbrowser

import httpx

log = logging.getLogger(__name__)


class Notifier:
    def __init__(
        self,
        desktop: bool = True,
        telegram_token: str | None = None,
        telegram_chat_id: str | None = None,
        min_interval_s: float = 2.0,
    ) -> None:
        self.desktop = desktop
        self.telegram_token = telegram_token
        self.telegram_chat_id = telegram_chat_id
        self.min_interval_s = min_interval_s
        self._last_sent: dict[str, float] = {}
        self._client: httpx.AsyncClient | None = None
        self._system = platform.system()

    async def connect(self) -> None:
        if self.telegram_token and self.telegram_chat_id:
            self._client = httpx.AsyncClient(timeout=httpx.Timeout(10.0))
            log.info("telegram alerts enabled")

    async def close(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    async def send(self, title: str, message: str, level: str = "info") -> None:
        # Rate limit per title so a flapping condition cannot spam you into
        # muting the whole channel.
        now = time.time()
        last = self._last_sent.get(title, 0.0)
        if now - last < self.min_interval_s:
            return
        self._last_sent[title] = now

        if self.desktop:
            await asyncio.get_event_loop().run_in_executor(
                None, self._desktop_notify, title, message, level
            )
        if self._client is not None:
            await self._telegram(title, message, level)

    def _desktop_notify(self, title: str, message: str, level: str) -> None:
        try:
            if self._system == "Linux" and shutil.which("notify-send"):
                urgency = {"critical": "critical", "warning": "normal"}.get(level, "low")
                subprocess.run(
                    ["notify-send", "-u", urgency, f"MCTB: {title}", message],
                    check=False,
                    timeout=5,
                )
            elif self._system == "Darwin":
                safe_msg = message.replace('"', "'")
                safe_title = title.replace('"', "'")
                subprocess.run(
                    [
                        "osascript",
                        "-e",
                        f'display notification "{safe_msg}" with title "MCTB: {safe_title}"',
                    ],
                    check=False,
                    timeout=5,
                )
            elif self._system == "Windows":
                safe_msg = message.replace("'", "")
                safe_title = title.replace("'", "")
                script = (
                    "[Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, "
                    "ContentType = WindowsRuntime] | Out-Null; "
                    "$t = [Windows.UI.Notifications.ToastNotificationManager]::GetTemplateContent(0); "
                    f"$t.GetElementsByTagName('text').Item(0).AppendChild($t.CreateTextNode('MCTB: {safe_title}')) | Out-Null; "
                    f"$t.GetElementsByTagName('text').Item(1).AppendChild($t.CreateTextNode('{safe_msg}')) | Out-Null; "
                    "[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier('MCTB')"
                    ".Show([Windows.UI.Notifications.ToastNotification]::new($t))"
                )
                subprocess.run(
                    ["powershell", "-NoProfile", "-Command", script], check=False, timeout=10
                )
        except Exception as exc:  # noqa: BLE001
            log.debug("desktop notification failed: %s", exc)

    async def _telegram(self, title: str, message: str, level: str) -> None:
        if self._client is None:
            return
        icon = {"critical": "\U0001f6a8", "warning": "⚠️"}.get(level, "ℹ️")
        try:
            await self._client.post(
                f"https://api.telegram.org/bot{self.telegram_token}/sendMessage",
                json={
                    "chat_id": self.telegram_chat_id,
                    "text": f"{icon} *{title}*\n{message}",
                    "parse_mode": "Markdown",
                },
            )
        except Exception as exc:  # noqa: BLE001
            log.debug("telegram send failed: %s", exc)


class BrowserOpener:
    """Opens the trade page when a position fills.

    The spec asked for this and it is genuinely useful — but only with a hard
    rate limit. A busy session would otherwise open thirty tabs in a minute
    and make the machine unusable at exactly the moment you need it.
    """

    def __init__(
        self,
        enabled: bool = True,
        url_template: str = "https://pump.fun/coin/{mint}",
        max_per_minute: int = 4,
    ) -> None:
        self.enabled = enabled
        self.url_template = url_template
        self.max_per_minute = max_per_minute
        self._opened: dict[str, float] = {}
        self._recent: list[float] = []

    def open_for(self, mint: str) -> bool:
        if not self.enabled:
            return False

        now = time.time()
        self._recent = [t for t in self._recent if now - t < 60]
        if len(self._recent) >= self.max_per_minute:
            log.debug("browser rate limit reached; not opening %s", mint[:10])
            return False
        if now - self._opened.get(mint, 0.0) < 300:
            return False

        url = self.url_template.format(mint=mint)
        try:
            webbrowser.open_new_tab(url)
            self._opened[mint] = now
            self._recent.append(now)
            log.info("opened %s", url)
            return True
        except Exception as exc:  # noqa: BLE001
            log.warning("could not open browser: %s", exc)
            return False
