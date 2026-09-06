import json
from datetime import datetime, timezone
from pathlib import Path

import scanner_v17 as v17

VERSION = "v18-hard-gated-first-detection"
FIRST_SEEN_FILE = "beam_first_seen.json"


def load_json(path, default):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except Exception:
        return default


def fnum(value, default=0.0):
    try:
        return float(value)
    except Exception:
        return default


def special_reacceleration(row):
    chg = fnum(row.get("change_24h_pct"))
    f = row.get("fast_5m") or {}
    m15 = row.get("m15") or {}
    return (
        20.0 < chg <= 35.0
        and fnum(f.get("price_change_per_5m_pct")) >= 0.8
        and fnum(f.get("turnover_intensity_vs_even_5m_x")) >= 6.0
        and bool(m15.get("low_rising"))
        and fnum(m15.get("vol_persistence_x")) >= 1.5
        and int(m15.get("recent_positive_count") or 0) >= 2
    )


def classify_track(row):
    chg = fnum(row.get("change_24h_pct"))
    if 0.0 <= chg < 8.0:
        return "PRE_IGNITION"
    if 8.0 <= chg <= 20.0:
        return "ACCELERATOR"
    if special_reacceleration(row):
        return "REACCELERATION_SPECIAL"
    if chg > 20.0:
        return "CHASE_EXCLUDED"
    return "WATCH_NEGATIVE"


def money_leads_price_score(row):
    f = row.get("fast_5m") or {}
    if not f.get("available"):
        return 0.0
    p5 = fnum(f.get("price_change_per_5m_pct"))
    intensity = fnum(f.get("turnover_intensity_vs_even_5m_x"))
    flow = fnum(f.get("trade_value_delta_per_5m_krw"))
    chg = fnum(row.get("change_24h_pct"))
    score = 0.0
    # The desired signature: money arrives first while price is still only beginning to react.
    if intensity >= 10:
        score += 24
    elif intensity >= 6:
        score += 20
    elif intensity >= 3:
        score += 14
    elif intensity >= 1.8:
        score += 8
    if -0.05 <= p5 <= 0.8:
        score += 14
    elif 0.8 < p5 <= 1.5:
        score += 8
    elif p5 > 2.0:
        score -= 8
    if flow >= 100_000_000:
        score += 8
    elif flow >= 30_000_000:
        score += 5
    elif flow >= 10_000_000:
        score += 2
    if 0 <= chg < 4:
        score += 8
    elif 4 <= chg < 8:
        score += 4
    return round(score, 2)


def apply_hard_gates(row):
    r = dict(row)
    track = classify_track(r)
    r["hard_track"] = track
    r["track"] = track
    lead = money_leads_price_score(r)
    r["money_leads_price_score"] = lead
    score = fnum(r.get("beam_score"))
    f = r.get("fast_5m") or {}
    p5 = fnum(f.get("price_change_per_5m_pct"))
    intensity = fnum(f.get("turnover_intensity_vs_even_5m_x"))
    sig = int(r.get("beam_signature_count") or 0)

    if track == "PRE_IGNITION":
        score += lead
        if intensity >= 3 and p5 >= 0.05:
            score += 8
    elif track == "ACCELERATOR":
        if intensity >= 3 and p5 >= 0.15:
            score += 10
        if p5 < 0:
            score -= 12
    elif track == "REACCELERATION_SPECIAL":
        score += 6
    elif track == "CHASE_EXCLUDED":
        score -= 80
    else:
        score -= 18

    r["beam_score_v18"] = round(score, 2)
    base_eligible = bool(r.get("beam_eligible")) and not r.get("warning_flag")
    hard_eligible = base_eligible and track in {"PRE_IGNITION", "ACCELERATOR", "REACCELERATION_SPECIAL"}
    r["beam_eligible_v18"] = hard_eligible

    status = "WATCH"
    if track == "CHASE_EXCLUDED":
        status = "NO_NEW_BUY_OVER_20PCT"
    elif track == "WATCH_NEGATIVE":
        status = "WATCH_NEGATIVE"
    elif not hard_eligible:
        status = "WATCH"
    elif track == "PRE_IGNITION":
        if score >= 90 and sig >= 3 and intensity >= 1.8 and p5 >= -0.05:
            status = "ENTER_CANDIDATE"
        elif score >= 76 and sig >= 2:
            status = "WAIT_CONFIRMATION"
    elif track == "ACCELERATOR":
        if score >= 92 and sig >= 4 and intensity >= 2.0 and p5 >= 0.15:
            status = "ENTER_CANDIDATE"
        elif score >= 80 and sig >= 3:
            status = "WAIT_CONFIRMATION"
    elif track == "REACCELERATION_SPECIAL":
        if score >= 110 and sig >= 5:
            status = "ENTER_CANDIDATE"
        else:
            status = "WAIT_CONFIRMATION"

    r.setdefault("entry_plan", {})["status"] = status
    return r


