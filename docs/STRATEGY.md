# Strategy and tuning

What each parameter actually does, how to change it responsibly, and how to
tell whether a change helped.

---

## How a trade gets taken

```
Candidate → hard gates → base score → personal edge → LLM → risk limits → size → buy
              (veto)      (0-100)      (learned)    (bounded)  (portfolio)
```

Every stage can reject. Only the last one can commit money.

---

## Hard gates

Binary filters. A token failing any of them is never bought, at any score.
Gates are the cheapest and highest-value part of the system: most money lost
to memecoin bots is lost to tokens that were never sellable, not to bad price
predictions.

| Gate | Default | What it is protecting you from |
|---|---|---|
| `min_age_s` | 8 | The first seconds belong to bundled snipers who are already positioned below your cost basis |
| `max_age_s` | 900 | Buying something whose move already happened |
| `min_sol_in_curve` | 3.0 | A curve so thin you move the price against yourself in both directions |
| `max_sol_in_curve` | 85 | Near graduation; the bonding-curve trade is over |
| `min_unique_buyers` | 8 | One wallet trading with itself to fake volume |
| `min_buy_sell_ratio` | 1.5 | More people leaving than arriving |
| `require_mint_authority_revoked` | true | **Never change.** Infinite supply can be printed onto your position |
| `require_freeze_authority_revoked` | true | **Never change.** Your tokens can be frozen so you can never sell |
| `max_dev_holding_pct` | 0.05 | The deployer being able to end the run alone |
| `max_top10_holding_pct` | 0.35 | Your exit being at ten strangers' discretion |
| `max_bundle_pct` | 0.20 | Bundled launches where you are the exit liquidity |
| `require_smart_wallet` | false | Set **true** for the single biggest quality improvement available |

**The one change most likely to help you:** set `require_smart_wallet: true`.
It cuts your trade count dramatically and removes most of the tokens that
looked fine and were not. If you only make one tuning change, make this one.

---

## Scoring weights

Each feature produces a 0..1 sub-score; the weighted sum is rescaled to
0..100. Weights are relative, so they need not sum to anything.

| Weight | Default | Feature |
|---|---|---|
| `w_smart_wallets` | 30 | Tracked wallets in, weighted by *their reputation with you* |
| `w_buy_pressure` | 18 | Buy/sell ratio |
| `w_unique_buyers` | 12 | Distinct buyers |
| `w_curve_progress` | 10 | Curve depth in the sweet spot |
| `w_holder_distribution` | 12 | Supply spread out rather than concentrated |
| `w_social` | 10 | Mentions, log-scaled by reach |
| `w_velocity` | 15 | Net SOL/sec inflow — is it happening *now* |
| `w_creator_history` | 8 | Has this deployer treated you well before |

Two notes on how these behave:

- **Smart wallets score on quality, not count.** One wallet with a 0.9
  reputation outscores four at 0.5. There is a test asserting this. Ten
  mediocre wallets piling in is not the same signal as one good one.
- **Velocity is the most perishable feature.** It uses a rolling 45-second
  window, not lifetime totals. A token with 400 buys four minutes ago is dead;
  one with 30 buys in the last twenty seconds is alive. Lifetime counters
  cannot tell those apart, and that difference is most of the edge.

`entry_threshold` (default 62) is the score required to open a position. It is
the fastest single dial: raise it to trade less and better, lower it to trade
more.

---

## Exits

Where the money is actually decided. A mediocre entry with disciplined exits
is profitable; a brilliant entry held too long is not.

Rules fire in strict priority order:

1. **Unsellable / stale price** → out immediately
2. **Rug signals** (dev selling, tracked wallets leaving) → out immediately
3. **Stop loss** → the line you drew before you had money on it
4. **Trailing stop** → protects a gain that already happened
5. **Take-profit ladder** → sells into strength in tranches
6. **Stagnation / max hold** → capital in a flat coin is not working

```yaml
exits:
  stop_loss_pct: 0.35
  take_profit_ladder:
    - { multiple: 1.6, fraction: 0.34 }   # de-risk
    - { multiple: 2.5, fraction: 0.33 }   # bank profit
    - { multiple: 5.0, fraction: 0.20 }   # leave 13% as a runner
  trailing_activate_pct: 0.45
  trailing_distance_pct: 0.25
  max_hold_s: 900
  stagnant_after_s: 240
  stagnant_min_gain_pct: 0.10
```

**Ladder fractions are of the ORIGINAL position**, not what remains. The three
rungs above sum to 0.87, deliberately leaving 13% to run — that residual is
what pays for the losers when something goes 20x.

**The stop loss is a target, not a guarantee.** Memecoins gap. A position can
go from -10% to -80% between two price reads with no chance to exit in
between. Size on the assumption that any position can go to zero.

**Why `stagnant_after_s` matters more than it looks.** A position that has not
moved in four minutes is unlikely to suddenly move, and every extra minute you
hold is another minute in which the dev can sell, the chart can break, or your
RPC can fail. Getting out of dead trades quickly frees capital and, more
importantly, reduces exposure to things going wrong.

