"""SQLite persistence — the bot's long-term memory.

This is the file that makes the system *yours*. Every signal you saw, every
trade you took, the full feature vector at entry and the realised outcome all
land here, and the personal-edge layer reads it back to work out which
patterns actually pay for you.

Back this file up. A fresh install with an empty database is a bot with
amnesia that will repeat mistakes you already paid for.
"""

from __future__ import annotations

import json
import logging
import time
from pathlib import Path
from typing import Any

import aiosqlite

from .models import Candidate, Decision, Fill, Position, WalletStat

log = logging.getLogger(__name__)

SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA synchronous=NORMAL;
PRAGMA foreign_keys=ON;

CREATE TABLE IF NOT EXISTS tokens (
    mint            TEXT PRIMARY KEY,
    name            TEXT,
    symbol          TEXT,
    creator         TEXT,
    first_seen      REAL,
    launch_sol      REAL DEFAULT 0,
    peak_sol_in_curve REAL DEFAULT 0,
    peak_multiple   REAL DEFAULT 0,
    migrated        INTEGER DEFAULT 0,
    rugged          INTEGER DEFAULT 0,
    last_update     REAL
);

CREATE TABLE IF NOT EXISTS candidates (
    id          TEXT PRIMARY KEY,
    ts          REAL,
    mint        TEXT,
    symbol      TEXT,
    trigger     TEXT,
    features    TEXT,
    safety      TEXT
);
CREATE INDEX IF NOT EXISTS idx_candidates_mint ON candidates(mint);
CREATE INDEX IF NOT EXISTS idx_candidates_ts   ON candidates(ts);

CREATE TABLE IF NOT EXISTS decisions (
    id          TEXT PRIMARY KEY,
    ts          REAL,
    mint        TEXT,
    symbol      TEXT,
    action      TEXT,
    score       REAL,
    size_sol    REAL,
    breakdown   TEXT,
    reason      TEXT,
    llm_reasoning TEXT
);
CREATE INDEX IF NOT EXISTS idx_decisions_ts ON decisions(ts);

CREATE TABLE IF NOT EXISTS fills (
    id          TEXT PRIMARY KEY,
    ts          REAL,
    mint        TEXT,
    symbol      TEXT,
    side        TEXT,
    ok          INTEGER,
    sol_amount  REAL,
    token_amount REAL,
    price_sol   REAL,
    fee_sol     REAL,
    signature   TEXT,
    paper       INTEGER,
    latency_ms  REAL,
    exit_reason TEXT,
    error       TEXT
);
CREATE INDEX IF NOT EXISTS idx_fills_mint ON fills(mint);
CREATE INDEX IF NOT EXISTS idx_fills_ts   ON fills(ts);

CREATE TABLE IF NOT EXISTS positions (
    id              TEXT PRIMARY KEY,
    mint            TEXT,
    symbol          TEXT,
    creator         TEXT,
    state           TEXT,
    opened_at       REAL,
    closed_at       REAL,
    entry_price_sol REAL,
    cost_sol        REAL,
    tokens_bought   REAL,
    tokens_held     REAL,
    realised_sol    REAL,
    fees_sol        REAL,
    peak_price_sol  REAL,
    pnl_sol         REAL,
    pnl_pct         REAL,
    entry_score     REAL,
    entry_features  TEXT,
    smart_wallets   TEXT,
    exit_reason     TEXT,
    paper           INTEGER
);
CREATE INDEX IF NOT EXISTS idx_positions_state  ON positions(state);
CREATE INDEX IF NOT EXISTS idx_positions_opened ON positions(opened_at);

