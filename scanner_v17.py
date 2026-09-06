import json
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path

import scanner_v16 as v16

VERSION = "v17-beam-calibrated-fast-state"
STATE_FILE = "beam_state.json"
COMPACT_FILE = "beam_top10_compact.json"
MAJOR_LOW_BETA = {"BTC","ETH","DOGE","XRP","SOL","BNB","TRX","LINK","LTC","BCH","ADA","XLM","DOT"}

_capture = {}
_orig_get_tickers = v16.get_tickers
_orig_base_row = v16.base_row
_orig_score_stage1 = v16.score_stage1


def load_json(path, default):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except Exception:
        return default


def parse_dt(value):
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except Exception:
        return None


def seed_previous_state():
    state = load_json(STATE_FILE, {})
    if state.get("tickers"):
        return state
    hist = load_json("policy_snapshot_history.json", [])
    if not isinstance(hist, list) or not hist:
        return {}
    candidates = [x for x in hist if isinstance(x, dict) and x.get("generated_at_utc") and x.get("snapshot")]
    if not candidates:
        return {}
    latest = max(candidates, key=lambda x: x.get("generated_at_utc", ""))
    tickers = {}
    for base, row in (latest.get("snapshot") or {}).items():
        tickers[base] = {
            "price": row.get("bithumb_krw_price"),
            "change24_pct": row.get("bithumb_24h_change_pct"),
            "trade24_krw": row.get("bithumb_24h_trade_krw"),
        }
    return {"generated_at_utc": latest.get("generated_at_utc"), "tickers": tickers, "seed": "policy_snapshot_history"}


PREVIOUS = seed_previous_state()


def patched_get_tickers(markets):
    rows = _orig_get_tickers(markets)
    _capture["markets"] = markets
    _capture["tickers"] = rows
    return rows


def fast_metrics(base, row):
    prev = (PREVIOUS.get("tickers") or {}).get(base) or {}
    prev_ts = parse_dt(PREVIOUS.get("generated_at_utc", ""))
    now = datetime.now(timezone.utc)
    elapsed = (now - prev_ts).total_seconds() / 60 if prev_ts else None
    if not prev or not elapsed or elapsed <= 0 or elapsed > 120:
        return {"available": False, "elapsed_min": round(elapsed, 1) if elapsed else None}
    factor = 5.0 / max(elapsed, 1.0)
    factor = max(0.05, min(factor, 2.0))
    price = float(row.get("price_krw") or 0)
    pprice = float(prev.get("price") or 0)
    change = float(row.get("change_24h_pct") or 0)
    pchange = float(prev.get("change24_pct") or 0)
    trade = float(row.get("trade_24h_krw") or 0)
    ptrade = float(prev.get("trade24_krw") or 0)
    price_raw = ((price / pprice - 1) * 100) if pprice else 0.0
    price_5m = price_raw * factor
    change_5m = (change - pchange) * factor
    trade_delta = max(0.0, trade - ptrade)
    trade_5m = trade_delta * factor
    baseline_5m = trade / 288.0 if trade > 0 else 0
    intensity = trade_5m / baseline_5m if baseline_5m else 0
    return {
        "available": True,
        "elapsed_min": round(elapsed, 1),
        "price_change_per_5m_pct": round(price_5m, 3),
        "change24_delta_per_5m_pct": round(change_5m, 3),
        "trade_value_delta_per_5m_krw": round(trade_5m),
        "turnover_intensity_vs_even_5m_x": round(intensity, 2),
    }


def patched_base_row(info, ticker, binance24):
    row = _orig_base_row(info, ticker, binance24)
    row["fast_5m"] = fast_metrics(row["base"], row)
    return row


