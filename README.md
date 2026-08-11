# MCTB — Memecoin Trading Bot

A distributed pump.fun sniping and copy-trading system for **two Raspberry Pis
and a host PC**, with a local LLM advisor and a learning layer that adapts to
your own results.

Built from [the design brief in `docs/REALITY_CHECK.md`](docs/REALITY_CHECK.md),
with several of its specifics corrected — pump.fun has no official API, X has
no WebSocket, and an LLM cannot make a trade decision in the few seconds a
launch is worth entering. That document explains every deviation.

---

> ### Read this first
>
> **Most memecoin snipe bots lose money.** You are competing against co-located
> servers with direct validator access and sub-10ms latency, from a Raspberry
> Pi behind a home router. On any trade decided by speed, you lose by default.
>
> This system is built to trade a *slower* edge competently: copy wallets with
> a demonstrated record, screen out tokens that were never sellable, size so a
> losing streak cannot end you, exit mechanically, and learn from your own
> results. That is a real strategy. It is not a guaranteed one.
>
> **It ships in paper mode and needs two separate switches to touch real
> money.** Assume you will lose whatever you fund it with, and fund it
> accordingly. [SAFETY.md](docs/SAFETY.md) is not optional reading.

---

## What it does

- **Watches every pump.fun launch** through two independent data paths, so no
  single provider can blind it.
- **Screens each token on chain** for the things that make a position
  unsellable — live mint authority, live freeze authority (the classic
  honeypot), concentrated holders, bundled launches.
- **Copies wallets that actually make you money.** It discovers wallets that
  were early on launches that ran, then re-scores them on the realised P&L of
  *your* copies — not on a public leaderboard.
- **Decides in microseconds** with a deterministic, itemised, fully testable
  scorer. Every decision can be reproduced from its stored feature vector.
- **Uses a local LLM off the hot path** for qualitative red flags and a nightly
  review of your trade history that proposes bounded parameter changes.
- **Manages exits mechanically** — stop loss, trailing stop, a take-profit
  ladder, stagnation and rug detection — in a loop nothing else can block.
- **Learns.** Every trade's entry features and outcome go into a local database
  that shifts future scoring toward what has actually paid you.
- **Shows you everything** on a live dashboard with a kill switch that works
  from your phone.

---

## Table of contents

