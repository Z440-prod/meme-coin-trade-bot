"""Logging: pretty on a console, JSON lines on disk for later analysis."""

from __future__ import annotations

import json
import logging
import logging.handlers
import sys
import time
from pathlib import Path

from rich.console import Console
from rich.logging import RichHandler

_SENSITIVE = ("passphrase", "private_key", "secret", "bearer", "api-key", "api_key")


class RedactFilter(logging.Filter):
    """Belt-and-braces guard against a key ending up in a log file.

    Nothing should ever log a secret, but logs get pasted into chats and
    issue trackers, so scrub anything that looks like one on the way out.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            msg = str(record.getMessage())
        except Exception:  # noqa: BLE001
            return True
        lowered = msg.lower()
        if any(token in lowered for token in _SENSITIVE):
            for token in _SENSITIVE:
                idx = lowered.find(token)
                while idx != -1:
                    # Redact the value that follows the sensitive key name.
                    tail_start = idx + len(token)
                    tail = msg[tail_start : tail_start + 80]
                    if any(sep in tail[:3] for sep in ("=", ":", '"', " ")):
                        msg = msg[:tail_start] + " <redacted>"
                        break
                    idx = lowered.find(token, idx + 1)
            record.msg = msg
            record.args = ()
        return True


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "ts": round(record.created, 3),
            "iso": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(record.created)),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return json.dumps(payload)


def setup_logging(
    level: str = "INFO", log_dir: str | Path | None = None, service: str = "mctb"
) -> None:
    root = logging.getLogger()
    root.setLevel(getattr(logging, level.upper(), logging.INFO))
    root.handlers.clear()

    console = RichHandler(
        console=Console(stderr=True),
        rich_tracebacks=True,
        show_path=False,
        markup=False,
        log_time_format="%H:%M:%S",
    )
    console.addFilter(RedactFilter())
    root.addHandler(console)

    if log_dir:
        path = Path(log_dir)
        path.mkdir(parents=True, exist_ok=True)
        # Rotate so a Pi's SD card cannot fill up and brick the service.
        fileh = logging.handlers.RotatingFileHandler(
            path / f"{service}.jsonl", maxBytes=20_000_000, backupCount=5
        )
        fileh.setFormatter(JsonFormatter())
        fileh.addFilter(RedactFilter())
        root.addHandler(fileh)

    # These are chatty and drown out anything useful.
    for noisy in ("websockets.client", "websockets.protocol", "httpx", "httpcore", "asyncio"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    sys.excepthook = lambda t, v, tb: logging.getLogger("mctb").critical(
        "uncaught exception", exc_info=(t, v, tb)
    )
