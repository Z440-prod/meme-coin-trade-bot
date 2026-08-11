# Reality check — where the original specification differs from what was built

The design document this system came from is a good high-level brief, but
several of its specifics are wrong, outdated, or would actively lose money if
implemented literally. This document lists every deliberate deviation and why.

Read this before asking "why doesn't it do X like the doc said".

---

## 1. pump.fun has no official public API

**The spec says:** "Utilize the Pump.fun API's WebSocket streaming… The API
provides endpoints for real-time data access without requiring an API key."

**Reality:** pump.fun does not publish a public trading or streaming API. The
citations in the source document point at third-party aggregators and scrapers,
not at anything official.

**What was built:** two independent data paths.

- **PumpPortal** (`scout/pumpportal.py`) — the de-facto third-party feed that
  most working bots use. Its new-token and migration streams are free; its
  per-token and per-account trade streams are metered and billed in SOL, so
  the Scout caps how many it subscribes to.
- **Direct Solana RPC** (`scout/rpc_watcher.py`) — subscribes to the pump.fun
  program's logs and decodes bonding curve accounts itself. Slower to notice a
  launch, but it depends on nothing but a Solana node.

Both run at once. The RPC path is authoritative for *price*, because a
third-party feed that lags during volatility is exactly the failure that
matters, and prices are what your stop loss keys off.

---

## 2. X (Twitter) API v2 has no WebSocket

**The spec says:** "The X Developer Platform offers WebSocket streaming for
posts, users, and trends."

**Reality:** it does not. X offers a chunked-HTTP *filtered stream* and a
polled *recent search*. Neither is a WebSocket, and which of them your account
can reach depends entirely on your access tier — the cheap tiers have
historically not included streaming at all.

**What was built:** `scout/social.py` polls recent search, which works across
the widest range of plans, respects rate-limit headers, and disables itself
cleanly on a 401/403 rather than hammering a plan that cannot serve it.

Social is also capped at a small share of the score and can never trigger a
trade on its own. It is the most easily gamed input in the system: paid
"calls", bot swarms, and coordinated posting all look exactly like signal.
**Do not pay for an X plan until the rest of the system is profitable
without it.**

---

## 3. The Kalshi Order Book API is irrelevant

**The spec says:** "The interface will integrate with the Kalshi Order Book
API or similar tools for real-time market data and order book visualization."

**Reality:** Kalshi is a US-regulated event-prediction market. It has no
connection to Solana, to pump.fun, or to memecoins. This citation appears to
be an error in the source document.

**What was built:** nothing. It was dropped. Market data comes from the
bonding curve, which is the actual venue these tokens trade on. Note also that
a bonding curve has no order book at all — it is a deterministic price
function — so "order book visualization" does not apply to this market
structure.

---

## 4. An LLM cannot be in the trade decision path

This is the most important deviation, and the one that would have cost the
most money.

**The spec says:** "RPi 2 runs a local LLM that analyzes the aggregated data,
identifies trading opportunities, and executes trades."

**Reality:** on a Raspberry Pi 5, a 3B-parameter model in 4-bit quantization
generates roughly 5–15 tokens per second. A 200-token answer therefore takes
15–40 seconds. The window in which a pump.fun launch is still worth entering
is a few seconds. An LLM in the decision path does not make the bot smarter;
it makes it arrive after the trade is gone.

There is a second problem beyond latency: a language model is non-deterministic
and non-reproducible. When a trade loses money, you need to be able to ask
*exactly why it was taken* and get an answer you can act on. "The model felt
good about it" is not something you can debug or improve.

**What was built:** a split.

- **`brain/scoring.py` decides.** Plain weighted arithmetic over the feature
  vector. Runs in microseconds, is fully deterministic, produces an itemised
  breakdown of every component, and is directly unit-tested.
- **`brain/llm.py` advises.** It runs off the hot path, is consulted only for
  candidates that already scored above threshold, has a hard timeout, and is
  bounded in how far it may move a score (`llm.max_bonus` / `max_penalty`). It
  can veto hard; it cannot manufacture conviction.

