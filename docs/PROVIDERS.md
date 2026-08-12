# Data providers — what each one actually gives you

All 28 requested integrations, with an honest assessment of each: whether it
has a usable API, what it costs, what MCTB does with it, and whether it is
worth having.

**Everything here is optional.** With zero API keys the system runs on six
free keyless providers that cover market data, security screening and
execution routing. That is a complete working system. Add keys deliberately.

Verify what you have at any time:

```bash
mctb providers          # status of all 23 adapters
mctb providers --probe  # live-check every endpoint
```

---

## The summary

| Tier | Providers | Cost | Verdict |
|---|---|---|---|
| **Free, keyless, verified working** | DEX Screener, GoPlus, Rugcheck, Jupiter, Raydium, Meteora | £0 | **Start here. This is most of the value.** |
| **Free tier, key required** | Helius, Dune | £0 | Helius is the single biggest upgrade available |
| **Paid, genuinely useful** | Birdeye, Cielo, Solscan | ~£30-100/mo | Only once profitable |
| **Paid, expensive** | Nansen, Arkham, LunarCrush, Kaito, DEXTools, Token Sniffer | £100-1500/mo | Only at real size |
| **No public API** | Photon, BullX, Bubblemaps | — | Deep links only |
| **Unofficial endpoints** | GMGN | — | Opt-in, fragile |

---

## Free and keyless — the backbone

These six were verified by direct API call while building this. They need no
registration, no key, and no payment.

### DEX Screener — `dexscreener`
`api.dexscreener.com` · free · no key

The best general market data available at any price. Price, liquidity,
volume, buy/sell counts across every timeframe, plus a **batch endpoint that
takes 30 mints in one request** — which is what makes pricing a whole
watchlist cheap.

