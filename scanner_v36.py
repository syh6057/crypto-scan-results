import json
from datetime import datetime, timezone
from pathlib import Path

VERSION = "v36-stateful-primary-continuity"
BRIDGE_FILE = "beam_breakout_bridge.json"
SELECTOR_FILE = "urgent_winner_selector.json"
STATE_FILE = "primary_tracking_state.json"
OUT_FILE = "primary_tracking_summary.json"

MIN_LOCK_RUNS = 3
REPLACE_CONFIRM_RUNS = 2
REPLACE_EDGE_MARGIN = 0.08
MAX_MISSING_GRACE_RUNS = 1
WARNING_MAX_RUNS = 2


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


def by_base(bridge):
    out = {}
    for key in ("promoted_top3", "watch_top5", "top_candidates"):
        for row in bridge.get(key) or []:
            if isinstance(row, dict) and row.get("base") and row.get("base") not in out:
                out[row["base"]] = row
    return out


def winner_edge(row):
    return f((row.get("winner_pattern") or {}).get("winner_edge"), -9.0)


def winner_rows(bridge):
    return [r for r in (bridge.get("promoted_top3") or []) if isinstance(r, dict) and r.get("base")]


def invalidation(row, state):
    if not row:
        return False, ["not_observable_this_run"]
    m = row.get("metrics") or {}
    ep = row.get("entry_plan") or state.get("entry_plan") or {}
    price = f(row.get("price_krw"))
    stop = f(ep.get("hard_stop_krw"))
    hard_stop_broken = bool(stop and price and price <= stop)
    structure_broken = (not bool(m.get("m15_close_above_ma"))) and (not bool(m.get("h1_close_above_ma")))
    flow_broken = f(m.get("buy_sell_ratio"), 1.0) < 0.75 and f(m.get("price_change_per_5m_pct"), 0.0) < -0.15
    volume_collapsed = f(m.get("m15_vol_persistence_x"), 1.0) < 0.65
    reasons = []
    if hard_stop_broken: reasons.append("hard_stop_broken")
    if structure_broken: reasons.append("m15_h1_structure_broken")
    if flow_broken: reasons.append("sell_flow_and_negative_pace")
    if volume_collapsed: reasons.append("volume_persistence_collapsed")
    invalid = hard_stop_broken or (structure_broken and flow_broken) or (structure_broken and volume_collapsed)
    return invalid, reasons


def warning_reasons(row):
    if not row:
        return ["not_observable_this_run"]
    m = row.get("metrics") or {}
    reasons = []
    if not bool(m.get("m15_close_above_ma")): reasons.append("below_m15_ma")
    if not bool(m.get("h1_close_above_ma")): reasons.append("below_h1_ma")
    if f(m.get("m15_vol_persistence_x"), 0.0) < 1.0: reasons.append("volume_persistence_below_1x")
    if f(m.get("buy_sell_ratio"), 1.0) < 0.9: reasons.append("buy_flow_weak")
    if f(m.get("price_change_per_5m_pct"), 0.0) < -0.05: reasons.append("short_pace_negative")
    return reasons


