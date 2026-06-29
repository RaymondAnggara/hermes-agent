# Phase 5b — Trading: trade-safety design (STOP GATE 5)

> **Status: DESIGN ONLY. No trading code exists.** This is the artifact for STOP GATE 5.
> Nothing here executes an order, holds an exchange key, or opens an exchange domain on the
> egress allowlist. Per the build prompt, the operator must approve this design AND a
> paper-mode run must pass before any live order. Exchange choice is deliberately deferred —
> the design is exchange-agnostic behind one adapter seam.

## 0. The one-paragraph summary

`#investment` stays advisory (5a, live). Trading is a **separate, default-OFF capability**
fronted by an explicit operator opt-in per order. It is a **service-gated tool** (footprint
ladder: NOT a core tool), enabled only on a dedicated channel, callable only after the
operator arms a one-shot confirmation (the `/build` pattern, reused). Every order passes a
**fail-closed, in-code risk gate** before it can reach an exchange adapter. The first
implementation ships **paper-only**; live execution is a second, separately-gated step. No
key in the system can withdraw or transfer funds, enforced at the exchange AND verified in
code at startup.

## 1. Non-negotiable safety invariants (each enforced as infrastructure, not prose)

| # | Invariant | Where enforced (infrastructure) |
|---|---|---|
| I1 | **No key can move funds off-exchange.** Trade-only keys; withdrawal + transfer + API-withdraw all DISABLED at the exchange. | Exchange-side key permissions + a **startup permission probe** that fail-closes the tool if it cannot positively confirm withdrawal is disabled. |
| I2 | **Hard position/notional caps in code.** Per-order max notional, per-period (rolling 24h) cumulative notional, max open position per symbol. | A pure `risk_gate()` function the executor MUST call; defaults fail-closed (cap=0 → everything rejected until configured). Caps live in `config.yaml`, but the *enforcement* and the floor (never exceed an absolute hard ceiling) are in code. |
| I3 | **Paper-first.** Dry-run executor simulates fills; zero real orders. Live mode is a separate flag the operator flips after reviewing paper results. | Executor selected by mode; `live` requires both a config flag AND the per-order arm (I4). |
| I4 | **Manual confirm on every live order.** No autonomous live trading. | Reuse the `/build` one-shot arm: `/trade <intent>` produces a PLAN; a live order requires the operator to arm `/trade-confirm <id>` (one order, short TTL). The agent cannot self-arm. |
| I5 | **Bootstrap/keys never in the prompt-injectable container.** Exchange keys in Bitwarden; injected like the platform tokens (Phase 5b secrets-hardening). | Same `launch_locked.sh` host-injection pattern; bootstrap stays on host. Egress value-redaction already scrubs key shapes. |
| I6 | **Exchange domains are default-deny until explicitly allowlisted.** | The locked egress proxy (`allowlist.filter`) must gain the exchange API host — a **guardrail-affecting change**, flagged loudly, operator-confirmed (Phase 6 rule). Until then, even a compromised agent cannot reach the exchange. |
| I7 | **Third-party trading code is untrusted.** No SDK/strategy code runs unreviewed. | Prefer a thin direct-REST adapter over a heavy SDK; any dependency reviewed line-by-line and pinned; runs with no credential reach beyond the injected trade key. |
| I8 | **Kill switch + caps + loop/budget guards apply.** `!stop` halts in-flight; turn/token caps; loop detection. | Existing gateway kill switch + per-channel `max_turns`; the executor checks `orchestrator_enabled`. |
| I9 | **Full audit trail.** Every order intent → decision → (paper|live) outcome is logged immutably. | Append-only audit store (SQLite table or JSONL under `~/.hermes/logs/trading/`), written before and after each exchange call; idempotency key per order. |

## 2. Where it sits in Hermes (footprint ladder — edges, not core)

- **NOT a core tool.** A core tool ships on every API call to every channel; a trading
  primitive must not. → **service-gated tool** (`check_fn` gates registration) or a
  **plugin**, enabled ONLY on the dedicated trading channel binding.
