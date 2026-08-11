# Architecture

## The three devices

```
┌──────────────────────────┐     ┌──────────────────────────┐
│      SCOUT  (RPi 1)      │     │      BRAIN  (RPi 2)      │
│                          │     │                          │
│  PumpPortal WebSocket ───┼─┐   │  ┌── deterministic       │
│  Solana RPC log watcher  │ │   │  │   scorer (µs)         │
│  X / social polling      │ │   │  ├── hard gates          │
│  Smart-wallet tracker    │ │   │  ├── risk limits         │
│  On-chain safety screen  │ │   │  ├── position sizing     │
│  Watchlist + features    │ │   │  ├── exit state machine  │
│                          │ │   │  ├── executor (paper/    │
│  emits: Candidate        │ │   │  │   live signing)       │
│         TokenTrade       │ │   │  └── LLM advisor         │
│         WalletTrade      │ │   │      (OFF the hot path)  │
│         Social           │ │   │                          │
│                          │ │   │  holds the ONLY key      │
│  holds no keys           │ │   │  emits: Decision, Fill,  │
└───────────┬──────────────┘ │   │         Snapshot         │
            │                │   └───────────┬──────────────┘
            │                │               │
            └────────────────┼───────────────┘
                             │
                    ┌────────▼─────────┐
                    │  REDIS  (bus)    │
                    │  pub/sub + a     │
                    │  durable journal │
                    └────────┬─────────┘
                             │
                 ┌───────────▼──────────────┐
                 │     CONSOLE  (Host PC)   │
                 │                          │
                 │  Web dashboard (WS)      │
                 │  Supervisory watchdog    │
                 │  Auto-open trade pages   │
                 │  Desktop / Telegram      │
                 │  Kill switch             │
                 │                          │
                 │  observes and commands;  │
                 │  holds no positions      │
                 └──────────────────────────┘
```

## Why three processes

Each device can crash, be redeployed, or be switched off without taking the
others with it.

- **Scout down** → no new entries. Open positions keep their exit rules,
  because the Brain prices them itself from chain state.
- **Console down** → you lose the dashboard. Trading is unaffected.
- **Brain down** → this is the one that matters. Open positions stop being
  managed. The Console detects the missing heartbeat within
  `console.brain_heartbeat_timeout_s` and alerts you.

The Brain is also the only machine that ever holds a private key. The Scout
parses untrusted JSON from third-party WebSockets all day and is the most
likely component to crash or be attacked; it can trade nothing.

## The two paths through the system

### Entry path — event-driven, must be fast

```
PumpPortal / RPC  →  Scout observes token, accumulates rolling stats
                  →  prelim triage score (cheap, no I/O)
                  →  on-chain safety screen (2-3 RPC reads)
                  →  publish Candidate
                          ↓  Redis
Brain             →  hard gates            (veto, microseconds)
                  →  deterministic score   (microseconds)
                  →  personal edge adjust  (microseconds, learned)
                  →  LLM advisory          (only if already qualifying,
                                            hard timeout, bounded effect)
                  →  risk limits           (portfolio-level)
                  →  size position
                  →  executor.buy()
```

The prelim triage exists because pump.fun launches thousands of tokens a day
and each safety screen costs RPC calls. Screening everything would exhaust any
RPC quota in minutes.

### Exit path — polled, must never be blocked

```
every 1.2s:  batch-read every open position's bonding curve (ONE rpc call)
          →  mark to market, update peaks, arm trailing stops
          →  evaluate_exit() per position
          →  executor.sell() on a match, with escalating fee retries
```

Plus an event-driven fast lane: a dev sell or a tracked wallet exiting arrives
over the bus and is acted on immediately rather than at the next poll.

The exit loop is a separate `asyncio` task from everything else and holds no
locks. Scoring, LLM calls, dashboard updates and the Scout being dead can all
stall without delaying an exit by a single tick.

## Message bus

Redis, because it gives pub/sub and a durable stream from one `apt install`
on one machine, and because all three devices need to find each other across a
LAN and survive one another's restarts.