def main():
    now = datetime.now(timezone.utc).isoformat()
    bridge = load_json(BRIDGE_FILE, {})
    selector = load_json(SELECTOR_FILE, {})
    state = load_json(STATE_FILE, {})
    if not isinstance(state, dict): state = {}
    rows = by_base(bridge)
    fresh_winners = winner_rows(bridge)
    fresh_primary = fresh_winners[0] if fresh_winners else None

    primary_base = state.get("primary_base")
    status = state.get("status") or "NONE"
    lock_runs = int(state.get("lock_runs") or 0)
    warning_runs = int(state.get("warning_runs") or 0)
    missing_runs = int(state.get("missing_runs") or 0)
    challenger_base = state.get("challenger_base")
    challenger_runs = int(state.get("challenger_runs") or 0)
    transition = "UNCHANGED"

    if not primary_base:
        if fresh_primary:
            primary_base = fresh_primary.get("base")
            status = "HOLD"
            lock_runs = 1
            warning_runs = 0
            missing_runs = 0
            transition = "NEW_PRIMARY"
    else:
        row = rows.get(primary_base)
        invalid, invalid_reasons = invalidation(row, state)
        if invalid:
            status = "INVALIDATED"
            transition = "PRIMARY_INVALIDATED"
        elif row:
            missing_runs = 0
            lock_runs += 1
            wr = warning_reasons(row)
            if len(wr) >= 2:
                warning_runs += 1
                status = "WARNING"
            else:
                warning_runs = 0
                status = "HOLD"
        else:
            missing_runs += 1
            status = "WARNING" if missing_runs <= MAX_MISSING_GRACE_RUNS else "INVALIDATED"
            transition = "PRIMARY_MISSING_GRACE" if status == "WARNING" else "PRIMARY_MISSING_INVALIDATED"

        # A new winner cannot replace a live primary on one noisy scan. It needs a clear edge for 2 consecutive runs.
        if fresh_primary and fresh_primary.get("base") != primary_base and status != "INVALIDATED":
            cand = fresh_primary.get("base")
            primary_row = rows.get(primary_base)
            edge_delta = winner_edge(fresh_primary) - winner_edge(primary_row or {})
            if cand == challenger_base and edge_delta >= REPLACE_EDGE_MARGIN:
                challenger_runs += 1
            elif edge_delta >= REPLACE_EDGE_MARGIN:
                challenger_base, challenger_runs = cand, 1
            else:
                challenger_base, challenger_runs = None, 0
            if lock_runs >= MIN_LOCK_RUNS and challenger_runs >= REPLACE_CONFIRM_RUNS:
                primary_base = cand
                status = "HOLD"
                lock_runs = 1
                warning_runs = 0
                missing_runs = 0
                challenger_base, challenger_runs = None, 0
                transition = "PRIMARY_REPLACED_CONFIRMED"
        elif not fresh_primary or (fresh_primary and fresh_primary.get("base") == primary_base):
            challenger_base, challenger_runs = None, 0

        if status == "INVALIDATED" and fresh_primary and fresh_primary.get("base") != primary_base:
            primary_base = fresh_primary.get("base")
            status = "HOLD"
            lock_runs = 1
            warning_runs = 0
            missing_runs = 0
            challenger_base, challenger_runs = None, 0
            transition = "INVALIDATED_TO_NEW_PRIMARY"

    row = rows.get(primary_base) if primary_base else None
    # If a previously selected primary is still observable and not invalidated, keep it actionable as a continuation.
    if row and status in {"HOLD", "WARNING"}:
        tracked = dict(row)
        tracked["tracking"] = {
            "version": VERSION, "status": status, "lock_runs": lock_runs,
            "warning_runs": warning_runs, "missing_runs": missing_runs,
            "transition": transition,
            "principle": "keep the same primary until explicit invalidation or a clearly superior challenger wins twice"
        }
        tracked["execution_allowed"] = status == "HOLD"
        tracked["promotion_grade"] = "A_URGENT_CONTINUATION" if status == "HOLD" else "WATCH_PRIMARY_WARNING"
        others = [x for x in (bridge.get("promoted_top3") or []) if x.get("base") != primary_base]
        bridge["promoted_top3"] = ([tracked] + others)[:3] if status == "HOLD" else others[:3]
        watches = [x for x in (bridge.get("watch_top5") or []) if x.get("base") != primary_base]
        if status == "WARNING":
            watches = [tracked] + watches
        bridge["watch_top5"] = watches[:5]
        bridge["top_candidates"] = ([tracked] + [x for x in (bridge.get("top_candidates") or []) if x.get("base") != primary_base])[:5]

    bridge["primary_continuity"] = {
        "generated_at_utc": now, "version": VERSION, "primary_base": primary_base,
        "status": status, "lock_runs": lock_runs, "warning_runs": warning_runs,
        "missing_runs": missing_runs, "challenger_base": challenger_base,
        "challenger_runs": challenger_runs, "transition": transition,
    }
    bridge["version"] = f"{bridge.get('version','')}+{VERSION}"
    if status == "HOLD" and row:
        bridge["status"] = "URGENT_CONTINUATION_HOLD"
    elif status == "WARNING" and row:
        bridge["status"] = "PRIMARY_WARNING"
    save_json(BRIDGE_FILE, bridge)

    state = {
        "updated_at_utc": now, "version": VERSION, "primary_base": primary_base,
        "status": status, "lock_runs": lock_runs, "warning_runs": warning_runs,
        "missing_runs": missing_runs, "challenger_base": challenger_base,
        "challenger_runs": challenger_runs, "transition": transition,
        "entry_plan": (row or {}).get("entry_plan") or state.get("entry_plan") or {},
        "last_price_krw": (row or {}).get("price_krw"),
        "last_winner_edge": winner_edge(row or {}),
    }
    save_json(STATE_FILE, state)
    save_json(OUT_FILE, {**state, "fresh_winner_matches": [x.get("base") for x in fresh_winners], "selector_status": selector.get("status")})
    print(json.dumps(state, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
