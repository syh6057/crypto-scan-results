import json
from datetime import datetime, timezone
from pathlib import Path

VERSION = "v39-full-market-forward-outcomes"
MARKET_FILE = "all_market_snapshot.json"
OUTCOME_FILE = "urgent_outcome_history.json"
BANK_FILE = "urgent_winner_bank.json"
SUMMARY_FILE = "full_market_outcome_summary.json"
MAX_OBSERVATIONS = 24
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


def ret_pct(cur, start):
    return ((cur / start) - 1.0) * 100.0 if cur and start else 0.0


def classify(event):
    mfe = f(event.get("mfe_pct"))
    mae = f(event.get("mae_pct"))
    cur = f(event.get("current_return_pct"))
    obs = len(event.get("observations") or [])
    if mfe >= SUCCESS_10_PCT:
        return "SUCCESS_THEN_GIVEBACK" if cur <= 2.0 else "SUCCESS_FORWARD_10"
    if mfe >= SUCCESS_5_PCT:
        return "SUCCESS_FORWARD_5"
    if mae <= FAIL_FAST_PCT and mfe < SUCCESS_5_PCT:
        return "FAIL_FAST_REVERSAL"
    if obs >= EXPIRE_OBSERVATIONS:
        return "EXPIRED_NO_EDGE"
    return "OPEN"


def ensure_bank(bank):
    if not isinstance(bank, dict): bank = {}
    bank.setdefault("positive", {})
    bank.setdefault("negative", {})
    bank.setdefault("giveback", {})
    return bank


def main():
    now = datetime.now(timezone.utc).isoformat()
    market = load_json(MARKET_FILE, {})
    rows = market.get("rows") or {}
    events = load_json(OUTCOME_FILE, {})
    if not isinstance(events, dict): events = {}
    bank = ensure_bank(load_json(BANK_FILE, {}))

    updated = 0
    missing = []
    transitions = []
    for eid, ev in events.items():
        if ev.get("label") not in (None, "OPEN", "SUCCESS_FORWARD_5"):
            continue
        base = ev.get("base")
        mr = rows.get(base)
        if not mr:
            missing.append(base)
            continue
        cur = f(mr.get("price_krw"))
        start = f(ev.get("start_price_krw"))
        if not cur or not start:
            continue
        r = ret_pct(cur, start)
        old_label = ev.get("label") or "OPEN"
        obs = ev.setdefault("observations", [])
        obs.append({
            "at_utc": now,
            "price_krw": cur,
            "return_pct": round(r, 4),
            "change_24h_pct": mr.get("change_24h_pct"),
            "source": VERSION,
        })
        ev["observations"] = obs[-MAX_OBSERVATIONS:]
        ev["current_return_pct"] = round(r, 4)
        ev["mfe_pct"] = round(max(f(ev.get("mfe_pct")), r), 4)
        ev["mae_pct"] = round(min(f(ev.get("mae_pct")), r), 4)
        ev["updated_at_utc"] = now
        ev["last_price_source"] = VERSION
        label = classify(ev)
        ev["label"] = label
        updated += 1
        if label != old_label:
            transitions.append({"base": base, "from": old_label, "to": label, "return_pct": round(r, 4)})

        snap = ev.get("snapshot") or {}
        if label in {"SUCCESS_FORWARD_10", "SUCCESS_THEN_GIVEBACK"} and snap:
            bank["positive"][base] = {"base": base, "label": label, "source": VERSION, "added_at_utc": now, "snapshot": snap}
            bank["negative"].pop(base, None)
            if label == "SUCCESS_THEN_GIVEBACK":
                bank["giveback"][base] = {"base": base, "label": label, "added_at_utc": now, "snapshot": snap}
        elif label == "FAIL_FAST_REVERSAL" and snap and base not in bank["positive"]:
            bank["negative"][base] = {"base": base, "label": label, "source": VERSION, "added_at_utc": now, "snapshot": snap}

    bank["updated_at_utc"] = now
    save_json(OUTCOME_FILE, events)
    save_json(BANK_FILE, bank)
    summary = {
        "generated_at_utc": now,
        "version": VERSION,
        "market_snapshot_healthy": bool(market.get("healthy")),
        "open_events_updated": updated,
        "open_event_bases_missing_from_full_market": sorted(set(missing)),
        "label_transitions": transitions,
        "positive_bank_size": len(bank.get("positive") or {}),
        "negative_bank_size": len(bank.get("negative") or {}),
        "principle": "No open URGENT event stops being measured merely because it falls out of the bridge ranking.",
    }
    save_json(SUMMARY_FILE, summary)
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
