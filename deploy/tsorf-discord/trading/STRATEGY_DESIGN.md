# Phase 5b — Trading strategy & confidence model (design)

> **Status: DESIGN ONLY. No code, no keys, no live trading.** Companion to
> [`TRADE_SAFETY_DESIGN.md`](./TRADE_SAFETY_DESIGN.md) (the safety/containment design). That
> doc owns the hard guardrails; THIS doc owns *what the agent analyzes and how it decides*.
> Still at STOP GATE 5 — nothing here is built until the operator approves and paper-mode runs.

## 0. Primary goal & honest framing (operator chose: decision-support + measure edge)

The agent is a **research analyst + risk enforcer that PROPOSES; the human decides.** It is
**not** an autopilot. Its first job is to find out *honestly whether any edge exists* before
real money is ever risked. We design so the system is valuable **whether or not** a
profitable edge turns out to exist.

Honesty truths this design is built around (do not let the UI's tidy "73%" hide these):

1. **Automated retail trading is, on average, a net loser** after fees/slippage. An LLM does
   not change that base rate. Goal = measure, not assume, an edge.
2. **A confidence % is a hypothesis to be tested, never a fact.** False precision is
   dangerous *because* it reads as trustworthy. The number must earn its precision against
   real outcomes (calibration, §5).
3. **News/social are weak and adversarial signals.** Public news is largely priced in;
   social sentiment is noisy and *actively manipulated*. Feeding it to the agent is also a
   **prompt-injection vector** (the Phase-4 threat). → untrusted-input discipline (§3) +
   low trust weight on social signals (§2).
4. **Paper/demo results systematically OVERSTATE live results** (perfect fills, no slippage,
   no latency, no emotion). Bake the haircut in; never trust paper at face value (§6).
5. **"Try many strategies, keep the best" = overfitting.** The best of N random strategies
   looks great in-sample and fails out-of-sample. → out-of-sample/walk-forward validation
   and selection-bias guards (§6).
6. **Leverage is the ruin button** (deferred indefinitely; its own future gate).

**Always report the benchmark.** Every strategy's performance is shown *next to* "did it beat
just buy-and-hold BTC, net of fees?" and "did it beat random entry?" A strategy that wins 60%
but underperforms buy-and-hold is worthless. This single comparison kills most self-deception.

## 1. Two separate layers — never conflate them

| Layer | Decides | Examples | Who can change it |
|---|---|---|---|
| **Risk caps** (guardrails — see TRADE_SAFETY_DESIGN §4) | how much *damage* is possible | `max_order_notional`, `max_period_notional` (24h), `max_position_per_symbol`, `allowed_symbols`, absolute `HARD_CEILING` | operator only; fail-closed; a strategy can **never** raise its own caps |
| **Strategy params** (this doc, §4) | *what* to trade | TP, SL, risk:reward, position-sizing rule, entry/exit logic, confidence-model weights | per-strategy; bounded *below* the caps |

The caps sit **above** every strategy and override it. Strategy = the driver's intent; caps =
the speed-limiter the driver cannot disable.

## 2. The confidence model — "a panel of advisors" (signed signals + logistic)

Replaces the operator's original `Igw·Itw·Ms = Ci` (multiplying [0,1] weights collapses toward
zero, can't represent bearish vs bullish, and penalizes missing data). The defensible form is
**logistic regression**, explained as a weighted advisor panel.

Each **signal** `sᵢ ∈ [−1, +1]` is one advisor's opinion (−1 strong sell, 0 no-opinion/unknown,
+1 strong buy). Each **weight** `wᵢ ≥ 0` is how much that advisor has earned our trust.

```
Step 1 — overall lean:   z = b + Σ (wᵢ · sᵢ)
Step 2 — to a percent:   p = 1 / (1 + e^−z)     (the sigmoid / S-curve)
```

