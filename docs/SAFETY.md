# Safety

Read this before you put real money anywhere near this system.

---

## The honest expectation

**Most memecoin snipe bots lose money.** Not because they are badly written,
but because of what they are competing against and what they are trading.

You are up against people running co-located servers with direct validator
relationships, custom Rust clients, Jito bundle access and sub-10-millisecond
latency. You are on a Raspberry Pi behind a home router. On any trade where
speed decides the outcome, you lose by default — and a large share of
profitable memecoin trades are decided by speed.

The market itself is adversarial in ways that are not obvious:

- A large fraction of launches are engineered to extract money from exactly
  the kind of automation you are running. Bundled launches, fake volume, and
  wallets that build a good on-chain record specifically to attract
  copy-traders they can then dump on are all normal.
- "Volume" is often the same handful of wallets trading with themselves.
- A token can be technically clean — authorities revoked, distribution fine —
  and still go to zero in ninety seconds because that was always the plan.

**Assume you will lose whatever you fund this wallet with.** That is not a
disclaimer, it is the base case. Fund it accordingly.

---

## The rules that actually protect you

### 1. A dedicated wallet, always

Create a **brand new wallet** for this bot. Never use one that holds anything
else — no NFTs, no other tokens, no savings.

The bot's key sits decrypted in memory on a device in your house that talks to
the internet. Treat that machine as compromisable, and make the worst case
"I lost the trading float" rather than "I lost everything I own".

Fund it with an amount whose total loss would be genuinely annoying but not
harmful. For most people starting out that is well under 1 SOL.

### 2. Paper trade first, for longer than you want to

Two safety latches must **both** be set before a single lamport moves:

```yaml
execution:
  mode: live                 # latch 1
  live_trading_enabled: true # latch 2
```

Two, deliberately, so no single careless edit can arm your wallet.

Before you set either:

- **At least 100 closed paper trades.** Fewer than that and your win rate is
  indistinguishable from noise.
- **Positive profit factor over those trades**, after the simulated fees and
  slippage. `mctb stats` shows it.
- **You have read `mctb stats --edge`** and the learned table makes sense to
  you rather than looking random.
- **You have survived a losing streak on paper** without wanting to change
  every parameter. That reaction is the thing that actually loses money.

Paper mode prices against the real chain and models latency, slippage, fees
and your own price impact. It is not free money, but it is free tuition.

### 3. Start live smaller than feels worth it

Your first live configuration should look absurd:

```yaml
risk:
  max_position_sol: 0.005
  max_concurrent_positions: 1
  max_positions_per_hour: 3
  daily_loss_limit_sol: 0.02
```

The purpose of the first live week is not profit. It is discovering everything
that behaves differently with real money — real fills, real latency, real
failed transactions, real RPC rate limits — while the cost of each lesson is a
fraction of a cent.

Scale up only after live results match paper results. If they diverge sharply,
the difference is real and paper was flattering you.

### 4. Never disable the authority checks

```yaml
gates:
  require_mint_authority_revoked: true   # keep true
  require_freeze_authority_revoked: true # keep true
```

These are not risk/reward trade-offs. They are checks for whether the token can
be sold at all:

- **Mint authority live** → the deployer can print unlimited new supply and
  dilute your position to nothing whenever they choose.
- **Freeze authority live** → the deployer can freeze your token account. You
  keep the tokens permanently and can never sell them at any price. This is the
  classic honeypot, and it is completely invisible from a price chart.

Even the `degen` preset leaves both on, and there is a test asserting that no
shipped preset can turn them off.

### 5. Know your three kill switches

Any of these stops all new entries immediately:

```bash
mctb kill                    # sets the bus flag and drops the file
touch KILL_SWITCH            # file alone works even if Redis is down
```
…or the red **KILL** button on the dashboard, which works from your phone.

To also exit everything you currently hold:

```bash
mctb close-all
```

None of the kill switches stop *exits*. Abandoning open positions is not a safe
state, so exit rules keep running regardless.

---

## Protecting the key

The keystore is encrypted with a scrypt-derived key (`mctb keystore create`).
Your private key is never written to disk in plaintext and never sent over the
network — the Brain builds and signs transactions locally and submits only the
signed bytes.

What you still need to do:

```bash
chmod 600 .env secrets/trading.keystore
chmod 700 secrets/
```

**On the passphrase.** Leaving `MCTB_KEYSTORE_PASSPHRASE` empty is the secure
option: you type it at startup and nothing on disk can unlock the wallet
alone. Setting it in `.env` lets systemd restart unattended, at the cost of
anyone with read access to that file having your wallet. Pick deliberately.

**Never** commit `.env`, `config/config.yaml`, or anything in `secrets/`. They
are in `.gitignore`; do not force-add them.

If you think the Pi has been compromised: move the funds out first, then
investigate. A new wallet costs nothing.

---

## Securing the network

The installer configures Redis for LAN access, which by default means
**unauthenticated**. Anyone on your network can then read your positions and
issue control commands, including telling the Brain to sell.

Set a password:

```bash
# /etc/redis/redis.conf
requirepass <a-long-random-string>
```

then put the same value in `MCTB_REDIS_PASSWORD` on all three devices and
restart Redis.

Do not port-forward Redis or the dashboard to the internet. If you want the
dashboard remotely, use Tailscale, WireGuard, or an SSH tunnel — never a port
forward. The dashboard has no authentication because it is designed to sit on
a trusted LAN.

---

## Risk limits, and why each exists

| Setting | What it prevents |
|---|---|
| `max_position_sol` | One trade being able to hurt |
| `position_pct_of_bankroll` | Size shrinking automatically as you lose — this is what turns a drawdown into a survivable one |
| `min_sol_reserve` | A wallet with no SOL left to pay the fee to *sell* its own tokens |
| `max_concurrent_positions` | Every position going wrong in the same market-wide dump |
| `daily_loss_limit_sol` | A bad day becoming a bad month |
| `consecutive_loss_limit` | Trading through a regime your strategy does not work in |
| `same_mint_cooldown_s` | Revenge-buying the coin that just stopped you out |
| `max_sol_deployed_per_hour` | A feed glitch or logic bug draining the wallet quickly |

The default `stop_loss_pct` is a *target*, not a guarantee. Memecoins gap.
A token can go from -10% to -80% between two price reads with no opportunity
to exit in between. Size on the assumption that any position can go to zero,
because sometimes one will.

---

## Legal and tax

Not legal or tax advice. Get advice appropriate to where you live.

- **Automated trading is legal** in most jurisdictions, but crypto regulation
  varies enormously and changes fast.
- **Every trade is probably a taxable event.** A bot doing dozens of trades a
  day generates a large reporting burden. The SQLite database is your record —
  keep it, back it up, and export it at year end.
- **Do not use money you need.** Rent, tuition, borrowed money, and anything
  you would have to explain losing all belong nowhere near this.

Some things are illegal essentially everywhere and this system will not help
you do them: wash trading to fake volume, coordinated pump-and-dump schemes,
trading on the basis of stolen private information, or operating a fund on
someone else's behalf without the relevant licences. Trading your own money on
public information is fine; manufacturing the information is not.

---

## When to stop

Set these lines before you start, while you are calm:

- A total-loss number that ends the experiment. Write it down.
- A time limit — "if this is not profitable after eight weeks, I stop."
- A behavioural line: if you find yourself widening the stop loss after a
  loss, raising position size to "make it back", or checking the dashboard
  compulsively, stop. Those are the reactions that turn a bounded experiment
  into an unbounded one.

The system enforces its own limits automatically. It cannot enforce yours.
