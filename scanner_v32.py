import json
from datetime import datetime, timezone
from pathlib import Path

VERSION = "v32-independent-early-alert-delivery"
BRIDGE_FILE = "beam_breakout_bridge.json"
EARLY_FILE = "beam_early_strong.json"
MISS_FILE = "beam_miss_audit.json"
FINAL_FILE = "beam_final_trade_gate.json"
OUT_FILE = "beam_delivery_alert.json"
STATE_FILE = "delivery_alert_state.json"
HISTORY_FILE = "delivery_alert_history.json"
HISTORY_LIMIT = 336  # 7 days at 30-minute cadence


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
    immediate = bool(row.get("immediate_quality")) or grade.startswith("A_")
    tier = "URGENT_EARLY" if immediate else "WATCH_EARLY"
    ep = row.get("episode") or {}
    key = f"{row.get('base')}|{tier}|{ep.get('started_at_utc') or row.get('lifetime_first_detected_at_utc') or 'na'}"
    return {
        "alert_key": key,
        "base": row.get("base"),
        "market": row.get("market"),
        "korean_name": row.get("korean_name"),
        "tier": tier,
        "source": source,
        "rank": rank,
        "must_surface_in_chat": True if source == "bridge_promoted" else rank <= 2,
        "price_krw": row.get("price_krw"),
        "change_24h_pct": row.get("change_24h_pct"),
        "trade_24h_krw": row.get("trade_24h_krw"),
        "promotion_grade": row.get("promotion_grade"),
        "score": row.get("score"),
        "priority_score": row.get("priority_score"),
        "signal_count": row.get("signal_count"),
        "flow_confirmation_count": row.get("flow_confirmation_count"),
        "episode": ep,
        "metrics": row.get("metrics") or {},
        "entry_plan": row.get("entry_plan") or {},
        "execution_permission": "EXTERNAL_RISK_CHECK_REQUIRED",
        "note": "Early breakout alert. Surface this even when the strict FINAL_BUY gate is NO_BUY; it is an early warning, not a full-size buy authorization.",
    }


def candidate_from_early(row, rank, source):
    if not isinstance(row, dict) or not row.get("base"):
        return None
    grade = str(row.get("grade") or "")
    tier = "URGENT_EARLY" if grade == "EARLY_STRONG_TEST" else "WATCH_EARLY"
    key = f"{row.get('base')}|{tier}|{row.get('first_detected_at_utc') or 'na'}"
    return {
        "alert_key": key,
        "base": row.get("base"),
        "market": row.get("market"),
        "tier": tier,
        "source": source,
        "rank": rank,
        "must_surface_in_chat": rank <= 2,
        "price_krw": row.get("price_krw"),
        "change_24h_pct": row.get("change_24h_pct"),
        "trade_24h_krw": row.get("trade_24h_krw"),
        "promotion_grade": grade,
        "score": row.get("score"),
        "first_detected_price_krw": row.get("first_detected_price_krw"),
        "first_detected_change_24h_pct": row.get("first_detected_change_24h_pct"),
        "relative_strength_vs_stronger_major_pct": row.get("relative_strength_vs_stronger_major_pct"),
        "trade_flow": row.get("trade_flow") or {},
        "orderbook": row.get("orderbook") or {},
        "entry_plan": row.get("entry_plan") or {},
        "execution_permission": "EXTERNAL_RISK_CHECK_REQUIRED",
        "note": "Early-strong alert. Delivery must not be suppressed by the strict final buy gate.",
    }


def dedupe(alerts):
    out = []
    seen = set()
    for a in alerts:
        if not a:
            continue
        base = a.get("base")
        if not base or base in seen:
            continue
        seen.add(base)
        out.append(a)
    return out


def audit_movers(miss):
    out = []
    for row in miss.get("current_top_movers") or []:
        if row.get("capture") != "EARLY_CAPTURE":
            continue
        first_ch = fnum(row.get("first_detected_change_24h_pct"), 999.0)
        current_ch = fnum(row.get("change_24h_pct"), -999.0)
        if first_ch < 8.0 and current_ch >= 10.0:
            out.append({
                "base": row.get("base"),
                "first_detected_change_24h_pct": row.get("first_detected_change_24h_pct"),
                "first_detected_price_krw": row.get("first_detected_price_krw"),
                "current_change_24h_pct": row.get("change_24h_pct"),
                "current_price_krw": row.get("price_krw"),
                "capture": "EARLY_CAPTURE",
                "lesson": "Scanner detected this before the move. Delivery must not wait for FINAL_BUY confirmation.",
            })
    return out[:20]