MCTB uses it for: primary market data, new-launch discovery, token search
(the terminal's `s` command), and the boosted-token feed.

> One caution: the "boosts" feed is **paid promotion**, not quality. A launch
> with real organic interest does not need to buy visibility. MCTB scores a
> boost as mild evidence *against* a token.

### GoPlus Security — `goplus`
`api.gopluslabs.io` · free · no key

Full Solana token-security object: mint/freeze/close authorities, metadata
mutability, transfer hooks, holder breakdown, DEX liquidity. Solana-native
rather than an EVM tool with Solana bolted on.

This and Rugcheck are the highest value-per-line integrations in the system.
Most money lost to memecoin bots is lost to tokens that were never sellable.

### Rugcheck — `rugcheck`
`api.rugcheck.xyz` · free tier, no key

Numeric risk score plus itemised findings, LP lock percentage, insider
analysis, and Token-2022 extension detection (transfer fees, permanent
delegates) that can silently break selling.

Kept separate from GoPlus deliberately: **disagreement between them is itself
a signal**, and the fusion layer records who said what.

### Jupiter — `jupiter`
`lite-api.jup.ag` · free · optional paid key

The Solana swap aggregator, and the most important execution integration here.

Two things it does that pump.fun-only routing cannot:

1. **Post-graduation coverage.** Once a bonding curve completes, a curve-only
   bot cannot trade the token at all — including selling a position it already
   holds. Jupiter routes to wherever liquidity moved.
2. **A free honeypot test.** If Jupiter cannot build a *sell* route, you have
   learned something critical before spending money. MCTB checks this on every
   candidate, and it catches tokens that pass every contract scan.

It also powers the **round-trip cost estimate**: fees plus price impact in both
directions, before you commit. On a thin token that routinely exceeds 20%,
meaning the trade needs a 20% move just to break even.

> The old `quote-api.jup.ag` host is dead. `lite-api.jup.ag` is current. A paid
> key raises rate limits roughly 7x, which only matters at high frequency.

### Raydium — `raydium` · Meteora — `meteora`
`api-v3.raydium.io` · `dlmm.datapi.meteora.ag` · free · no key

Pool metadata and TVL for post-graduation tokens. Secondary to Jupiter, which
already sees their liquidity — useful mainly for venue-specific detail.

> Meteora moved hosts: `dlmm-api.meteora.ag` now 404s, `dlmm.datapi.meteora.ag`
> is current.

---

## Free tier, key required

### Helius — `helius` ⭐ **the biggest single upgrade**
`helius.dev` · generous free tier

One key does three jobs:

1. **A fast RPC endpoint.** The public Solana endpoint is rate-limited to the
   point of being unusable — you will see constant 429s and it will look like
   the bot is broken when it is only throttled. Set `MCTB_RPC_URL` to your
   Helius URL as well as `MCTB_HELIUS_API_KEY`.
2. **Parsed transactions.** Raw Solana transactions are instruction soup;
   reconstructing "this wallet swapped X for Y" yourself is a lot of work.
3. **Wallet funding lineage** — which is what powers entity resolution, the
   feature that stops five sybil wallets counting as five independent signals.

If you get exactly one key, get this one.

### Dune — `dune`
`api.dune.com` · free tier with limited executions

Custom SQL over indexed chain data. **A batch tool, not a real-time one** —
queries take seconds to minutes. MCTB runs it on a slow schedule at `MANUAL`
tier, which structurally prevents it from ever touching a trading decision.

You supply your own saved query IDs; the useful ones depend on what you want
to research.

---

## Paid — worth it once you are profitable

### Birdeye — `birdeye`
`public-api.birdeye.so` · billed per compute unit

Adds what DEX Screener lacks: OHLCV candles, top-holder lists, and **unique
trader counts** — the last of which is genuinely valuable because unique
traders are much harder to fake than raw volume.

Billing is per compute unit, not per request, so one expensive endpoint can
burn a plan faster than many cheap ones. MCTB caches it hard.

### Cielo — `cielo`
`feed-api.cielo.finance` · has a free tier

The most practical smart-money service for this use case: Solana-first,
real-time wallet feeds, approachable pricing. Wallet P&L and activity.

### Solscan Pro — `solscan`
`pro-api.solscan.io` · paid

Holder lists and account analytics. Overlaps with Helius; take one or the
other unless you have a specific need.

---

## Paid and expensive — only at real size

### Nansen — `nansen` · Arkham — `arkham`

Best-in-class wallet labelling and entity attribution, priced for
institutions.

> **A caution that applies to this whole category, and that no vendor will
> tell you:** a public "smart money" label is a crowded trade. If a service
> has labelled a wallet and sold that label to thousands of subscribers,
> everyone copying it arrives at the same time and the edge is diluted or
> inverted. There are wallets whose entire business model is building a clean
> public record to attract copy-traders they can then dump on.
>
> MCTB treats these as *inputs*, never verdicts. Your own wallet reputation —
> built from realised P&L on trades you actually copied — always outranks a
> third-party label.

Where they are unambiguously useful is **negative labelling**: knowing an
address is an exchange, bridge or market maker is valuable and not crowded,
because nobody is front-running "this is Binance".

Arkham's off-chain attribution genuinely complements MCTB's own clustering,
which infers entities from on-chain funding behaviour.

### LunarCrush — `lunarcrush` · Kaito — `kaito`

Social volume, sentiment, and attention/mindshare scores.

Kaito has one specific high-value use: **grading the accounts that shill a
token.** Ten mentions from accounts with no attention history is noise; two
from accounts with real sustained mindshare is signal. MCTB uses it exactly
that way.

Social is the easiest thing in this market to fake. See "Astroturf detection"
below for what MCTB does about that.

### DEXTools — `dextools` · Token Sniffer — `tokensniffer`

DEXTools' differentiator is its proprietary audit score; note the plan tier is
part of the URL path (`providers.dextools_plan` in config).

Token Sniffer's Solana coverage is thin — it is EVM-focused. Weighted low in
fusion. Skip unless you also trade EVM chains.

---

## No public API — deep links only

### Photon · BullX

**These are trading frontends, not data providers.** They publish no API. The
only way to "integrate" them would be to drive a headless browser against a
Cloudflare-protected UI, which breaks whenever they ship a change, risks an IP
ban, and plausibly violates their terms.

**What you lose: very little.** Their underlying data is DEX Screener, Birdeye
and chain state, all of which MCTB reads directly. Their value is UI and
one-click execution — and for execution MCTB routes through Jupiter, which
sees the same liquidity.

So MCTB exposes them as **deep links**: one keystroke from the terminal to
their view of a token. That never breaks.

### Bubblemaps

Its public data endpoint returned HTTP 500 during testing, and documented
programmatic access is enterprise-tier.

**You are not losing much here either.** Bubblemaps visualises wallet
clustering — and MCTB computes clustering from funding trails directly, which
is the same insight, locally, with no rate limit. The link is kept because the
visual is a good human sanity check before a large manual buy.

### GMGN — `gmgn` (opt-in, off by default)

GMGN has undocumented internal endpoints that circulate in bot communities.
They are not a published API: no versioning, no stability guarantee,
aggressive bot protection, and they change without notice.

Enable with `providers.gmgn_opt_in: true` only if you accept that it may break
or get you rate-limited at any time. MCTB weights it low regardless, and the
fusion layer never lets a single unverified provider decide anything.

---

## Alerts and automation

| Service | Direction | Setup |
|---|---|---|
| **Telegram** | out | Bot token from @BotFather, chat id from @userinfobot |
| **Discord** | out | Server Settings → Integrations → Webhooks |
| **Zapier** | both | Catch Hook URL out; POST to `/webhook/<secret>` in |
| **n8n** | both | Webhook node both ways |
| **TradingView** | in | Alert → Webhook URL → `/webhook/<secret>` |

Inbound webhooks are covered in [AUTOMATION.md](AUTOMATION.md). The short
version: they are HMAC-signed, replay-protected, and **cannot open positions
unless you explicitly enable `webhooks.allow_trading`**, which the config
refuses to accept without a secret set.

---

## How fusion uses all of this

Providers return vendor-shaped data. `intel/fusion.py` turns several partial
answers into one, under three rules:

**Blockers are OR, quality is consensus.** If any credible source says the
freeze authority is live, that is a blocker — one source saying "unsafe"
outweighs three saying "fine", because the cost of being wrong is asymmetric.
Graded quality signals are averaged with a median, so one wrong provider
cannot drag the answer.

**Disagreement is recorded, not resolved silently.** When GoPlus and Rugcheck
disagree the field is marked contested and confidence drops. A contested
"safe" is not the same as an uncontested one.

**Absence is never safety.** A provider that failed to answer contributes
nothing and never counts as a clean bill of health.

Every dossier carries provenance — which providers contributed, which
disagreed, which were missing — so months later you can answer "did we know?"

### Cross-provider checks you get for free

These are computed from data the free providers already return, and most
retail bots never compute them:

- **Wash-trade suspicion** — huge volume against thin liquidity with few
  unique wallets means the same coins cycling between the same hands.
- **Round-trip cost** — what entering and exiting would actually cost, before
  you commit.
- **Sell-route existence** — a honeypot test that contract scans cannot do.
- **Price-source divergence** — when two independent sources disagree on price
  by more than 15%, one is stale, and stale prices blow through stop losses
  without firing them.
- **Astroturf detection** — social attention is graded on *shape*, not level:
  posting-interval regularity, near-duplicate text, account age, follower
  uniformity, single-author domination. A high mention count with a high
  astroturf score scores **worse** than no mentions at all.

---

## Recommended progression

**Week 1 — free only.** Six keyless providers. Paper mode. Learn the terminal,
watch what gets blocked and why.

**Week 2 — add Helius (free).** Set it as your RPC too. This unlocks entity
resolution, which is the feature that most changes what the bot does.

**Only once paper results are consistently profitable:** add Birdeye or Cielo.

**Only at size where the fee is a rounding error:** consider Nansen or Arkham.

Do not pay for anything until the free stack is profitable. If it is not, a
paid data feed will not fix it — you would be paying for more of the input
that is already not working.
