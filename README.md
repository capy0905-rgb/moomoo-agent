# moomoo-agent

A modular, **paper-only** AI trading agent for the **Moomoo x Qoder AI Trading Agent Challenge**.
It scans a small US universe (SPY / QQQ / NVDA), runs a four-step research pipeline, applies
hard risk gates on the Asia/Singapore competition clock, and writes an auditable decision to
`logs/decisions.jsonl`. It never touches live funds.

> **PAPER ONLY.** `config/trading_config.json` pins `meta.mode = "paper-only"`,
> `live_trading_enabled = false`, and the simulated US account `5829755` ($100,000).
> `src/runner.py` refuses to start if either flag changes. No code path calls a live
> order endpoint; `--paper` only writes an order *intent* for human/agent review.

---

## Architecture

```
moomoo-mcp (quote_market_snapshot, quote_cur_kline, sim_trade_*)
        │  agent fetches quotes + bars  (data capture, no orders)
        ▼
logs/market_snapshot.json   ← normalized, point-in-time market data
        │
        ▼
src/runner.py  ─────────────────────────────────────────────────────────────
  │ 1. risk_guard.classify_session      SGT session + competition calendar
  │ 2. research_pipeline.evaluate_symbol
  │      strategy_orb.evaluate_orb       ORB baseline + momentum / mean-reversion filters
  │      score_factors                   macro · fundamentals · technicals · sentiment
  │      build_thesis / build_outlook     falsifiable thesis + invalidation rules
  │      expectation                     p_win, expectancy in R, pass/fail
  │ 3. rank_candidates                   best expectancy first
  │ 4. risk_guard.size_position          risk budget × allocation cap
  │ 5. risk_guard.pre_trade_checks       every hard gate, each PASS/FAIL
  └──────────────────────────────────────────────────────────────────────────
        ▼
logs/decisions.jsonl   →   dashboard/index.html
logs/risk_state.json       (equity, day start, kill switch, peak halt)
```

The Python layer is **read-only with respect to the broker**: it consumes a snapshot file and
produces a decision/order-intent file. Submission (if ever) is a separate, explicitly confirmed
MCP call to `sim_trade_input_order`.

## Repository layout

| Path | Purpose |
| --- | --- |
| `README.md` | Architecture, rules, setup, runbook |
| `config/trading_config.json` | Universe, account, SGT sessions, calendar, risk limits, strategy + research parameters |
| `skills/market_scanner.md` | Reusable Qoder skill: scan → ranked candidates |
| `skills/research_pipeline.md` | Reusable Qoder skill: thesis → factors → outlook → vehicle |
| `skills/risk_auditor.md` | Reusable Qoder skill: session/calendar + pre-trade risk audit |
| `src/strategy_orb.py` | Opening-range breakout baseline with confirmation filters |
| `src/research_pipeline.py` | Four-step research pipeline (direction-aware scoring, expectancy) |
| `src/risk_guard.py` | SGT calendar, sizing, drawdown kill switches, pre-trade checks |
| `src/runner.py` | Orchestrator (`--dry-run` / `--paper`), JSONL logging, terminal report |
| `logs/decisions.jsonl` | Append-only audit trail (one JSON object per run) |
| `logs/market_snapshot.json` | Last normalized snapshot captured from moomoo-mcp |
| `logs/risk_state.json` | Persistent equity / kill-switch state |
| `dashboard/index.html` | Standalone dashboard (no dependencies, opens from disk) |
| `src/export_dashboard.py` | Bundles dashboard + log into one shareable HTML file (account id redacted) |
| `tests/fixtures/replay_2026-11-18T0300SGT.json` | Same-session K-line replay used to exercise the ENTER path |

## Setup

* Python **3.12+**, standard library only — no `pip install` required.
* `zoneinfo` uses the system IANA database. Windows without `tzdata` falls back to a fixed
  UTC+8 clock in `risk_guard.py` (Singapore has no DST), so behaviour is identical.
* No API keys are stored in the repo; market data is pulled through the connected **moomoo-mcp**
  server in the Qoder session.

```bash
cd "moomoo trading agent"
python src/runner.py --dry-run
```

## Session isolation (Asia/Singapore, UTC+8 — the competition clock)

US regular hours 09:30–16:00 ET = **22:30–05:00 SGT (+1 day)**. The trading date is the SGT date
before 09:00, otherwise the same SGT date.

| SGT window | Session | Mode | Orders |
| --- | --- | --- | --- |
| 17:00 – 22:30 | `PRE_MARKET` | `LIMIT_ONLY` | Strict limit only; market orders blocked; spread ≤ 0.15% |
| 22:30 – 05:00 (+1d) | `REGULAR` | `TRADE` | Market or limit; active execution window |
| 05:00 – 09:00 (+1d) | `AFTER_HOURS` | `LIMIT_ONLY` | Strict limit only; market orders blocked |
| 09:00 – 17:00 | `CLOSED` | `CLOSED` | Scan / audit only; no entries |