- **Adding** (not multiplying) means two "buy" advisors reinforce each other, and an advisor
  who says "I don't know" (`s=0`) contributes exactly 0 — no opinion, no penalty.
- `z` carries **direction**: negative = lean sell, 0 = coin-flip, positive = lean buy.
- The **sigmoid** squashes any `z` into a sane 0–100% (being twice as sure ≠ "150%").
- `b` (bias) = starting tilt, default 0 (= 50/50 before any advisor speaks).

Sigmoid shape (intuition; no hand-math needed):

| `z` | −2 | −1 | 0 | +0.5 | +1 | +2 |
|---|---|---|---|---|---|---|
| `p` | 12% | 27% | **50%** | 62% | 73% | 88% |

**Worked example — "buy BTC?"** (note social is deliberately the *least*-trusted advisor):

| Signal | opinion `sᵢ` | trust `wᵢ` | contribution |
|---|---|---|---|
| General economy | +0.3 | 0.5 | 0.15 |
| BTC social/news | +0.6 | 0.3 (low: noisy/manipulable) | 0.18 |
| Market data (price/vol) | +0.2 | 1.0 (hard data) | 0.20 |

`z = 0.53` → `p ≈ 63%` → mild buy. If social were *unknown* (`s=0`): `z=0.35` → 59% (less
evidence, no fake penalty). If market data turned bearish (`s=−0.7`): `z=−0.37` → 41% → lean
skip — the trusted bearish advisor correctly outweighs the weak bullish ones.

> The confidence number is **never directly settable by free text.** Prose is converted to
> *structured signals* by a constrained extraction step; the model computes the number. This is
> a security property (anti prompt-injection), not just a modeling choice.

## 3. Signal catalog — how each input becomes an `sᵢ`

Each signal type maps prose/data → a number in [−1,+1] + a **default trust weight** (later
learned, §5). All free-text sources are wrapped `<untrusted_tool_result>` and feature-extracted.

- **General information weight** (`Igw`) — macro/economic stability: rate decisions, broad
  risk-on/off, regulatory climate. Source: reputable news via the advisory (`#investment`)
  research path. Default trust: medium.
- **Targeted information weight** (`Itw`) — symbol-specific: project news, scam/danger flags
  (e.g. "is DOGE a known pump target?"), liquidity/listing risk. Source: research + reputable
  data. Default trust: medium-low (symbol news is often hype). A **danger flag** (scam/halt/
  delisting) forces `s` strongly negative regardless of other inputs.
- **Market analysis** (`Ms`) — hard data from the exchange API / one trusted data provider:
  price trend, buy/sell volume, simple indicators (e.g. moving-average cross, RSI, ATR for
  volatility). Default trust: **highest** (it's measured, not narrated).
- **Trader knowledge** — operator's own read ("I think this is bullish") entered explicitly,
  as its own signal with its own (operator-tunable) weight.

Market data must come from a **trusted, egress-allowlisted** source (the agent runs in a
network-locked container; see §10), never ad-hoc scraping.

## 4. Strategy params (the `/add-strat` payload) + registry

A *strategy* is a named bundle of decision params, stored in the registry and bounded by the
caps. The risk-management params that actually matter (standard, well-established — they manage
*risk*, they do not manufacture *edge*):

- **Stop-loss (SL)** and **take-profit (TP)** — prefer volatility-based (e.g. ATR multiples)
  over round numbers.
- **Risk:reward ratio** — e.g. risk 1 to make 2. Below ~1:1 needs a high win-rate to survive.
- **Position sizing** — *risk a fixed small % of capital per trade* (the "1% rule"), not a
  fixed dollar guess. (Still hard-capped by `max_order_notional`.)
- **Max concurrent positions**, **max holding time**, **max-drawdown kill** (stop trading if
  down X%).
- **Entry/exit rule** — the signal/confidence threshold that triggers a proposal.

Each strategy is **challenged against current signals** to produce *its* confidence; multiple
strategies per symbol are allowed (array), but see the overfitting guard (§6) — more candidates
is NOT more edge.

## 5. Learning — two distinct mechanisms (both need lots of data)

**A) Learn the weights `wᵢ`** (which advisors to trust). After each trade resolves, nudge
weights toward the configuration that would have predicted the outcome (logistic-regression
fit). Useless signals drift toward 0 on their own (the noisy social advisor may self-mute).
This is the "agent learns" goal, done in a standard, inspectable way.

