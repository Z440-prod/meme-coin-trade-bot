# MCTB — Memecoin Trading Terminal

An intelligence and execution platform for Solana memecoins, running on **two
Raspberry Pis and a host PC**.

Fuses 20+ data sources into one cross-checked view, resolves which wallets are
secretly the same actor, screens every token for the things that make it
unsellable, routes execution through Jupiter, and learns from your own trade
history — with a keyboard-driven terminal on top.

```
mctb token <mint>          →  full dossier: security, market, entity graph, 12 links
mctb providers             →  what every data source can currently answer
mctb console               →  the terminal at :8787/terminal
```

---

> ### Read this first
>
> **Most memecoin bots lose money.** You are competing against co-located
> servers with direct validator access and sub-10ms latency, from a Raspberry
> Pi behind a home router. On any trade decided purely by speed, you lose.
>
> This system does not try to win that race. It competes on **information and
> discipline**: seeing things other bots do not check, refusing trades that
> cannot be exited, sizing so a losing streak cannot end you, and learning
> from your own results rather than generic advice.
>
> That is a real edge. It is not a guaranteed one. It ships in paper mode,
> needs two separate switches to touch real money, and
> [SAFETY.md](docs/SAFETY.md) is not optional reading.

---

## What it actually does that other bots don't

**Collapses sybil wallets before counting consensus.** A naive bot sees "5
smart wallets bought this" and scores it as five independent confirmations. In
reality those five were funded from the same address eleven minutes ago —
they are *one actor*, possibly one who knows what your bot looks for and is
manufacturing exactly that signal. MCTB clusters wallets by funding lineage,
temporal batching, co-buying and behavioural fingerprint, then **counts each
entity once**.

**Tests whether you can actually sell, before buying.** Every candidate gets a
live Jupiter sell quote. If no sell route exists, the trade is refused — and
that catches honeypots which pass every contract scan, because the contract
is fine and the *market* is the trap.

**Prices the round trip before committing.** Fees plus price impact in both
directions. On a thin token this routinely exceeds 20%, meaning the position
needs a 20% move just to break even. Worth knowing before, not after.

**Detects manufactured volume.** Huge volume against thin liquidity with few
unique wallets is the same coins cycling between the same hands. Computed from
free data that most bots never bother to cross-reference.

**Grades attention by shape, not level.** Social volume is trivially bought.
MCTB scores posting-interval regularity, near-duplicate text, account age and
follower uniformity — so a high mention count with a high astroturf score
scores **worse** than no mentions at all.

**Cross-checks every provider against every other.** Blockers are OR'd (one
credible "unsafe" beats three "fine"), quality is medianed, disagreement is
recorded and drops confidence. Every answer carries provenance.

**Learns what pays *you*.** Every trade's entry features and outcome feed a
local expectancy table, shrunk by sample size and hard-capped so a lucky
streak cannot rewrite your strategy.

---

## Contents