CREATE TABLE IF NOT EXISTS wallets (
    address         TEXT PRIMARY KEY,
    label           TEXT,
    seeded          INTEGER DEFAULT 0,
    tokens_bought   INTEGER DEFAULT 0,
    wins            INTEGER DEFAULT 0,
    losses          INTEGER DEFAULT 0,
    copied_trades   INTEGER DEFAULT 0,
    copied_pnl_sol  REAL DEFAULT 0,
    avg_entry_sol_in_curve REAL DEFAULT 0,
    last_seen       REAL,
    score           REAL DEFAULT 0.5,
    subscribed      INTEGER DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_wallets_score ON wallets(score DESC);

-- Creator reputation: did this deployer's previous launches treat you well?
CREATE TABLE IF NOT EXISTS creators (
    address     TEXT PRIMARY KEY,
    launches    INTEGER DEFAULT 0,
    rugs        INTEGER DEFAULT 0,
    wins        INTEGER DEFAULT 0,
    pnl_sol     REAL DEFAULT 0,
    blocked     INTEGER DEFAULT 0,
    last_seen   REAL
);

-- Aggregated outcomes per feature bucket. This is the personal edge.
CREATE TABLE IF NOT EXISTS edge_stats (
    feature     TEXT,
    bucket      TEXT,
    trades      INTEGER DEFAULT 0,
    wins        INTEGER DEFAULT 0,
    pnl_sol     REAL DEFAULT 0,
    updated     REAL,
    PRIMARY KEY (feature, bucket)
);

CREATE TABLE IF NOT EXISTS daily_stats (
    day         TEXT PRIMARY KEY,
    trades      INTEGER DEFAULT 0,
    wins        INTEGER DEFAULT 0,
    losses      INTEGER DEFAULT 0,
    pnl_sol     REAL DEFAULT 0,
    fees_sol    REAL DEFAULT 0,
    volume_sol  REAL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS llm_notes (
    id      TEXT PRIMARY KEY,
    ts      REAL,
    kind    TEXT,
    mint    TEXT,
    text    TEXT,
    data    TEXT
);

CREATE TABLE IF NOT EXISTS kv (
    key     TEXT PRIMARY KEY,
    value   TEXT,
    updated REAL
);
"""


def _j(value: Any) -> str:
    return json.dumps(value, default=str)


class Database:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self._db: aiosqlite.Connection | None = None

    async def connect(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._db = await aiosqlite.connect(str(self.path))
        self._db.row_factory = aiosqlite.Row
        await self._db.executescript(SCHEMA)
        await self._db.commit()
        log.info("database ready at %s", self.path)

    async def close(self) -> None:
        if self._db is not None:
            await self._db.close()
            self._db = None

    @property
    def db(self) -> aiosqlite.Connection:
        if self._db is None:
            raise RuntimeError("Database.connect() must be awaited before use")
        return self._db

    # -- writes -------------------------------------------------------------

    async def record_token(
        self, mint: str, name: str, symbol: str, creator: str, launch_sol: float = 0.0
    ) -> None:
        await self.db.execute(
            """INSERT INTO tokens (mint, name, symbol, creator, first_seen, launch_sol, last_update)
               VALUES (?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(mint) DO UPDATE SET last_update=excluded.last_update""",
            (mint, name, symbol, creator, time.time(), launch_sol, time.time()),
        )
        if creator:
            await self.db.execute(
                """INSERT INTO creators (address, launches, last_seen) VALUES (?, 1, ?)
                   ON CONFLICT(address) DO UPDATE SET
                     launches = launches + 1, last_seen = excluded.last_seen""",
                (creator, time.time()),
            )
        await self.db.commit()

    async def record_candidate(self, cand: Candidate) -> None:
        await self.db.execute(
            "INSERT OR REPLACE INTO candidates VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                cand.id,
                cand.ts,
                cand.mint,
                cand.symbol,
                cand.trigger,
                cand.features.model_dump_json(),
                cand.safety.model_dump_json(),
            ),
        )
        await self.db.commit()

    async def record_decision(self, dec: Decision) -> None:
        await self.db.execute(
            "INSERT OR REPLACE INTO decisions VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                dec.id,
                dec.ts,
                dec.mint,
                dec.symbol,
                dec.action,
                dec.score,
                dec.size_sol,
                dec.breakdown.model_dump_json(),
                dec.reason,
                dec.llm_reasoning,
            ),
        )
        await self.db.commit()

    async def record_fill(self, fill: Fill) -> None:
        await self.db.execute(
            "INSERT OR REPLACE INTO fills VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                fill.id,
                fill.ts,
                fill.mint,
                fill.symbol,
                fill.side,
                int(fill.ok),
                fill.sol_amount,
                fill.token_amount,
                fill.price_sol,
                fill.fee_sol,
                fill.signature,
                int(fill.paper),
                fill.latency_ms,
                fill.exit_reason,
                fill.error,
            ),
        )
        await self.db.commit()

    async def upsert_position(self, pos: Position) -> None:
        await self.db.execute(
            """INSERT OR REPLACE INTO positions VALUES
               (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                pos.id,
                pos.mint,
                pos.symbol,
                pos.creator,
                pos.state.value,
                pos.opened_at,
                pos.closed_at,
                pos.entry_price_sol,
                pos.cost_sol,
                pos.tokens_bought,
                pos.tokens_held,
                pos.realised_sol,
                pos.fees_sol,
                pos.peak_price_sol,
                pos.total_pnl_sol,
                pos.pnl_pct,
                pos.entry_score,
                pos.entry_features.model_dump_json(),
                _j(pos.smart_wallets),
                pos.exit_reason,
                int(pos.paper),
            ),
        )
        await self.db.commit()

    async def upsert_wallet(self, w: WalletStat) -> None:
        await self.db.execute(
            "INSERT OR REPLACE INTO wallets VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                w.address,
                w.label,
                int(w.seeded),
                w.tokens_bought,
                w.wins,
                w.losses,
                w.copied_trades,
                w.copied_pnl_sol,
                w.avg_entry_sol_in_curve,
                w.last_seen,
                w.score,
                int(w.subscribed),
            ),
        )
        await self.db.commit()

    async def bump_creator(
        self, address: str, *, rug: bool = False, win: bool = False, pnl_sol: float = 0.0
    ) -> None:
        if not address:
            return
        await self.db.execute(
            """INSERT INTO creators (address, launches, rugs, wins, pnl_sol, last_seen)
               VALUES (?, 0, ?, ?, ?, ?)
               ON CONFLICT(address) DO UPDATE SET
                 rugs = rugs + ?, wins = wins + ?, pnl_sol = pnl_sol + ?,
                 last_seen = excluded.last_seen""",
            (
                address,
                int(rug),
                int(win),
                pnl_sol,
                time.time(),
                int(rug),
                int(win),
                pnl_sol,
            ),
        )
        await self.db.commit()

    async def block_creator(self, address: str) -> None:
        await self.db.execute(
            """INSERT INTO creators (address, blocked, last_seen) VALUES (?, 1, ?)
               ON CONFLICT(address) DO UPDATE SET blocked = 1""",
            (address, time.time()),
        )
        await self.db.commit()

    async def bump_edge(
        self, feature: str, bucket: str, *, win: bool, pnl_sol: float
    ) -> None:
        await self.db.execute(
            """INSERT INTO edge_stats (feature, bucket, trades, wins, pnl_sol, updated)
               VALUES (?, ?, 1, ?, ?, ?)
               ON CONFLICT(feature, bucket) DO UPDATE SET
                 trades = trades + 1, wins = wins + ?, pnl_sol = pnl_sol + ?,
                 updated = excluded.updated""",
            (feature, bucket, int(win), pnl_sol, time.time(), int(win), pnl_sol),
        )
        await self.db.commit()

    async def bump_daily(
        self, day: str, *, win: bool, pnl_sol: float, fees_sol: float, volume_sol: float
    ) -> None:
        await self.db.execute(
            """INSERT INTO daily_stats (day, trades, wins, losses, pnl_sol, fees_sol, volume_sol)
               VALUES (?, 1, ?, ?, ?, ?, ?)
               ON CONFLICT(day) DO UPDATE SET
                 trades = trades + 1, wins = wins + ?, losses = losses + ?,
                 pnl_sol = pnl_sol + ?, fees_sol = fees_sol + ?, volume_sol = volume_sol + ?""",
            (
                day,
                int(win),
                int(not win),
                pnl_sol,
                fees_sol,
                volume_sol,
                int(win),
                int(not win),
                pnl_sol,
                fees_sol,
                volume_sol,
            ),
        )
        await self.db.commit()

    async def record_llm_note(
        self, note_id: str, ts: float, kind: str, mint: str, text: str, data: dict
    ) -> None:
        await self.db.execute(
            "INSERT OR REPLACE INTO llm_notes VALUES (?,?,?,?,?,?)",
            (note_id, ts, kind, mint, text, _j(data)),
        )
        await self.db.commit()

    async def set_kv(self, key: str, value: Any) -> None:
        await self.db.execute(
            """INSERT INTO kv (key, value, updated) VALUES (?, ?, ?)
               ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated=excluded.updated""",
            (key, _j(value), time.time()),
        )
        await self.db.commit()

    async def get_kv(self, key: str, default: Any = None) -> Any:
        cur = await self.db.execute("SELECT value FROM kv WHERE key = ?", (key,))
        row = await cur.fetchone()
        return json.loads(row["value"]) if row else default

    # -- reads --------------------------------------------------------------

    async def open_positions(self) -> list[dict]:
        cur = await self.db.execute(
            "SELECT * FROM positions WHERE state IN ('opening','open','closing')"
        )
        return [dict(r) for r in await cur.fetchall()]

    async def closed_positions(self, limit: int = 500) -> list[dict]:
        cur = await self.db.execute(
            "SELECT * FROM positions WHERE state='closed' ORDER BY closed_at DESC LIMIT ?",
            (limit,),
        )
        return [dict(r) for r in await cur.fetchall()]

    async def wallets(self, limit: int = 200) -> list[WalletStat]:
        cur = await self.db.execute(
            "SELECT * FROM wallets ORDER BY score DESC LIMIT ?", (limit,)
        )
        rows = await cur.fetchall()
        return [
            WalletStat(
                address=r["address"],
                label=r["label"] or "",
                seeded=bool(r["seeded"]),
                tokens_bought=r["tokens_bought"],
                wins=r["wins"],
                losses=r["losses"],
                copied_trades=r["copied_trades"],
                copied_pnl_sol=r["copied_pnl_sol"],
                avg_entry_sol_in_curve=r["avg_entry_sol_in_curve"],
                last_seen=r["last_seen"] or 0.0,
                score=r["score"],
                subscribed=bool(r["subscribed"]),
            )
            for r in rows
        ]

    async def creator_reputation(self, address: str) -> dict[str, Any]:
        cur = await self.db.execute("SELECT * FROM creators WHERE address = ?", (address,))
        row = await cur.fetchone()
        if row is None:
            return {"launches": 0, "rugs": 0, "wins": 0, "pnl_sol": 0.0, "blocked": False}
        return {
            "launches": row["launches"],
            "rugs": row["rugs"],
            "wins": row["wins"],
            "pnl_sol": row["pnl_sol"],
            "blocked": bool(row["blocked"]),
        }

    async def blocked_creators(self) -> set[str]:
        cur = await self.db.execute("SELECT address FROM creators WHERE blocked = 1")
        return {r["address"] for r in await cur.fetchall()}

    async def edge_stats(self) -> dict[tuple[str, str], dict[str, Any]]:
        cur = await self.db.execute("SELECT * FROM edge_stats")
        return {
            (r["feature"], r["bucket"]): {
                "trades": r["trades"],
                "wins": r["wins"],
                "pnl_sol": r["pnl_sol"],
            }
            for r in await cur.fetchall()
        }

    async def daily_stat(self, day: str) -> dict[str, Any]:
        cur = await self.db.execute("SELECT * FROM daily_stats WHERE day = ?", (day,))
        row = await cur.fetchone()
        if row is None:
            return {"trades": 0, "wins": 0, "losses": 0, "pnl_sol": 0.0}
        return dict(row)

    async def recent_mint_trade(self, mint: str, within_s: float) -> bool:
        """Have we already traded this mint recently? Prevents revenge-buying
        the same coin that just stopped us out."""
        cur = await self.db.execute(
            "SELECT 1 FROM positions WHERE mint = ? AND opened_at > ? LIMIT 1",
            (mint, time.time() - within_s),
        )
        return await cur.fetchone() is not None

    async def performance_summary(self, limit: int = 1000) -> dict[str, Any]:
        """Headline metrics over closed positions — the numbers that tell you
        whether any of this is working."""
        cur = await self.db.execute(
            "SELECT pnl_sol, pnl_pct, cost_sol, fees_sol, opened_at, closed_at, exit_reason "
            "FROM positions WHERE state='closed' ORDER BY closed_at DESC LIMIT ?",
            (limit,),
        )
        rows = [dict(r) for r in await cur.fetchall()]
        if not rows:
            return {"trades": 0}

        pnls = [r["pnl_sol"] or 0.0 for r in rows]
        wins = [p for p in pnls if p > 0]
        losses = [p for p in pnls if p <= 0]
        gross_win = sum(wins)
        gross_loss = abs(sum(losses))
        total = sum(pnls)

        # Max drawdown over the equity curve, oldest -> newest.
        equity, peak, max_dd = 0.0, 0.0, 0.0
        for p in reversed(pnls):
            equity += p
            peak = max(peak, equity)
            max_dd = max(max_dd, peak - equity)

        mean = total / len(pnls)
        variance = sum((p - mean) ** 2 for p in pnls) / len(pnls)
        std = variance**0.5
        exit_reasons: dict[str, int] = {}
        for r in rows:
            reason = r["exit_reason"] or "unknown"
            exit_reasons[reason] = exit_reasons.get(reason, 0) + 1

        return {
            "trades": len(pnls),
            "wins": len(wins),
            "losses": len(losses),
            "win_rate": len(wins) / len(pnls),
            "total_pnl_sol": total,
            "avg_pnl_sol": mean,
            "best_sol": max(pnls),
            "worst_sol": min(pnls),
            "gross_profit_sol": gross_win,
            "gross_loss_sol": gross_loss,
            # Profit factor > 1 means the winners outweigh the losers.
            "profit_factor": (gross_win / gross_loss) if gross_loss > 0 else float("inf"),
            "max_drawdown_sol": max_dd,
            # Per-trade Sharpe-like ratio; not annualised, but comparable
            # between your own strategy versions.
            "sharpe_per_trade": (mean / std) if std > 0 else 0.0,
            "total_fees_sol": sum(r["fees_sol"] or 0.0 for r in rows),
            "avg_hold_s": sum(
                (r["closed_at"] or 0) - (r["opened_at"] or 0) for r in rows
            )
            / len(rows),
            "exit_reasons": exit_reasons,
        }
