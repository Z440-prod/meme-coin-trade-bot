# Troubleshooting

Start here:

```bash
mctb preflight          # checks every dependency and endpoint
journalctl -u mctb-brain -f   # or mctb-scout, mctb-llm
```

---

## Nothing connects

### `Could not reach Redis at ...`

The bus is down or unreachable. Work through it in order:

```bash
# On the host PC (where Redis runs):
redis-cli ping                      # expect: PONG
sudo systemctl status redis-server

# From a Pi:
redis-cli -h 192.168.1.50 ping      # use your host's actual IP
```

If local works but remote does not, Redis is only listening on localhost:

```bash
sudo nano /etc/redis/redis.conf
#   bind 0.0.0.0
#   protected-mode no        (or set requirepass instead — better)
sudo systemctl restart redis-server
```

Then confirm `MCTB_REDIS_URL` on both Pis points at the host's real LAN IP,
not `127.0.0.1`, and that a firewall is not in the way:

```bash
sudo ufw allow from 192.168.1.0/24 to any port 6379
```

### The services start but the dashboard shows both as "never seen"

They are on different Redis instances, or different databases. Check the
**whole** URL matches on all three devices — including the trailing `/0`.

---

## No candidates are appearing

Check the Scout's heartbeat detail on the dashboard, or:

```bash
journalctl -u mctb-scout -f | grep -E "candidate|feed"
```

**`feed_connected: false`** — PumpPortal is unreachable. Verify:

```bash
curl -I https://pumpportal.fun
```

The Scout reconnects automatically with backoff. If it never connects, the
service may be down or your IP may be temporarily banned for having opened too
many connections (the feed permits exactly one; MCTB multiplexes over one, but
running several Scout instances against the same IP will trip this). Bans
expire hourly.

**`tokens_seen` climbing but `candidates` stuck at 0** — your gates are
rejecting everything. This is usually correct behaviour, especially on
`conservative`. Confirm with:

```bash
mctb simulate --tokens 200
```

The rejection table shows which gate dominates. If it is
`no tracked wallet has bought this`, either add `seed_wallets` in your config
or set `require_smart_wallet: false` and let auto-discovery build the list
over a few days.

**Everything at 0** — check `min_prelim_score_for_safety_check`. Set too high,
nothing ever reaches the on-chain screen.

---

## Buys keep failing

### `no price available`

The Brain could not read the bonding curve. Either the mint is not a pump.fun
token, it has already migrated to a DEX, or your RPC failed. Check the RPC:

```bash
mctb preflight | grep -i rpc
```

### `insufficient paper bankroll`

Paper mode ran out of simulated SOL. Raise
`execution.paper_starting_bankroll_sol` or restart to reset.

### Live buys fail with `submit failed` or time out

Almost always the RPC endpoint. The public one cannot sustain this workload.
Get a dedicated endpoint (see [HARDWARE.md](HARDWARE.md)).

If you are on a good endpoint and still failing, raise
`execution.priority_fee_sol`. A fee that was fine last week may not be enough
during congestion.

**A timed-out send does not mean the trade failed.** It may well have landed.
MCTB deliberately does not retry sends, because a duplicate buy is worse than
a missed one. Check the wallet on Solscan before assuming anything, and let
the Brain reconcile on restart.

---

## Exits are failing

This is the failure that costs real money — treat it as urgent.

```bash
journalctl -u mctb-brain | grep -i "COULD NOT EXIT"
```

The executor already retries with escalating priority fees. If it exhausted
them:

1. **Sell manually right now**, on pump.fun or any Solana DEX aggregator. Do
   not wait for the bot.
2. Then work out why.

Common causes:

- **Freeze authority was never revoked** and your account has been frozen. You
  cannot sell, at any price, ever. This is why that gate exists and why it must
  stay on.
- **No liquidity left** — the curve has been drained. The position is worth
  approximately nothing; the bot writes it off.
- **RPC failure during the exit window.** Configure `MCTB_RPC_URL_FALLBACK`.
- **Priority fee too low during congestion.** Raise
  `execution.exit_priority_fee_sol` — exits deserve to overpay.

---

## The Brain went silent

The Console alerts on this within `console.brain_heartbeat_timeout_s` because
open positions are unmanaged while it lasts.

```bash
sudo systemctl status mctb-brain
journalctl -u mctb-brain -n 100 --no-pager
```

**If positions are open and you cannot restart it quickly, exit manually.**
Do not wait.

Frequent causes on a Pi:

- **Out of memory.** Usually the LLM. `free -h`, then either use a smaller
  model or move it to the host PC.
- **SD card corruption.** `dmesg | grep -i error`. This is the most common
  hardware failure mode; see the storage warning in HARDWARE.md.
- **Undervoltage.** `vcgencmd get_throttled` — anything other than
  `throttled=0x0` means your power supply is inadequate.
- **Disk full.** `df -h`. Logs rotate, but a runaway loop can still fill a
  small card.

Open positions are restored automatically on restart, with their exit rules
active again.

---

## The LLM

### `LLM server not reachable`

Not fatal — the bot trades fine without it. To fix:

```bash
sudo systemctl status mctb-llm
./scripts/setup_llm.sh serve      # run in the foreground to see errors
```

### It is reachable but never seems to be consulted

Check `llm.min_score_to_consult`. If it is above your `entry_threshold`, very
few candidates ever qualify. Also check for timeouts:

```bash
journalctl -u mctb-brain | grep -i "llm timed out"
```

If it is timing out constantly, the model is too slow for the window. Measure
what you actually have:

```bash
./scripts/setup_llm.sh bench
```

Then either use a smaller model, raise `llm.timeout_s` (accepting the entry
delay), or move inference to the host PC. Leaving it timing out is also a
legitimate choice — the nightly review still works, and that is where the LLM
is most useful anyway.

### It returns unparseable output

Small models struggle with strict JSON. MCTB already requests
`response_format: json_object` and falls back to extracting the first balanced
brace group. If `parse_failures` in the heartbeat is high, use a larger model —
1.5B models are noticeably worse at this than 3B.

---

## Redis is filling up

```bash
redis-cli info memory
redis-cli xlen mctb.journal
```

Lower `bus.journal_maxlen`. It is capped and trimmed approximately, so it can
run somewhat over the limit — that is normal.

---

## The database

```bash
sqlite3 data/mctb.db "PRAGMA integrity_check;"
make backup
```

If it is corrupt, restore from `backups/`. If you have no backup, delete it and
start over — you lose all learned edge and trade history, which is a real loss.
**Back it up regularly.** `make backup` takes a second.

---

## Getting your money out, right now

In order of speed:

```bash
mctb close-all            # sells everything at market
```

Or the **Close all** button on the dashboard, which works from a phone.

If the bot is not responding at all, do it by hand: open your wallet in
Phantom or Solflare and sell the positions directly on pump.fun. **The bot is
not required to access your funds** — the keystore holds a standard Solana
private key, and you can import it into any wallet.

To stop new entries but leave exits running:

```bash
mctb kill
# or
touch KILL_SWITCH
```

---

## Still stuck

Collect this before asking anyone for help:

```bash
mctb preflight                          # redacts secrets
mctb config-show                        # masks secrets
journalctl -u mctb-brain -n 200 --no-pager
mctb stats
```

Logs are also written as JSON lines to `logs/`, with a redaction filter for
anything that looks like a key. Check before pasting anywhere regardless —
never share `.env`, `config/config.yaml`, or `secrets/`.
