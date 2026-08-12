"""Runtime tuning — the process-level speed settings.

Small, unglamorous changes with measurable effect on a Raspberry Pi. None of
them is clever; together they matter more than most algorithmic tuning,
because they apply to every operation the process performs.

**uvloop.** A drop-in replacement for asyncio's event loop, written in Cython
over libuv. Typically 2-4x faster on socket-heavy workloads, which is exactly
what this is: dozens of concurrent WebSocket and HTTP streams. One line, no
code changes, and it is the single largest free speedup available.

**Event loop debug off, GC tuned.** Python's generational garbage collector
runs young-generation sweeps very frequently by default. Under a high
allocation rate — parsing thousands of JSON messages a minute — those sweeps
introduce latency spikes at unpredictable moments. Raising the thresholds
trades a little memory for materially smoother tail latency.

**Thread pool sizing.** The default `ThreadPoolExecutor` sizes itself from CPU
count, which on a 4-core Pi means a pool large enough to starve the event loop
if anything blocking sneaks in.
"""

from __future__ import annotations

import asyncio
import gc
import logging
import os
import platform
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Any

log = logging.getLogger(__name__)

_TUNED = False


def install_uvloop() -> bool:
    """Swap in uvloop if available. Returns whether it took effect."""
    if sys.platform == "win32":
        return False
    try:
        import uvloop

        asyncio.set_event_loop_policy(uvloop.EventLoopPolicy())
        log.info("uvloop installed (2-4x faster event loop)")
        return True
    except ImportError:
        log.info(
            "uvloop not installed — running on the stock asyncio loop. "
            "Install with: pip install uvloop"
        )
        return False


def tune_gc(gen0: int = 50_000, gen1: int = 50, gen2: int = 50) -> None:
    """Reduce GC pause frequency under high allocation rates.

    The default gen0 threshold of 700 means a collection every 700 net object
    allocations, which under a busy feed is many times a second. Raising it
    trades memory for fewer latency spikes — the right trade for a trading
    loop, where a 15ms pause at the wrong moment costs a fill.
    """
    gc.set_threshold(gen0, gen1, gen2)
    # Freeze everything allocated during import so the collector stops
    # rescanning module-level objects that will never become garbage.
    gc.collect()
    gc.freeze()
    log.debug("gc thresholds set to (%d, %d, %d) and startup heap frozen", gen0, gen1, gen2)


def tune_event_loop(loop: asyncio.AbstractEventLoop | None = None, max_threads: int = 4) -> None:
    loop = loop or asyncio.get_event_loop()
    loop.set_debug(False)
    loop.set_default_executor(ThreadPoolExecutor(max_workers=max_threads, thread_name_prefix="mctb"))


def apply(gc_tuning: bool = True, uvloop_enabled: bool = True) -> dict[str, Any]:
    """Apply every runtime tuning. Call once, before the loop starts."""
    global _TUNED
    if _TUNED:
        return {"already_tuned": True}

    result: dict[str, Any] = {
        "python": platform.python_version(),
        "platform": platform.machine(),
        "cpus": os.cpu_count() or 1,
    }
    result["uvloop"] = install_uvloop() if uvloop_enabled else False
    if gc_tuning:
        tune_gc()
        result["gc_tuned"] = True

    _TUNED = True
    return result


class LatencyTracker:
    """Rolling latency percentiles for the paths that matter.

    Averages hide the problem. A decision path that averages 3ms but has a
    p99 of 900ms will still miss the trades that matter, because the slow
    cases correlate with exactly the busy moments you are trying to trade.
    """

    def __init__(self, window: int = 2000) -> None:
        self.window = window
        self._samples: dict[str, list[float]] = {}
        self._counts: dict[str, int] = {}

    def record(self, name: str, ms: float) -> None:
        samples = self._samples.setdefault(name, [])
        samples.append(ms)
        if len(samples) > self.window:
            samples.pop(0)
        self._counts[name] = self._counts.get(name, 0) + 1

    def timer(self, name: str) -> _Timer:
        return _Timer(self, name)

    def percentiles(self, name: str) -> dict[str, float]:
        samples = sorted(self._samples.get(name, []))
        if not samples:
            return {}

        def pct(p: float) -> float:
            index = min(len(samples) - 1, int(len(samples) * p))
            return round(samples[index], 3)

        return {
            "count": self._counts.get(name, 0),
            "p50": pct(0.50),
            "p90": pct(0.90),
            "p99": pct(0.99),
            "max": round(samples[-1], 3),
            "mean": round(sum(samples) / len(samples), 3),
        }

    def report(self) -> dict[str, dict[str, float]]:
        return {name: self.percentiles(name) for name in sorted(self._samples)}


class _Timer:
    __slots__ = ("tracker", "name", "started")

    def __init__(self, tracker: LatencyTracker, name: str) -> None:
        self.tracker = tracker
        self.name = name
        self.started = 0.0

    def __enter__(self) -> _Timer:
        self.started = time.perf_counter()
        return self

    def __exit__(self, *_exc: object) -> None:
        self.tracker.record(self.name, (time.perf_counter() - self.started) * 1000)


#: Process-wide tracker. Reported on the terminal's latency panel.
LATENCY = LatencyTracker()