**B) Calibration** (does "70%" really mean 70%?). Bucket past predictions by confidence band;
compare predicted vs **actually realized** win-rate; remap so outputs match reality (e.g.
"70%"→"58%" if that band only won 58%). This is the operator's `Ci·Fw` idea done *per band,
from data*, instead of one blunt global multiplier.

**Sample-size honesty (critical):** 100 trades is far too few — that's mostly luck. To tell a
real ~55% edge from a 50% coin-flip needs on the order of *several hundred to 1000+* trades,
and *more* if multiple strategies were screened. So there is **no fixed N to "graduate to
live."** Graduation is evidence-based (§6), not a trade count.

## 6. Validation — "is there actually an edge?"

A strategy may be considered for (tiny) live ONLY when, on **out-of-sample / walk-forward**
data (train on past window, test on a later unseen window), it:

1. is **statistically significant** over a few hundred+ trades (not a lucky streak), AND
2. **beats buy-and-hold of the same asset, net of realistic fees + slippage**, AND
3. **beats random entry** with the same risk params (proves the *signal* adds value, not just
   the risk management), AND
4. survives the **paper→live haircut** assumption (we explicitly down-rate paper results).

Selection-bias guard: if K strategies are screened, the bar rises (or we hold out a fresh
window the winner never touched). The "auto-suggest profitable symbols/strategies" feature is
**idea-generation only** — anything it surfaces earns its track record from scratch, and it
*suggests*, never auto-adds.

## 7. Data model (SQLite — Hermes already runs on SQLite/FTS5)

A dedicated `trading.db` (separate from `state.db`/`kanban.db`). Storage is cheap → record
everything:

- `symbols` — watched symbols (from `/add-symbol`).
- `strategies` — registry: name + params (§4) (from `/add-strat`).
- `signals` — every signal value at each decision time (type, raw source ref, extracted `s`,
  weight used). Append-only.
- `predictions` — symbol, strategy, the computed `z`, raw `p`, calibrated `p`, threshold,
  action proposed.
