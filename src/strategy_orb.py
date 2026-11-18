"""Opening Range Breakout (ORB) baseline with momentum / mean-reversion filters.

Pure stdlib. The strategy consumes a normalized market snapshot (see
``src/market_data.py``) and never talks to the broker.

Inputs per symbol:
  bars_5m    : regular-session intraday bars, oldest first
  daily_bars : daily bars, oldest first (for ATR)
  last       : latest regular-session price

The output is an ``ORBSetup`` describing either a ready entry (with stop and
target) or the reason no entry exists. Every filter records a ``Check`` so the
decision log is auditable.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from statistics import median
from typing import Any


# --------------------------------------------------------------------------- #
# data types
# --------------------------------------------------------------------------- #
@dataclass
class Check:
    rule: str
    value: Any
    limit: Any
    passed: bool
    detail: str = ""

    def as_dict(self) -> dict:
        return {
            "rule": self.rule,
            "value": self.value,
            "limit": self.limit,
            "pass": self.passed,
            "detail": self.detail,
        }


@dataclass
class ORBSetup:
    action: str                     # LONG | SHORT | NONE
    reason: str
    entry: float | None = None
    stop: float | None = None
    target: float | None = None
    risk_per_share: float | None = None
    atr: float | None = None
    or_high: float | None = None
    or_low: float | None = None
    extension_atr: float | None = None
    momentum: str = "unknown"       # bullish | bearish | mixed
    volume_ratio: float | None = None
    bars_used: int = 0
    checks: list[Check] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "action": self.action,
            "reason": self.reason,
            "entry": self.entry,
            "stop": self.stop,
            "target": self.target,
            "risk_per_share": self.risk_per_share,
            "atr": self.atr,
            "opening_range": {"high": self.or_high, "low": self.or_low},
            "extension_atr": self.extension_atr,
            "momentum": self.momentum,
            "volume_ratio": self.volume_ratio,
            "bars_used": self.bars_used,
            "checks": [c.as_dict() for c in self.checks],
        }


# --------------------------------------------------------------------------- #
# small math helpers
# --------------------------------------------------------------------------- #
def _pct(a: float, b: float) -> float:
    return (a - b) / b * 100.0 if b else 0.0


def ema_series(values: list[float], period: int) -> list[float]:
    if not values:
        return []
    k = 2.0 / (period + 1.0)
    out = [values[0]]
    for v in values[1:]:
        out.append(v * k + out[-1] * (1.0 - k))
    return out


def _bar_get(bar: Any, *keys: str) -> float:
    for key in keys:
        if isinstance(bar, dict) and bar.get(key) is not None:
            return float(bar[key])
    raise KeyError(f"bar missing any of {keys}: {bar!r}")


def _bar_volume(bar: Any) -> float:
    if isinstance(bar, dict):
        for key in ("v", "volume"):
            if bar.get(key) is not None:
                return float(bar[key])
        return 0.0
    return float(bar[5]) if len(bar) > 5 else 0.0


def compute_atr(daily_bars: list[Any], period: int = 14) -> float | None:
    """Wilder ATR over the last ``period`` daily bars; None when data is thin."""
    if not daily_bars or len(daily_bars) < 3:
        return None
    bars = daily_bars[-(period + 1):]
    trs: list[float] = []
    prev_close: float | None = None
    for bar in bars:
        high = _bar_get(bar, "high", "high_price")
        low = _bar_get(bar, "low", "low_price")
        close = _bar_get(bar, "close", "close_price")
        if prev_close is None:
            trs.append(high - low)
        else:
            trs.append(max(high - low, abs(high - prev_close), abs(low - prev_close)))
        prev_close = close
    if not trs:
        return None
    atr = sum(trs) / len(trs)
    return atr if atr > 0 else None


def _infer_bar_minutes(bars: list[dict]) -> float:
    stamps = [float(b["t"]) for b in bars if isinstance(b, dict) and b.get("t")]
    if len(stamps) < 2:
        return 5.0
    deltas = [b - a for a, b in zip(stamps, stamps[1:]) if b > a]
    if not deltas:
        return 5.0
    return max(1.0, median(deltas) / 60_000.0)


# --------------------------------------------------------------------------- #
# opening range
# --------------------------------------------------------------------------- #
def compute_opening_range(
    bars: list[dict], minutes: int
) -> tuple[float, float, list[dict], list[dict]]:
    """Split bars into the opening-range window and the post-OR session."""
    if not bars:
        raise ValueError("no intraday bars")
    bar_minutes = _infer_bar_minutes(bars)
    or_count = max(1, int(round(minutes / bar_minutes)))
    or_bars = bars[:or_count]
    post_bars = bars[or_count:]
    or_high = max(_bar_get(b, "h", "high") for b in or_bars)
    or_low = min(_bar_get(b, "l", "low") for b in or_bars)
    return or_high, or_low, or_bars, post_bars


# --------------------------------------------------------------------------- #
# strategy
# --------------------------------------------------------------------------- #
def evaluate_orb(symbol: str, data: dict, config: dict) -> ORBSetup:
    """Evaluate the ORB + filters for one symbol. Read-only."""
    cfg = config["strategy"]
    risk_cfg = config["risk"]
    checks: list[Check] = []

    bars = data.get("bars_5m") or []
    if len(bars) < int(cfg.get("min_intraday_bars", 4)):
        return ORBSetup(
            action="NONE",
            reason=f"INSUFFICIENT_BARS: {len(bars)} intraday bars",
            checks=[Check("intraday_bars", len(bars), cfg.get("min_intraday_bars", 4), False)],
        )

    last = float(data.get("last") or _bar_get(bars[-1], "c", "close"))
    atr = compute_atr(data.get("daily_bars") or [], int(cfg.get("atr_period", 14)))
    if atr is None:
        # fall back to the observed session range; flagged in the checks below
        day_high = float(data.get("high") or max(_bar_get(b, "h", "high") for b in bars))
        day_low = float(data.get("low") or min(_bar_get(b, "l", "low") for b in bars))
        atr = max(day_high - day_low, last * 0.005)

    or_high, or_low, or_bars, post_bars = compute_opening_range(
        bars, int(cfg.get("orb_minutes", 30))
    )
    or_mid = (or_high + or_low) / 2.0
    or_width_pct = _pct(or_high, or_low)
    atr_pct = atr / last * 100.0 if last else 0.0

    avg_or_vol = sum(_bar_volume(b) for b in or_bars) / max(len(or_bars), 1)
    buffer_pct = float(cfg.get("orb_breakout_buffer_pct", 0.05))
    tol_pct = float(cfg.get("retest_tolerance_pct", 0.15))
    up_trigger = or_high * (1 + buffer_pct / 100.0)
    down_trigger = or_low * (1 - buffer_pct / 100.0)

    closes = [_bar_get(b, "c", "close") for b in bars]
    ema_fast = ema_series(closes, int(cfg.get("momentum_ema_fast", 9)))[-1]
    ema_slow = ema_series(closes, int(cfg.get("momentum_ema_slow", 21)))[-1]
    if ema_fast > ema_slow and last > or_mid:
        momentum = "bullish"
    elif ema_fast < ema_slow and last < or_mid:
        momentum = "bearish"
    else:
        momentum = "mixed"

    breakout_idx = None
    direction = None
    volume_ratio = None
    for i, bar in enumerate(post_bars):
        close = _bar_get(bar, "c", "close")
        vol = _bar_volume(bar)
        ratio = vol / avg_or_vol if avg_or_vol else 0.0
        if close > up_trigger and ratio >= float(cfg.get("breakout_volume_ratio_min", 1.2)):
            breakout_idx, direction, volume_ratio = i, "LONG", ratio
            break
        if close < down_trigger and ratio >= float(cfg.get("breakout_volume_ratio_min", 1.2)):
            breakout_idx, direction, volume_ratio = i, "SHORT", ratio
            break

    checks.append(Check(
        "opening_range_valid", round(or_width_pct, 3), ">0", or_width_pct > 0,
        f"OR ${or_low:.2f}-${or_high:.2f} ({or_width_pct:.2f}% wide)",
    ))
    checks.append(Check(
        "atr_sanity", round(atr_pct, 3),
        f"<={cfg.get('max_atr_pct', 4.0)}",
        atr_pct <= float(cfg.get("max_atr_pct", 4.0)),
        f"ATR ${atr:.2f} = {atr_pct:.2f}% of last",
    ))
    checks.append(Check(
        "breakout_with_volume", round(volume_ratio, 2) if volume_ratio else None,
        f">={cfg.get('breakout_volume_ratio_min', 1.2)}",
        breakout_idx is not None,
        "no qualified breakout" if breakout_idx is None else f"{direction} breakout, volume {volume_ratio:.2f}x OR avg",
    ))

    if breakout_idx is None:
        return ORBSetup(
            action="NONE",
            reason=(
                "NO_BREAKOUT: last "
                f"${last:.2f} inside opening range ${or_low:.2f}-${or_high:.2f}"
            ),
            atr=atr, or_high=or_high, or_low=or_low,
            extension_atr=round((last - or_mid) / atr, 2) if atr else None,
            momentum=momentum, bars_used=len(bars), checks=checks,
        )

    # Retest / failure tracking after the breakout bar
    followed = post_bars[breakout_idx + 1:]
    failed = False
    retest_ok = False
    for bar in followed:
        high = _bar_get(bar, "h", "high")
        low = _bar_get(bar, "l", "low")
        close = _bar_get(bar, "c", "close")
        if direction == "LONG":
            if close < or_high * (1 - tol_pct / 100.0):
                failed = True
                break
            if low <= or_high * (1 + tol_pct / 100.0) and close >= or_high:
                retest_ok = True
        else:
            if close > or_low * (1 + tol_pct / 100.0):
                failed = True
                break
            if high >= or_low * (1 - tol_pct / 100.0) and close <= or_low:
                retest_ok = True

    sustained = False
    if followed:
        if direction == "LONG":
            sustained = sum(1 for b in followed if _bar_get(b, "c", "close") >= or_high) >= 2
        else:
            sustained = sum(1 for b in followed if _bar_get(b, "c", "close") <= or_low) >= 2

    checks.append(Check("breakout_not_failed", not failed, True, not failed,
                        "price closed back inside the opening range" if failed else "holding outside OR"))
    checks.append(Check("retest_or_sustained", retest_ok or sustained, True, retest_ok or sustained,
                        "retest held" if retest_ok else ("sustained hold" if sustained else "no retest/hold yet")))

    extension = (last - or_mid) / atr if atr else 0.0
    max_ext = float(cfg.get("max_extension_vs_atr", 1.5))
    mean_reversion_ok = abs(extension) <= max_ext
    checks.append(Check(
        "not_overextended", round(extension, 2), f"|x|<={max_ext}", mean_reversion_ok,
        "chasing extended move" if not mean_reversion_ok else "within ATR envelope",
    ))

    momentum_ok = (momentum == "bullish") if direction == "LONG" else (momentum == "bearish")
    checks.append(Check(
        "momentum_agrees", momentum, direction.lower(), momentum_ok,
        "EMA9/EMA21 and OR-mid position align" if momentum_ok else "momentum conflicts with breakout",
    ))

    if failed:
        return ORBSetup("NONE", f"FAILED_BREAKOUT: {direction} broke and reclaimed the range",
                        atr=atr, or_high=or_high, or_low=or_low, extension_atr=round(extension, 2),
                        momentum=momentum, volume_ratio=round(volume_ratio, 2),
                        bars_used=len(bars), checks=checks)
    if not mean_reversion_ok:
        return ORBSetup("NONE", f"MEAN_REVERSION_RISK: {direction} extended {extension:.2f} ATR from OR mid",
                        atr=atr, or_high=or_high, or_low=or_low, extension_atr=round(extension, 2),
                        momentum=momentum, volume_ratio=round(volume_ratio, 2),
                        bars_used=len(bars), checks=checks)
    if not momentum_ok:
        return ORBSetup("NONE", f"MOMENTUM_CONFLICT: {direction} breakout vs {momentum} momentum",
                        atr=atr, or_high=or_high, or_low=or_low, extension_atr=round(extension, 2),
                        momentum=momentum, volume_ratio=round(volume_ratio, 2),
                        bars_used=len(bars), checks=checks)
    if not (retest_ok or sustained):
        return ORBSetup("NONE", f"WAITING_RETEST: {direction} breakout not yet confirmed by retest/hold",
                        atr=atr, or_high=or_high, or_low=or_low, extension_atr=round(extension, 2),
                        momentum=momentum, volume_ratio=round(volume_ratio, 2),
                        bars_used=len(bars), checks=checks)

    entry = round(last, 2)
    stop_pct = float(risk_cfg.get("stop_loss_pct", 1.5)) / 100.0
    max_stop_pct = float(risk_cfg.get("max_stop_pct", 3.0)) / 100.0
    atr_stop = float(risk_cfg.get("stop_atr_multiple", 2.0)) * atr
    r_multiple = float(risk_cfg.get("take_profit_r_multiple", 2.0))

    if direction == "LONG":
        structural = entry - or_low
        distance = min(max(structural, atr_stop, entry * stop_pct), entry * max_stop_pct)
        distance = max(distance, entry * 0.005)
        stop = round(entry - distance, 2)
        target = round(entry + r_multiple * (entry - stop), 2)
        action = "LONG"
    else:
        structural = or_high - entry
        distance = min(max(structural, atr_stop, entry * stop_pct), entry * max_stop_pct)
        distance = max(distance, entry * 0.005)
        stop = round(entry + distance, 2)
        target = round(entry - r_multiple * (stop - entry), 2)
        action = "SHORT"

    return ORBSetup(
        action=action,
        reason=(
            f"{action} ORB: {'breakout above' if action == 'LONG' else 'breakdown below'} "
            f"${or_high if action == 'LONG' else or_low:.2f}, volume {volume_ratio:.2f}x, "
            f"{'retest held' if retest_ok else 'sustained hold'}, {momentum} momentum"
        ),
        entry=round(entry, 2),
        stop=stop,
        target=target,
        risk_per_share=round(distance, 4),
        atr=round(atr, 2),
        or_high=round(or_high, 2),
        or_low=round(or_low, 2),
        extension_atr=round(extension, 2),
        momentum=momentum,
        volume_ratio=round(volume_ratio, 2),
        bars_used=len(bars),
        checks=checks,
    )
