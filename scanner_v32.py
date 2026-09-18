import json
from datetime import datetime, timezone
from pathlib import Path

VERSION = "v32.3-early-bench-visible-delivery"
BRIDGE_FILE = "beam_breakout_bridge.json"
EARLY_FILE = "beam_early_strong.json"
MISS_FILE = "beam_miss_audit.json"
FINAL_FILE = "beam_final_trade_gate.json"
OUT_FILE = "beam_delivery_alert.json"
STATE_FILE = "delivery_alert_state.json"
HISTORY_FILE = "delivery_alert_history.json"
HISTORY_LIMIT = 336


def load_json(path, default):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except Exception:
        return default


def save_json(path, obj):
    Path(path).write_text(json.dumps(obj, ensure_ascii=False, indent=2), encoding="utf-8")


def fnum(v, default=0.0):
    try:
        return float(v)
    except Exception:
        return default


def candidate_from_bridge(row, rank, source):
    if not isinstance(row, dict) or not row.get("base"):
        return None
    grade = str(row.get("promotion_grade") or "")
    execution_allowed = row.get("execution_allowed") is True
    tracking = row.get("tracking") or {}
    winner_match = grade == "A_URGENT_WINNER_MATCH" and execution_allowed
    continuation_hold = grade == "A_URGENT_CONTINUATION" and execution_allowed and tracking.get("status") == "HOLD"
    actionable = winner_match or continuation_hold
    tier = "URGENT_EARLY" if source == "bridge_promoted" and actionable else "WATCH_EARLY"
    ep = row.get("episode") or {}
    key = f"{row.get('base')}|{tier}|{ep.get('started_at_utc') or row.get('lifetime_first_detected_at_utc') or 'na'}"
    note = "Fresh winner-matched early breakout; external risk check required before money." if winner_match else ("Stateful continuation HOLD; same primary preserved until explicit invalidation or confirmed replacement. External risk check required before money." if continuation_hold else "Watch only; neither fresh winner-match nor continuity HOLD authorized execution.")
    return {
        "alert_key": key, "base": row.get("base"), "market": row.get("market"), "korean_name": row.get("korean_name"),
        "tier": tier, "source": source, "rank": rank, "must_surface_in_chat": True if tier == "URGENT_EARLY" else rank <= 2,
        "price_krw": row.get("price_krw"), "change_24h_pct": row.get("change_24h_pct"), "trade_24h_krw": row.get("trade_24h_krw"),
        "promotion_grade": grade, "score": row.get("score"), "priority_score": row.get("priority_score"),
        "signal_count": row.get("signal_count"), "flow_confirmation_count": row.get("flow_confirmation_count"),
        "episode": ep, "metrics": row.get("metrics") or {}, "entry_plan": row.get("entry_plan") or {}, "tracking": tracking,
        "execution_allowed": execution_allowed, "execution_permission": "EXTERNAL_RISK_CHECK_REQUIRED" if actionable else "WATCH_ONLY", "note": note,
    }


def candidate_from_early(row, rank, source, promoted_bases):
    if not isinstance(row, dict) or not row.get("base"):
        return None
    grade = str(row.get("grade") or "")
    base = row.get("base")
    key = f"{base}|WATCH_EARLY|{row.get('first_detected_at_utc') or 'na'}"
    return {
        "alert_key": key, "base": base, "market": row.get("market"), "tier": "WATCH_EARLY", "source": source,
        "rank": rank, "must_surface_in_chat": (source in {"early_newly_promoted","early_best_test","early_best_watch","early_ranked"} and rank <= 3), "price_krw": row.get("price_krw"), "change_24h_pct": row.get("change_24h_pct"),
        "trade_24h_krw": row.get("trade_24h_krw"), "promotion_grade": grade, "score": row.get("score"),
        "first_detected_price_krw": row.get("first_detected_price_krw"), "first_detected_change_24h_pct": row.get("first_detected_change_24h_pct"),
        "relative_strength_vs_stronger_major_pct": row.get("relative_strength_vs_stronger_major_pct"),
        "trade_flow": row.get("trade_flow") or {}, "orderbook": row.get("orderbook") or {}, "entry_plan": row.get("entry_plan") or {},
        "execution_allowed": False, "execution_permission": "WATCH_ONLY",
        "note": "Raw early detector is watch-only. Actionable URGENT requires fresh winner match or v36 continuation HOLD in bridge_promoted."
    }


def dedupe(alerts):
    out, seen = [], set()
    for a in alerts:
        if not a or not a.get("base") or a.get("base") in seen:
            continue
        seen.add(a["base"]); out.append(a)
    return out


def audit_movers(miss):
    out = []
    for row in miss.get("current_top_movers") or []:
        if row.get("capture") == "EARLY_CAPTURE" and fnum(row.get("first_detected_change_24h_pct"), 999) < 8 and fnum(row.get("change_24h_pct"), -999) >= 10:
            out.append({"base": row.get("base"), "first_detected_change_24h_pct": row.get("first_detected_change_24h_pct"), "first_detected_price_krw": row.get("first_detected_price_krw"), "current_change_24h_pct": row.get("change_24h_pct"), "current_price_krw": row.get("price_krw"), "capture": "EARLY_CAPTURE"})
    return out[:20]