- `trades` — mode (`paper`|`demo`|`live`), entry/exit, fees, realized P&L, outcome (TP/SL/
  timeout), linked prediction. Append-only (mirrors the safety doc's audit store).
- `weights` / `calibration` — current learned weights + the reliability mapping, versioned so
  changes are traceable and reversible.

## 8. Commands (Hermes slash-commands, on `#trading`)

- `/add-symbol <SYMBOL>` — watch a symbol.
- `/add-strat <name> <params…>` — register a strategy.
- `/symbol-list`, `/strat-list` — show what's watched / registered.
- `/strat-to-symbol-confidence [symbol|*] [strat|*]` — current confidence + **# of trained
  samples** + calibration status (so a number always comes with how much data backs it).
- `/trade-journal [N]` — recent decisions + outcomes + running vs-benchmark P&L. Keeps the
  honesty one command away.
- Trade actions reuse the safety design: `/trade <intent>` → plan; `/trade-confirm <id>` →
  the only path to a live order (manual confirm per order).

## 9. Execution ladder (each step its own gate)

1. **Advisory** (`#investment`, live today) — research + signals, no orders.
2. **Paper (own simulator)** — full path (signals → confidence → risk-gate → plan → simulated
   fill). No key, no exchange contact, exchange domain still default-denied. Proves the caps
   reject bad orders and the model runs end-to-end.
3. **Demo (Bybit demo, recommended exchange)** — real Bybit API shape + live prices, *simulated*
   funds (50k demo USDT). Demo key host-injected (can't touch real money). Validates the real
   integration.
4. **Tiny live** — only after §6 is met AND a separate operator go-ahead. Live trade key via a
   **signing proxy** (agent never holds the secret — see TRADE_SAFETY_DESIGN §5/§Q), withdrawal
   **verified disabled** at startup via Bybit `GET /v5/user/query-api` (fail-closed), exchange
   domain allowlisted (a separate, loudly-flagged guardrail change), smallest size, `!stop`
   armed, manual confirm every order.

## 10. Cadence & containment

- **Cron tick** (Hermes already has a scheduler; `#agent-home` uses it): every N minutes,
  refresh market data + new info, recompute signals; if a setup fires → propose (paper) or ask
  to confirm (live). Not HFT — the news/swing approach doesn't need millisecond speed.
- **Egress:** the market-data + news sources must be on the locked container's **allowlist**
  (default-deny otherwise). Prefer the exchange's own market data or one reputable provider.
- **Untrusted input:** all news/social wrapped `<untrusted_tool_result>`, feature-extracted;
  the confidence number is computed, never set by prose (§2).

## 11. How it maps onto Hermes (footprint ladder — edges, not core)

- A **trading plugin** (NOT a core tool): owns `trading.db`, the confidence model, strategy
  registry, risk-gate, executors. Plugins work within provided ABCs and don't touch core files.
- The trade tool is **service-gated** and bound only to `#trading` (absent elsewhere → no trade
  surface for prompt injection on other channels). `#investment` stays advisory/no-keys.
- Reuses: `/build`-style one-shot arm (for `/trade-confirm`), per-channel toolset/turn caps,
  egress proxy + secret redaction, the Phase-5b Bitwarden host-injection, the Phase-4
  model-key-proxy pattern (template for the live signing proxy).

## 12. Anti-patterns this design refuses

| Tempting | Why it's a trap | Guard |
|---|---|---|
| Trust the tidy "73%" | false precision reads as truth | calibration + always show sample count + benchmark |
| Go live after 100 paper wins | 100 ≈ luck; paper overstates live | evidence-based graduation (§6), not a count |
| Keep the best of many strategies | overfitting/selection bias | out-of-sample/walk-forward + raised bar |
| Let a hot tweet set confidence | manipulation + prompt injection | untrusted framing + low social weight + computed number |
| Add leverage to boost returns | ruin risk | deferred indefinitely; own future gate |
| Strategy raises its own size | bypasses safety | caps override strategy, fail-closed |

## 13. Open items / placeholders (operator)

- **Caps numbers** (TRADE_SAFETY_DESIGN §Q3): `max_order_notional`, `max_period_notional`,
  `max_position_per_symbol`, `allowed_symbols` (start BTC/ETH?), `HARD_CEILING`.
- **Exchange:** Bybit *recommended* (verifiable withdrawal-disabled keys + strong demo) —
  confirm you can open an account (Indonesia availability) before we commit.
- **Trusted market-data source** to allowlist (Bybit's own market data is the simplest).
- Initial signal set + default trust weights to start from (before learning kicks in).

## 14. Build order if approved (design-only until then)

1. `trading.db` schema + the confidence model (pure functions: signals→`z`→`p`) + adversarial
   tests (incl. unknown-signal and direction cases). No I/O, no keys.
2. Strategy registry + the commands (`/add-symbol` … `/trade-journal`), read-only.
3. PaperExecutor + cron tick → paper proposals on `#trading`. **Paper-review gate.**
4. Calibration + weight-learning + the validation/benchmark report.
5. Bybit-demo integration (demo key, host-injected) + withdrawal-disabled startup probe.
6. (Only after §6 met + separate go-ahead) live signing proxy + allowlist change + first tiny
   watched order. **Live gate.**
