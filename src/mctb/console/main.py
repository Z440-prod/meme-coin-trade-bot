"""Console service — Host PC.

A FastAPI server that mirrors the Brain's state off the bus, pushes it to a
browser dashboard over WebSocket, runs the supervisory watchdog, opens trade
pages on fill, and gives you a kill switch you can hit from your phone.

The Console is strictly a client of the trading system. It can observe and it
can issue commands, but it holds no positions and makes no decisions. Killing
it does not affect trading — which is exactly the property you want from the
machine you also use for everything else.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import signal
import time
from collections import deque
from pathlib import Path
from typing import Any

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from ..bus import Bus
from ..config import Config
from ..db import Database
from ..logging_setup import setup_logging
from ..models import (
    Alert,
    ControlCommand,
    Decision,
    Fill,
    Heartbeat,
    LLMNote,
    PortfolioSnapshot,
    Topic,
)
from .notify import BrowserOpener, Notifier
from .supervisor import Supervisor

log = logging.getLogger(__name__)

STATIC_DIR = Path(__file__).parent / "static"


class ConsoleState:
    """Everything the dashboard needs, mirrored from the bus."""

    def __init__(self, max_events: int = 400) -> None:
        self.snapshot: PortfolioSnapshot | None = None
        self.decisions: deque[dict] = deque(maxlen=max_events)
        self.fills: deque[dict] = deque(maxlen=max_events)
        self.alerts: deque[dict] = deque(maxlen=100)
        self.llm_notes: deque[dict] = deque(maxlen=50)
        self.heartbeats: dict[str, dict] = {}
        self.performance: dict[str, Any] = {}
        self.edge_table: list[dict] = []
        self.started_at = time.time()

    def as_dict(self) -> dict[str, Any]:
        return {
            "snapshot": self.snapshot.model_dump(mode="json") if self.snapshot else None,
            "decisions": list(self.decisions)[-60:],
            "fills": list(self.fills)[-60:],
            "alerts": list(self.alerts)[-30:],
            "llm_notes": list(self.llm_notes)[-10:],
            "heartbeats": self.heartbeats,
            "performance": self.performance,
            "edge_table": self.edge_table[:30],
            "uptime_s": round(time.time() - self.started_at),
            "server_ts": time.time(),
        }


class ConnectionManager:
    def __init__(self) -> None:
        self.active: set[WebSocket] = set()

    async def connect(self, ws: WebSocket) -> None:
        await ws.accept()
        self.active.add(ws)
        log.info("dashboard connected (%d total)", len(self.active))

    def disconnect(self, ws: WebSocket) -> None:
        self.active.discard(ws)

    async def broadcast(self, payload: dict[str, Any]) -> None:
        if not self.active:
            return
        dead = []
        for ws in list(self.active):
            try:
                await ws.send_json(payload)
            except Exception:  # noqa: BLE001
                dead.append(ws)
        for ws in dead:
            self.disconnect(ws)


class Console:
    def __init__(self, config: Config) -> None:
        self.config = config
        self.bus = Bus(
            config.bus.redis_url,
            config.bus.redis_password,
            source="console",
            journal_maxlen=config.bus.journal_maxlen,
        )
        self.db = Database(config.resolve(config.paths.db_file))
        self.state = ConsoleState()
        self.manager = ConnectionManager()

        self.notifier = Notifier(
            desktop=config.console.desktop_notifications,
            telegram_token=config.telegram_bot_token if config.console.telegram_alerts else None,
            telegram_chat_id=config.telegram_chat_id if config.console.telegram_alerts else None,
        )
        self.browser = BrowserOpener(
            enabled=config.console.auto_open_browser,
            url_template=config.console.browser_url_template,
        )
        self.supervisor = Supervisor(
            enabled=config.console.supervisor_enabled,
            stop_loss_pct=config.console.supervisor_stop_loss_pct,
            heartbeat_timeout_s=config.console.brain_heartbeat_timeout_s,
        )
        self.supervisor.send_command = self._send_command
        self.supervisor.send_alert = self._handle_alert

        self._running = False
        self.app = self._build_app()

    # -- bus plumbing -------------------------------------------------------

    async def _send_command(self, command: ControlCommand) -> None:
        await self.bus.publish(Topic.CONTROL, command)

    async def _handle_alert(self, alert: Alert) -> None:
        self.state.alerts.append(alert.model_dump(mode="json"))
        await self.notifier.send(alert.title, alert.message, alert.level)
        await self.manager.broadcast({"type": "alert", "data": alert.model_dump(mode="json")})

    async def _bus_loop(self) -> None:
        async for topic, message in self.bus.subscribe(
            Topic.SNAPSHOT,
            Topic.DECISION,
            Topic.FILL,
            Topic.ALERT,
            Topic.HEARTBEAT,
            Topic.LLM_NOTE,
        ):
            if not self._running:
                break
            try:
                await self._handle_message(topic, message)
            except Exception as exc:  # noqa: BLE001
                log.debug("error handling %s: %s", topic, exc)

    async def _handle_message(self, topic: str, message: Any) -> None:
        if isinstance(message, PortfolioSnapshot):
            self.state.snapshot = message
            self.supervisor.note_snapshot(message)
            await self.manager.broadcast(
                {"type": "snapshot", "data": message.model_dump(mode="json")}
            )

        elif isinstance(message, Decision):
            payload = message.model_dump(mode="json")
            self.state.decisions.append(payload)
            await self.manager.broadcast({"type": "decision", "data": payload})

        elif isinstance(message, Fill):
            payload = message.model_dump(mode="json")
            self.state.fills.append(payload)
            await self.manager.broadcast({"type": "fill", "data": payload})

            if message.side == "buy" and message.ok:
                # The spec's "open the trade website on execution" feature.
                await asyncio.get_event_loop().run_in_executor(
                    None, self.browser.open_for, message.mint
                )
                await self.notifier.send(
                    f"Bought {message.symbol or message.mint[:10]}",
                    f"{message.sol_amount:.4f} SOL at {message.price_sol:.10f}"
                    + (" (paper)" if message.paper else " — LIVE"),
                    "info",
                )
            elif message.side == "sell" and not message.ok:
                await self.notifier.send(
                    f"EXIT FAILED: {message.symbol or message.mint[:10]}",
                    f"{message.error}. This may need manual intervention.",
                    "critical",
                )

        elif isinstance(message, Alert):
            await self._handle_alert(message)

        elif isinstance(message, Heartbeat):
            self.state.heartbeats[message.service] = {
                "ts": message.ts,
                "healthy": message.healthy,
                "detail": message.detail,
            }
            self.supervisor.note_heartbeat(message.service)

        elif isinstance(message, LLMNote):
            payload = message.model_dump(mode="json")
            self.state.llm_notes.append(payload)
            await self.manager.broadcast({"type": "llm_note", "data": payload})
            if message.kind == "review":
                await self.notifier.send(
                    "Nightly review ready", message.text[:200], "info"
                )

    async def _supervisor_loop(self) -> None:
        while self._running:
            await asyncio.sleep(3.0)
            try:
                await self.supervisor.check(self.state.snapshot)
            except Exception as exc:  # noqa: BLE001
                log.exception("supervisor pass failed: %s", exc)

    async def _stats_loop(self) -> None:
        """Refresh the slower-moving analytics the dashboard shows."""
        while self._running:
            try:
                self.state.performance = await self.db.performance_summary()
                rows = await self.db.edge_stats()
                table = [
                    {
                        "feature": feature,
                        "bucket": bucket,
                        "trades": v["trades"],
                        "win_rate": round(v["wins"] / v["trades"], 3) if v["trades"] else 0.0,
                        "avg_pnl_pct": round(100 * v["pnl_sol"] / v["trades"], 2)
                        if v["trades"]
                        else 0.0,
                    }
                    for (feature, bucket), v in rows.items()
                    if v["trades"] >= 5
                ]
                table.sort(key=lambda r: r["avg_pnl_pct"], reverse=True)
                self.state.edge_table = table
            except Exception as exc:  # noqa: BLE001
                log.debug("stats refresh failed: %s", exc)
            await asyncio.sleep(15.0)

    # -- HTTP / WS ----------------------------------------------------------

    def _build_app(self) -> FastAPI:
        app = FastAPI(title="MCTB Console", docs_url=None, redoc_url=None)

        if STATIC_DIR.exists():
            app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

        @app.get("/")
        async def index() -> FileResponse:
            return FileResponse(str(STATIC_DIR / "index.html"))

        @app.get("/api/state")
        async def get_state() -> JSONResponse:
            return JSONResponse(self.state.as_dict())

        @app.get("/api/health")
        async def health() -> JSONResponse:
            brain = self.state.heartbeats.get("brain", {})
            scout = self.state.heartbeats.get("scout", {})
            now = time.time()
            return JSONResponse(
                {
                    "console": "ok",
                    "brain": {
                        "seen": bool(brain),
                        "age_s": round(now - brain.get("ts", 0), 1) if brain else None,
                        "healthy": brain.get("healthy", False),
                    },
                    "scout": {
                        "seen": bool(scout),
                        "age_s": round(now - scout.get("ts", 0), 1) if scout else None,
                        "healthy": scout.get("healthy", False),
                    },
                    "mode": self.state.snapshot.mode if self.state.snapshot else "unknown",
                }
            )

        @app.get("/api/history")
        async def history(limit: int = 100) -> JSONResponse:
            return JSONResponse({"positions": await self.db.closed_positions(limit)})

        @app.post("/api/control/{command}")
        async def control(command: str, mint: str | None = None) -> JSONResponse:
            allowed = {"kill", "resume", "close_position", "close_all", "request_snapshot"}
            if command not in allowed:
                return JSONResponse(
                    {"ok": False, "error": f"unknown command '{command}'"}, status_code=400
                )
            if command == "close_position" and not mint:
                return JSONResponse(
                    {"ok": False, "error": "close_position needs a mint"}, status_code=400
                )

            if command == "kill":
                await self.bus.set_kill_switch(True, "console kill switch")
            elif command == "resume":
                await self.bus.set_kill_switch(False)

            await self._send_command(
                ControlCommand(source="console", command=command, mint=mint)  # type: ignore[arg-type]
            )
            log.warning("control command issued from dashboard: %s %s", command, mint or "")
            return JSONResponse({"ok": True, "command": command, "mint": mint})

        @app.websocket("/ws")
        async def websocket_endpoint(ws: WebSocket) -> None:
            await self.manager.connect(ws)
            try:
                # Send the whole current state immediately so a freshly opened
                # dashboard is populated rather than empty until the next event.
                await ws.send_json({"type": "init", "data": self.state.as_dict()})
                while True:
                    # Incoming messages are only used as a keepalive.
                    await ws.receive_text()
            except WebSocketDisconnect:
                pass
            except Exception:  # noqa: BLE001
                pass
            finally:
                self.manager.disconnect(ws)

        return app

    # -- lifecycle ----------------------------------------------------------

    async def start(self) -> None:
        await self.bus.connect()
        await self.db.connect()
        await self.notifier.connect()
        self._running = True

        # Replay recent journal entries so the dashboard has context on start.
        try:
            for topic, message in await self.bus.replay(count=200):
                with contextlib.suppress(Exception):
                    await self._handle_message(topic, message)
        except Exception as exc:  # noqa: BLE001
            log.debug("journal replay skipped: %s", exc)

        await self._send_command(ControlCommand(source="console", command="request_snapshot"))

    async def stop(self) -> None:
        self._running = False
        await self.notifier.close()
        await self.db.close()
        await self.bus.close()


async def run_console(config: Config) -> None:
    import uvicorn

    setup_logging(config.log_level, config.resolve(config.paths.log_dir), "console")
    console = Console(config)
    await console.start()

    tasks = [
        asyncio.create_task(console._bus_loop(), name="bus"),
        asyncio.create_task(console._supervisor_loop(), name="supervisor"),
        asyncio.create_task(console._stats_loop(), name="stats"),
    ]

    server = uvicorn.Server(
        uvicorn.Config(
            console.app,
            host=config.console.host,
            port=config.console.port,
            log_level="warning",
            access_log=False,
        )
    )

    log.warning(
        "console running — open http://localhost:%d (or http://<this-machine-ip>:%d from your phone)",
        config.console.port,
        config.console.port,
    )

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        with contextlib.suppress(NotImplementedError):
            loop.add_signal_handler(sig, lambda: setattr(server, "should_exit", True))

    try:
        await server.serve()
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await console.stop()
        log.info("console stopped")