def patched_score_stage1(row):
    row = _orig_score_stage1(row)
    fast = row.get("fast_5m") or {}
    if not fast.get("available"):
        return row
    price = float(fast.get("price_change_per_5m_pct") or 0)
    delta = float(fast.get("change24_delta_per_5m_pct") or 0)
    trade = float(fast.get("trade_value_delta_per_5m_krw") or 0)
    intensity = float(fast.get("turnover_intensity_vs_even_5m_x") or 0)
    pre = float(row.get("pre_ignition_score_raw") or 0)
    accel = float(row.get("accelerator_score_raw") or 0)
    if 0.10 <= price <= 1.5:
        pre += 12
    elif 1.5 < price <= 3.0:
        pre += 7
    if price >= 0.35:
        accel += 13
    elif price >= 0.15:
        accel += 7
    if delta >= 0.35:
        pre += 8; accel += 7
    elif delta >= 0.15:
        pre += 4; accel += 3
    if intensity >= 6:
        pre += 18; accel += 14
    elif intensity >= 3:
        pre += 13; accel += 10
    elif intensity >= 1.8:
        pre += 8; accel += 6
    if trade >= 50_000_000:
        pre += 6; accel += 6
    elif trade >= 15_000_000:
        pre += 3; accel += 3
    if price < -0.8:
        pre -= 8; accel -= 10
    row["pre_ignition_score_raw"] = round(pre, 2)
    row["accelerator_score_raw"] = round(accel, 2)
    return row


def signature_count(row):
    f = row.get("fast_5m") or {}
    m5 = row.get("m5") or {}
    m15 = row.get("m15") or {}
    d1 = row.get("d1") or {}
    checks = [
        float(f.get("turnover_intensity_vs_even_5m_x") or 0) >= 1.8,
        float(f.get("price_change_per_5m_pct") or 0) >= 0.15,
        float(m5.get("vol_spike_x") or 0) >= 1.5,
        float(m15.get("vol_spike_x") or 0) >= 1.5,
        float(m15.get("vol_persistence_x") or 0) >= 1.4,
        float(d1.get("previous_day_volume_x") or 0) >= 1.4,
        bool(row.get("sector_rotation_reason")),
    ]
    return sum(bool(x) for x in checks)


def calibrate(row):
    r = deepcopy(row)
    score = float(r.get("beam_score") or 0)
    penalty = 0.0
    bonus = 0.0
    base = r.get("base")
    week = float((r.get("d1") or {}).get("week_change_pct") or 0)
    prev_vol = float((r.get("d1") or {}).get("previous_day_volume_x") or 0)
    m5 = r.get("m5") or {}
    f = r.get("fast_5m") or {}
    sig = signature_count(r)
    if base in MAJOR_LOW_BETA:
        penalty += 28
    if week > 80:
        penalty += 16
    elif week > 55 and prev_vol < 1.0:
        penalty += 12
    if float(m5.get("vol_persistence_x") or 0) < 0.8 and not m5.get("low_rising"):
        penalty += 6
    if sig < 2:
        penalty += 14
    elif sig >= 4:
        bonus += 8
    if f.get("available") and float(f.get("turnover_intensity_vs_even_5m_x") or 0) >= 3 and float(f.get("price_change_per_5m_pct") or 0) >= 0.15:
        bonus += 8
    score = score + bonus - penalty
    r["beam_signature_count"] = sig
    r["calibration_bonus"] = round(bonus, 2)
    r["calibration_penalty"] = round(penalty, 2)
    r["beam_score"] = round(score, 2)
    r["beam_eligible"] = base not in MAJOR_LOW_BETA and not r.get("warning_flag") and sig >= 2
    status = (r.get("entry_plan") or {}).get("status")
    if base in MAJOR_LOW_BETA:
        status = "WATCH_LARGE_CAP_NOT_BEAM_TARGET"
    elif r.get("warning_flag"):
        status = "SPECULATIVE_WARNING_ONLY"
    elif score >= 78 and sig >= 3:
        status = "ENTER_CANDIDATE"
    elif score >= 66 and sig >= 2:
        status = "WAIT_CONFIRMATION"
    else:
        status = "WATCH"
    r.setdefault("entry_plan", {})["status"] = status
    return r


