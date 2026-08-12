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

from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from ..automation import WebhookConfig, WebhookReceiver
from ..bus import Bus
from ..config import Config
from ..db import Database
from ..intel import EntityGraph, IntelEngine, NarrativeEngine
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
from ..providers import ProviderRegistry, Tier
from ..providers.links import all_token_links, all_wallet_links
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

        # --- Terminal intelligence -----------------------------------------
        # The Console runs its own provider registry for user-driven lookups.
        # Deliberately separate from the Brain's: a human typing a mint into
        # the terminal must never consume the rate-limit budget the trading
        # loop depends on.
        self.registry = ProviderRegistry(config)
        self.graph = EntityGraph()
        self.intel = IntelEngine(self.registry, self.graph, NarrativeEngine(self.registry))

        self.webhooks = WebhookReceiver(
            WebhookConfig(
                secret=config.webhooks.secret,
                allow_trading=config.webhooks.allow_trading,
                allow_kill=config.webhooks.allow_kill,
                max_age_s=config.webhooks.max_age_s,
                allowed_ips=config.webhooks.allowed_ips,
            )
        )
        self._register_webhook_handlers()

        self._running = False
        self.app = self._build_app()

    # -- webhook handlers ---------------------------------------------------

    def _register_webhook_handlers(self) -> None:
        """Map inbound webhook actions onto bus commands.

        Every action becomes a normal ControlCommand, so an inbound webhook
        can do exactly what the dashboard buttons can do and nothing more.
        """

        async def _kill(signal) -> dict:
            await self.bus.set_kill_switch(True, f"webhook: {signal.reason or signal.source}")
            await self._send_command(
                ControlCommand(source="webhook", command="kill", value=signal.reason)
            )
            return {"killed": True}

        async def _resume(_signal) -> dict:
            await self.bus.set_kill_switch(False)
            await self._send_command(ControlCommand(source="webhook", command="resume"))
            return {"resumed": True}

        async def _close_all(_signal) -> dict:
            await self._send_command(ControlCommand(source="webhook", command="close_all"))
            return {"closing": True}

        async def _sell(signal) -> dict:
            if not signal.mint:
                return {"error": "mint required"}
            await self._send_command(
                ControlCommand(source="webhook", command="close_position", mint=signal.mint)
            )
            return {"closing": signal.mint}

        async def _signal(signal) -> dict:
            """Informational: surface it and enrich, but never auto-buy.

            An external alert is evidence, not an instruction. It goes through
            the same scoring the bot applies to everything else.
            """
            self.intel.request_enrichment(signal.mint, signal.symbol)
            await self._handle_alert(
                Alert(
                    source=f"webhook.{signal.source}",
                    level="info",
                    title=f"External signal: {signal.symbol or signal.mint[:10]}",
                    message=signal.reason or "no reason supplied",
                )
            )
            return {"noted": True, "mint": signal.mint}

        async def _dossier(signal) -> dict:
            if not signal.mint:
                return {"error": "mint required"}
            dossier = await self.intel.assemble(
                signal.mint, signal.symbol, tier=Tier.COLD, timeout_s=6.0
            )
            return dossier.model_dump(mode="json")

        self.webhooks.on("kill", _kill)
        self.webhooks.on("resume", _resume)
        self.webhooks.on("close_all", _close_all)
        self.webhooks.on("sell", _sell)
        self.webhooks.on("signal", _signal)
        self.webhooks.on("watch", _signal)
        self.webhooks.on("note", _signal)
        self.webhooks.on("dossier", _dossier)
        # Deliberately no "buy" handler by default. Even with
        # allow_trading: true, an inbound message routes through `signal` so
        # the bot's own gates and scoring still decide. Wire a direct buy
        # yourself only if you genuinely want an external system to be able to
        # spend your funds without the bot's own checks.

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

        # --- Terminal endpoints --------------------------------------------

        @app.get("/terminal")
        async def terminal() -> FileResponse:
            return FileResponse(str(STATIC_DIR / "terminal.html"))

        @app.get("/api/token/{mint}")
        async def token_dossier(mint: str, deep: bool = False) -> JSONResponse:
            """Full intelligence dossier for one token."""
            if not (32 <= len(mint) <= 44):
                return JSONResponse({"error": "not a valid mint address"}, status_code=400)
            try:
                dossier = await self.intel.assemble(
                    mint,
                    tier=Tier.MANUAL if deep else Tier.COLD,
                    timeout_s=15.0 if deep else 6.0,
                    include_narrative=True,
                )
                return JSONResponse(dossier.model_dump(mode="json"))
            except Exception as exc:  # noqa: BLE001
                log.exception("dossier assembly failed for %s", mint)
                return JSONResponse({"error": str(exc)}, status_code=500)

        @app.get("/api/wallet/{address}")
        async def wallet_dossier(address: str) -> JSONResponse:
            try:
                intel = await self.intel.wallet_dossier(address, tier=Tier.MANUAL)
                payload = intel.model_dump(mode="json")
                payload["links"] = all_wallet_links(address)
                cluster = self.graph.cluster_of(address)
                payload["cluster"] = cluster.to_dict() if cluster else None
                return JSONResponse(payload)
            except Exception as exc:  # noqa: BLE001
                return JSONResponse({"error": str(exc)}, status_code=500)

        @app.get("/api/search")
        async def search(q: str, limit: int = 20) -> JSONResponse:
            provider = self.registry.get("dexscreener")
            if provider is None or not provider.available:
                return JSONResponse({"error": "search provider unavailable"}, status_code=503)
            result = await provider.search(q, limit)  # type: ignore[attr-defined]
            return JSONResponse({"results": result.data or [], "error": result.error})

        @app.get("/api/discovery")
        async def discovery(kind: str = "new", limit: int = 40) -> JSONResponse:
            """Discovery feeds: new launches, or what is being promoted."""
            method = "get_new_pairs" if kind == "new" else "get_trending"
            from ..providers import Capability

            capability = Capability.NEW_PAIRS if kind == "new" else Capability.TRENDING
            results = await self.registry.query_all(
                capability, method, limit, max_tier=Tier.COLD, timeout_s=8.0, limit=2
            )
            merged: list[dict] = []
            seen: set[str] = set()
            for result in results:
                for row in result.data or []:
                    mint = row.get("mint")
                    if mint and mint not in seen:
                        seen.add(mint)
                        row["source"] = result.provider
                        merged.append(row)
            return JSONResponse({"kind": kind, "items": merged[:limit]})

        @app.get("/api/providers")
        async def providers() -> JSONResponse:
            return JSONResponse(
                {
                    "providers": self.registry.describe(),
                    "coverage": self.registry.coverage_report(),
                    "capabilities": self.registry.capability_matrix(),
                    "http": self.registry.http_stats(),
                    "intel": self.intel.stats(),
                }
            )

        @app.post("/api/providers/probe")
        async def probe_providers() -> JSONResponse:
            return JSONResponse({"results": await self.registry.probe_all()})

        @app.get("/api/graph")
        async def graph_view(limit: int = 15) -> JSONResponse:
            return JSONResponse(
                {"stats": self.graph.stats(), "clusters": self.graph.top_clusters(limit)}
            )

        @app.get("/api/links/{mint}")
        async def links(mint: str) -> JSONResponse:
            return JSONResponse(all_token_links(mint))

        # --- Inbound webhooks (TradingView / Zapier / n8n) -----------------

        @app.post("/webhook/{path_secret}")
        async def inbound_webhook(path_secret: str, request: Request) -> JSONResponse:
            if not self.config.webhooks.enabled:
                return JSONResponse({"error": "webhooks disabled"}, status_code=404)

            configured = self.config.webhooks.secret
            if not configured:
                return JSONResponse({"error": "no webhook secret configured"}, status_code=503)

            # Constant-time path check so the URL cannot be brute-forced by
            # timing.
            import hmac as _hmac

            if not _hmac.compare_digest(path_secret, configured):
                log.warning("inbound webhook rejected: bad path secret")
                return JSONResponse({"error": "not found"}, status_code=404)

            client_ip = request.client.host if request.client else ""
            if not self.webhooks.check_ip(client_ip):
                log.warning("inbound webhook rejected: IP %s not allowed", client_ip)
                return JSONResponse({"error": "forbidden"}, status_code=403)

            body = await request.body()

            # HMAC is required for anything that can trade. Informational
            # actions accept path-secret-only, since TradingView cannot sign.
            signature = request.headers.get("X-MCTB-Signature", "")
            timestamp = request.headers.get("X-MCTB-Timestamp", "")
            signed = False
            if signature:
                ok, why = self.webhooks.verify_signature(body, signature, timestamp)
                if not ok:
                    log.warning("inbound webhook rejected: %s", why)
                    return JSONResponse({"error": why}, status_code=401)
                signed = True

            signal = self.webhooks.parse(body, source=f"webhook/{client_ip}")
            if signal is None:
                return JSONResponse({"error": "unparseable payload"}, status_code=400)

            from ..automation.inbound import TRADING_ACTIONS

            if signal.action in TRADING_ACTIONS and not signed:
                return JSONResponse(
                    {
                        "error": (
                            f"'{signal.action}' requires an HMAC signature. "
                            "Send X-MCTB-Signature and X-MCTB-Timestamp headers."
                        )
                    },
                    status_code=401,
                )

            result = await self.webhooks.dispatch(signal)
            return JSONResponse(result, status_code=200 if result.get("ok") else 400)

        @app.get("/api/webhooks")
        async def webhook_status() -> JSONResponse:
            return JSONResponse(self.webhooks.stats())

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

        try:
            self.registry.build()
            await self.registry.start()
            coverage = self.registry.coverage_report()
            log.info(
                "terminal intel: %d/%d providers ready",
                coverage["ready"], coverage["total"],
            )
        except Exception as exc:  # noqa: BLE001
            log.error("provider registry failed to start: %s", exc)

        if self.config.webhooks.enabled and self.config.webhooks.secret:
            log.warning(
                "inbound webhooks live at POST /webhook/<secret> "
                "(trading %s, kill %s)",
                "ALLOWED" if self.config.webhooks.allow_trading else "blocked",
                "allowed" if self.config.webhooks.allow_kill else "blocked",
            )

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
        self.intel.stop()
        with contextlib.suppress(Exception):
            await self.registry.close()
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
