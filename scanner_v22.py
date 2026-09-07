import json
from datetime import datetime, timezone
from pathlib import Path

VERSION = "v22-miss-audit-profit-feedback"
STATE_FILE = "beam_state.json"
FIRST_SEEN_FILE = "beam_first_seen.json"
SCAN_FILE = "beam_scan_result.json"
ONE_PICK_FILE = "beam_one_pick_summary.json"
OUT_FILE = "beam_miss_audit.json"

MIN_MOVER_CHANGE = 10.0
MIN_MOVER_TRADE_KRW = 500_000_000
EARLY_CAPTURE_MAX_CHANGE = 8.0
FAILED_ACCEL_MIN_PEAK = 10.0
FAILED_ACCEL_RETRACE_PCT = 7.0


def load_json(path, default):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except Exception:
        return default


def fnum(v, default=0.0):
    try:
        return float(v)
    except Exception:
        return default


def mover_audit(state, first_seen, scan):
    tickers = state.get("tickers") or {}
    stage2 = {r.get("base") for r in (scan.get("stage2_ranked") or []) if r.get("base")}
    movers = []
    for base, row in tickers.items():
        ch = fnum(row.get("change24_pct"))
        trade = fnum(row.get("trade24_krw"))
        if ch < MIN_MOVER_CHANGE or trade < MIN_MOVER_TRADE_KRW:
            continue
        fs = first_seen.get(base) if isinstance(first_seen, dict) else None
        if not fs:
            capture = "MISS_NOT_CAPTURED_STAGE2"
            first_change = None
            first_price = None
        else:
            first_change = fnum(fs.get("first_change_24h_pct"), 999)
            first_price = fs.get("first_price_krw")
            capture = "EARLY_CAPTURE" if first_change < EARLY_CAPTURE_MAX_CHANGE else "LATE_CAPTURE"
        movers.append({
            "base": base,
            "price_krw": row.get("price"),
            "change_24h_pct": ch,
            "trade_24h_krw": trade,
            "capture": capture,
            "first_detected_change_24h_pct": first_change,
            "first_detected_price_krw": first_price,
            "in_current_stage2": base in stage2,
        })
    movers.sort(key=lambda x: (x["change_24h_pct"], x["trade_24h_krw"]), reverse=True)
    return movers


def failed_acceleration_feedback(first_seen):
    out = []
    if not isinstance(first_seen, dict):
        return out
    for base, fs in first_seen.items():
        if not isinstance(fs, dict):
            continue
        peak = fnum(fs.get("peak_return_since_first_pct"), -999)
        current = fnum(fs.get("return_since_first_pct"), -999)
        if peak < FAILED_ACCEL_MIN_PEAK or peak >= 30:
            continue
        drawdown = peak - current
        if drawdown < FAILED_ACCEL_RETRACE_PCT:
            continue
        out.append({
            "base": base,
            "first_price_krw": fs.get("first_price_krw"),
            "last_price_krw": fs.get("last_price_krw"),
            "peak_price_krw": fs.get("peak_price_krw"),
            "current_return_since_first_pct": current,
            "peak_return_since_first_pct": peak,
            "giveback_from_peak_pct_points": round(drawdown, 2),
            "feedback": "FAILED_ACCELERATION_OR_PROFIT_GIVEBACK",
        })
    out.sort(key=lambda x: x["giveback_from_peak_pct_points"], reverse=True)
    return out[:20]


def profit_protection(one_pick):
    pick = one_pick.get("one_pick") or {}
    if not pick:
        return {"action": "NONE", "reason": "no_designated_one_pick"}
    current = fnum(pick.get("return_since_designation_pct"), 0.0)
    state = load_json("one_pick_state.json", {})
    raw_pick = state.get("one_pick") or {}
    peak = fnum(raw_pick.get("peak_return_pct"), current)
    giveback = peak - current
    if peak >= 15 and giveback >= 8:
        action = "LOCK_PROFIT_AGGRESSIVELY"
    elif peak >= 10 and giveback >= 6:
        action = "TAKE_PARTIAL_PROFIT"
    else:
        action = "HOLD_THESIS"
    return {
        "base": pick.get("base"),
        "current_return_pct": current,
        "peak_return_pct": peak,
        "giveback_pct_points": round(giveback, 2),
        "action": action,
        "rule": "+10 peak/6pt giveback => partial; +15 peak/8pt giveback => aggressive lock",
    }


def main():
    state = load_json(STATE_FILE, {})
    first_seen = load_json(FIRST_SEEN_FILE, {})
    scan = load_json(SCAN_FILE, {})
    one_pick = load_json(ONE_PICK_FILE, {})
    movers = mover_audit(state, first_seen, scan)
    failed = failed_acceleration_feedback(first_seen)
    missed = [x for x in movers if x["capture"] != "EARLY_CAPTURE"]
    out = {
        "generated_at_utc": scan.get("generated_at_utc") or datetime.now(timezone.utc).isoformat(),
        "version": VERSION,
        "health": scan.get("health"),
        "top_mover_audit_policy": {
            "mover_change_min_pct": MIN_MOVER_CHANGE,
            "mover_trade_min_krw": MIN_MOVER_TRADE_KRW,
            "early_capture_requires_first_detection_below_pct": EARLY_CAPTURE_MAX_CHANGE,
            "purpose": "measure what the scanner missed instead of assuming ranking quality",
        },
        "current_top_movers": movers[:30],
        "missed_or_late_count": len(missed),
        "missed_or_late": missed[:30],
        "failed_acceleration_examples": failed,
        "profit_protection": profit_protection(one_pick),
        "feedback_applied": [
            "top movers get an explicit recall lane before stage1/stage2 caps",
            "missing/late top movers are automatically audited every run",
            "10-15pct peaks that give back 6-8 percentage points trigger profit protection instead of waiting only for hard invalidation",
        ],
    }
    Path(OUT_FILE).write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(out, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