Where the LLM genuinely earns its keep is the **nightly review**: reading a few
hundred trades and noticing that every loss last week entered above 40 SOL of
curve depth is a real, useful piece of analysis, and at 4am nothing is waiting
on it.

If you want the LLM to be useful on live candidates too, run it on the host
PC's GPU and point `MCTB_LLM_BASE_URL` at that machine.

---

## 5. Stop-loss enforcement does not belong on the host PC

**The spec says:** "The host PC will calculate and enforce high-risk stop
losses pre-defined by the user."

**Reality:** the host PC is a desktop. It sleeps, reboots, installs updates
and gets closed for the night. If it owns your stop loss, then shutting your
laptop leaves every open position completely unprotected.

**What was built:** two layers with different jobs.

- **Primary stop: the Brain** (`brain/exits.py`, `brain/portfolio.py`). Lives
  next to the execution path, on a device meant to run unattended, in a loop
  that shares nothing with scoring or the dashboard.
- **Supervisory backstop: the Console** (`console/supervisor.py`). Watches for
  the failure the Brain cannot report itself — no heartbeat, or a position that
  blew through a deliberately *wider* threshold, meaning the Brain's own stop
  should already have fired and did not. On either, it orders an exit over the
  bus.

The thresholds are intentionally different. If both layers used the same
number they would race and you would get duplicate sell orders on every
ordinary stop-out.

---

## 6. Two Raspberry Pis is a reasonable split, for a different reason

The spec's rationale is workload distribution. That is not really the benefit —
a single Pi 5 could handle both roles' CPU load comfortably.

The actual benefit is **fault isolation and blast radius**. The Brain is the
only machine that ever holds a private key. It runs a small amount of code,
has one job, and can be locked down. The Scout talks to third-party WebSockets,
parses untrusted JSON from the internet, and is the component most likely to
crash, hang or be attacked — and it holds no keys and can trade nothing.

You can run everything on one machine (`mctb all`) and it works. You give up
that isolation, which matters more the more money is involved.

---

## 7. Things the spec did not mention that turn out to matter more

The source document spends its detail budget on architecture. In practice,
these are what determine whether the thing makes or loses money:

| What | Why it matters | Where |
|---|---|---|
| **Freeze authority checks** | The classic honeypot. Your tokens get frozen and you can never sell at any price. Invisible on a chart. | `solana/checks.py` |
| **Mint authority checks** | The deployer can print unlimited supply on top of your position. | `solana/checks.py` |
| **Price impact from your own order** | On a thin curve this dwarfs fees. Spot price × quantity is a lie. | `solana/curve.py` |
| **Bundled-launch detection** | Buys in the launch slot mean insiders are already positioned below your cost basis. You are their exit. | `scout/observer.py` |
| **Exit retry with escalating fees** | Failing to *get out* is the expensive failure. Worth overpaying for. | `brain/executor.py` |
| **Position restore after a crash** | A restarted Brain that forgets its positions leaves them with no stop loss at all. | `brain/portfolio.py` |
| **RPC latency and rate limits** | The public endpoint will rate-limit you constantly, and every 429 is a missed fill or a delayed exit. | `preflight.py` |
| **Clock drift** | A Pi has no battery-backed clock. Drift breaks transaction building and every age-based rule, in ways that look like bad luck. | `preflight.py` |

---

## 8. The honest part

The source document ends: "The system is designed to maximize profitability
while managing risk."

No amount of good engineering makes memecoin sniping reliably profitable. You
are competing against people running co-located infrastructure with direct
validator relationships and sub-10ms latency, on a Raspberry Pi behind a home
router. On the trades where speed decides the outcome, you lose by default.

What this system can realistically do is trade a *slower* edge competently:
copy wallets with a demonstrated record, screen out the tokens that were never
sellable, size positions so a losing streak does not end you, exit
mechanically instead of emotionally, and learn from your own results rather
than from generic advice.

That is a real strategy. It is not a fast one, and it is not a guaranteed one.
See [SAFETY.md](SAFETY.md) before you fund a wallet.
