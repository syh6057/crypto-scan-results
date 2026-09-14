import json
from datetime import datetime, timezone
from pathlib import Path

VERSION = "v35-durable-urgent-outcome-bank"
BRIDGE_FILE = "beam_breakout_bridge.json"
AUDIT_FILE = "beam_miss_audit.json"
BRIDGE_HISTORY_FILE = "breakout_bridge_history.json"
OUTCOME_FILE = "urgent_outcome_history.json"
BANK_FILE = "urgent_winner_bank.json"
SUMMARY_FILE = "urgent_outcome_summary.json"
MAX_EVENTS = 600
MAX_OBSERVATIONS = 12
FAIL_FAST_PCT = -2.5
SUCCESS_5_PCT = 5.0
SUCCESS_10_PCT = 10.0
EXPIRE_OBSERVATIONS = 8


def load_json(path, default):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except Exception:
        return default


def save_json(path, value):
    Path(path).write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")


def f(v, default=0.0):
    try:
        return float(v)
    except Exception:
        return default


def iter_history_rows(history):
    for snap in history if isinstance(history, list) else []:
        for key in ("promoted", "watch"):
            for row in snap.get(key) or []:
                if isinstance(row, dict) and row.get("base"):
                    yield row


def earliest_rows_by_base(history):
    out = {}
    for row in iter_history_rows(history):
        base = row.get("base")
        if base and base not in out:
            out[base] = row
    return out


def raw_urgent_candidates(bridge):
    rows, seen = [], set()
    for row in (bridge.get("promoted_top3") or []) + (bridge.get("watch_top5") or []):
        if not isinstance(row, dict):
            continue
        base = row.get("base")
        if not base or base in seen:
            continue
        was_urgent = bool(row.get("execution_gate")) or str(row.get("promotion_grade") or "").startswith("A_")
        if was_urgent:
            seen.add(base)
            rows.append(row)
    return rows


def all_bridge_rows(bridge):
    out = {}
    for row in (bridge.get("promoted_top3") or []) + (bridge.get("watch_top5") or []) + (bridge.get("top_candidates") or []):
        if isinstance(row, dict) and row.get("base") and row.get("base") not in out:
            out[row["base"]] = row
    return out


def event_id(row):
    ep = row.get("episode") or {}
    anchor = ep.get("started_at_utc") or row.get("lifetime_first_detected_at_utc") or row.get("first_detected_at_utc") or "na"
    return f"{row.get('base')}|{anchor}"


def pct(cur, start):
    return ((cur / start) - 1.0) * 100.0 if start else 0.0


def snapshot_for_training(row):
    keep = {
        "base": row.get("base"), "market": row.get("market"), "price_krw": row.get("price_krw"),
        "change_24h_pct": row.get("change_24h_pct"), "trade_24h_krw": row.get("trade_24h_krw"),
        "lane": row.get("lane"), "score": row.get("score"), "raw_score": row.get("raw_score"),
        "priority_score": row.get("priority_score"), "signal_count": row.get("signal_count"),
        "flow_confirmation_count": row.get("flow_confirmation_count"), "promotion_grade": row.get("promotion_grade"),
        "episode": row.get("episode") or {}, "metrics": row.get("metrics") or {},
    }
    return keep


def ensure_bank(bank):
    if not isinstance(bank, dict):
        bank = {}
    bank.setdefault("version", VERSION)
    bank.setdefault("updated_at_utc", None)
    bank.setdefault("positive", {})
    bank.setdefault("negative", {})
    bank.setdefault("giveback", {})
    return bank


def add_positive(bank, base, snapshot, source, label, now):
    if not base or not snapshot:
        return
    bank["positive"][base] = {"base": base, "label": label, "source": source, "added_at_utc": now, "snapshot": snapshot}
    bank["negative"].pop(base, None)


def add_negative(bank, base, snapshot, source, label, now):
    if not base or not snapshot or base in bank["positive"]:
        return
    bank["negative"][base] = {"base": base, "label": label, "source": source, "added_at_utc": now, "snapshot": snapshot}


def classify(event):
    mfe = f(event.get("mfe_pct"), 0.0)
    mae = f(event.get("mae_pct"), 0.0)
    cur = f(event.get("current_return_pct"), 0.0)
    obs = len(event.get("observations") or [])
    if mfe >= SUCCESS_10_PCT:
        if cur <= 2.0:
            return "SUCCESS_THEN_GIVEBACK"
        return "SUCCESS_FORWARD_10"
    if mfe >= SUCCESS_5_PCT:
        return "SUCCESS_FORWARD_5"
    if mae <= FAIL_FAST_PCT and mfe < SUCCESS_5_PCT:
        return "FAIL_FAST_REVERSAL"
    if obs >= EXPIRE_OBSERVATIONS:
        return "EXPIRED_NO_EDGE"
    return "OPEN"


