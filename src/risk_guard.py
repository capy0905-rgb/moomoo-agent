"""Risk guard: SGT session calendar, pre-trade limits, sizing, kill switches.

All session and calendar decisions are made in Asia/Singapore time, which is
the competition clock. The guard is pure logic: it never places orders and
never talks to the broker.

Key rules implemented here
--------------------------
* Pre-market 17:00-22:30 SGT and after-hours 05:00-09:00 SGT: limit orders only.
* Regular 22:30-05:00 SGT (+1d): market or limit orders.
* Closed gap 09:00-17:00 SGT: no entries.
* Competition calendar: Thanksgiving, Black Friday half-day, weekend, hard cutoff.
* Max ticker allocation 15% of equity, gross exposure 45%.
* Max spread 0.15%.
* Daily drawdown kill switch at 2%; peak-to-trough halt at 8%.
* Every entry must carry a stop and a target.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

try:
    SGT = ZoneInfo("Asia/Singapore")
except ZoneInfoNotFoundError:  # Windows without the IANA tzdata package; SGT has no DST
    SGT = timezone(timedelta(hours=8))


# --------------------------------------------------------------------------- #
# session / calendar
# --------------------------------------------------------------------------- #
@dataclass
class SessionState:
    now_sgt: str
    trading_date: str
    session: str                  # PRE_MARKET | REGULAR | AFTER_HOURS | CLOSED
    mode: str                     # TRADE | LIMIT_ONLY | FLATTEN | AUDIT_ONLY | CLOSED | CUTOFF
    tradeable: bool
    entries_allowed: bool
    order_policy: str             # market-or-limit | limit-only | none
    flatten_deadline_sgt: str | None = None
    checks_stop_sgt: str | None = None
    holiday: bool = False
    half_day: bool = False
    reason: str = ""

    def as_dict(self) -> dict:
        return {
            "now_sgt": self.now_sgt,
            "trading_date": self.trading_date,
            "session": self.session,
            "mode": self.mode,
            "tradeable": self.tradeable,
            "entries_allowed": self.entries_allowed,
            "order_policy": self.order_policy,
            "flatten_deadline_sgt": self.flatten_deadline_sgt,
            "checks_stop_sgt": self.checks_stop_sgt,
            "holiday": self.holiday,
            "half_day": self.half_day,
            "reason": self.reason,
        }


def parse_dt(value: str) -> datetime:
    dt = datetime.fromisoformat(value)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=SGT)
    return dt.astimezone(SGT)


def _hhmm(value: str) -> time:
    h, m = value.split(":")
    return time(int(h), int(m))


def _fmt(dt: datetime | None) -> str | None:
    return dt.isoformat(timespec="seconds") if dt else None


def classify_session(now_sgt: datetime, config: dict) -> SessionState:
    """Resolve the session and competition status for an SGT timestamp."""
    now = now_sgt.astimezone(SGT)
    cal = config["calendar"]
    comp = config["meta"]["competition"]
    notes = cal.get("notes", {})

    start = parse_dt(comp["start_sgt"])
    cutoff = parse_dt(comp["hard_cutoff_sgt"])

    def state(session: str, mode: str, tradeable: bool, entries: bool, policy: str,
              reason: str, **kw: Any) -> SessionState:
        # The US trading day runs 17:00 SGT -> 09:00 SGT next day; before 09:00
        # the session belongs to the previous SGT calendar date.
        trading_date = now.date() if now.time() >= time(9, 0) else now.date() - timedelta(days=1)
        return SessionState(
            now_sgt=_fmt(now),
            trading_date=trading_date.isoformat(),
            session=session,
            mode=mode,
            tradeable=tradeable,
            entries_allowed=entries,
            order_policy=policy,
            reason=reason,
            **kw,
        )

    if now < start:
        return state("CLOSED", "CLOSED", False, False, "none", "Competition has not started.")
    if now > cutoff:
        return state("CLOSED", "CUTOFF", False, False, "none",
                     "Competition hard cutoff passed (2026-11-30 23:59 SGT).")

    trading_date = now.date() if now.time() >= time(9, 0) else now.date() - timedelta(days=1)
    if cal.get("weekend_closed", True) and trading_date.weekday() >= 5:
        return state("CLOSED", "CLOSED", False, False, "none", "Weekend: US markets closed.")

    if trading_date.isoformat() in cal.get("closed_dates_sgt", []):
        note = notes.get(trading_date.isoformat(), "Market holiday.")
        return state("CLOSED", "AUDIT_ONLY", False, False, "none", note, holiday=True)

    half_day = next(
        (h for h in cal.get("half_days", []) if h["date_sgt"] == trading_date.isoformat()),
        None,
    )
    if half_day:
        flatten_by = parse_dt(half_day["flatten_by_sgt"])
        checks_stop = parse_dt(half_day["checks_stop_sgt"])
        entries_blocked_at = flatten_by - timedelta(
            minutes=int(config["risk"].get("flatten_lead_minutes", 30))
        )
        if now >= checks_stop:
            return state("CLOSED", "CLOSED", False, False, "none",
                         f"{half_day['label']}: trading checks stopped at {_fmt(checks_stop)}.",
                         half_day=True)
        if now >= entries_blocked_at:
            return state("AFTER_HOURS", "FLATTEN", False, False, "limit-only",
                         f"{half_day['label']}: flatten-only window; no new entries.",
                         half_day=True,
                         flatten_deadline_sgt=_fmt(flatten_by),
                         checks_stop_sgt=_fmt(checks_stop))
        if now >= _hhmm_datetime(now, "17:00"):
            session = _clock_session(now)
            return state(session, "LIMIT_ONLY" if session != "REGULAR" else "TRADE",
                         True, True, "limit-only" if session != "REGULAR" else "market-or-limit",
                         f"{half_day['label']}: new entries blocked from {_fmt(entries_blocked_at)}.",
                         half_day=True,
                         flatten_deadline_sgt=_fmt(flatten_by),
                         checks_stop_sgt=_fmt(checks_stop))

    session = _clock_session(now)
    if session == "CLOSED":
        return state("CLOSED", "CLOSED", False, False, "none",
                     "US market closed (09:00-17:00 SGT gap). Scans allowed, no entries.")
    if session == "REGULAR":
        return state("REGULAR", "TRADE", True, True, "market-or-limit",
                     "Regular session: active execution window.")
    if session == "PRE_MARKET":
        return state("PRE_MARKET", "LIMIT_ONLY", True, True, "limit-only",
                     "Pre-market: scan or strict limit orders; market orders blocked.")
    return state("AFTER_HOURS", "LIMIT_ONLY", True, True, "limit-only",
                 "After-hours: scan or strict limit orders; market orders blocked.")


def _hhmm_datetime(now: datetime, hhmm: str) -> datetime:
    return now.replace(hour=_hhmm(hhmm).hour, minute=_hhmm(hhmm).minute,
                       second=0, microsecond=0)


def _clock_session(now: datetime) -> str:
    t = now.time()
    if time(17, 0) <= t < time(22, 30):
        return "PRE_MARKET"
    if t >= time(22, 30) or t < time(5, 0):
        return "REGULAR"
    if time(5, 0) <= t < time(9, 0):
        return "AFTER_HOURS"
    return "CLOSED"


# --------------------------------------------------------------------------- #
# risk state
# --------------------------------------------------------------------------- #
DEFAULT_CASH = 0.0


def default_state(starting_equity: float, today: date) -> dict:
    return {
        "starting_equity": starting_equity,
        "cash": starting_equity,
        "equity": starting_equity,
        "peak_equity": starting_equity,
        "day": today.isoformat(),
        "day_start_equity": starting_equity,
        "daily_realized_pnl": 0.0,
        "positions": [],
        "kill_switch": {"active": False, "date": None, "reason": None},
        "peak_halt": {"active": False, "date": None, "reason": None},
    }


def load_state(path: Path, starting_equity: float, today: date) -> dict:
    if path.exists():
        with path.open("r", encoding="utf-8") as fh:
            state = json.load(fh)
        base = default_state(starting_equity, today)
        base.update(state)
        state = base
    else:
        state = default_state(starting_equity, today)

    if state["day"] != today.isoformat():
        state["day"] = today.isoformat()
        state["day_start_equity"] = state.get("equity", starting_equity)
        state["daily_realized_pnl"] = 0.0
        if state["kill_switch"].get("date") != today.isoformat():
            state["kill_switch"] = {"active": False, "date": None, "reason": None}
    return state


def save_state(path: Path, state: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    with tmp.open("w", encoding="utf-8") as fh:
        json.dump(state, fh, indent=2)
    tmp.replace(path)


def mark_to_market(state: dict, prices: dict[str, float]) -> dict:
    """Recompute equity from cash + positions using latest prices."""
    mv = 0.0
    for pos in state.get("positions", []):
        px = float(prices.get(pos["symbol"], pos.get("last_price") or pos["avg_price"]))
        pos["last_price"] = px
        pos["market_value"] = round(float(pos["qty"]) * px, 2)
        mv += pos["market_value"]
    state["market_value"] = round(mv, 2)
    state["equity"] = round(float(state.get("cash", 0.0)) + mv, 2)
    state["peak_equity"] = round(max(float(state.get("peak_equity", 0.0)), state["equity"]), 2)
    return state


def drawdown_metrics(state: dict, config: dict) -> dict:
    equity = float(state["equity"])
    peak = float(state.get("peak_equity") or equity) or equity
    day_start = float(state.get("day_start_equity") or equity) or equity
    daily_dd_pct = (equity - day_start) / day_start * 100.0 if day_start else 0.0
    peak_dd_pct = (equity - peak) / peak * 100.0 if peak else 0.0

    risk = config["risk"]
    if daily_dd_pct <= -abs(float(risk["daily_kill_switch_pct"])):
        state["kill_switch"] = {
            "active": True,
            "date": state["day"],
            "reason": f"Daily drawdown {daily_dd_pct:.2f}% breached "
                      f"-{risk['daily_kill_switch_pct']}% limit",
        }
    if peak_dd_pct <= -abs(float(risk["peak_drawdown_halt_pct"])):
        state["peak_halt"] = {
            "active": True,
            "date": state["day"],
            "reason": f"Peak drawdown {peak_dd_pct:.2f}% breached "
                      f"-{risk['peak_drawdown_halt_pct']}% limit",
        }

    return {
        "equity": round(equity, 2),
        "day_start_equity": round(day_start, 2),
        "daily_pnl": round(equity - day_start, 2),
        "daily_drawdown_pct": round(daily_dd_pct, 3),
        "peak_equity": round(peak, 2),
        "peak_drawdown_pct": round(peak_dd_pct, 3),
        "kill_switch_active": bool(state["kill_switch"]["active"]),
        "peak_halt_active": bool(state["peak_halt"]["active"]),
    }


# --------------------------------------------------------------------------- #
# sizing and pre-trade checks
# --------------------------------------------------------------------------- #
@dataclass
class Sizing:
    qty: int
    notional: float
    risk_amount: float
    risk_pct_equity: float
    alloc_pct: float
    binding: str

    def as_dict(self) -> dict:
        return {
            "qty": self.qty,
            "notional": round(self.notional, 2),
            "risk_amount": round(self.risk_amount, 2),
            "risk_pct_equity": round(self.risk_pct_equity, 3),
            "alloc_pct": round(self.alloc_pct, 2),
            "binding": self.binding,
        }


@dataclass
class RiskCheck:
    rule: str
    value: Any
    limit: Any
    passed: bool
    detail: str = ""

    def as_dict(self) -> dict:
        return {"rule": self.rule, "value": self.value, "limit": self.limit,
                "pass": self.passed, "detail": self.detail}


def size_position(equity: float, entry: float, stop: float, config: dict,
                  alloc_pct_cap: float | None = None) -> Sizing:
    risk = config["risk"]
    per_share = abs(entry - stop)
    if per_share <= 0 or entry <= 0 or equity <= 0:
        return Sizing(0, 0.0, 0.0, 0.0, 0.0, "invalid")

    risk_amount = equity * float(risk["risk_per_trade_pct"]) / 100.0
    qty_risk = int(risk_amount // per_share)

    cap_pct = float(alloc_pct_cap if alloc_pct_cap is not None
                    else risk["max_ticker_allocation_pct"])
    alloc_cap = equity * cap_pct / 100.0
    qty_alloc = int(alloc_cap // entry)
    qty = max(0, min(qty_risk, qty_alloc))
    binding = "risk" if qty_risk <= qty_alloc else "allocation"
    notional = qty * entry
    return Sizing(
        qty=qty,
        notional=notional,
        risk_amount=qty * per_share,
        risk_pct_equity=(qty * per_share) / equity * 100.0,
        alloc_pct=notional / equity * 100.0,
        binding=binding,
    )


def current_exposure(state: dict) -> tuple[float, dict[str, float]]:
    by_symbol: dict[str, float] = {}
    for pos in state.get("positions", []):
        by_symbol[pos["symbol"]] = by_symbol.get(pos["symbol"], 0.0) + float(
            pos.get("market_value", 0.0)
        )
    return sum(by_symbol.values()), by_symbol


def pre_trade_checks(
    *,
    symbol: str,
    quote: dict,
    session: SessionState,
    state: dict,
    sizing: Sizing,
    order_type: str,
    direction: str,
    entry: float,
    stop: float,
    target: float,
    config: dict,
    acc_id: str,
    data_age_min: float,
) -> list[RiskCheck]:
    """Every check that must pass before an order intent becomes submittable."""
    risk = config["risk"]
    equity = float(state["equity"])
    checks: list[RiskCheck] = []

    checks.append(RiskCheck(
        "paper_account", acc_id, config["account"]["acc_id"],
        acc_id == config["account"]["acc_id"] and config["account"]["type"] == "simulated",
        "paper account verified" if acc_id == config["account"]["acc_id"] else "NOT the configured paper account",
    ))
    checks.append(RiskCheck(
        "session_entries_allowed", session.session, "trading mode",
        session.entries_allowed, session.reason,
    ))
    policy_ok = not (session.order_policy == "limit-only" and order_type == "market")
    checks.append(RiskCheck(
        "order_type_policy", order_type, session.order_policy, policy_ok,
        "market orders blocked outside the regular session" if not policy_ok else "ok",
    ))
    checks.append(RiskCheck(
        "kill_switch_inactive", state["kill_switch"]["active"], False,
        not state["kill_switch"]["active"],
        state["kill_switch"].get("reason") or "clear",
    ))
    checks.append(RiskCheck(
        "peak_halt_inactive", state["peak_halt"]["active"], False,
        not state["peak_halt"]["active"],
        state["peak_halt"].get("reason") or "clear",
    ))
    checks.append(RiskCheck(
        "daily_drawdown_limit", round(
            (equity - float(state["day_start_equity"])) / float(state["day_start_equity"]) * 100, 3
        ) if state["day_start_equity"] else 0.0,
        f">-{risk['daily_kill_switch_pct']}%",
        (equity - float(state["day_start_equity"])) / float(state["day_start_equity"]) * 100
        > -abs(float(risk["daily_kill_switch_pct"])) if state["day_start_equity"] else True,
    ))

    bid = quote.get("bid")
    ask = quote.get("ask")
    if bid and ask and ask > bid:
        spread_pct = (ask - bid) / ((ask + bid) / 2.0) * 100.0
        checks.append(RiskCheck(
            "spread_limit", round(spread_pct, 4), f"<={risk['max_spread_pct']}%",
            spread_pct <= float(risk["max_spread_pct"]),
            f"bid {bid} / ask {ask}",
        ))
    else:
        checks.append(RiskCheck("spread_limit", None, f"<={risk['max_spread_pct']}%", False,
                                "missing bid/ask - cannot verify liquidity"))

    checks.append(RiskCheck(
        "price_floor", quote.get("last"), f">={risk['min_price']}",
        float(quote.get("last") or 0) >= float(risk["min_price"]),
    ))

    max_age = (config["meta"]["staleness"]["max_data_age_min_regular"]
               if session.session == "REGULAR"
               else config["meta"]["staleness"]["max_data_age_min_extended"])
    checks.append(RiskCheck(
        "data_freshness_min", round(float(data_age_min), 2), f"<={max_age}",
        float(data_age_min) <= float(max_age),
    ))

    if order_type == "limit":
        limit_px = ask if direction == "LONG" else bid
        drift_pct = abs(float(limit_px or entry) - entry) / entry * 100.0
        checks.append(RiskCheck(
            "limit_price_drift", round(drift_pct, 4), "<=0.25%", drift_pct <= 0.25,
            f"limit {limit_px} vs signal entry {entry}",
        ))

    brackets_ok = stop is not None and target is not None and (
        (direction == "LONG" and stop < entry < target)
        or (direction == "SHORT" and target < entry < stop)
    )
    checks.append(RiskCheck(
        "brackets_present", {"stop": stop, "target": target}, "stop<entry<target",
        brackets_ok, "mandatory stop-loss and take-profit",
    ))
    rr = abs(target - entry) / abs(entry - stop) if stop != entry else 0.0
    checks.append(RiskCheck(
        "reward_risk", round(rr, 2), f">={risk['take_profit_r_multiple']}",
        rr + 1e-2 >= float(risk["take_profit_r_multiple"]),
    ))

    checks.append(RiskCheck("size_positive", sizing.qty, ">=1", sizing.qty >= 1,
                            f"binding cap: {sizing.binding}"))

    _, by_symbol = current_exposure(state)
    projected_symbol = by_symbol.get(symbol, 0.0) + sizing.notional
    alloc_pct = projected_symbol / equity * 100.0 if equity else 0.0
    checks.append(RiskCheck(
        "ticker_allocation_pct", round(alloc_pct, 2),
        f"<={risk['max_ticker_allocation_pct']}%",
        alloc_pct <= float(risk["max_ticker_allocation_pct"]),
    ))

    gross, _ = current_exposure(state)
    gross_pct = (gross + sizing.notional) / equity * 100.0 if equity else 0.0
    checks.append(RiskCheck(
        "gross_exposure_pct", round(gross_pct, 2),
        f"<={risk['max_gross_exposure_pct']}%",
        gross_pct <= float(risk["max_gross_exposure_pct"]),
    ))

    return checks
