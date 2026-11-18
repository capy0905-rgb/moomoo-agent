"""Four-step research pipeline: Thesis -> Factors -> Outlook -> Vehicle & Expectancy.

Step 1  Thesis        falsifiable statement + invalidation rules
Step 2  Factors       macro, fundamentals, technicals, sentiment, each scored
                      0-100 as *favorability for the candidate direction*
Step 3  Outlook       bias, expected move, scenarios, confidence
Step 4  Vehicle       tradable vehicle + expectancy gate

The candidate direction comes from the ORB setup, so a bearish tape scores
high for a short candidate and low for a long candidate. Pure stdlib; read-only.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from strategy_orb import ORBSetup, evaluate_orb


# --------------------------------------------------------------------------- #
# data types
# --------------------------------------------------------------------------- #
@dataclass
class Factor:
    score: float
    weight: float
    notes: str

    def as_dict(self) -> dict:
        return {"score": round(self.score, 1), "weight": self.weight, "notes": self.notes}


@dataclass
class ResearchResult:
    symbol: str
    name: str
    asset_class: str
    last: float
    orb: ORBSetup
    thesis: dict
    factors: dict[str, Factor]
    composite: float
    bias: str
    outlook: dict
    expectancy: dict
    vehicle: dict = field(default_factory=dict)

    def as_dict(self) -> dict:
        return {
            "symbol": self.symbol,
            "name": self.name,
            "asset_class": self.asset_class,
            "last": self.last,
            "steps": {
                "thesis": self.thesis,
                "factors": {k: v.as_dict() for k, v in self.factors.items()},
                "composite": round(self.composite, 2),
                "outlook": self.outlook,
                "vehicle": self.vehicle,
            },
            "orb": self.orb.as_dict(),
            "expectancy": self.expectancy,
        }


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
def _clamp(value: float, lo: float = 0.0, hi: float = 100.0) -> float:
    return max(lo, min(hi, value))


def day_change_pct(data: dict) -> float:
    last = float(data.get("last") or 0)
    prev = float(data.get("prev_close") or 0)
    return (last - prev) / prev * 100.0 if prev else 0.0


def macro_context(snapshot: dict) -> dict:
    symbols = snapshot.get("symbols", {})
    changes = {sym: day_change_pct(d) for sym, d in symbols.items()}
    breadth = sum(1 for c in changes.values() if c > 0) / len(changes) if changes else 0.5
    return {
        "changes": changes,
        "spy_pct": changes.get("US.SPY"),
        "qqq_pct": changes.get("US.QQQ"),
        "breadth": breadth,
    }


# --------------------------------------------------------------------------- #
# step 2 factors (direction-aware)
# --------------------------------------------------------------------------- #
def score_factors(symbol_meta: dict, data: dict, orb: ORBSetup,
                  macro: dict, config: dict, direction: str) -> dict[str, Factor]:
    weights = config["research"]["weights"]
    sign = 1.0 if direction == "LONG" else (-1.0 if direction == "SHORT" else 0.0)
    last = float(data.get("last") or 0)
    prev = float(data.get("prev_close") or last)

    # Macro: index tape + breadth. Negative tape favours shorts.
    tape = 0.6 * (macro.get("spy_pct") or 0.0) + 0.4 * (macro.get("qqq_pct") or 0.0)
    beta_coef = 8.0 if symbol_meta.get("asset_class") == "index_etf" else 5.0
    breadth_term = (macro.get("breadth", 0.5) - 0.5) * 20.0
    macro_score = _clamp(50 + sign * (tape * beta_coef + breadth_term))
    macro_notes = (
        f"SPY {macro.get('spy_pct'):+.2f}%, QQQ {macro.get('qqq_pct'):+.2f}%, "
        f"breadth {macro.get('breadth', 0.5) * 100:.0f}% advancing "
        f"(scored for a {direction.lower() or 'neutral'} candidate)"
    ) if macro.get("spy_pct") is not None else "no index reference data"

    # Fundamentals: baseline has no live earnings/valuation feed wired in.
    pe_ttm = data.get("pe_ttm")
    fund_score, fund_notes = 50.0, "No live fundamentals feed wired into baseline; neutral."
    if symbol_meta.get("asset_class") == "single_stock" and pe_ttm:
        fund_notes = f"PE(TTM) {pe_ttm:.1f}; no earnings event feed; neutral."
    elif symbol_meta.get("asset_class") == "index_etf":
        fund_notes = "Index basket - no single-name earnings risk; neutral."

    # Technicals: ORB state, trend alignment, volume, extension.
    tech = 50.0
    if orb.or_high is not None and orb.or_low is not None and orb.or_high > orb.or_low:
        or_mid = (orb.or_high + orb.or_low) / 2.0
        loc_pct = (last - or_mid) / or_mid * 100.0
        tech += _clamp(loc_pct * sign * 12, -18, 18)
    momentum_agrees = (
        (sign > 0 and orb.momentum == "bullish") or (sign < 0 and orb.momentum == "bearish")
    )
    momentum_conflicts = (
        (sign > 0 and orb.momentum == "bearish") or (sign < 0 and orb.momentum == "bullish")
    )
    if momentum_agrees:
        tech += 8
    elif momentum_conflicts:
        tech -= 8
    if orb.volume_ratio and orb.volume_ratio >= config["strategy"]["breakout_volume_ratio_min"]:
        tech += 6
    if "FAILED_BREAKOUT" in orb.reason:
        tech -= 15
    if "MEAN_REVERSION_RISK" in orb.reason:
        tech -= 10
    if orb.action != "NONE":
        tech += 6
    tech_score = _clamp(tech)
    tech_notes = (
        f"{orb.action}; {orb.reason.split(':')[0]}; momentum {orb.momentum}; "
        f"extension {orb.extension_atr} ATR; volume {orb.volume_ratio or 0}x OR avg"
    )

    # Sentiment: extended-hours direction. A positive drift is adverse for a short.
    sent_parts = []
    for key in ("pre_market", "after_market", "overnight"):
        block = data.get(key) or {}
        if block.get("change_rate") is not None:
            sent_parts.append(float(block["change_rate"]))
    sent_chg = sum(sent_parts) / len(sent_parts) if sent_parts else 0.0
    gap = (float(data.get("open") or last) - prev) / prev * 100.0 if prev else 0.0
    sent_score = _clamp(50 + sign * (sent_chg * 6 + gap * 4))
    sent_notes = (
        f"extended-hours avg {sent_chg:+.2f}%, session gap {gap:+.2f}%"
        if sent_parts else f"no extended-hours prints; session gap {gap:+.2f}%"
    )

    return {
        "macro": Factor(macro_score, float(weights["macro"]), macro_notes),
        "fundamentals": Factor(fund_score, float(weights["fundamentals"]), fund_notes),
        "technicals": Factor(tech_score, float(weights["technicals"]), tech_notes),
        "sentiment": Factor(sent_score, float(weights["sentiment"]), sent_notes),
    }


# --------------------------------------------------------------------------- #
# step 1 thesis and step 3 outlook
# --------------------------------------------------------------------------- #
def build_thesis(symbol_meta: dict, data: dict, orb: ORBSetup,
                 composite: float, bias: str, config: dict) -> dict:
    last = float(data.get("last") or 0)
    prev = float(data.get("prev_close") or last)
    gap = (float(data.get("open") or last) - prev) / prev * 100.0 if prev else 0.0

    if orb.action in ("LONG", "SHORT"):
        statement = (
            f"{symbol_meta['symbol']} is set up {orb.action.lower()} via an opening-range "
            f"{'breakout' if orb.action == 'LONG' else 'breakdown'} at ${orb.entry:.2f} "
            f"(OR ${orb.or_low:.2f}-${orb.or_high:.2f}) on {orb.volume_ratio or 0:.2f}x volume "
            f"with {orb.momentum} momentum; expect ~1 ATR (${orb.atr:.2f}) of follow-through "
            f"intraday. Conviction {composite:.0f}/100 -> {bias}."
        )
        invalidation = [
            f"stop hit: price {'below' if orb.action == 'LONG' else 'above'} ${orb.stop:.2f}",
            "failed breakout retest: close back inside the opening range",
            f"spread > {config['risk']['max_spread_pct']}% of mid",
            f"abnormal volatility spike: ATR% > {config['strategy']['max_atr_pct']}%",
            "session policy violated (e.g. market order outside the regular session)",
        ]
    else:
        statement = (
            f"{symbol_meta['symbol']} has no qualified ORB entry ({orb.reason}); "
            f"gap {gap:+.2f}%, last ${last:.2f} vs OR "
            f"${orb.or_low or 0:.2f}-${orb.or_high or 0:.2f}. Flat until price breaks and holds "
            "outside the range on above-average volume."
        )
        invalidation = [
            "any entry attempt before a confirmed breakout with volume",
            f"spread > {config['risk']['max_spread_pct']}% of mid",
            "data older than the session staleness limit",
        ]

    catalysts = [
        f"session gap {gap:+.2f}% vs prior close ${prev:.2f}",
        (f"opening range ${orb.or_low:.2f}-${orb.or_high:.2f} "
         f"(${orb.or_high - orb.or_low:.2f} wide)")
        if orb.or_low and orb.or_high else "opening range unavailable",
        f"{orb.momentum} momentum on EMA{config['strategy']['momentum_ema_fast']}/"
        f"EMA{config['strategy']['momentum_ema_slow']}",
    ]
    return {
        "statement": statement,
        "direction": bias,
        "catalysts": catalysts,
        "invalidation_rules": invalidation,
    }


def build_outlook(orb: ORBSetup, composite: float, factors: dict[str, Factor],
                  bias: str, config: dict) -> dict:
    atr = orb.atr or 0.0
    last = orb.entry or 0.0
    strong = sum(1 for f in factors.values() if f.score > 60)
    if composite >= 75 and strong >= 3:
        confidence = "HIGH"
    elif composite >= float(config["research"]["min_conviction"]):
        confidence = "MEDIUM"
    else:
        confidence = "LOW"
    return {
        "bias": bias,
        "horizon": "intraday",
        "expected_move": f"+/- ${atr:.2f} ({atr / last * 100:.2f}%) at 1 ATR" if last else "n/a",
        "scenario_up": (
            f"hold above ${orb.or_high:.2f}"
            + (f" -> target ${orb.target:.2f}" if orb.action == "LONG" else "")
            if orb.or_high else "n/a"
        ),
        "scenario_down": (
            f"hold below ${orb.or_low:.2f}"
            + (f" -> target ${orb.target:.2f}" if orb.action == "SHORT" else "")
            if orb.or_low else "n/a"
        ),
        "confidence": confidence,
    }


# --------------------------------------------------------------------------- #
# step 4 expectancy
# --------------------------------------------------------------------------- #
def expectation(composite: float, config: dict) -> dict:
    r_cfg = config["research"]
    risk_cfg = config["risk"]
    p_win = float(r_cfg["win_prob_base"]) + (composite - 50.0) * float(
        r_cfg["win_prob_slope_per_point"]
    )
    p_win = max(0.0, min(float(r_cfg["win_prob_cap"]), p_win))
    r_multiple = float(risk_cfg["take_profit_r_multiple"])
    cost = float(risk_cfg["expectancy_cost_r"])
    e_r = p_win * r_multiple - (1 - p_win) - cost
    return {
        "p_win": round(p_win, 3),
        "r_multiple": r_multiple,
        "cost_r": cost,
        "expectancy_r": round(e_r, 3),
        "min_required_r": float(risk_cfg["min_expectancy_r"]),
        "verdict": "PASS" if e_r >= float(risk_cfg["min_expectancy_r"]) else "REJECT",
    }


# --------------------------------------------------------------------------- #
# entry point
# --------------------------------------------------------------------------- #
def evaluate_symbol(symbol_meta: dict, data: dict, macro: dict,
                    config: dict, session: Any = None) -> ResearchResult:
    orb = evaluate_orb(symbol_meta["symbol"], data, config)
    direction = orb.action if orb.action in ("LONG", "SHORT") else "NEUTRAL"
    factors = score_factors(symbol_meta, data, orb, macro, config, direction)
    composite = sum(f.score * f.weight for f in factors.values())
    min_conviction = float(config["research"]["min_conviction"])
    bias = direction if composite >= min_conviction else "NEUTRAL"
    thesis = build_thesis(symbol_meta, data, orb, composite, bias, config)
    outlook = build_outlook(orb, composite, factors, bias, config)
    exp = expectation(composite, config)

    vehicle = {
        "symbol": symbol_meta["symbol"],
        "asset_class": symbol_meta.get("asset_class"),
        "max_allocation_pct": symbol_meta.get("max_allocation_pct"),
        "expectancy_r": exp["expectancy_r"],
        "verdict": exp["verdict"],
    }
    if orb.action == "NONE":
        vehicle["verdict"] = "NO_TRADE"
        vehicle["reason"] = orb.reason
    elif composite < min_conviction:
        vehicle["verdict"] = "REJECT"
        vehicle["reason"] = f"conviction {composite:.1f} < {min_conviction}"
    elif exp["verdict"] == "REJECT":
        vehicle["reason"] = "expectancy below threshold"

    return ResearchResult(
        symbol=symbol_meta["symbol"],
        name=symbol_meta.get("name", symbol_meta["symbol"]),
        asset_class=symbol_meta.get("asset_class", "unknown"),
        last=float(data.get("last") or 0),
        orb=orb,
        thesis=thesis,
        factors=factors,
        composite=composite,
        bias=bias,
        outlook=outlook,
        expectancy=exp,
        vehicle=vehicle,
    )


def rank_candidates(results: list[ResearchResult]) -> list[ResearchResult]:
    """Tradeable candidates ranked by expectancy, then conviction."""
    live = [r for r in results if r.orb.action != "NONE" and r.vehicle.get("verdict") == "PASS"]
    return sorted(live, key=lambda r: (r.expectancy["expectancy_r"], r.composite), reverse=True)