def main():
    now = datetime.now(timezone.utc).isoformat()
    bridge = load_json(BRIDGE_FILE, {})
    audit = load_json(AUDIT_FILE, {})
    bridge_history = load_json(BRIDGE_HISTORY_FILE, [])
    earliest = earliest_rows_by_base(bridge_history)
    events = load_json(OUTCOME_FILE, {})
    if not isinstance(events, dict):
        events = {}
    bank = ensure_bank(load_json(BANK_FILE, {}))

    # Durable legacy seeds: once an early-captured >=10% mover is observed, keep its earliest snapshot permanently.
    for x in audit.get("current_top_movers") or []:
        base = x.get("base")
        if base and x.get("capture") == "EARLY_CAPTURE" and f(x.get("change_24h_pct")) >= 10.0 and base in earliest:
            add_positive(bank, base, snapshot_for_training(earliest[base]), "miss_audit_early_capture", "LEGACY_EARLY_CAPTURE_10", now)

    # Seed negatives only when the same base has never been proven positive.
    for x in audit.get("failed_acceleration_examples") or []:
        base = x.get("base")
        if base and base in earliest and base not in bank["positive"]:
            add_negative(bank, base, snapshot_for_training(earliest[base]), "miss_audit_failed_acceleration", "LEGACY_FAILED_ACCELERATION", now)

    raw = raw_urgent_candidates(bridge)
    current_rows = all_bridge_rows(bridge)

    # Register every actual URGENT event before v34 selection, preserving the exact alert-time snapshot.
    for row in raw:
        eid = event_id(row)
        if eid not in events:
            start = f(row.get("price_krw"))
            events[eid] = {
                "event_id": eid, "base": row.get("base"), "started_at_utc": now,
                "anchor_started_at_utc": (row.get("episode") or {}).get("started_at_utc"),
                "start_price_krw": start, "start_change_24h_pct": row.get("change_24h_pct"),
                "snapshot": snapshot_for_training(row), "mfe_pct": 0.0, "mae_pct": 0.0,
                "current_return_pct": 0.0, "label": "OPEN", "observations": []
            }

    # Update all open events whenever the base is still observable in the bridge universe.
    for eid, ev in list(events.items()):
        if ev.get("label") not in (None, "OPEN", "SUCCESS_FORWARD_5"):
            continue
        base = ev.get("base")
        row = current_rows.get(base)
        if not row:
            continue
        cur = f(row.get("price_krw"))
        start = f(ev.get("start_price_krw"))
        ret = pct(cur, start) if cur and start else 0.0
        obs = ev.setdefault("observations", [])
        obs.append({"at_utc": now, "price_krw": cur, "return_pct": round(ret, 4), "change_24h_pct": row.get("change_24h_pct")})
        ev["observations"] = obs[-MAX_OBSERVATIONS:]
        ev["current_return_pct"] = round(ret, 4)
        ev["mfe_pct"] = round(max(f(ev.get("mfe_pct")), ret), 4)
        ev["mae_pct"] = round(min(f(ev.get("mae_pct")), ret), 4)
        label = classify(ev)
        ev["label"] = label
        ev["updated_at_utc"] = now

        if label in ("SUCCESS_FORWARD_10", "SUCCESS_THEN_GIVEBACK"):
            add_positive(bank, base, ev.get("snapshot"), "forward_urgent_outcome", label, now)
            if label == "SUCCESS_THEN_GIVEBACK":
                bank["giveback"][base] = {"base": base, "label": label, "added_at_utc": now, "snapshot": ev.get("snapshot")}
        elif label == "FAIL_FAST_REVERSAL":
            add_negative(bank, base, ev.get("snapshot"), "forward_urgent_outcome", label, now)

    # Keep event history bounded while never preferring to delete open events.
    if len(events) > MAX_EVENTS:
        ordered = sorted(events.items(), key=lambda kv: kv[1].get("started_at_utc") or "")
        removable = [k for k, v in ordered if v.get("label") != "OPEN"]
        for k in removable[:max(0, len(events) - MAX_EVENTS)]:
            events.pop(k, None)

    bank["updated_at_utc"] = now
    bank["version"] = VERSION
    save_json(OUTCOME_FILE, events)
    save_json(BANK_FILE, bank)

    labels = {}
    for ev in events.values():
        labels[ev.get("label", "UNKNOWN")] = labels.get(ev.get("label", "UNKNOWN"), 0) + 1
    summary = {
        "generated_at_utc": now, "version": VERSION, "tracked_events": len(events), "labels": labels,
        "positive_bank_size": len(bank["positive"]), "negative_bank_size": len(bank["negative"]),
        "giveback_bank_size": len(bank["giveback"]),
        "positive_bases": sorted(bank["positive"].keys()), "negative_bases": sorted(bank["negative"].keys()),
        "new_raw_urgent": [x.get("base") for x in raw],
        "principle": "Persist actual URGENT snapshots and label forward MFE/MAE; >=10% success stays positive even if it later gives back, while fast reversal is a separate negative class."
    }
    save_json(SUMMARY_FILE, summary)
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