1. [Try it in five minutes](#1-try-it-in-five-minutes)
2. [How it works](#2-how-it-works)
3. [What you need](#3-what-you-need)
4. [Full install — all three devices](#4-full-install--all-three-devices)
   - [4.1 Host PC](#41-host-pc-console--redis)
   - [4.2 RPi 1 — Scout](#42-rpi-1--scout)
   - [4.3 RPi 2 — Brain](#43-rpi-2--brain)
   - [4.4 The local LLM](#44-the-local-llm)
5. [Configuration](#5-configuration)
6. [Running it](#6-running-it)
7. [Paper trading](#7-paper-trading-do-this-for-weeks)
8. [Going live](#8-going-live)
9. [Daily operation](#9-daily-operation)
10. [Command reference](#10-command-reference)
11. [Repository layout](#11-repository-layout)

---

## 1. Try it in five minutes

You do not need any Raspberry Pi hardware to start. Everything runs on one
machine, and this is genuinely where you should spend your first few weeks.

```bash
git clone https://github.com/z440-prod/meme-coin-trade-bot.git
cd meme-coin-trade-bot

python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[console,dev]"

cp config/config.example.yaml config/config.yaml
cp .env.example .env
```

Run the offline simulator — no network, no Redis, no API keys:

```bash
mctb simulate --tokens 200
```

You get a funnel showing where candidates were rejected, which gates dominate,
and what the strategy would have done. Change a value in
`config/config.yaml`, run it again, see the difference.

Then run the real thing against the live pump.fun feed, in paper mode:

```bash
# one-time: Redis for the message bus
sudo apt install redis-server     # macOS: brew install redis

mctb preflight     # checks everything before you start
mctb all           # runs scout + brain + console in one process
```

Open **http://localhost:8787**.

You are now watching real launches, screening them on chain, and paper-trading
them with simulated fills priced against real bonding curves. No money is at
risk — `execution.mode` is `paper` and `live_trading_enabled` is `false`.

---

## 2. How it works

```
┌──────────────────┐        ┌──────────────────┐
│  SCOUT  (RPi 1)  │        │  BRAIN  (RPi 2)  │
│                  │        │                  │
│ PumpPortal feed  │        │ hard gates       │
│ Solana RPC watch │        │ scorer (µs)      │
│ social polling   │        │ personal edge    │
│ wallet tracking  │        │ LLM (advisory)   │
│ on-chain screen  │        │ risk + sizing    │
│                  │        │ exit machine     │
│ emits Candidates │        │ signs + executes │
│ holds no keys    │        │ HOLDS THE KEY    │
└────────┬─────────┘        └─────────┬────────┘
         │                            │
         └──────────┬─────────────────┘
                    │
            ┌───────▼────────┐
            │  REDIS  (bus)  │
            └───────┬────────┘
                    │
         ┌──────────▼───────────┐
         │  CONSOLE  (Host PC)  │
         │  dashboard · alerts  │
         │  watchdog · killswitch│
         └──────────────────────┘
```

Three processes so that any one can crash or be redeployed without taking the
others down. The Brain is the only machine that ever holds a private key; the
Scout parses untrusted JSON from the internet all day and can trade nothing.

**The decision path is arithmetic, not AI.** A quantized 3B model on a Pi
generates 5–15 tokens/sec, so a 200-token answer takes 15–40 seconds — far
longer than the window in which a launch is still worth entering. So the
scorer decides (microseconds, deterministic, unit-tested) and the LLM advises
off the hot path, bounded in how far it can move a score. Where it genuinely
earns its keep is the nightly review, where nothing is waiting on it.

Full detail in [ARCHITECTURE.md](docs/ARCHITECTURE.md).

---

## 3. What you need

### Minimum (paper trading — start here)

- Any computer with Python 3.10+
- Redis
- A Solana RPC endpoint (the free public one works for paper trading)

### Full deployment

| | Recommended | Why |
|---|---|---|
| **RPi 1** (Scout) | Pi 5, 8GB, NVMe SSD, Ethernet | Feed ingestion, screening |
| **RPi 2** (Brain) | Pi 5, 8GB, NVMe SSD, Ethernet | Execution + LLM; the RAM is for the model |
| **Host PC** | Anything with a browser | Dashboard + Redis. A GPU here is a big upgrade for the LLM |
| **RPC endpoint** | Helius / QuickNode / Triton free tier | **The public endpoint will rate-limit you into missing fills** |

Two things people get wrong and regret:

- **Use Ethernet, not Wi-Fi.** Wi-Fi adds 10–50ms of jitter. You are already
  losing the latency race; don't hand over another 30ms to avoid a cable.
- **Use an SSD, not a cheap microSD.** Constant SQLite and log writes kill
  consumer SD cards, often within months, and they corrupt silently. This is
  the most common cause of a Pi trading bot mysteriously dying.

Buying guide and full wiring instructions: [HARDWARE.md](docs/HARDWARE.md).

---

## 4. Full install — all three devices

Do the host PC first — the Pis need its IP address.

### 4.1 Host PC (Console + Redis)

```bash
git clone https://github.com/z440-prod/meme-coin-trade-bot.git
cd meme-coin-trade-bot
./scripts/install_host.sh
```

This installs Python dependencies, installs and configures Redis for LAN
access, creates `config/config.yaml` and `.env`, and prints this machine's
LAN IP. **Write that IP down** — both Pis need it.

Secure Redis before you go anywhere near live trading. By default it is now
reachable from your LAN with no password, which means anyone on your network
can read your positions and command the Brain to sell:

```bash
sudo nano /etc/redis/redis.conf
#   requirepass <a-long-random-string>
sudo systemctl restart redis-server
```

Put the same value in `MCTB_REDIS_PASSWORD` in `.env` on all three devices.

Then:

```bash
source .venv/bin/activate
nano .env            # set MCTB_REDIS_URL to this machine's IP
mctb preflight
mctb console
```

Open **http://localhost:8787**. It will show both services as "never seen"
until the Pis are up.

---

### 4.2 RPi 1 — Scout

Flash **Raspberry Pi OS 64-bit** (Lite is fine), enable SSH, set the hostname
to `mctb-scout`, then:

```bash
ssh pi@mctb-scout.local
sudo apt update && sudo apt full-upgrade -y && sudo reboot
```

After it comes back:

```bash
git clone https://github.com/z440-prod/meme-coin-trade-bot.git
cd meme-coin-trade-bot
./scripts/install_pi.sh scout
```

The installer handles system packages, NTP (a Pi has no battery-backed clock,
and drift breaks transaction building in ways that look like bad luck), the
virtual environment, and a systemd unit.

Configure it:

```bash
nano .env
```

```bash
MCTB_REDIS_URL=redis://192.168.1.50:6379/0     # your host PC's IP
MCTB_REDIS_PASSWORD=<the password you set>
MCTB_RPC_URL=https://your-endpoint.example.com/?api-key=...
MCTB_RPC_WS_URL=wss://your-endpoint.example.com/?api-key=...
```

Copy `config/config.yaml` from the host PC so all three devices agree, then:

```bash
source .venv/bin/activate
mctb preflight
sudo systemctl enable --now mctb-scout
journalctl -u mctb-scout -f
```

Within a minute you should see new tokens arriving. The dashboard's Scout
indicator turns green.

---

### 4.3 RPi 2 — Brain

Same flash process, hostname `mctb-brain`.

```bash
ssh pi@mctb-brain.local
git clone https://github.com/z440-prod/meme-coin-trade-bot.git
cd meme-coin-trade-bot
./scripts/install_pi.sh brain
```

Note this installs the `[trade]` extras — `solders` for transaction signing and
`cryptography` for the keystore.

```bash
nano .env      # same Redis + RPC values as the Scout
```

Copy `config/config.yaml` across, then:

```bash
source .venv/bin/activate
mctb preflight
sudo systemctl enable --now mctb-brain
journalctl -u mctb-brain -f
```

You should see `brain ready — mode: paper`.

**Do not create a wallet keystore yet.** Paper trading needs no wallet at all.
Come back to that in [section 8](#8-going-live), after weeks of paper results.

---

### 4.4 The local LLM

Optional. The bot trades fine without it and degrades gracefully if it is
unreachable.

On the Brain:

```bash
./scripts/setup_llm.sh install          # builds llama.cpp — 10-25 min on a Pi 5
./scripts/setup_llm.sh model llama3.2-3b
./scripts/setup_llm.sh bench            # measure YOUR hardware
./scripts/setup_llm.sh service          # install + start as systemd
```

`bench` matters. It tells you tokens/sec on your actual hardware, which is what
`llm.timeout_s` should be set against. On a Pi 5 expect 5–15 tok/s — enough for
the nightly review, marginal for live candidates.

**If your host PC has a GPU, run the model there instead.** You go from ~10
tok/s to hundreds, which is the difference between the LLM being useful only
overnight and being useful on live candidates too. Run the server on the PC
and set this on the Brain:

```bash
MCTB_LLM_BASE_URL=http://192.168.1.50:8080
```

---

## 5. Configuration

Two files, with a deliberate split:

| File | Contains | Committed? |
|---|---|---|
| `config/config.yaml` | Strategy, risk, exits — identical on all three devices | Gitignored, but safe to share |
| `.env` | Secrets and per-machine endpoints | **Never share** |

`config.yaml` is merged *on top of* a strategy preset, so your edits always
beat the preset:

```yaml
strategy_preset: balanced    # conservative | balanced | degen
```

| Preset | Entry threshold | Requires a tracked wallet | Max position | Expect |
|---|---|---|---|---|
| `conservative` | 72 | **yes** | 0.02 SOL | a few trades a day |
| `balanced` | 62 | no | 0.05 SOL | steady flow |
| `degen` | 52 | no | **0.015 SOL** | constant, mostly losers |

**Start with `conservative`.** It takes far fewer trades and most of what it
skips would have lost money. Note that `degen` sizes *smaller*, not larger —
more aggression means more shots, each one smaller, which is the only way a
low-hit-rate strategy survives its own losing streaks.

The single highest-value change available to you:

```yaml
gates:
  require_smart_wallet: true    # only buy what a proven wallet already bought
```

Every parameter explained: [STRATEGY.md](docs/STRATEGY.md).

### Seeding wallets to copy

Auto-discovery builds a wallet list over a few days of running. To start
faster, add wallets you already trust (find them on gmgn.ai, dexscreener or
Solscan):

```yaml
wallets:
  seed_wallets:
    - "AArPXm8JatJiuyEffuC1un2Sc835SULa4uQqDcaGpAjV"
```

Seeded wallets start trusted but not blindly — once you have copied one a few
times, its score is driven by the realised P&L of *your* copies. A wallet
whose calls keep losing you money loses influence no matter how good its
public record looks.

---

## 6. Running it

### As services (production)

```bash
sudo systemctl enable --now mctb-scout    # RPi 1
sudo systemctl enable --now mctb-brain    # RPi 2
sudo systemctl enable --now mctb-llm      # RPi 2, optional

journalctl -u mctb-brain -f
```

On the host PC, run `mctb console` — or add it to your startup items.

### In the foreground (development)

```bash
mctb scout      # RPi 1
mctb brain      # RPi 2
mctb console    # host PC
mctb all        # everything in one process
```

### The dashboard

**http://localhost:8787**, or `http://<host-pc-ip>:8787` from your phone on
the same network.

It shows open positions with live P&L and per-position exit buttons,
performance metrics, the decision and fill feeds, tracked wallet reputations,
the learned edge table, LLM notes, and service health. The **KILL** and
**Close all** buttons work from anywhere on your LAN.

Do not port-forward it to the internet — it has no authentication because it
is designed for a trusted LAN. Use Tailscale or an SSH tunnel for remote
access.

---

## 7. Paper trading (do this for weeks)

Paper mode prices against real bonding curves and models latency, slippage,
pump.fun's fee, and your own order's price impact. It is not free money, but
it is free tuition.

Watch your results:

```bash
mctb stats            # performance metrics + recent trades
mctb stats --edge     # what the bot has learned from YOUR trades
```

The metrics that matter, in order:

| Metric | Read it as |
|---|---|
| **Profit factor** | Gross wins ÷ gross losses. Below 1.0 you are losing money. |
| **Max drawdown** | Worst peak-to-trough. Would you really have kept going? |
| **Expectancy** | Average P&L per trade — the only thing that compounds |
| Win rate | **Least** informative. A 25% win rate can be excellent here. |
| Exit reasons | Where your rules actually fire. Often surprising. |

If exits are dominated by `stagnant` or `max_hold`, your entries are early. If
dominated by `stop_loss`, they are late.

Tuning discipline, which is harder than it sounds:

- **One change at a time.** Two and you learn nothing.
- **50+ trades per change.** Below that you are reading noise.
- **Check the funnel before touching weights** — `mctb simulate --tokens 200`
  shows which gate is rejecting everything. If 90% die at one gate, that gate
  *is* your strategy.

---

## 8. Going live

**Only after all of these are true:**

- [ ] 100+ closed paper trades
- [ ] Profit factor above 1.0 across them
- [ ] You have read `mctb stats --edge` and it makes sense to you
- [ ] You have sat through a losing streak on paper without wanting to rewrite
      everything — that reaction is what actually loses money
- [ ] You have read [SAFETY.md](docs/SAFETY.md) in full

### Create a dedicated wallet

In Phantom or Solflare, create a **brand new wallet**. Never reuse one holding
anything else. The key sits decrypted in memory on an internet-connected
device in your house; make the worst case "I lost the trading float".

Export its private key (Settings → Manage Accounts → Show Private Key — the
long base58 string, *not* the seed phrase).

On the Brain, and only the Brain:

```bash
source .venv/bin/activate
mctb keystore create
```

Nothing is echoed to the terminal and the key is never written unencrypted.
It prints the public address — fund that with an amount whose total loss would
be annoying but not harmful. **Well under 1 SOL to start.**

```bash
chmod 600 .env secrets/trading.keystore
chmod 700 secrets/
mctb keystore show     # confirm the address and balance
```

### Arm it

Both latches, deliberately, so no single careless edit can arm your wallet:

```yaml
execution:
  mode: live
  live_trading_enabled: true
```

Start absurdly small. The first live week is not for profit — it is for
discovering everything that behaves differently with real money while each
lesson costs a fraction of a cent:

```yaml
risk:
  max_position_sol: 0.005
  max_concurrent_positions: 1
  max_positions_per_hour: 3
  daily_loss_limit_sol: 0.02
```

```bash
mctb preflight                    # will warn loudly that live is armed
sudo systemctl restart mctb-brain
journalctl -u mctb-brain -f
```

Watch the first several trades in real time. Scale up only once live results
match paper results — if they diverge sharply, the difference is real and paper
was flattering you.

### Stopping, urgently

```bash
mctb kill          # stop all new entries
touch KILL_SWITCH  # works even if Redis is down
mctb close-all     # sell everything at market, now
```

Or the red buttons on the dashboard, from your phone. Exits keep running
regardless — abandoning open positions is not a safe state.

If the bot is unresponsive entirely, import the key into Phantom and sell
manually. **The bot is not required to access your funds.**

---

## 9. Daily operation

```bash
mctb stats                    # how are we doing
mctb stats --edge             # what has it learned
mctb review                   # run the LLM trade review now
make backup                   # snapshot the database
```

**Back up the database.** `data/mctb.db` holds every trade, the learned feature
table, wallet reputations and creator blocklists. A fresh install with an empty
database is a bot with amnesia that will repeat mistakes you already paid for.

The nightly review (default 04:00 UTC) has the local LLM read your performance,
learned edge and recent trades, then report what preceded losses and propose
specific parameter changes. Proposals are validated against an allowlist with
hard bounds, so a confidently wrong suggestion cannot set your position size to
10 SOL. `auto_apply_tuning` defaults to false and should stay there — the value
is in the analysis, not in letting a 3B model edit your risk limits.

Things going wrong: [TROUBLESHOOTING.md](docs/TROUBLESHOOTING.md).

---

## 10. Command reference

```
mctb scout | brain | console | all     run a service
mctb simulate --tokens 200             offline synthetic-market test
mctb preflight                         check every dependency and endpoint
mctb stats [--edge]                    performance and learned edge
mctb review                            run the LLM trade review now
mctb kill / resume                     engage or clear the kill switch
mctb close-all                         sell every open position at market
mctb keystore create | show            manage the encrypted wallet
mctb config-show                       resolved config, secrets masked
```

```
make install-dev   install with all extras
make test          run the test suite (192 tests)
make lint          ruff
make sim           run the simulator
make backup        snapshot the database
```

---

## 11. Repository layout

```
src/mctb/
├── config.py            all tunables, validated at startup
├── models.py            event contract shared by all three devices
├── bus.py               Redis pub/sub + durable journal
├── db.py                SQLite — trades, learned edge, reputations
├── keystore.py          scrypt-encrypted wallet storage
├── preflight.py         pre-flight checks
├── simulator.py         offline synthetic market
├── cli.py               the `mctb` command
│
├── solana/
│   ├── curve.py         bonding curve decode + pricing + quoting
│   ├── rpc.py           async RPC with failover
│   └── checks.py        mint/freeze authority, concentration, bundles
│
├── scout/               RPi 1
│   ├── pumpportal.py    multiplexed WebSocket feed
│   ├── rpc_watcher.py   independent RPC path + price oracle
│   ├── social.py        X polling (optional)
│   ├── wallets.py       smart-wallet discovery and reputation
│   ├── observer.py      rolling per-token statistics
│   └── main.py
│
├── brain/               RPi 2
│   ├── gates.py         hard filters + portfolio risk limits
│   ├── scoring.py       deterministic scorer + position sizing
│   ├── edge.py          the personal learning layer
│   ├── llm.py           local LLM advisor (off the hot path)
│   ├── executor.py      paper + live signing
│   ├── exits.py         exit state machine (pure, fully tested)
│   ├── portfolio.py     position lifecycle and accounting
│   └── main.py
│
└── console/             Host PC
    ├── supervisor.py    backstop stop-loss + heartbeat watchdog
    ├── notify.py        desktop/Telegram alerts, browser opening
    ├── static/          the dashboard (no build step)
    └── main.py

docs/     ARCHITECTURE · HARDWARE · STRATEGY · SAFETY · TROUBLESHOOTING · REALITY_CHECK
scripts/  install_pi.sh · install_host.sh · setup_llm.sh
tests/    192 tests — curve maths, exits, gates, scoring, learning, lifecycle
```

---

## Licence

MIT. See [LICENSE](LICENSE).

Provided as-is, with no warranty. Trading cryptocurrency carries a substantial
risk of total loss. You are responsible for your own funds, your own
configuration, and your own compliance with the law where you live.