def main():
    now = datetime.now(timezone.utc).isoformat()
    bridge = load_json(BRIDGE_FILE, {})
    early = load_json(EARLY_FILE, {})
    miss = load_json(MISS_FILE, {})
    final = load_json(FINAL_FILE, {})
    previous = load_json(STATE_FILE, {})
    history = load_json(HISTORY_FILE, [])
    if not isinstance(history, list):
        history = []

    alerts = []
    for i, row in enumerate(bridge.get("promoted_top3") or [], start=1):
        alerts.append(candidate_from_bridge(row, i, "bridge_promoted"))
    for i, row in enumerate(bridge.get("watch_top5") or [], start=1):
        alerts.append(candidate_from_bridge(row, i, "bridge_watch"))

    early_rows = [
        (early.get("newly_promoted"), 1, "early_newly_promoted"),
        (early.get("best_test_candidate"), 2, "early_best_test"),
        (early.get("best_watch_candidate"), 3, "early_best_watch"),
    ]
    for row, rank, source in early_rows:
        alerts.append(candidate_from_early(row, rank, source))

    alerts = dedupe(alerts)
    urgent = [x for x in alerts if x.get("tier") == "URGENT_EARLY"]
    watch = [x for x in alerts if x.get("tier") == "WATCH_EARLY"]
    urgent.sort(key=lambda x: (fnum(x.get("priority_score"), fnum(x.get("score"))), fnum(x.get("trade_24h_krw"))), reverse=True)
    watch.sort(key=lambda x: (fnum(x.get("priority_score"), fnum(x.get("score"))), fnum(x.get("trade_24h_krw"))), reverse=True)
    alerts = (urgent + watch)[:8]

    prev_keys = set(previous.get("active_alert_keys") or [])
    active_keys = [x.get("alert_key") for x in alerts if x.get("alert_key")]
    new_alerts = [x for x in alerts if x.get("alert_key") not in prev_keys]

    bridge_promoted = [x.get("base") for x in (bridge.get("promoted_top3") or []) if x.get("base")]
    delivered_bases = [x.get("base") for x in alerts]
    missing_promoted = [b for b in bridge_promoted if b not in delivered_bases]
    if missing_promoted:
        raise RuntimeError(f"DELIVERY_REGRESSION_MISSING_PROMOTED:{missing_promoted}")

    mover_audit = audit_movers(miss)
    result = {
        "generated_at_utc": now,
        "version": VERSION,
        "status": "EARLY_ALERT" if alerts else "NO_EARLY_ALERT",
        "delivery_required": bool(alerts),
        "primary_alert": alerts[0] if alerts else None,
        "alerts": alerts,
        "new_alerts": new_alerts,
        "strict_final_trade_gate_status": final.get("status"),
        "delivery_contract": {
            "independent_of_final_buy_gate": True,
            "must_surface_bridge_promoted_top3": True,
            "no_silent_drop_when_final_gate_no_buy": True,
            "early_alert_is_not_full_size_buy_authorization": True,
            "external_news_unlock_listing_delisting_tokenomics_check_required_before_money": True,
            "goal": "surface asymmetric breakout candidates before confirmation; never promise a 2x outcome",
        },
        "regression_checks": {
            "bridge_promoted_count": len(bridge_promoted),
            "bridge_promoted_all_delivered": not missing_promoted,
            "missing_promoted": missing_promoted,
            "historical_early_capture_examples": mover_audit,
        },
        "root_cause_guard": {
            "old_failure_mode": "assistant_or_consumer_read_only_strict_FINAL_BUY_and_ignored_early_capture_outputs",
            "fix": "dedicated_delivery_file_surfaces_early_candidates_even_when_FINAL_BUY_is_NO_BUY",
        },
    }

    state = {
        "updated_at_utc": now,
        "active_alert_keys": active_keys,
        "active_bases": delivered_bases,
        "last_status": result["status"],
    }
    history.append({
        "generated_at_utc": now,
        "status": result["status"],
        "primary_base": (result.get("primary_alert") or {}).get("base"),
        "alert_bases": delivered_bases,
        "new_alert_bases": [x.get("base") for x in new_alerts],
        "final_gate_status": final.get("status"),
    })

    save_json(OUT_FILE, result)
    save_json(STATE_FILE, state)
    save_json(HISTORY_FILE, history[-HISTORY_LIMIT:])
    print(json.dumps({
        "version": VERSION,
        "status": result["status"],
        "primary": (result.get("primary_alert") or {}).get("base"),
        "alerts": delivered_bases,
        "new_alerts": [x.get("base") for x in new_alerts],
        "final_gate": final.get("status"),
    }, ensure_ascii=False))


if __name__ == "__main__":
    main()
