# Hardware and network setup

## What to buy

### Raspberry Pi ×2

| Part | Recommended | Notes |
|---|---|---|
| Board | **Pi 5, 8GB** ×2 | The Brain needs the RAM for the LLM. A 4GB Pi 5 works if you run the LLM on the host PC instead. |
| Storage | **NVMe SSD + M.2 HAT**, or an A2-rated microSD (64GB+) | See the warning below. |
| Power | **Official 27W USB-C PSU** | Non-official supplies cause brownouts under load that look like random crashes. |
| Cooling | **Active cooler** | A throttled Pi adds latency exactly when the market is busiest. |
| Network | **Ethernet** | Not optional. See below. |

**On storage.** A cheap microSD card is the single most common cause of a Pi
trading bot mysteriously dying. This workload writes constantly — logs, the
SQLite database, WAL files — and consumer SD cards wear out and start
corrupting data silently, often within months. An NVMe SSD on an M.2 HAT costs
a little more and removes an entire category of failure. If you must use SD,
buy an A2-rated card from a brand you recognise and back up the database.

**On networking.** Use Ethernet on both Pis. Wi-Fi adds 10–50ms of jitter and
drops packets under load. In a system where your competition measures latency
in single-digit milliseconds, throwing away 30ms to save running a cable is
not a reasonable trade.

### Host PC

Anything that runs a modern browser. Windows, macOS or Linux. It runs the
dashboard and Redis, neither of which is demanding.

If it has a discrete GPU, consider running the LLM there instead of on the
Brain — you go from ~10 tokens/sec to hundreds, which is the difference
between the LLM being useful only for the nightly review and being useful on
live candidates too.

### Cost

Roughly £250–400 / $300–500 for two Pi 5 8GB kits with NVMe storage, assuming
you already have a PC and a router.

**Spend that money on paper trading first.** The hardware is worthless if the
strategy does not work, and you can validate the strategy on any single
machine — including the PC you already own — before buying anything.

---

## Wiring it up

```
                    ┌──────────────┐
                    │    Router    │
                    └──┬───┬────┬──┘
              ethernet │   │    │ ethernet
                  ┌────┘   │    └────┐
                  │        │         │
            ┌─────▼───┐ ┌──▼──────┐ ┌▼─────────┐
            │  RPi 1  │ │ Host PC │ │  RPi 2   │
            │  Scout  │ │ Console │ │  Brain   │
            │         │ │ + Redis │ │ + LLM    │
            │ no keys │ │         │ │ HAS KEY  │
            └─────────┘ └─────────┘ └──────────┘
```

All three on the same LAN. Redis lives on the host PC by default.

### Static addresses

Give all three devices fixed IPs, or the bus breaks the first time your router
reshuffles its DHCP leases. Easiest route is a DHCP reservation in your
router's admin page — reserve by MAC address.

Find each device's MAC with `ip link show eth0`.

Suggested layout:

| Device | Address |
|---|---|
| Host PC (Console + Redis) | `192.168.1.50` |
| RPi 1 (Scout) | `192.168.1.51` |
| RPi 2 (Brain) | `192.168.1.52` |

Then on **both Pis**, in `.env`:

```bash
MCTB_REDIS_URL=redis://192.168.1.50:6379/0
```

---

## Preparing each Pi

### 1. Flash the OS

Use Raspberry Pi Imager. Choose **Raspberry Pi OS (64-bit)** — the Lite
version is fine and preferable for the Scout, since neither Pi needs a desktop.

32-bit will not work. The installer checks and refuses.

In Imager, click the gear icon before writing and set:

- hostname (`mctb-scout` / `mctb-brain` — makes SSH much less confusing)
- enable SSH, with a public key rather than a password
- your Wi-Fi credentials as a fallback only; you will use Ethernet
- locale and timezone

### 2. First boot

```bash
ssh pi@mctb-scout.local

sudo apt update && sudo apt full-upgrade -y
sudo reboot
```

### 3. Boot from NVMe (if you fitted an SSD)

```bash
sudo raspi-config
# Advanced Options → Boot Order → NVMe/USB Boot
sudo rpi-eeprom-update -a
sudo reboot
```

### 4. Verify the clock

A Pi has no battery-backed real-time clock. It gets the time from the network,
and if that fails it silently starts drifting — which breaks transaction
building and every age-based rule in the strategy, in ways that look like bad
luck rather than a bug.

```bash
timedatectl
# want: "System clock synchronized: yes" and "NTP service: active"
```

The installer enables `chrony` and `timedatectl set-ntp true`, and
`mctb preflight` checks the drift against a network reference.

---

## Getting a good RPC endpoint

**This matters more than the hardware.** The default public endpoint
(`api.mainnet-beta.solana.com`) is rate-limited to the point of being unusable
for trading. You will see constant 429s, missed fills and delayed exits, and it
will look like the bot is broken when it is only being throttled.

Free tiers that are good enough to start:

| Provider | Notes |
|---|---|
| **Helius** | Generous free tier, Solana-specialised, good docs |
| **QuickNode** | Free tier available, reliable |
| **Triton** | Performance-focused |
| **Shyft** | Free tier, Solana-focused |

Sign up, create a Solana mainnet endpoint, and put both URLs in `.env` on
**both Pis**:

```bash
MCTB_RPC_URL=https://your-endpoint.example.com/?api-key=...
MCTB_RPC_WS_URL=wss://your-endpoint.example.com/?api-key=...
```

Configuring a second provider as `MCTB_RPC_URL_FALLBACK` gives you automatic
failover and is worth the ten minutes.

Check what you actually got:

```bash
mctb preflight
```

The RPC check reports latency and probes for rate limiting. Under 300ms is
good; over 800ms and you will lose fills you should have won.

---

## Where to put the LLM

| Placement | Speed | Verdict |
|---|---|---|
| Brain (Pi 5, 3B model) | ~5–15 tok/s | Fine for the nightly review; too slow for most live candidates |
| Brain (Pi 5, 1.5B model) | ~15–25 tok/s | Usable on live candidates, weaker judgement |
| **Host PC with a GPU** | 100+ tok/s | **Best option if you have one** |
| Host PC, CPU only | ~20–40 tok/s | Still much better than the Pi |

To offload it, run the llama.cpp server on the host PC and set this on the
Brain:

```bash
MCTB_LLM_BASE_URL=http://192.168.1.50:8080
```

Measure what you actually have before tuning `llm.timeout_s`:

```bash
./scripts/setup_llm.sh bench
```

---

## Power and resilience

Consider a small UPS for the Pis. A power cut mid-trade leaves positions open
with nothing managing them until the devices boot again. The Brain restores
open positions on startup and resumes their exit rules, but that only helps
once it is actually running.

If you cannot do a UPS: keep `max_hold_s` short, so the maximum exposure from
an outage is bounded by how long a position could have been open anyway.

---

## Running it all on one machine

You do not need any of this to start. Everything runs on a single machine:

```bash
mctb all
```

You give up fault isolation and the key-separation property, which matter more
the more money is involved. But for paper trading and tuning — which is where
you should be spending your first few weeks — one machine is entirely
sufficient, and it is the sensible way to find out whether you want to buy
hardware at all.