def tracking_priority(row):
    status = (row.get("entry_plan") or {}).get("status")
    track = row.get("hard_track")
    status_rank = {"ENTER_CANDIDATE": 3, "WAIT_CONFIRMATION": 2, "WATCH": 1}.get(status, 0)
    track_rank = {"PRE_IGNITION": 3, "ACCELERATOR": 2, "REACCELERATION_SPECIAL": 1}.get(track, 0)
    return (status_rank, track_rank, fnum(row.get("beam_score_v18")))


def update_first_seen(rows, generated_at):
    hist = load_json(FIRST_SEEN_FILE, {})
    if not isinstance(hist, dict):
        hist = {}
    for r in rows:
        if not r.get("beam_eligible_v18"):
            continue
        base = r.get("base")
        if not base:
            continue
        price = fnum(r.get("price_krw"))
        chg = fnum(r.get("change_24h_pct"))
        score = fnum(r.get("beam_score_v18"))
        item = hist.get(base)
        if not isinstance(item, dict):
            item = {
                "first_detected_at_utc": generated_at,
                "first_price_krw": price,
                "first_change_24h_pct": chg,
                "first_score": score,
                "first_track": r.get("hard_track"),
            }
        item["last_seen_at_utc"] = generated_at
        item["last_price_krw"] = price
        item["last_change_24h_pct"] = chg
        item["last_score"] = score
        item["last_track"] = r.get("hard_track")
        item["peak_price_krw"] = max(fnum(item.get("peak_price_krw")), price)
        item["peak_change_24h_pct"] = max(fnum(item.get("peak_change_24h_pct"), -999), chg)
        first_price = fnum(item.get("first_price_krw"))
        item["return_since_first_pct"] = round((price / first_price - 1) * 100, 2) if first_price else None
        item["peak_return_since_first_pct"] = round((item["peak_price_krw"] / first_price - 1) * 100, 2) if first_price else None
        hist[base] = item
    Path(FIRST_SEEN_FILE).write_text(json.dumps(hist, ensure_ascii=False, indent=2), encoding="utf-8")
    return hist


def compact(r, first_seen):
    base = r.get("base")
    return {
        "base": base,
        "market": r.get("market"),
        "korean_name": r.get("korean_name"),
        "price_krw": r.get("price_krw"),
        "change_24h_pct": r.get("change_24h_pct"),
        "trade_24h_krw": r.get("trade_24h_krw"),
        "track": r.get("hard_track"),
        "beam_score": r.get("beam_score_v18"),
        "signature": r.get("beam_signature_count"),
        "money_leads_price_score": r.get("money_leads_price_score"),
        "status": (r.get("entry_plan") or {}).get("status"),
        "entry_plan": r.get("entry_plan"),
        "fast_5m": r.get("fast_5m"),
        "m5": r.get("m5"),
        "m15": r.get("m15"),
        "h1": r.get("h1"),
        "h4": r.get("h4"),
        "d1": r.get("d1"),
        "orderbook": r.get("orderbook"),
        "trade_flow": r.get("trade_flow"),
        "warning": r.get("warning_flag"),
        "first_detection": first_seen.get(base),
    }