1. [Five minutes](#1-five-minutes)
2. [Architecture](#2-architecture)
3. [Hardware](#3-hardware)
4. [Install — all three devices](#4-install--all-three-devices)
5. [Data providers](#5-data-providers)
6. [The terminal](#6-the-terminal)
7. [Alerts and automation](#7-alerts-and-automation)
8. [Speed](#8-speed)
9. [Paper trading](#9-paper-trading)
10. [Going live](#10-going-live)
11. [Command reference](#11-command-reference)
12. [Layout](#12-layout)

---

## 1. Five minutes

No Raspberry Pi needed. Everything runs on one machine, and this is where you
should spend your first weeks.

```bash
git clone https://github.com/z440-prod/meme-coin-trade-bot.git
cd meme-coin-trade-bot

python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[all,dev]"

cp config/config.example.yaml config/config.yaml
cp .env.example .env
```

Look up a real token right now — **no API keys, no Redis, no setup**:

```bash
mctb token 8LXPAS3MR5oSeEC1LqGZyHVpgs1Qw6m9My6iDKTPpump
```

```
SPIDERMAN  8LXPAS3MR5oSeEC1LqGZyHVpgs1Qw6m9My6iDKTPpump
assembled in 793ms at cold tier

No blockers found
 Liquidity                                $0
 Volume 24h                          $51,782
 Risk score                            1/100
 Confidence                             high
 Sources           goplus, rugcheck, jupiter
 Mint authority                      revoked
 Freeze authority                    revoked
 Sellable                                yes
 Top 10 hold                           38.1%
```

Three independent providers agreed, in under a second, for free.

See what you have:

```bash
mctb providers            # 6 ready with zero keys, all critical gaps covered
mctb simulate --tokens 200  # offline strategy test, no network
```

Then run the whole system in paper mode:

```bash
sudo apt install redis-server     # macOS: brew install redis
mctb preflight
mctb all
```

Dashboard at **http://localhost:8787** · terminal at **/terminal**.

---

## 2. Architecture

```
┌──────────────────┐        ┌──────────────────────────────┐
│  SCOUT  (RPi 1)  │        │       BRAIN  (RPi 2)         │
│                  │        │                              │
│ PumpPortal feed  │        │  gates → score → intel →     │
│ RPC log watcher  │        │  risk → size → route → exec  │
│ social polling   │        │                              │
│ wallet tracking  │        │  entity graph · fusion       │
│ on-chain screen  │        │  personal edge · LLM         │
│                  │        │  exit state machine          │
│ emits Candidates │        │  HOLDS THE ONLY KEY          │
└────────┬─────────┘        └──────────┬───────────────────┘
         │                             │
         └───────────┬─────────────────┘
                     │  Redis
         ┌───────────▼────────────────┐
         │     CONSOLE  (Host PC)     │
         │  terminal · dashboard      │
         │  watchdog · killswitch     │
         │  inbound webhooks          │
         └────────────────────────────┘
```

**Why the decision path is arithmetic, not AI.** A quantized 3B model on a Pi
needs 15–40 seconds for a useful answer; the entry window is a few seconds. So
the scorer decides — microseconds, deterministic, unit-tested — and the LLM
advises off the hot path with a bounded effect. It earns its keep on the
nightly trade review, where nothing waits on it.

**Why 20 providers don't make it slow.** Tiered enrichment. The entry path
runs at `HOT` tier, which performs **zero network I/O** — it reads what
background workers already cached. Anything missing is queued, so the *next*
decision is better informed and an open position can still be exited on it.

Full detail: [ARCHITECTURE.md](docs/ARCHITECTURE.md).

---

## 3. Hardware

| | Recommended | Why |
|---|---|---|
| **RPi 1** (Scout) | Pi 5 8GB, NVMe SSD, **Ethernet** | Feed ingestion, screening |
| **RPi 2** (Brain) | Pi 5 8GB, NVMe SSD, **Ethernet** | Execution + LLM |
| **Host PC** | Anything with a browser | Terminal + Redis. A GPU is a big LLM upgrade |
| **RPC** | Helius free tier | The public endpoint **will** cost you fills |

Two mistakes people regret: **Wi-Fi** (adds 10–50ms jitter — you are already
losing the latency race) and **cheap microSD** (constant SQLite writes kill
them, silently, usually within months).

Full buying and wiring guide: [HARDWARE.md](docs/HARDWARE.md).

---

## 4. Install — all three devices

Host PC first; the Pis need its IP.

### 4.1 Host PC

```bash
git clone https://github.com/z440-prod/meme-coin-trade-bot.git
cd meme-coin-trade-bot
./scripts/install_host.sh
```

Installs dependencies and Redis, configures it for LAN access, and prints this
machine's IP. **Write it down.**

Secure Redis before going live — by default it is now reachable from your LAN
with no password, which means anyone on your network can command the Brain:

```bash
sudo nano /etc/redis/redis.conf     # requirepass <long-random-string>
sudo systemctl restart redis-server
```

Put the same value in `MCTB_REDIS_PASSWORD` on all three devices.

```bash
source .venv/bin/activate
nano .env          # MCTB_REDIS_URL → this machine's IP
mctb preflight
mctb console
```

### 4.2 RPi 1 — Scout

Flash **Raspberry Pi OS 64-bit** (Lite), hostname `mctb-scout`, SSH on.

```bash
ssh pi@mctb-scout.local
sudo apt update && sudo apt full-upgrade -y && sudo reboot
```

```bash
git clone https://github.com/z440-prod/meme-coin-trade-bot.git
cd meme-coin-trade-bot
./scripts/install_pi.sh scout
nano .env      # Redis URL + RPC URL
```

Copy `config/config.yaml` from the host so all three agree, then:

```bash
source .venv/bin/activate
mctb preflight
sudo systemctl enable --now mctb-scout
journalctl -u mctb-scout -f
```

### 4.3 RPi 2 — Brain

Same, hostname `mctb-brain`:

```bash
./scripts/install_pi.sh brain      # installs signing + keystore extras
nano .env                          # same Redis + RPC values
source .venv/bin/activate
mctb preflight
sudo systemctl enable --now mctb-brain
```

You should see `brain ready — mode: paper`.

**Do not create a wallet yet.** Paper trading needs none. That is
[section 10](#10-going-live), after weeks of results.

### 4.4 Local LLM (optional)

```bash
./scripts/setup_llm.sh install          # builds llama.cpp, 10-25 min
./scripts/setup_llm.sh model llama3.2-3b
./scripts/setup_llm.sh bench            # measure YOUR hardware
./scripts/setup_llm.sh service
```

If the host PC has a GPU, run the model there instead and point the Brain at
it — you go from ~10 tok/s to hundreds:

```bash
MCTB_LLM_BASE_URL=http://192.168.1.50:8080
```

---

## 5. Data providers

**Every key is optional.** With none, six free keyless providers cover market
data, security screening and execution:

```
DEX Screener · GoPlus · Rugcheck · Jupiter · Raydium · Meteora
```

```bash
mctb providers --probe
```

```
6 ready · 11 awaiting keys · 5 link-only
All critical capabilities are covered.
```

### The one key worth getting first

**Helius** (free tier, `helius.dev`) does three jobs at once:

1. A fast RPC endpoint — the public one is rate-limited to the point of
   breaking your fills
2. Parsed transactions
3. **Wallet funding lineage**, which powers entity resolution — the feature
   that stops five sybil wallets counting as five signals

```bash
MCTB_HELIUS_API_KEY=your-key
MCTB_RPC_URL=https://mainnet.helius-rpc.com/?api-key=your-key
```

### Everything else

| Have no public API | Paid, useful later | Expensive, only at size |
|---|---|---|
| Photon, BullX, Bubblemaps (deep links) | Birdeye, Cielo, Solscan | Nansen, Arkham, LunarCrush, Kaito, DEXTools |

GMGN's endpoints are undocumented and behind bot protection — opt-in via
`providers.gmgn_opt_in`, off by default.

**Full honest breakdown of all 28, with what each is worth:**
[PROVIDERS.md](docs/PROVIDERS.md).

> Do not pay for anything until the free stack is profitable. If it isn't, a
> paid feed will not fix it — you would be buying more of an input that is
> already not working.

---

## 6. The terminal

**http://localhost:8787/terminal** — keyboard-driven, six live panels.

```
positions │ dossier         │ providers
──────────┼─────────────────┼──────────
feed      │ entity clusters │ perf + latency
```

### Commands

| | |
|---|---|
| `<mint>` | Load a full dossier |
| `s <query>` | Search tokens |
| `w <wallet>` | Wallet dossier + cluster |
| `new` / `hot` | New launches / paid promotions |
| `deep` | Re-run current dossier at MANUAL tier |
| `probe` | Live-check every provider |
| `kill` / `resume` / `closeall` | Control |

### Keys

`/` focus · `j`/`k` navigate · `d` dossier · `x` exit position ·
`1`-`3` switch feed · `?` help

The dossier panel shows blockers in red, cluster warnings in blue, a risk bar,
wash-trade suspicion, authority status, sellability, and one-click links to all
12 external tools — including the ones with no API.

The **entity clusters** panel is the one to watch. When it shows
`C4a2f8b1 · 5 wallets · funded by 8xK2m..v within 12 min of each other`, that
is five apparent signals that are actually one actor.

---

## 7. Alerts and automation

**Out:** Telegram, Discord, and any JSON webhook (Zapier, n8n, Make).

```bash
MCTB_TELEGRAM_BOT_TOKEN=...    # @BotFather
MCTB_TELEGRAM_CHAT_ID=...      # @userinfobot
MCTB_DISCORD_WEBHOOK_URL=...
```

**In:** TradingView, Zapier, n8n can drive MCTB.

```bash
openssl rand -hex 32           # → MCTB_WEBHOOK_SECRET
```

```yaml
webhooks:
  enabled: true
  allow_trading: false   # leave false unless you are certain
```

Security is three-layered: secret in the path (constant-time compared),
HMAC-SHA256 with replay protection, and a capability allowlist. Trading
actions need a signature **and** `allow_trading`; the config refuses to start
if you enable trading without a secret. Even then, an inbound `buy` routes
through normal scoring — external systems can say *what to look at*, not
*what to buy*.

```bash
mctb webhook-test http://192.168.1.50:8787 --action signal --mint <mint>
```

Full guide incl. TradingView/n8n/Zapier recipes: [AUTOMATION.md](docs/AUTOMATION.md).

---

## 8. Speed

Twenty data sources, and none of them on the critical path.

| Technique | Effect |
|---|---|
| **Tiered enrichment** | Entry decisions read cache only — zero network I/O |
| **Stale-while-revalidate** | Serve instantly, refresh behind the caller |
| **Single-flight coalescing** | Concurrent identical requests become one |
| **HTTP/2 + warm pools** | Saves a 100–300ms TLS handshake per call |
| **Per-provider token buckets** | Never discover a rate limit the hard way |
| **Circuit breakers** | A dead provider costs microseconds, not seconds |
| **Batch endpoints** | 30 tokens priced in one request |
| **uvloop** | 2–4x faster event loop |
| **GC tuning** | Fewer latency spikes under high allocation |

```bash
pip install uvloop     # then it is automatic
```

The terminal's latency panel shows p50/p99/max per path. Watch **p99**, not
the average — a path averaging 3ms with a 900ms p99 still misses the trades
that matter, because slow cases correlate with busy moments.

---

## 9. Paper trading

Paper mode prices against real bonding curves and models latency, slippage,
fees and your own price impact.

```bash
mctb stats            # performance
mctb stats --edge     # what it has learned from YOUR trades
```

| Metric | Read as |
|---|---|
| **Profit factor** | Gross wins ÷ gross losses. Below 1.0 you are losing. |
| **Max drawdown** | Would you really have kept going? |
| Win rate | **Least** informative. 25% can be excellent here. |
| Exit reasons | Where rules actually fire. Often surprising. |

Discipline: one change at a time, 50+ trades per change, and check the funnel
(`mctb simulate --tokens 200`) before touching weights — if 90% die at one
gate, that gate *is* your strategy.

---

## 10. Going live

**Only when all of these are true:**

- [ ] 100+ closed paper trades
- [ ] Profit factor above 1.0
- [ ] `mctb stats --edge` makes sense to you
- [ ] You sat through a losing streak without wanting to rewrite everything
- [ ] You have read [SAFETY.md](docs/SAFETY.md) in full

```bash
# On the Brain, and only the Brain:
mctb keystore create      # dedicated NEW wallet, key never stored plaintext
chmod 600 .env secrets/trading.keystore
chmod 700 secrets/
mctb keystore show        # fund this address, well under 1 SOL to start
```

Two latches, deliberately:

```yaml
execution:
  mode: live
  live_trading_enabled: true
```

Start absurdly small — the first week is for discovering what behaves
differently with real money, while each lesson costs a fraction of a cent:

```yaml
risk:
  max_position_sol: 0.005
  max_concurrent_positions: 1
  daily_loss_limit_sol: 0.02
```

**Stopping:**

```bash
mctb kill          # stop new entries
touch KILL_SWITCH  # works even if Redis is down
mctb close-all     # sell everything now
```

Exits keep running regardless — abandoning open positions is not a safe state.
If the bot is unresponsive entirely, import the key into Phantom and sell
manually. **The bot is not required to access your funds.**

---

## 11. Command reference

```
mctb scout | brain | console | all     run a service
mctb token <mint> [--deep]             intelligence dossier
mctb providers [--probe]               data source status
mctb simulate --tokens 200             offline strategy test
mctb preflight                         check everything
mctb stats [--edge]                    performance and learning
mctb review                            LLM trade review now
mctb webhook-test <url>                verify automation wiring
mctb kill | resume | close-all         control
mctb keystore create | show            wallet
```

```
make install-dev   make test   make lint   make sim   make backup
```

---

## 12. Layout

```
src/mctb/
├── providers/        23 adapters behind one interface
│   ├── base.py         contract: capabilities, tiers, health
│   ├── http.py         rate limit · circuit break · cache · coalesce
│   ├── registry.py     capability routing and fan-out
│   ├── dexscreener.py  security.py  execution.py  market.py
│   ├── onchain.py      smartmoney.py  social.py  links.py
├── intel/            fusion and entity resolution
│   ├── fusion.py       cross-provider consensus + provenance
│   ├── graph.py        wallet clustering — the sybil collapse
│   ├── narrative.py    astroturf detection
│   └── dossier.py      tiered enrichment engine
├── brain/            RPi 2
│   ├── scoring.py      deterministic scorer
│   ├── intel_gate.py   applying fused intel, bounded
│   ├── router.py       venue selection + cost estimation
│   ├── edge.py         personal learning layer
│   ├── exits.py        exit state machine (pure, fully tested)
│   └── executor.py     paper + live signing
├── scout/            RPi 1 — feeds, screening, wallet tracking
├── console/          host PC — terminal, dashboard, webhooks
├── automation/       inbound + outbound
├── solana/           curve maths, RPC, contract checks
└── runtime.py        uvloop, GC, latency tracking

docs/   PROVIDERS · AUTOMATION · ARCHITECTURE · HARDWARE
        STRATEGY · SAFETY · TROUBLESHOOTING · REALITY_CHECK
tests/  280 tests
```

---

## Licence

MIT. See [LICENSE](LICENSE).

Provided as-is, no warranty. Trading cryptocurrency carries substantial risk of
total loss. You are responsible for your own funds, your own configuration, and
your own compliance with the law where you live.
