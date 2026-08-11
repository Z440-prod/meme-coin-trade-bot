"""Supervisory watchdog on the host PC.

A design note, because the source specification and good engineering disagree
here and the disagreement matters.

The spec puts stop-loss enforcement on the host PC. That is the wrong place
for the *primary* stop: the host PC is a desktop machine that gets shut down,
goes to sleep, updates itself and reboots. If it owns your stop loss, then
closing your laptop means your positions have no stop loss at all.

So the primary stop lives in the Brain, next to the execution path, on a
device meant to run unattended. What the Console provides instead is a second,
independent layer that catches the failure the Brain cannot catch itself:

  - **Brain gone silent.** No heartbeat for N seconds means positions are
    open with nothing watching them. That is the emergency.
  - **A stop that should have fired and did not.** The Console watches the
    same positions with a deliberately wider threshold. If a position blows
    through it, the Brain has failed and the Console orders the exit over the
    bus.

Wider on purpose: if both layers used the same number they would race, and
you would get duplicate sell orders on every ordinary stop-out.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field

from ..models import Alert, ControlCommand, PortfolioSnapshot, Position

log = logging.getLogger(__name__)


@dataclass
class SupervisorState:
    last_snapshot_ts: float = 0.0
    last_heartbeat_ts: float = 0.0
    brain_alive: bool = False
    alerts_sent: dict[str, float] = field(default_factory=dict)
    interventions: int = 0


class Supervisor:
    def __init__(
        self,
        enabled: bool = True,
        stop_loss_pct: float = 0.50,
        heartbeat_timeout_s: float = 20.0,
        stale_position_s: float = 120.0,
    ) -> None:
        self.enabled = enabled
        self.stop_loss_pct = stop_loss_pct
        self.heartbeat_timeout_s = heartbeat_timeout_s
        self.stale_position_s = stale_position_s
        self.state = SupervisorState()

        # Set by the Console: async callables.
        self.send_command = None
        self.send_alert = None

        self._ordered_exit: dict[str, float] = {}

    def note_snapshot(self, snapshot: PortfolioSnapshot) -> None:
        self.state.last_snapshot_ts = time.time()

    def note_heartbeat(self, service: str) -> None:
        if service == "brain":
            self.state.last_heartbeat_ts = time.time()
            self.state.brain_alive = True

    async def check(self, snapshot: PortfolioSnapshot | None) -> list[str]:
        """Run one supervision pass. Returns human-readable findings."""
        if not self.enabled:
            return []

        findings: list[str] = []
        now = time.time()

        # --- Is the Brain alive? --------------------------------------------
        if self.state.last_heartbeat_ts:
            silence = now - self.state.last_heartbeat_ts
            if silence > self.heartbeat_timeout_s:
                if self.state.brain_alive:
                    self.state.brain_alive = False
                    open_count = len(snapshot.positions) if snapshot else 0
                    message = (
                        f"No heartbeat from the Brain for {silence:.0f}s."
                        + (
                            f" {open_count} position(s) are open with nothing managing them. "
                            "Check RPi 2 now."
                            if open_count
                            else " No open positions, so nothing is at risk."
                        )
                    )
                    findings.append(message)
                    await self._alert(
                        "Brain unreachable", message, "critical" if open_count else "warning"
                    )
            elif not self.state.brain_alive:
                self.state.brain_alive = True
                await self._alert("Brain back online", "Heartbeat resumed.", "info")

        if snapshot is None:
            return findings

        # --- Backstop stop loss ----------------------------------------------
        for position in snapshot.positions:
            finding = await self._check_position(position, now)
            if finding:
                findings.append(finding)

        return findings

    async def _check_position(self, position: Position, now: float) -> str | None:
        if position.entry_price_sol <= 0 or position.tokens_held <= 0:
            return None
        # Do not re-order an exit we already ordered.
        if now - self._ordered_exit.get(position.mint, 0.0) < 60:
            return None

        label = position.symbol or position.mint[:10]

        # Price has stopped updating — the Brain may be alive but its pricing
        # path is not.
        if position.last_price_ts and (now - position.last_price_ts) > self.stale_position_s:
            self._ordered_exit[position.mint] = now
            self.state.interventions += 1
            message = (
                f"{label} has had no price update for "
                f"{now - position.last_price_ts:.0f}s. Ordering an exit."
            )
            await self._alert("Supervisor: stale position", message, "critical")
            await self._order_exit(position.mint, "supervisor_stale_price")
            return message

        loss = 1.0 - (position.current_price_sol / position.entry_price_sol)
        if loss >= self.stop_loss_pct:
            self._ordered_exit[position.mint] = now
            self.state.interventions += 1
            message = (
                f"{label} is down {loss:.0%}, past the supervisory limit of "
                f"{self.stop_loss_pct:.0%}. The Brain's own stop should have fired "
                "already — ordering an exit."
            )
            await self._alert("Supervisor: backstop stop loss", message, "critical")
            await self._order_exit(position.mint, "supervisor_stop_loss")
            return message

        return None

    async def _order_exit(self, mint: str, reason: str) -> None:
        if self.send_command is None:
            log.error("supervisor wanted to exit %s but has no command channel", mint)
            return
        log.critical("SUPERVISOR ORDERING EXIT of %s (%s)", mint[:10], reason)
        await self.send_command(
            ControlCommand(source="console", command="close_position", mint=mint, value=reason)
        )

    async def _alert(self, title: str, message: str, level: str) -> None:
        # Do not repeat the same alert more than once a minute.
        now = time.time()
        if now - self.state.alerts_sent.get(title, 0.0) < 60:
            return
        self.state.alerts_sent[title] = now

        log_fn = {
            "critical": log.critical,
            "warning": log.warning,
        }.get(level, log.info)
        log_fn("%s: %s", title, message)

        if self.send_alert is not None:
            await self.send_alert(
                Alert(source="console.supervisor", level=level, title=title, message=message)  # type: ignore[arg-type]
            )
