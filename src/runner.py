"""moomoo-agent runner: scan -> research -> risk -> decision log.

Modes
-----
--dry-run (default)  Full evaluation using a live MCP market snapshot. Writes a
                     decision record to logs/decisions.jsonl. Never submits.
--paper              Same evaluation, but also writes an order *intent* to
                     logs/pending_orders/ for the agent to submit through
                     mcp__moomoo__sim_trade_input_order after review. This
                     process itself never talks to the broker, and refuses any
                     account other than the configured simulated account.

Usage
-----
python src/runner.py --dry-run
python src/runner.py --dry-run --simulate-time 2026-11-18T04:55:00+08:00
python src/runner.py --paper --acc-id 5829755 --confirm-paper
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

try:
    UTC = ZoneInfo("UTC")
except ZoneInfoNotFoundError:  # Windows without the IANA tzdata package
    UTC = timezone.utc

try:  # support both `python src/runner.py` and `python -m src.runner`
    from risk_guard import (
        SGT, classify_session, drawdown_metrics, load_state, mark_to_market,
        parse_dt, pre_trade_checks, save_state, size_position,
    )
    from research_pipeline import evaluate_symbol, macro_context, rank_candidates
except ImportError:  # pragma: no cover
    from .risk_guard import (
        SGT, classify_session, drawdown_metrics, load_state, mark_to_market,
        parse_dt, pre_trade_checks, save_state, size_position,
    )
    from .research_pipeline import evaluate_symbol, macro_context, rank_candidates

ROOT = Path(__file__).resolve().parent.parent


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
def load_json(path: Path) -> dict:
    with path.open("r", encoding="utf-8") as fh:
        return json.load(fh)


def append_jsonl(path: Path, record: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(record, separators=(",", ":")) + "\n")


def spread_pct(quote: dict) -> float | None:
    bid, ask = quote.get("bid"), quote.get("ask")
    if bid and ask and ask > bid:
        return (ask - bid) / ((ask + bid) / 2.0) * 100.0
    return None


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="moomoo-agent paper trading runner")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true", help="evaluate and log only (default)")
    mode.add_argument("--paper", action="store_true", help="write an order intent for MCP submission")
    parser.add_argument("--config", default=str(ROOT / "config" / "trading_config.json"))
    parser.add_argument("--snapshot", default=str(ROOT / "logs" / "market_snapshot.json"))
    parser.add_argument("--simulate-time", default=None,
                        help="evaluate at a fixed SGT timestamp (verification only)")
    parser.add_argument("--acc-id", default=None, help="paper account id (must match config)")
    parser.add_argument("--confirm-paper", action="store_true",
                        help="required with --paper: confirms paper-only intent generation")
    return parser.parse_args(argv)


# --------------------------------------------------------------------------- #
# main
# --------------------------------------------------------------------------- #
def run(args: argparse.Namespace) -> int:
    config = load_json(Path(args.config))
    if config["meta"].get("mode") != "paper-only" or \
            config["meta"]["competition"].get("live_trading_enabled"):
        print("FATAL: config is not paper-only. Refusing to run.", file=sys.stderr)
        return 2

    account = config["account"]
    if args.paper:
        if not args.confirm_paper:
            print("FATAL: --paper requires --confirm-paper (paper intents only).", file=sys.stderr)
            return 2
        if args.acc_id != account["acc_id"]:
            print(f"FATAL: --paper requires the simulated account {account['acc_id']}.",
                  file=sys.stderr)
            return 2

    snapshot_path = Path(args.snapshot)
    if not snapshot_path.exists():
        print(f"FATAL: snapshot not found: {snapshot_path}\n"
              "Build it from moomoo-mcp quotes (see README 'Data flow').", file=sys.stderr)
        return 2
    snapshot = load_json(snapshot_path)

    now_sgt = parse_dt(args.simulate_time) if args.simulate_time else datetime.now(SGT)
    session = classify_session(now_sgt, config)
    today = now_sgt.date()

    paths = {name: ROOT / rel for name, rel in config["logging"].items()}
    state = load_state(paths["state_file"], float(account["starting_equity"]), today)

    symbols = snapshot.get("symbols", {})
    prices = {sym: float(d.get("last") or 0) for sym, d in symbols.items() if d.get("last")}
    mark_to_market(state, prices)
    dd = drawdown_metrics(state, config)

    quote_times = [parse_dt(d["quote_time_sgt"]) for d in symbols.values() if d.get("quote_time_sgt")]
    data_age_min = max(0.0, (now_sgt - max(quote_times)).total_seconds() / 60.0) if quote_times else 1e9

    macro = macro_context(snapshot)
    results = []
    errors = []
    for meta in config["universe"]:
        data = symbols.get(meta["symbol"])
        if not data:
            errors.append(f"{meta['symbol']}: missing from snapshot")
            continue
        try:
            results.append(evaluate_symbol(meta, data, macro, config, session))
        except Exception as exc:  # keep the run auditable even if one symbol fails
            errors.append(f"{meta['symbol']}: {exc}")

    candidates = rank_candidates(results)
    chosen = candidates[0] if candidates else None
    session_blocked = not session.entries_allowed

    order: dict = {"status": "NONE", "submitted": False}
    risk_checks: list[dict] = []
    sizing = None
    decision = "NO_TRADE"
    rationale = ""

    if chosen:
        direction = chosen.orb.action
        entry = float(chosen.orb.entry)
        stop = float(chosen.orb.stop)
        target = float(chosen.orb.target)
        meta = next(m for m in config["universe"] if m["symbol"] == chosen.symbol)
        sizing = size_position(dd["equity"], entry, stop, config,
                               float(meta.get("max_allocation_pct", 15.0)))
        quote = symbols[chosen.symbol]
        limit_price = quote.get("ask") if direction == "LONG" else quote.get("bid")
        limit_price = float(limit_price or entry)

        checks = pre_trade_checks(
            symbol=chosen.symbol, quote=quote, session=session, state=state,
            sizing=sizing, order_type="limit", direction=direction,
            entry=entry, stop=stop, target=target, config=config,
            acc_id=account["acc_id"], data_age_min=data_age_min,
        )
        risk_checks = [c.as_dict() for c in checks]
        failed = [c.rule for c in checks if not c.passed]

        order = {
            "status": "BLOCKED",
            "submitted": False,
            "acc_id": account["acc_id"],
            "market_id": account["market_id"],
            "symbol": chosen.symbol,
            "side": "BUY" if direction == "LONG" else "SELL",
            "qty": sizing.qty,
            "order_type": "limit",
            "limit_price": round(limit_price, 2),
            "time_in_force": "DAY",
            "stop_loss": stop,
            "take_profit": target,
            "brackets": {"stop": stop, "target": target, "rr": round(
                abs(target - entry) / abs(entry - stop), 2) if entry != stop else None},
            "est_notional": round(sizing.notional, 2),
        }

        if session.mode == "AUDIT_ONLY":
            decision = "AUDIT_ONLY"
            rationale = session.reason
        elif session.mode in ("CLOSED", "CUTOFF"):
            decision = "BLOCKED_SESSION"
            rationale = session.reason
        elif session.mode == "FLATTEN":
            decision = "FLATTEN_ONLY"
            rationale = session.reason
        elif failed:
            decision = "BLOCKED_RISK"
            rationale = "Risk checks failed: " + ", ".join(failed)
        else:
            decision = f"ENTER_{direction}"
            rationale = chosen.orb.reason
            if args.paper:
                order["status"] = "PENDING_MCP_SUBMIT"
            else:
                order["status"] = "DRY_RUN_NOT_SUBMITTED"
    else:
        reasons = [f"{r.symbol}: {r.orb.reason}" for r in results]
        reasons += errors
        if session.mode == "AUDIT_ONLY":
            decision = "AUDIT_ONLY"
        elif session.mode in ("CLOSED", "CUTOFF"):
            decision = "BLOCKED_SESSION"
        elif session.mode == "FLATTEN":
            decision = "FLATTEN_ONLY"
        else:
            decision = "NO_TRADE"
        rationale = "No qualified setup. " + " | ".join(reasons) if reasons else "No symbols."

    record = {
        "ts_utc": now_sgt.astimezone(UTC).isoformat(timespec="seconds"),
        "ts_sgt": now_sgt.isoformat(timespec="seconds"),
        "sgt_date": now_sgt.date().isoformat(),
        "run_mode": "paper" if args.paper else "dry-run",
        "simulated_clock": bool(args.simulate_time),
        "session": session.as_dict(),
        "calendar": {
            "holiday": session.holiday,
            "half_day": session.half_day,
            "flatten_deadline_sgt": session.flatten_deadline_sgt,
            "checks_stop_sgt": session.checks_stop_sgt,
        },
        "data": {
            "source": snapshot.get("source", "unknown"),
            "captured_at_sgt": snapshot.get("captured_at_sgt"),
            "data_age_min": round(data_age_min, 2),
            "market": {
                sym: {
                    "last": d.get("last"), "prev_close": d.get("prev_close"),
                    "bid": d.get("bid"), "ask": d.get("ask"),
                    "spread_pct": round(spread_pct(d), 4) if spread_pct(d) is not None else None,
                    "quote_time_sgt": d.get("quote_time_sgt"),
                }
                for sym, d in symbols.items()
            },
        },
        "research": [r.as_dict() for r in results],
        "errors": errors,
        "decision": decision,
        "rationale": rationale,
        "selected": chosen.symbol if chosen else None,
        "thesis": chosen.thesis if chosen else None,
        "invalidation_checks": (chosen.orb.as_dict()["checks"] if chosen else []),
        "risk_checks": risk_checks,
        "sizing": sizing.as_dict() if sizing else None,
        "order": order,
        "pnl": {
            "equity_before": round(dd["equity"], 2),
            "daily_pnl": dd["daily_pnl"],
            "daily_drawdown_pct": dd["daily_drawdown_pct"],
            "peak_drawdown_pct": dd["peak_drawdown_pct"],
            "kill_switch_active": dd["kill_switch_active"],
            "peak_halt_active": dd["peak_halt_active"],
        },
    }

    append_jsonl(paths["decisions_file"], record)
    save_state(paths["state_file"], state)

    if args.paper and order.get("status") == "PENDING_MCP_SUBMIT":
        out_dir = paths["pending_orders_dir"]
        out_dir.mkdir(parents=True, exist_ok=True)
        stamp = now_sgt.strftime("%Y%m%dT%H%M%S")
        intent_path = out_dir / f"order_{stamp}.json"
        intent_path.write_text(json.dumps(order, indent=2), encoding="utf-8")
        order["intent_file"] = str(intent_path.relative_to(ROOT))

    print_report(record, results, session, dd, data_age_min)
    print(f"  log: {paths['decisions_file'].relative_to(ROOT)}  "
          f"state: {paths['state_file'].relative_to(ROOT)}")
    if args.paper:
        if order.get("status") == "PENDING_MCP_SUBMIT":
            print(f"  pending intent: {order['intent_file']}")
            print("  -> submit via mcp__moomoo__sim_trade_input_order after human review.")
        else:
            print("  no intent written (blocked or no setup).")
    else:
        print("  DRY RUN: no order was submitted.")
    return 0


def print_report(record: dict, results: list, session, dd: dict, age: float) -> None:
    line = "=" * 78
    print(line)
    print(f" moomoo-agent | PAPER ONLY | mode: {record['run_mode'].upper()}"
          f"{' | SIMULATED CLOCK' if record['simulated_clock'] else ''}")
    print(f" {record['ts_sgt']}  (UTC{record['ts_utc'][-6:]})")
    print(line)

    print(f"SESSION   {session.session}   mode={session.mode}   "
          f"entries={'yes' if session.entries_allowed else 'NO'}   policy={session.order_policy}")
    print(f"  trading_date={session.trading_date}  holiday={session.holiday}  "
          f"half_day={session.half_day}")
    if session.flatten_deadline_sgt:
        print(f"  flatten by {session.flatten_deadline_sgt} | checks stop {session.checks_stop_sgt}")
    print(f"  {session.reason}")

    print("\nMARKET    (source: %s, age %.1f min)" % (record["data"]["source"], age))
    for sym, m in record["data"]["market"].items():
        chg = ((m["last"] - m["prev_close"]) / m["prev_close"] * 100
               if m.get("last") and m.get("prev_close") else 0.0)
        spr = f"{m['spread_pct']:.4f}%" if m.get("spread_pct") is not None else "n/a"
        print(f"  {sym:<8} ${m['last']:>9,.2f}  {chg:+6.2f}%  "
              f"bid {m.get('bid')} / ask {m.get('ask')}  spread {spr}  quote {m.get('quote_time_sgt')}")

    print("\nRESEARCH  (step 1 thesis -> step 2 factors -> step 3 outlook -> step 4 vehicle)")
    for r in results:
        f = r.factors
        print(f"  {r.symbol:<8} composite {r.composite:5.1f}  bias {r.bias:<7} "
              f"[macro {f['macro'].score:.0f} | fund {f['fundamentals'].score:.0f} | "
              f"tech {f['technicals'].score:.0f} | sent {f['sentiment'].score:.0f}]")
        print(f"           {r.orb.action}: {r.orb.reason}")
        print(f"           expectancy {r.expectancy['expectancy_r']:+.3f}R "
              f"(p_win {r.expectancy['p_win']:.3f}, min {r.expectancy['min_required_r']}R) "
              f"-> {r.expectancy['verdict']}")

    print("\nRISK      equity $%s  daily %+.2f (%+.2f%%)  peak DD %.2f%%  kill=%s" % (
        f"{dd['equity']:,.2f}", dd["daily_pnl"], dd["daily_drawdown_pct"],
        dd["peak_drawdown_pct"], dd["kill_switch_active"]))
    if record["sizing"]:
        s = record["sizing"]
        print(f"  size {s['qty']} sh  notional ${s['notional']:,.0f} ({s['alloc_pct']:.2f}% equity)  "
              f"risk ${s['risk_amount']:,.0f} ({s['risk_pct_equity']:.2f}%)  binding={s['binding']}")
    for c in record["risk_checks"]:
        print(f"  [{'PASS' if c['pass'] else 'FAIL'}] {c['rule']}: {c['value']} (limit {c['limit']})")

    if record["order"].get("symbol"):
        o = record["order"]
        print(f"\nORDER INTENT  {o['side']} {o['qty']} {o['symbol']}  {o['order_type']} @ "
              f"${o['limit_price']}  stop ${o['stop_loss']}  target ${o['take_profit']}  "
              f"R:R {o['brackets']['rr']}")
        print(f"  status: {o['status']}  (submitted={o['submitted']})")

    print(f"\nDECISION  {record['decision']}")
    print(f"  {record['rationale']}")
    print(line)


def main(argv: list[str] | None = None) -> int:
    return run(parse_args(argv))


if __name__ == "__main__":
    raise SystemExit(main())