def main():
    now = datetime.now(timezone.utc).isoformat()
    bridge, early, miss, final = load_json(BRIDGE_FILE, {}), load_json(EARLY_FILE, {}), load_json(MISS_FILE, {}), load_json(FINAL_FILE, {})
    previous, history = load_json(STATE_FILE, {}), load_json(HISTORY_FILE, [])
    if not isinstance(history, list): history = []

    promoted_rows = bridge.get("promoted_top3") or []
    promoted_bases = {x.get("base") for x in promoted_rows if x.get("base")}
    alerts = [candidate_from_bridge(r, i, "bridge_promoted") for i, r in enumerate(promoted_rows, 1)]
    alerts += [candidate_from_bridge(r, i, "bridge_watch") for i, r in enumerate(bridge.get("watch_top5") or [], 1)]
    early_rows = [(early.get("newly_promoted"),1,"early_newly_promoted"),(early.get("best_test_candidate"),2,"early_best_test"),(early.get("best_watch_candidate"),3,"early_best_watch")]
    alerts += [candidate_from_early(r, rank, src, promoted_bases) for r, rank, src in early_rows]
    # Surface the ranked early-strong bench directly so genuine pre-move candidates are not buried
    # behind stricter execution gates or transient bridge rankings. These remain WATCH_ONLY.
    ranked_early = [r for r in (early.get("ranked_candidates") or []) if isinstance(r, dict) and r.get("grade") != "REJECT"][:5]
    alerts += [candidate_from_early(r, i, "early_ranked", promoted_bases) for i, r in enumerate(ranked_early, 1)]
    alerts = dedupe(alerts)

    urgent = [x for x in alerts if x.get("tier") == "URGENT_EARLY"]
    watch = [x for x in alerts if x.get("tier") == "WATCH_EARLY"]
    urgent.sort(key=lambda x:(fnum(x.get("priority_score"),fnum(x.get("score"))),fnum(x.get("trade_24h_krw"))), reverse=True)
    watch.sort(key=lambda x:(fnum(x.get("priority_score"),fnum(x.get("score"))),fnum(x.get("trade_24h_krw"))), reverse=True)
    must_surface_watch = [x for x in watch if x.get("must_surface_in_chat")]
    other_watch = [x for x in watch if not x.get("must_surface_in_chat")]
    # Keep urgent first, but reserve visibility for the early-strong bench. Visibility never grants execution.
    alerts = dedupe(urgent + must_surface_watch + other_watch)[:12]

    allowed_grades = {"A_URGENT_WINNER_MATCH", "A_URGENT_CONTINUATION"}
    illegal = [x.get("base") for x in urgent if x.get("base") not in promoted_bases or x.get("promotion_grade") not in allowed_grades or x.get("execution_allowed") is not True]
    if illegal: raise RuntimeError(f"ILLEGAL_URGENT_BYPASS:{illegal}")

    prev_keys = set(previous.get("active_alert_keys") or [])
    active_keys = [x.get("alert_key") for x in alerts if x.get("alert_key")]
    new_alerts = [x for x in alerts if x.get("alert_key") not in prev_keys]
    delivered_bases = [x.get("base") for x in alerts]
    missing_promoted = [b for b in promoted_bases if b not in delivered_bases]
    if missing_promoted: raise RuntimeError(f"DELIVERY_REGRESSION_MISSING_PROMOTED:{missing_promoted}")

    result = {
      "generated_at_utc":now,"version":VERSION,"status":"URGENT_PRIMARY_ALERT" if urgent else ("WATCH_ONLY" if alerts else "NO_EARLY_ALERT"),
      "delivery_required":bool(urgent),"primary_alert":urgent[0] if urgent else None,"alerts":alerts,"new_alerts":new_alerts,
      "strict_final_trade_gate_status":final.get("status"),
      "delivery_contract":{"actionable_urgent_requires_fresh_winner_or_v36_continuation":True,"raw_early_is_watch_only":True,"external_risk_check_required_before_money":True,"never_promise_2x":True},
      "early_strong_ranked_top5":[x for x in alerts if x.get("source") == "early_ranked"][:5],
      "regression_checks":{"bridge_promoted_count":len(promoted_bases),"bridge_promoted_all_delivered":not missing_promoted,"illegal_urgent_bypass":illegal,"historical_early_capture_examples":audit_movers(miss)},
      "root_cause_guard":{"old_failure_mode":"primary changed every scan because selection was stateless","fix":"v36 locks a primary across scans; v32 only delivers fresh winners or explicit v36 HOLD continuations"}}
    state={"updated_at_utc":now,"active_alert_keys":active_keys,"active_bases":delivered_bases,"last_status":result["status"],"primary_base":(result.get("primary_alert") or {}).get("base")}
    history.append({"generated_at_utc":now,"status":result["status"],"primary_base":(result.get("primary_alert") or {}).get("base"),"alert_bases":delivered_bases,"new_alert_bases":[x.get("base") for x in new_alerts],"final_gate_status":final.get("status")})
    save_json(OUT_FILE,result); save_json(STATE_FILE,state); save_json(HISTORY_FILE,history[-HISTORY_LIMIT:])
    print(json.dumps({"version":VERSION,"status":result["status"],"primary":(result.get("primary_alert") or {}).get("base"),"alerts":delivered_bases,"urgent":[x.get("base") for x in urgent]},ensure_ascii=False))

if __name__ == "__main__": main()