Competition calendar (all in `config/trading_config.json`):

| Date (SGT) | Rule |
| --- | --- |
| Thu 2026-11-26 | Thanksgiving — full close. `AUDIT_ONLY`: block all trades, log and audit risk only |
| Fri 2026-11-27 | Black Friday half-day — entries until 01:30 SGT Nov 28; **FLATTEN 01:30–02:00**; flatten by **02:00 SGT Nov 28**; trading checks stop **06:00 SGT Nov 28** |
| Sat–Sun 2026-11-28/29 | Weekend close |
| Mon 2026-11-30 23:59 SGT | Competition hard cutoff; afterwards all runs are `CUTOFF` |

The calendar is regression-checked across these timestamps (holiday → `AUDIT_ONLY`,
half-day → `FLATTEN`, weekend → `CLOSED`, after cutoff → `CUTOFF`).

## Risk controls

* `max_ticker_allocation_pct = 15` · `max_gross_exposure_pct = 45`
* `risk_per_trade_pct = 0.75` · ATR/structural stop with `max_stop_pct = 3.0`, `take_profit_r_multiple = 2.0`
* Daily kill switch at **−2.00%** of day-start equity; peak-to-trough halt at **−8.00%**
* Max spread **0.15%** of mid, data freshness 15 min (regular) / 30 min (extended)
* Mandatory stop + target on every entry (`brackets_present`, `reward_risk`)
* `limit_price_drift ≤ 0.25%` — if the book has moved away from the signal price, the intent is blocked

**Thesis invalidation** (checked by the ORB before an entry is produced, and re-checked by the
risk guard): failed breakout / range reclaim, spread > 0.15%, ATR% spike above 4%, mean-reversion
extension > 1.5 ATR from the opening-range mid, momentum conflict, stale data, and limit drift.
Every check is recorded as `PASS`/`FAIL` in the decision record.

## Position sizing

```
risk_amount = equity × 0.75%
qty_risk    = risk_amount ÷ |entry − stop|
qty_alloc   = (equity × 15%) ÷ entry
qty         = min(qty_risk, qty_alloc)          # binding cap is logged
```

## Research pipeline

1. **Thesis** — falsifiable statement, session gap, catalysts, invalidation rules.
2. **Inputs** — macro (SPY/QQQ tape + breadth), fundamentals (neutral baseline / PE-TTM when
   present), technicals (OR location, momentum, breakout volume, extension), sentiment
   (pre-market / after-hours / overnight drift + session gap). Each factor is scored 0–100 as
   *favorability for the candidate direction*, so a bearish tape scores high for a short.
3. **Outlook** — bias (candidate direction only if composite ≥ 62), 1-ATR expected move,
   up/down scenarios, confidence.
4. **Vehicle & expectancy** — `p_win = 0.33 + (composite − 50) × 0.005` (capped 0.62),
   `E = p_win × 2R − (1 − p_win) − 0.05`; PASS at `E ≥ 0.15R`. Highest-E candidate wins.

## Running

```bash
# 1) Evaluate the live snapshot, log the decision, never submit
python src/runner.py --dry-run

# 2) Replay the same session at 03:00 SGT (regular session) to exercise the ENTER path
python src/runner.py --dry-run \
  --snapshot tests/fixtures/replay_2026-11-18T0300SGT.json \
  --simulate-time 2026-11-18T03:00:00+08:00

# 3) Paper mode: write an order intent for review (no broker call)
python src/runner.py --paper --acc-id 5829755 --confirm-paper
```

* `--simulate-time` is for verification only and is flagged as `simulated_clock` in the log.
* `--paper` refuses any account other than the configured simulated account and writes
  `logs/pending_orders/order_<stamp>.json`. Submission would be a separate
  `mcp__moomoo__sim_trade_input_order` call after human review.

### Refreshing `logs/market_snapshot.json`

Use the moomoo-mcp tools in this order:

1. `quote_market_snapshot` for `["US.SPY","US.QQQ","US.NVDA"]` → last/open/high/low/prev_close,
   `bid_price`/`ask_price`, `update_time`, `pe_ttm`, extended-hours blocks.
2. `quote_cur_kline` `ktype=8` (30-min), `autype=0`, `num≈20` → keep only bars of the current
   US trading date, map to `{t,o,h,l,c,v}` (oldest first).