- **Channel:** a dedicated `#trading` (or a gated mode on `#investment` — operator's call).
  `channel_toolsets` allowlist includes the trade tool ONLY here; everywhere else it is
  absent (not hidden — absent), so prompt injection elsewhere has no trade surface.
- **Advisory stays separate.** `#investment` (5a) keeps `[web, memory]`, no trade tool. The
  research persona already refuses buy/sell/position-size calls; that stays.
- **Reuses existing infra:** `/build`-style one-shot arm (`_handle_build_command` pattern),
  per-channel toolset/turn caps, write-jail philosophy (fail-closed), egress proxy + secret
  redaction, Bitwarden host-injection (just built).

## 3. Order lifecycle (paper and live share one path until the final hop)

```
operator: /trade buy 0.01 BTC limit 60000        (on #trading, operator-only)
  │
  ▼
1. PARSE  → structured OrderIntent{side, symbol, qty, type, limit_px, tif}
2. RESOLVE market data (read-only) → est. notional, current position
3. risk_gate(intent, account_state, caps)   ← PURE, fail-closed
      reject → reply with reason, audit(rejected), STOP
      accept → produce OrderPlan{intent, est_notional, caps_remaining, id}
4. reply PLAN to operator (no order placed yet); audit(planned)
5a. PAPER mode: simulate fill at plan price → audit(paper_filled) → reply
5b. LIVE mode:  require operator `/trade-confirm <id>` within TTL
        not armed / TTL expired → audit(expired), STOP
        armed → exchange_adapter.place_order(intent)   ← the ONLY live hop
                audit(live_submitted) → poll status → audit(live_filled|rejected)
```

The `risk_gate` (step 3) and the adapter (step 5b) are the two security-critical seams.
Everything before step 5b is identical in paper and live, so paper-mode genuinely exercises
the real decision path.

## 4. The risk gate (in-code, fail-closed) — the core of I2

A pure function, unit-tested adversarially, with NO I/O:

```
risk_gate(intent, account_state, caps) -> Decision(accept|reject, reason, caps_after)
  reject if caps is None or any cap <= 0            # unconfigured = closed
  reject if est_notional(intent) > caps.max_order_notional
  reject if rolling_24h_notional + est_notional > caps.max_period_notional
  reject if projected_position(symbol) > caps.max_position_per_symbol
  reject if total_open_exposure(all symbols) + est_notional > HARD_CEILING   # global backstop
  reject if intent.symbol not in caps.allowed_symbols
  reject if intent unparseable / non-finite / negative qty
  else accept
```

- Caps come from `config.yaml` (`trading.caps`), but code clamps every cap to the absolute
  `HARD_CEILING` constant — config can only make caps *tighter*, never looser.
- **`HARD_CEILING` governs TOTAL exposure** = the sum of all open positions across every
  symbol, so it stays a real backstop as `allowed_symbols` grows (N symbols × per-symbol cap
  can otherwise creep past it). No single config edit or bug can deploy more than this.
- `allowed_symbols` is an explicit allowlist (no "trade anything").
- Rolling-24h notional + open exposure are read from the audit store (durable across restarts).

**Confirmed caps (v1, $100 Bybit spot account — 2026-06-29).** Sized *below* the account so
they actually bind (spot/no-leverage already physically caps loss at the $100 balance):
`max_order_notional: 20`, `max_position_per_symbol: 40`, `max_period_notional: 100` (24h),
`allowed_symbols: ["BTC","ETH"]`, `HARD_CEILING: 90` (total exposure). These scale up via an
operator-gated config edit (+ `HARD_CEILING` bump) when the account is funded further. At $100
the explicit goal is **validation, not profit** — fees/min-order-sizes eat a large % at this
size, so v1 success = "plumbing works, caps hold, honest track record," not returns.

## 5. Keys & egress (extends Phase 5b secrets-hardening, already live)

- **Storage:** trade-only exchange key/secret in Bitwarden ("Hermes Agent"), e.g.
  `EXCHANGE_API_KEY` / `EXCHANGE_API_SECRET` (+ passphrase if the exchange needs one).
- **Injection (DECIDED):** **host-injection for the Bybit *demo* key** (can't touch real
  money → low stakes), via the same `launch_locked.sh` that handles `DISCORD_BOT_TOKEN`/
  `TAVILY_API_KEY`. For the **live** key → a **signing proxy** (agent never holds the secret;
  models the Phase-4 model-key proxy, implementing Bybit's HMAC-SHA256 request signing). The
  live proxy is built only at the live step, not before.
- **Egress (I6):** add the exchange REST host to `network/allowlist.filter`. This is the
  single change that lets the agent reach the exchange at all — **guardrail-affecting**,
  so: separate commit, loud flag, operator confirm, and it goes in LAST (after paper mode
  passes), so during paper testing the exchange is still unreachable by L3.
- **Egress redaction:** bare 64-char exchange keys are covered by the value-redaction layer
  once configured as secret-named env vars (`*_SECRET`, `*_API_KEY` — see
  `gateway/secret_egress.py` `_SECRET_ENV_NAME_RE`). PEM/CDP/AWS/Stripe shapes already added
  (W2). Verify the chosen exchange's key shape is covered before live.

## 6. Withdrawal-disabled verification (I1) — don't trust the dashboard

At tool-registration / startup, probe the key's own permissions and **fail-closed** (refuse to
register the trade tool) unless it positively reports withdrawal/transfer DISABLED. **Bybit
(confirmed exchange)** exposes exactly this: `GET /v5/user/query-api` returns a `permissions`
object — we require `"Withdraw"`, `"AccountTransfer"`, and `"SubMemberTransfer"` to all be
**absent** from `permissions.Wallet` (and may also assert `readOnly`/`ips` as configured).
"We set it in the UI" is not enforcement — the bot verifies it every startup.

## 7. Paper mode (I3) — first deliverable, gated on its own

- A `PaperExecutor` simulates fills (at limit price, or last trade for market orders) and
  writes the same audit records as live, tagged `paper`. No exchange call, no key needed,
  exchange domain still default-denied.
- Run a scripted set of intents through the FULL path (parse → risk_gate → plan → paper
  fill), including adversarial ones (over-cap, bad symbol, negative qty, cap exhaustion over
  24h) and confirm every reject fires. Review the audit log together.
- Only after that review do we discuss enabling `live` (separate gate).

## 8. What this design explicitly does NOT do (yet)

- No autonomous/algorithmic trading, no scheduled trading, no "the bot decides to trade."
  Every live order is operator-initiated and operator-confirmed.
- No margin/leverage/derivatives in v1 (spot only, long-only unless operator asks).
- No portfolio rebalancing, no stop-loss automation (those are autonomous actions → later,
  separate gates if ever).
- Exchange chosen (Bybit) + caps set, but **no key created, no allowlist change, no code** —
  decisions recorded; build still pending past STOP GATE 5.

## 9. Build order if approved (each its own small, reviewable step)

1. `risk_gate()` pure module + adversarial tests (no I/O, no keys). 
2. `OrderIntent`/`OrderPlan` types + parser + audit store (append-only) + tests.
3. `PaperExecutor` + the `/trade` plan flow on `#trading` (paper only; tool service-gated).
4. **PAPER-MODE REVIEW with operator.** ← gate
5. Exchange adapter (thin REST, reviewed, pinned) + withdrawal-disabled startup probe.
6. Key into Bitwarden + launcher injection; key-shape egress check.
7. `/trade-confirm` live arm + `LiveExecutor` (still no egress to exchange yet).
8. **Allowlist the exchange host** (guardrail-affecting, loud flag, operator confirm). ← gate
9. First live order: smallest possible size, operator-confirmed, watched, with `!stop` armed.

## Decisions resolved at STOP GATE 5 discussion (2026-06-29)

1. **Exchange: Bybit** — account created + verified. Verifiable withdrawal-disabled keys
   (`GET /v5/user/query-api`, §6) + strong demo env (`api-demo.bybit.com`, 50k simulated USDT).
2. **Trade-key handling:** host-injection for the **demo** key; **signing proxy** for the
   **live** key (agent never holds it; HMAC-SHA256 signing). See §5.
3. **Caps (v1 / $100):** `max_order_notional: 20`, `max_position_per_symbol: 40`,
   `max_period_notional: 100` (24h), `allowed_symbols: ["BTC","ETH"]`, `HARD_CEILING: 90`
   (total-exposure backstop). See §4. Scale up later via operator-gated config edit.
4. **Channel: dedicated `#trading`** (the trade tool lives only here; `#investment` stays
   advisory/no-keys and acts as the research feeder).
5. **Confirmed: paper-mode first, no live order until a separate explicit go-ahead.**

> Still STOP GATE 5: these decisions are recorded, but NO trading code/keys/allowlist change
> exists. The strategy/confidence model + data model are in [`STRATEGY_DESIGN.md`](./STRATEGY_DESIGN.md).
> The first build step (pure confidence-model + SQLite schema, no keys/money) is now fully specced.