def postprocess_v18():
    result = load_json("beam_scan_result.json", {})
    rows = [apply_hard_gates(r) for r in (result.get("stage2_ranked") or [])]
    rows.sort(key=tracking_priority, reverse=True)
    generated_at = result.get("generated_at_utc") or datetime.now(timezone.utc).isoformat()
    first_seen = update_first_seen(rows, generated_at)

    eligible = [r for r in rows if r.get("beam_eligible_v18")]
    pre = [r for r in eligible if r.get("hard_track") == "PRE_IGNITION"]
    accel = [r for r in eligible if r.get("hard_track") == "ACCELERATOR"]
    special = [r for r in eligible if r.get("hard_track") == "REACCELERATION_SPECIAL"]

    def status_sort(group):
        return sorted(group, key=tracking_priority, reverse=True)

    pre, accel, special = status_sort(pre), status_sort(accel), status_sort(special)
    enter_pre = [r for r in pre if (r.get("entry_plan") or {}).get("status") == "ENTER_CANDIDATE"]
    enter_accel = [r for r in accel if (r.get("entry_plan") or {}).get("status") == "ENTER_CANDIDATE"]
    wait_pre = [r for r in pre if (r.get("entry_plan") or {}).get("status") == "WAIT_CONFIRMATION"]
    wait_accel = [r for r in accel if (r.get("entry_plan") or {}).get("status") == "WAIT_CONFIRMATION"]
    enter_special = [r for r in special if (r.get("entry_plan") or {}).get("status") == "ENTER_CANDIDATE"]
    wait_special = [r for r in special if (r.get("entry_plan") or {}).get("status") == "WAIT_CONFIRMATION"]

    # Prefer early entries. Accelerator candidates fill gaps; >20% special re-entries are last resort only.
    ordered = enter_pre + enter_accel + wait_pre + wait_accel + enter_special + wait_special
    seen = set()
    candidates = []
    for r in ordered:
        if r.get("base") in seen:
            continue
        seen.add(r.get("base"))
        candidates.append(r)
        if len(candidates) == 2:
            break

    result["source_version"] = VERSION
    result["stage2_ranked"] = rows
    result["trade_candidates"] = candidates
    result["pre_ignition_top5"] = pre[:5]
    result["accelerator_top5"] = accel[:5]
    result["reacceleration_special_top5"] = special[:5]
    result["hard_gate_policy"] = {
        "pre_ignition_change_24h_pct": "0<=x<8",
        "accelerator_change_24h_pct": "8<=x<=20",
        "over_20_pct": "excluded from new-buy ranking unless strict reacceleration special",
        "ranking_priority": "money-leads-price first; PRE preferred over ACCEL",
        "first_detection_tracking": FIRST_SEEN_FILE,
    }
    result.setdefault("health", {})["analysis_ready"] = bool(len(candidates) >= 2 and result.get("health", {}).get("analysis_ready", True))
    if len(candidates) < 2:
        result["health"].setdefault("hard_blockers", []).append("fewer_than_two_v18_candidates")

    Path("beam_scan_result.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    summary = {
        "generated_at_utc": generated_at,
        "source_version": VERSION,
        "health": result.get("health"),
        "hard_gate_policy": result.get("hard_gate_policy"),
        "trade_candidates": [compact(r, first_seen) for r in candidates],
        "pre_ignition_top5": [compact(r, first_seen) for r in pre[:5]],
        "accelerator_top5": [compact(r, first_seen) for r in accel[:5]],
        "top10_calibrated": [compact(r, first_seen) for r in rows[:10]],
    }
    Path("beam_latest_summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    Path("beam_top10_compact.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")


def main():
    v17.main()
    postprocess_v18()
    print(json.dumps(load_json("beam_top10_compact.json", {}), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