---

## Risk limits

These are about your account, not the token. A perfect setup is still a bad
trade if you are already at maximum exposure.

```yaml
risk:
  max_position_sol: 0.05           # absolute ceiling
  position_pct_of_bankroll: 0.02   # the important one — see below
  min_sol_reserve: 0.05            # gas money, never spent
  max_concurrent_positions: 3
  daily_loss_limit_sol: 0.25       # halts for the day
  consecutive_loss_limit: 5        # halts and cools off
```

`position_pct_of_bankroll` deserves attention because it is doing more work
than it appears to. Sizing as a *fraction* means the bot automatically shrinks
its own positions as it loses. That is the mechanism that turns a drawdown
into a survivable one rather than a terminal one, and it works without you
having to notice or intervene.

Note that the `degen` preset sizes **smaller** than `balanced`, not larger.
More aggressive means more shots, each one smaller. A low-hit-rate,
fat-tailed strategy only survives its own losing streaks that way.

---

## The personal edge layer

Every closed trade's entry features are bucketed and stored with the outcome.
Future candidates are adjusted by how that bucket has actually performed *for
you* — your execution, your latency, your sizes, your exit rules.

```yaml
scoring:
  personal_edge_enabled: true
  personal_edge_max_adjust: 15.0   # hard cap in points
  personal_edge_min_samples: 12    # per bucket, before it counts at all
```

Two properties keep it from overfitting:

- **Shrinkage.** Every adjustment is scaled by a confidence factor that grows
  with sample size. Three lucky trades nudge; forty consistent ones steer.
- **Expectancy over hit rate.** Weighted 80/20 toward realised P&L rather than
  win rate, because memecoin returns are fat-tailed. A setup winning 15% of
  the time at +8x beats one winning 80% at +10%, and ranking by win rate gets
  that exactly backwards.

Inspect what it has learned:

```bash
mctb stats --edge
```

If a row looks obviously wrong, it is usually sample size. Raise
`personal_edge_min_samples`.

**This is the part that makes the bot yours.** It is also why the SQLite
database is worth backing up — `make backup`.

---

## How to tune without fooling yourself

**Change one thing at a time.** Two changes at once and you learn nothing from
the result.

**Give each change 50+ trades.** Below that you are reading noise. This is
genuinely hard to accept when something has just lost five in a row.

**Check the funnel before touching weights.** Run the simulator:

```bash
mctb simulate --tokens 200
```

It prints where candidates are being rejected. If 90% die at one gate, that
gate is your strategy — adjusting scoring weights will do nothing.

**Watch the right metrics.** In order of how much they tell you:

| Metric | Read it as |
|---|---|
| **Profit factor** | Gross wins ÷ gross losses. Below 1.0 you are losing. This is the number that matters. |
| **Max drawdown** | The worst peak-to-trough. Ask yourself honestly whether you would have kept going through it. |
| **Expectancy** (avg P&L) | The only thing that compounds |
| Win rate | **Least** informative here. A 25% win rate can be excellent. |
| Exit reason counts | Where your rules are actually firing — often surprising |

If `exit_reasons` is dominated by `stagnant` or `max_hold`, your entries are
early or your holds are too short for the moves you are catching. If it is
dominated by `stop_loss`, your entries are late.

**The simulator is not a backtest.** Its price paths are generated from a
plausible distribution, not from history. A good result there means your
config is coherent, not that it is profitable. Real paper trading against the
live feed is the actual test, and it takes weeks.

---

## The presets

| | conservative | balanced | degen |
|---|---|---|---|
| Entry threshold | 72 | 62 | 52 |
| Min age | 25s | 8s | 3s |
| Requires a tracked wallet | **yes** | no | no |
| Max position | 0.02 | 0.05 | **0.015** |
| Concurrent positions | 2 | 3 | 6 |
| Stop loss | 28% | 35% | 45% |
| Expect | a few trades a day | steady flow | constant, mostly losers |

**Start with `conservative`.** It takes far fewer trades, and most of what it
skips would have lost money. It is also the fastest way to accumulate a
trustworthy learned edge, because its trades are less noisy.

Move to `balanced` once conservative is profitable on paper. Only try `degen`
if you have understood why its position sizes are *smaller*.

---

## The nightly LLM review

At `llm.nightly_review_hour_utc`, the local model reads your performance
summary, the learned edge table and your recent trades, and reports back:
what preceded losses, whether exits fired at the right times, and specific
parameter changes it can justify from the data.

```bash
mctb review    # run it now instead of waiting
```

Proposals are validated against an allowlist with hard bounds
(`TUNABLE_BOUNDS` in `brain/llm.py`), so a confidently wrong suggestion cannot
set your position size to 10 SOL. Anything outside the allowlist or its range
is rejected and shown to you as rejected.

`auto_apply_tuning` defaults to **false**, and should stay there. Read the
report yourself. The value is in the analysis — "every loss this week entered
above 40 SOL of curve depth" is a genuinely useful observation — not in
letting a 3B model edit your risk parameters unsupervised.
