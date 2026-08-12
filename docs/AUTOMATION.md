# Alerts and automation

MCTB talks to the outside world in both directions: it pushes alerts to your
phone, and it accepts commands from TradingView, Zapier, n8n or anything else
that can send an HTTP POST.

---

## Outbound alerts

### Telegram

The most useful channel, because it reaches your phone from a Pi in your house
with no infrastructure.

1. Message **@BotFather** on Telegram, send `/newbot`, follow the prompts.
2. Copy the token it gives you.
3. Message **@userinfobot** to get your numeric chat id.
4. In `.env`:

```bash
MCTB_TELEGRAM_BOT_TOKEN=1234567890:AAxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
MCTB_TELEGRAM_CHAT_ID=123456789
```

That is all — `alerts.telegram` flips on automatically when both are present.

### Discord

Server Settings → Integrations → Webhooks → New Webhook → Copy URL.

```bash
MCTB_DISCORD_WEBHOOK_URL=https://discord.com/api/webhooks/...
```

Alerts arrive as embeds with colour-coded severity and clickable links to
every explorer.

### Generic webhooks (Zapier, n8n, Make, IFTTT)

Any URL that accepts a JSON POST:

```yaml
alerts:
  webhooks:
    - "https://hooks.zapier.com/hooks/catch/123456/abcdef/"
    - "https://n8n.yourhost.tld/webhook/mctb"
```

Payload shape:

```json
{
  "source": "mctb",
  "title": "Bought SPIDERMAN",
  "message": "0.0200 SOL at 0.0000070740",
  "level": "info",
  "timestamp": 1786519284.9,
  "data": {}
}
```

### Rate limiting

Alerts are deduplicated by title within `dedupe_window_s` (60s default) and
globally throttled by `min_interval_s`. This is deliberate: **a channel that
floods is a channel you mute, and a muted channel is worse than none** —
you will miss the one message that mattered.

---

## Inbound webhooks

This lets external systems drive MCTB. It can also move money, so read the
security section before enabling it.

### Enabling

```bash
# Generate a long secret
openssl rand -hex 32
```

```bash
# .env
MCTB_WEBHOOK_SECRET=<that value>
```

```yaml
# config.yaml
webhooks:
  enabled: true
  allow_trading: false   # leave false unless you are certain
  allow_kill: true
```

The endpoint is `POST http://<host-pc>:8787/webhook/<secret>`.

### Security model

Three layers, because this endpoint can spend your money:

1. **The secret is in the URL path**, so the endpoint is not discoverable.
   Compared in constant time, so it cannot be brute-forced by timing.
2. **HMAC-SHA256 signatures** over `timestamp.body`, with replay protection —
   each signature is valid for 300 seconds and accepted exactly once.
3. **A capability allowlist.** Trading actions are refused unless
   `allow_trading` is true *and* the request is signed. The config **refuses
   to start** if you set `allow_trading: true` without a secret.

| Action | Needs signature | Needs `allow_trading` |
|---|---|---|
| `signal`, `watch`, `note`, `dossier` | no | no |
| `kill`, `resume` | no | no (safe — it only stops) |
| `buy`, `sell`, `close_all` | **yes** | **yes** |

Note that `buy` has **no direct handler by default**. Even with trading
enabled, an inbound buy routes through `signal`, so the bot's own gates,
scoring and risk limits still decide. An external system can tell MCTB *what
to look at*; it cannot override MCTB's judgement about whether to buy.

### Testing your wiring

```bash
mctb webhook-test http://192.168.1.50:8787 --action signal --mint <mint>
```

Signs the request properly and prints the response.

---

## TradingView

TradingView cannot compute HMAC signatures, so it can only use the unsigned
actions — which is exactly why trading actions require a signature.

**Alert setup:** create an alert, set Notifications → Webhook URL to
`https://your-host/webhook/<secret>`, and put this in the message box:

```json
{"action": "signal", "mint": "{{ticker}}", "reason": "{{strategy.order.comment}}"}
```

Plain text also works — MCTB parses TradingView's bare format:

```
signal 8LXPAS3MR5oSeEC1LqGZyHVpgs1Qw6m9My6iDKTPpump
```

Strategy words are mapped automatically: `long`/`entry` → `buy`,
`short`/`exit`/`close` → `sell`.

**TradingView needs a publicly reachable HTTPS URL.** Do not port-forward your
console directly. Put it behind a reverse proxy with TLS, and set
`webhooks.allowed_ips` to TradingView's published ranges.

---

## n8n

Both directions.

**MCTB → n8n:** add a Webhook node, copy its URL into `alerts.webhooks`.

**n8n → MCTB:** an HTTP Request node with a Crypto node computing the
signature:

```javascript
// Code node, before the HTTP Request
const body = JSON.stringify({action: "kill", reason: "risk rule fired"});
const ts = String(Date.now() / 1000);
const crypto = require('crypto');
const sig = crypto.createHmac('sha256', $env.MCTB_SECRET)
                  .update(ts + '.' + body).digest('hex');
return [{json: {body, headers: {
  'X-MCTB-Timestamp': ts,
  'X-MCTB-Signature': sig,
  'Content-Type': 'application/json'
}}}];
```

---

## Zapier

**MCTB → Zapier:** create a Zap with a "Catch Hook" trigger, put its URL in
`alerts.webhooks`. You then have MCTB events in Zapier's whole ecosystem —
Sheets logging, SMS, whatever.

**Zapier → MCTB:** a Webhooks by Zapier "POST" action to
`https://your-host/webhook/<secret>`. Zapier cannot easily compute HMAC, so
stick to informational actions.

---

## Useful patterns

**Log every trade to a spreadsheet.** Zapier catch hook → Google Sheets. Your
own tax record, updated live.

**Escalate only what matters.** Route `critical` alerts to Telegram and
everything else to Discord, so your phone only buzzes for exit failures and
dead services.

**External risk kill.** An n8n schedule that checks something MCTB cannot see
— SOL price crashing, a exchange outage — and sends `kill`. This is the single
most valuable inbound automation, and it needs no signature.

**Second-opinion research.** Send `dossier` for a mint from any workflow and
get the full fused intelligence back as JSON.

---

## What not to do

**Do not expose the console to the public internet.** It has no
authentication beyond the webhook path — the dashboard itself is wide open.
Use Tailscale, WireGuard or an SSH tunnel. If TradingView forces you to expose
something, expose *only* `/webhook/` through a reverse proxy and nothing else.

**Do not enable `allow_trading` casually.** An inbound message that can open
positions is a path from someone else's system into your wallet. The default
is off, the secret is mandatory, and the signature is mandatory — three
deliberate obstacles. Leave them in place unless you have a specific reason.

**Do not put the secret in a URL you paste anywhere.** It is both the path and
the HMAC key. Treat it like a password, because it is one.