def save_state():
    tickers = _capture.get("tickers") or {}
    markets = _capture.get("markets") or {}
    out = {"generated_at_utc": datetime.now(timezone.utc).isoformat(), "tickers": {}}
    for market, t in tickers.items():
        info = markets.get(market) or {}
        base = info.get("base") or market.split("-", 1)[-1]
        out["tickers"][base] = {
            "price": t.get("trade_price"),
            "change24_pct": round(float(t.get("signed_change_rate") or 0) * 100, 4),
            "trade24_krw": t.get("acc_trade_price_24h"),
        }
    Path(STATE_FILE).write_text(json.dumps(out, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")


def compact(r):
    return {
        "base": r.get("base"), "market": r.get("market"), "korean_name": r.get("korean_name"),
        "price_krw": r.get("price_krw"), "change_24h_pct": r.get("change_24h_pct"),
        "trade_24h_krw": r.get("trade_24h_krw"), "track": r.get("track"),
        "beam_score": r.get("beam_score"), "beam_signature_count": r.get("beam_signature_count"),
        "beam_eligible": r.get("beam_eligible"), "sector": r.get("sector"),
        "sector_rotation_reason": r.get("sector_rotation_reason"), "fast_5m": r.get("fast_5m"),
        "m5": r.get("m5"), "m15": r.get("m15"), "h1": r.get("h1"), "h4": r.get("h4"), "d1": r.get("d1"),
        "orderbook": r.get("orderbook"), "trade_flow": r.get("trade_flow"),
        "warning_flag": r.get("warning_flag"), "entry_plan": r.get("entry_plan"),
    }


def postprocess():
    result = load_json("beam_scan_result.json", {})
    rows = [calibrate(r) for r in (result.get("stage2_ranked") or [])]
    rows.sort(key=lambda r: float(r.get("beam_score") or -999), reverse=True)
    eligible = [r for r in rows if r.get("beam_eligible")]
    enter = [r for r in eligible if (r.get("entry_plan") or {}).get("status") == "ENTER_CANDIDATE"]
    wait = [r for r in eligible if (r.get("entry_plan") or {}).get("status") == "WAIT_CONFIRMATION"]
    rest = [r for r in eligible if r not in enter and r not in wait]
    candidates = (enter + wait + rest)[:2]
    result["source_version"] = VERSION
    result["calibration"] = {
        "target": "FLOCK/ARB/RAY/TEMCO-like beam discovery",
        "large_cap_deprioritized": sorted(MAJOR_LOW_BETA),
        "requires_multi-signal_signature": True,
        "fast_state_source": PREVIOUS.get("seed") or ("beam_state" if PREVIOUS else "none"),
    }
    result["stage2_ranked"] = rows
    result["trade_candidates"] = candidates
    result["pre_ignition_top5"] = sorted(eligible, key=lambda r: float(r.get("pre_ignition_score") or -999), reverse=True)[:5]
    result["accelerator_top5"] = sorted(eligible, key=lambda r: float(r.get("accelerator_score") or -999), reverse=True)[:5]
    result.setdefault("health", {})["analysis_ready"] = bool(len(candidates) >= 2 and result.get("health", {}).get("analysis_ready", True))
    if len(candidates) < 2:
        result["health"].setdefault("hard_blockers", []).append("fewer_than_two_beam_eligible_candidates")
    Path("beam_scan_result.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    summary = {
        "generated_at_utc": result.get("generated_at_utc"), "source_version": VERSION,
        "health": result.get("health"), "data_sources": result.get("data_sources"),
        "logic": result.get("logic"), "calibration": result.get("calibration"),
        "trade_candidates": [compact(r) for r in candidates],
        "top10_calibrated": [compact(r) for r in rows[:10]],
    }
    Path("beam_latest_summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    Path(COMPACT_FILE).write_text(json.dumps({
        "generated_at_utc": result.get("generated_at_utc"), "source_version": VERSION,
        "health": result.get("health"),
        "top10": [{"base":r.get("base"),"price_krw":r.get("price_krw"),"change_24h_pct":r.get("change_24h_pct"),"beam_score":r.get("beam_score"),"signature":r.get("beam_signature_count"),"status":(r.get("entry_plan") or {}).get("status"),"fast_5m":r.get("fast_5m"),"sector":r.get("sector"),"warning":r.get("warning_flag")} for r in rows[:10]],
        "trade_candidates": [{"base":r.get("base"),"price_krw":r.get("price_krw"),"beam_score":r.get("beam_score"),"status":(r.get("entry_plan") or {}).get("status")} for r in candidates],
    }, ensure_ascii=False, indent=2), encoding="utf-8")


def main():
    v16.get_tickers = patched_get_tickers
    v16.base_row = patched_base_row
    v16.score_stage1 = patched_score_stage1
    v16.main()
    postprocess()
    save_state()
    compact_result = load_json(COMPACT_FILE, {})
    print(json.dumps(compact_result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
