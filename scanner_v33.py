import json
from datetime import datetime, timezone
from pathlib import Path

VERSION = "v33-execution-quality-feedback-gate"
BRIDGE_FILE = "beam_breakout_bridge.json"
MARKET_STATE_FILE = "beam_state.json"
STATE_FILE = "execution_quality_state.json"
HISTORY_FILE = "execution_quality_history.json"

MAX_HISTORY = 336
FAIL_RETURN_PCT = -2.5
SUCCESS_MFE_PCT = 5.0
MAX_OPEN_RUNS = 8
MIN_CONFIRMATIONS = 2
MIN_M15_VOL_PERSISTENCE = 1.10
MIN_BUY_SELL_RATIO = 1.10
MIN_BID_ASK_DEPTH_RATIO = 0.90
MIN_5M_PACE = -0.20
MAX_RECENT_FAILURE_STREAK = 1


def load_json(path, default):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except Exception:
        return default


def save_json(path, value):
    Path(path).write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")


def fnum(v, default=0.0):
    try:
        return float(v)
    except Exception:
        return default


def pct(a, b):
    a, b = fnum(a), fnum(b)
    return (a / b - 1.0) * 100.0 if a > 0 and b > 0 else None


def ticker_price(market_state, base):
    row = (market_state.get("tickers") or {}).get(base) or {}
    return fnum(row.get("price"))


def quality_checks(row, failure_streak):
    m = row.get("metrics") or {}
    confirmations = int(row.get("confirmations") or 0)
    checks = {
        "two_run_confirmation": confirmations >= MIN_CONFIRMATIONS,
        "m15_volume_persistence": fnum(m.get("m15_vol_persistence_x")) >= MIN_M15_VOL_PERSISTENCE,
        "m15_higher_low": bool(m.get("m15_low_rising")),
        "m15_above_ma": bool(m.get("m15_close_above_ma")),
        "h1_higher_low": bool(m.get("h1_low_rising")),
        "h1_above_ma": bool(m.get("h1_close_above_ma")),
        "buy_flow": fnum(m.get("buy_sell_ratio")) >= MIN_BUY_SELL_RATIO,
        "orderbook_support": fnum(m.get("bid_ask_depth_ratio")) >= MIN_BID_ASK_DEPTH_RATIO,
        "non_negative_short_pace": fnum(m.get("price_change_per_5m_pct"), -999.0) >= MIN_5M_PACE,
        "no_repeat_failure_pattern": failure_streak <= MAX_RECENT_FAILURE_STREAK,
    }
    failed = [k for k, ok in checks.items() if not ok]
    return checks, failed


def infer_failure_causes(snapshot):
    m = snapshot.get("metrics") or {}
    causes = []
    if int(snapshot.get("confirmations") or 0) < MIN_CONFIRMATIONS:
        causes.append("single_run_promotion")
    if fnum(m.get("m15_vol_persistence_x")) < MIN_M15_VOL_PERSISTENCE:
        causes.append("volume_spike_not_persistent")
    if not bool(m.get("m15_low_rising")):
        causes.append("m15_no_higher_low")
    if not bool(m.get("h1_low_rising")):
        causes.append("h1_no_higher_low")
    if not bool(m.get("m15_close_above_ma")) or not bool(m.get("h1_close_above_ma")):
        causes.append("ma_structure_incomplete")
    if fnum(m.get("buy_sell_ratio")) < MIN_BUY_SELL_RATIO:
        causes.append("weak_buy_flow")
    if fnum(m.get("bid_ask_depth_ratio")) < MIN_BID_ASK_DEPTH_RATIO:
        causes.append("ask_depth_dominant")
    if fnum(m.get("price_change_per_5m_pct"), -999.0) < MIN_5M_PACE:
        causes.append("negative_short_term_pace")
    return causes or ["unclassified_reversal"]


def compact_snapshot(row):
    return {
        "base": row.get("base"),
        "price_krw": row.get("price_krw"),
        "confirmations": row.get("confirmations"),
        "promotion_grade": row.get("promotion_grade"),
        "priority_score": row.get("priority_score"),
        "metrics": row.get("metrics") or {},
    }