| Channel | Publisher | Consumers | Purpose |
|---|---|---|---|
| `mctb.token.new` | Scout | — | new launch observed |
| `mctb.token.trade` | Scout | Brain | trades on subscribed tokens |
| `mctb.wallet.trade` | Scout | Brain | a tracked wallet traded |
| `mctb.migration` | Scout | Brain | token graduated to a DEX |
| `mctb.social` | Scout | — | social mention |
| `mctb.candidate` | Scout | Brain | screened trade candidate |
| `mctb.decision` | Brain | Console | buy or skip, with reasoning |
| `mctb.fill` | Brain | Console | execution result |
| `mctb.snapshot` | Brain | Console | full portfolio state, 1 Hz |
| `mctb.control` | Console/CLI | Brain | kill, resume, close |
| `mctb.heartbeat` | all | Console | liveness |
| `mctb.alert` | all | Console | notifications |
| `mctb.llm` | Brain | Console | LLM notes and nightly review |

Everything also lands in the `mctb.journal` Redis stream, capped by
`bus.journal_maxlen`, so the Console can replay recent history on startup
instead of showing a blank dashboard.

## State ownership

There is exactly one source of truth for each thing:

| State | Owner | Notes |
|---|---|---|
| Open positions | **Brain** | in memory + SQLite; restored on restart |
| Trade history, learned edge | **Brain** | SQLite at `paths.db_file` |
| Wallet reputation | **Brain** writes, Scout reads | SQLite |
| Price | **Chain**, read by both | never a third-party quote |
| Kill switch | Redis key + a file | either engages it |
| Dashboard state | **Console** | a mirror, never authoritative |

The Console mirrors the Brain's snapshots. It never computes portfolio state
itself, so a Console bug cannot corrupt your accounting.

## The learning layer

This is what makes the system yours rather than generic, and it has three
independent parts, all fed from your own closed trades:

**Feature-bucket expectancy** (`brain/edge.py`). Every trade's entry features
are bucketed and stored with the realised outcome. Future candidates are
adjusted up or down by how that bucket has actually performed for you. Shrunk
by sample size and hard-capped, so a lucky streak cannot rewrite your strategy.
Weighted toward expectancy rather than hit rate, because memecoin returns are
fat-tailed and win rate ranks fat-tailed strategies backwards.

**Wallet reputation** (`scout/wallets.py`). Wallets are discovered by watching
who bought launches that later ran. Once you are actually copying one, its
score shifts to being driven by realised P&L on *your* copies — the only
measure that pays rent. A wallet whose calls keep losing you money loses
influence regardless of how good its public record looks.

**Creator blocklist** (`brain/portfolio.py`). A position that loses most of its
value within minutes is treated as a rug, and that deployer is blocked
permanently. Serial ruggers reuse wallets more than you would expect.

All three live in one SQLite file. **Back it up.** A fresh install with an
empty database is a bot with amnesia that will repeat mistakes you already
paid for.

## Where the money actually goes

Roughly, per round trip, on top of price movement:

- pump.fun trade fee: ~1% each way
- priority fees: `execution.priority_fee_sol` in, `exit_priority_fee_sol` out
- price impact: your own order moving a thin curve, both directions
- slippage: someone else's transaction landing between decision and inclusion

A 0.05 SOL position on a thin curve can easily give up 6–10% round trip before
the price does anything at all. This is why `size_position()` refuses to open
positions below 0.001 SOL, and why the paper executor models all four costs —
a simulation without them will report a losing strategy as profitable.

## Further reading

- [HARDWARE.md](HARDWARE.md) — what to buy and how to wire it
- [STRATEGY.md](STRATEGY.md) — tuning, and what each parameter really does
- [SAFETY.md](SAFETY.md) — read before funding a wallet
- [REALITY_CHECK.md](REALITY_CHECK.md) — deviations from the original spec
- [TROUBLESHOOTING.md](TROUBLESHOOTING.md) — when things break
