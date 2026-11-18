---
name: research-pipeline
description: Run a 4-step research pipeline (Thesis -> Multi-Factor Inputs -> Outlook -> Vehicle Selection & Expectancy) for a single symbol, producing an auditable thesis with explicit invalidation rules. Use for every proposed entry before risk review.
version: 1.0.0
inputs:
  - symbol and its market snapshot (quote, 5m bars, daily bars, bid/ask)
  - session state from risk_guard.classify_session
  - config/trading_config.json
outputs:
  - thesis (statement + catalysts + invalidation rules)
  - factor scores (macro, fundamentals, technicals, sentiment) and composite
  - outlook (bias, horizon, scenarios)
  - vehicle + expectancy verdict
references:
  - src/research_pipeline.py
  - skills/risk_auditor.md
---

# Research Pipeline Skill

Four steps, in order. Each step must cite measured inputs; no invented facts.
If a factor has no live input, score it neutrally (50) and say why in `notes`.

## Step 1 - Thesis

Write one falsifiable statement in this shape:

> "\<SYMBOL\> is \<setup\> because \<evidence\>; I expect \<move\> by \<horizon\>;
> the idea is wrong if \<invalidation\>."

Required fields:
- `direction`: LONG / SHORT / NEUTRAL
- `catalysts`: at most 3, each tied to an observed input (gap, ORB, volume, session flow)
- `invalidation_rules`: at least 2, each mechanically checkable, e.g.
  - `spread_pct <= 0.15`
  - `close back inside opening range` (failed breakout retest)
  - `ATR% > max_atr_pct` (abnormal volatility spike)
  - `price < entry - stop_distance`
  - `session mode != TRADE/limit-only policy violated`

## Step 2 - Multi-Factor Inputs

Score each 0-100 (50 = neutral) and weight per `research.weights`:

| Factor | Inputs to use | Notes |
|---|---|---|
| Macro | SPY day change, QQQ day change, index breadth proxy, gap direction | 25% |
| Fundamentals | asset class, event risk, valuation/earnings proximity if known | 15% |
| Technicals | ORB status, EMA9/EMA21, location vs OR mid, ATR%, volume ratio | 40% |
| Sentiment | pre-market / after-hours / overnight direction, volume vs average | 20% |

Factors are scored as **favorability for the candidate direction** (a bearish tape scores
high for a short candidate). Composite = weighted mean. Bias equals the candidate direction
only when composite >= `min_conviction` (62); otherwise NEUTRAL and no trade.

Rules:
- For index ETFs, fundamentals default to neutral with the note "index basket, no
  single-name earnings risk".
- Never treat pre-market or after-hours prints as confirmation of regular-session trend;
  they are sentiment inputs only.

## Step 3 - Outlook

- `horizon`: intraday to 2 sessions.
- `expected_move`: anchored to ATR (e.g. 0.8-1.2 ATR day).
- `scenario_up` / `scenario_down`: price levels + what confirms each.
- `confidence`: maps composite to LOW / MEDIUM / HIGH. High requires >= 3 factors > 60.

## Step 4 - Vehicle Selection & Expectancy

Choose the tradable vehicle from `universe` (e.g. express broad beta via SPY/QQQ, or
higher-beta via NVDA). Reject if:
- spread > `max_spread_pct`
- price < `min_price`
- projected allocation > `max_allocation_pct` (15% of paper equity)
- projected gross exposure > `max_gross_exposure_pct`

Expectancy gate:

```
p_win  = min(win_prob_cap, win_prob_base + (composite - 50) * win_prob_slope_per_point)
E_r    = p_win * take_profit_r_multiple - (1 - p_win) - expectancy_cost_r
PASS   = E_r >= min_expectancy_r
```

Emit `PASS` or `REJECT` with the exact numbers. Do not round away a failure.

## Output format

```json
{
  "symbol": "US.SPY",
  "steps": {
    "thesis": {"statement": "...", "direction": "LONG", "catalysts": ["..."], "invalidation_rules": ["..."]},
    "factors": {
      "macro": {"score": 64, "weight": 0.25, "notes": "SPY -1.0% day, QQQ -1.3%, risk-off"},
      "fundamentals": {"score": 50, "weight": 0.15, "notes": "index basket, neutral"},
      "technicals": {"score": 71, "weight": 0.40, "notes": "ORB short with retest"},
      "sentiment": {"score": 38, "weight": 0.20, "notes": "after-hours bounce"}
    },
    "composite": 58.4,
    "outlook": {"bias": "NEUTRAL", "horizon": "intraday", "expected_move": "...", "confidence": "LOW"},
    "vehicle": {"symbol": "US.SPY", "expectancy_r": 0.12, "verdict": "REJECT", "reason": "below min_expectancy_r"}
  }
}
```

Stop at the first hard failure and hand off to `skills/risk_auditor.md` with full context.