def main():
    now = datetime.now(timezone.utc).isoformat()
    bridge = load_json(BRIDGE_FILE, {})
    market_state = load_json(MARKET_STATE_FILE, {})
    state = load_json(STATE_FILE, {"active": {}, "asset_feedback": {}})
    history = load_json(HISTORY_FILE, [])
    if not isinstance(history, list):
        history = []

    active = state.get("active") or {}
    feedback = state.get("asset_feedback") or {}
    closed = []

    # Update every previously tracked recommendation with realized MFE/MAE.
    for base in list(active.keys()):
        rec = active.get(base) or {}
        current = ticker_price(market_state, base)
        entry = fnum(rec.get("entry_price_krw"))
        if current <= 0 or entry <= 0:
            continue
        ret = pct(current, entry)
        rec["runs"] = int(rec.get("runs") or 0) + 1
        rec["last_price_krw"] = current
        rec["last_return_pct"] = round(ret, 3) if ret is not None else None
        rec["mfe_pct"] = round(max(fnum(rec.get("mfe_pct"), -999.0), ret), 3)
        rec["mae_pct"] = round(min(fnum(rec.get("mae_pct"), 999.0), ret), 3)

        status = None
        if ret is not None and ret <= FAIL_RETURN_PCT:
            status = "FAILED_FAST_REVERSAL"
        elif fnum(rec.get("mfe_pct")) >= SUCCESS_MFE_PCT:
            status = "SUCCESS_MFE_5"
        elif rec["runs"] >= MAX_OPEN_RUNS:
            status = "EXPIRED_NO_FOLLOW_THROUGH"

        if status:
            fb = feedback.get(base) or {"failure_streak": 0, "successes": 0, "failures": 0}
            if status.startswith("FAILED") or status.startswith("EXPIRED"):
                fb["failures"] = int(fb.get("failures") or 0) + 1
                fb["failure_streak"] = int(fb.get("failure_streak") or 0) + 1
                fb["last_failure_causes"] = infer_failure_causes(rec.get("snapshot") or {})
                fb["last_failure_at_utc"] = now
            else:
                fb["successes"] = int(fb.get("successes") or 0) + 1
                fb["failure_streak"] = 0
                fb["last_success_at_utc"] = now
            feedback[base] = fb
            rec["status"] = status
            rec["closed_at_utc"] = now
            closed.append(rec)
            active.pop(base, None)
        else:
            active[base] = rec

    promoted = list(bridge.get("promoted_top3") or [])
    watches = list(bridge.get("watch_top5") or [])
    actionable = []
    demoted = []

    for row in promoted:
        base = row.get("base")
        fb = feedback.get(base) or {}
        streak = int(fb.get("failure_streak") or 0)
        checks, failed = quality_checks(row, streak)
        row["execution_gate"] = {
            "version": VERSION,
            "actionable": not failed,
            "checks": checks,
            "failed_checks": failed,
            "recent_failure_streak": streak,
            "rule": "URGENT_EARLY is detection only; execution requires two-run + 15m/1h structure + persistent flow/orderbook confirmation",
        }
        if failed:
            row["promotion_grade"] = "WATCH_AFTER_EXECUTION_GATE"
            row["immediate_quality"] = False
            row["confirmed_quality"] = False
            row["execution_allowed"] = False
            demoted.append(row)
        else:
            row["promotion_grade"] = "A_EXECUTION_CONFIRMED"
            row["execution_allowed"] = True
            actionable.append(row)
            if base and base not in active:
                active[base] = {
                    "base": base,
                    "opened_at_utc": now,
                    "entry_price_krw": row.get("price_krw"),
                    "last_price_krw": row.get("price_krw"),
                    "runs": 0,
                    "mfe_pct": 0.0,
                    "mae_pct": 0.0,
                    "snapshot": compact_snapshot(row),
                    "status": "OPEN",
                }

    # Only execution-confirmed candidates remain promoted; detections are preserved as watch candidates.
    combined_watch = demoted + watches
    seen = set()
    dedup_watch = []
    for row in combined_watch:
        base = row.get("base")
        if not base or base in seen:
            continue
        seen.add(base)
        dedup_watch.append(row)

    bridge["version"] = f"{bridge.get('version', 'v31')}+{VERSION}"
    bridge["promoted_top3"] = actionable[:3]
    bridge["watch_top5"] = dedup_watch[:5]
    bridge["top_candidates"] = (actionable + dedup_watch)[:5]
    bridge["execution_quality_gate"] = {
        "generated_at_utc": now,
        "version": VERSION,
        "actionable_bases": [x.get("base") for x in actionable[:3]],
        "demoted_bases": [x.get("base") for x in demoted],
        "closed_feedback": closed,
        "rules": {
            "confirmations_min": MIN_CONFIRMATIONS,
            "m15_vol_persistence_min": MIN_M15_VOL_PERSISTENCE,
            "buy_sell_ratio_min": MIN_BUY_SELL_RATIO,
            "bid_ask_depth_ratio_min": MIN_BID_ASK_DEPTH_RATIO,
            "m15_and_h1_higher_low_required": True,
            "m15_and_h1_above_ma_required": True,
            "fast_failure_return_pct": FAIL_RETURN_PCT,
            "success_mfe_pct": SUCCESS_MFE_PCT,
            "max_open_runs": MAX_OPEN_RUNS,
        },
    }
    if actionable:
        bridge["status"] = "EXECUTION_CONFIRMED_CANDIDATE"
    elif dedup_watch:
        bridge["status"] = "WATCH_ONLY"
    else:
        bridge["status"] = "NO_SIGNAL"

    state = {
        "version": VERSION,
        "updated_at_utc": now,
        "active": active,
        "asset_feedback": feedback,
    }
    history.append({
        "generated_at_utc": now,
        "actionable": [x.get("base") for x in actionable[:3]],
        "demoted": [{"base": x.get("base"), "failed_checks": (x.get("execution_gate") or {}).get("failed_checks")} for x in demoted],
        "closed": closed,
        "asset_feedback": feedback,
    })

    save_json(BRIDGE_FILE, bridge)
    save_json(STATE_FILE, state)
    save_json(HISTORY_FILE, history[-MAX_HISTORY:])
    print(json.dumps({
        "version": VERSION,
        "actionable": [x.get("base") for x in actionable[:3]],
        "demoted": [x.get("base") for x in demoted],
        "closed": [{"base": x.get("base"), "status": x.get("status"), "mfe_pct": x.get("mfe_pct"), "mae_pct": x.get("mae_pct")} for x in closed],
    }, ensure_ascii=False))


if __name__ == "__main__":
    main()