3. `quote_cur_kline` `ktype=2` (daily), `autype=0`, `num≈30` → `daily_bars` high/low/close for ATR.
4. Set `quote_time_sgt` from `update_time` (ms → SGT) so the freshness check is meaningful.

## Decision log schema (`logs/decisions.jsonl`)

```
ts_utc, ts_sgt, sgt_date, run_mode, simulated_clock
session { now_sgt, trading_date, session, mode, tradeable, entries_allowed, order_policy,
          flatten_deadline_sgt, checks_stop_sgt, holiday, half_day, reason }
calendar { holiday, half_day, flatten_deadline_sgt, checks_stop_sgt }
data { source, captured_at_sgt, data_age_min,
       market { symbol: { last, prev_close, bid, ask, spread_pct, quote_time_sgt } } }
research [ { symbol, name, asset_class, last,
             steps { thesis, factors, composite, outlook, vehicle }, orb, expectancy } ]
errors, decision, rationale, selected, thesis,
invalidation_checks [ { rule, value, limit, pass, detail } ],
risk_checks        [ { rule, value, limit, pass, detail } ],
sizing { qty, notional, risk_amount, risk_pct_equity, alloc_pct, binding },
order { status, submitted, acc_id, market_id, symbol, side, qty, order_type, limit_price,
        time_in_force, stop_loss, take_profit, brackets, est_notional },
pnl { equity_before, daily_pnl, daily_drawdown_pct, peak_drawdown_pct,
      kill_switch_active, peak_halt_active }
```

`decision` is one of `ENTER_LONG`, `ENTER_SHORT`, `NO_TRADE`, `BLOCKED_SESSION`,
`BLOCKED_RISK`, `FLATTEN_ONLY`, `AUDIT_ONLY`.

## Dashboard

Easiest: double-click `open_dashboard.cmd` in the repo root. It starts a local-only server
(`http://127.0.0.1:8765`, not reachable from the network) and opens
`dashboard/index.html`, which then auto-loads `logs/decisions.jsonl`. Close the server
window to stop it.

Opening `dashboard/index.html` directly also works, but browsers forbid a local page from
reading other local files, so click **Choose file** and pick `logs/decisions.jsonl`.

It shows equity / daily P&L / drawdown / kill switch, the session state, per-symbol research
scores, the selected thesis, invalidation and risk check tables, the order intent, and the
decision history. Everything is parsed locally; no data leaves the browser.

## Share a read-only snapshot

The dashboard is static. To give someone a link, export a single file with the log embedded:

```bash
python src/export_dashboard.py
# wrote dashboard/share.html (2 records, 50,512 bytes)
```

`dashboard/share.html` opens with a double-click (no server, no file picker) and can be
uploaded to any static host. The exporter redacts the paper account id by default; pass
`--keep-account` only if the audience should see it.

Hosting options, public by default:

| Host | Effort | Notes |
|---|---|---|
| Qoder Sites | one command | integrated in Qoder; can be published and re-published |
| Netlify Drop (`app.netlify.com/drop`) | drag one file | instant HTTPS URL, no account needed to start |
| GitHub Pages | needs a repo | good if the repo is already on GitHub |
| `cloudflared tunnel --url http://127.0.0.1:8765` | temporary | live (not a snapshot), only while your PC and server run |

A share page exposes the whole decision log (theses, prices, risk checks) and is marked
`noindex`. Treat the link as public unless the host adds access control.

## Verification (2026-11-18)

* Live dry run at **14:07 SGT** → `CLOSED` gap, `BLOCKED_SESSION`; research still computed
  (SPY composite 66.2 SHORT, QQQ 64.7 SHORT, NVDA no breakout) and the 0.685% after-hours
  limit drift was flagged — the session gate blocked the intent.
* Replay at **03:00 SGT** (regular session) → `ENTER_SHORT` **SPY 20 sh @ 731.05**, stop
  **752.98**, target **687.19** (R:R 2.0), 14.62% allocation, 0.44% risk; all 15 pre-trade
  checks passed, `status = DRY_RUN_NOT_SUBMITTED`, `submitted = false`.
* Calendar regression: Thanksgiving `AUDIT_ONLY`, Black Friday `FLATTEN` at 01:45 SGT,
  checks stopped at 06:30 SGT, weekend `CLOSED`, cutoff `CUTOFF`.

## Known limitations

* Fundamentals are a neutral baseline unless `pe_ttm` is present; no live earnings feed.
* ORB is evaluated on the completed/current session bars; extended-hours entries are limit-only
  and require the drift gate to pass.
* Brackets are recorded on the order intent; there is no intraday bracket-management loop in
  this baseline — a host agent executes/reviews intents through moomoo-mcp.
* The dashboard visualizes the log; it does not place or modify orders.
