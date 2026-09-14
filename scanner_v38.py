import json
from datetime import datetime, timezone
from pathlib import Path

VERSION = "v38-fail-closed-decision-integrity"
BRIDGE_FILE = "beam_breakout_bridge.json"
MARKET_FILE = "all_market_snapshot.json"
STATE_FILE = "decision_integrity_state.json"
OUT_FILE = "decision_integrity_summary.json"

MIN_TRADE_KRW = 100_000_000
MAX_PRICE_DIVERGENCE_PCT = 1.5
CONFIRM_RUNS = 2
FAST_TRACK_MIN_EDGE = 0.12
FAST_TRACK_MIN_POS_SIM = 0.66


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


def pct_diff(a, b):
    return abs(a / b - 1.0) * 100.0 if a and b else 999.0


def data_quality(row, market_row=None):
    m = row.get("metrics") or {}
    reasons = []
    price = f(row.get("price_krw"))
    trade = f(row.get("trade_24h_krw"))
    bsr = f(m.get("buy_sell_ratio"), -1.0)
    depth = f(m.get("bid_ask_depth_ratio"), -1.0)
    vper = f(m.get("m15_vol_persistence_x"), -1.0)
    pace = f(m.get("price_change_per_5m_pct"), -999.0)

    if price <= 0: reasons.append("invalid_price")
    if trade < MIN_TRADE_KRW: reasons.append("insufficient_24h_trade_value")
    if vper < 0: reasons.append("missing_volume_persistence")
    if pace <= -999: reasons.append("missing_short_pace")
    if bsr < 0: reasons.append("missing_buy_sell_ratio")
    if depth < 0: reasons.append("missing_depth_ratio")

    # 99 is a sentinel in scanner_v16 when sampled sells are zero; do not treat it as literal 99x buying.
    if bsr >= 50: reasons.append("buy_sell_sentinel_or_zero_sell_sample")
    if depth > 20 or (0 <= depth < 0.05): reasons.append("extreme_orderbook_depth_ratio")
    if bsr >= 5 and 0 <= depth <= 0.25: reasons.append("flow_orderbook_contradiction_buy_vs_ask_wall")
    if 0 <= bsr <= 0.2 and depth >= 5: reasons.append("flow_orderbook_contradiction_sell_vs_bid_wall")

    if market_row:
        live = f(market_row.get("price_krw"))
        if live <= 0:
            reasons.append("market_snapshot_missing_price")
        elif price > 0 and pct_diff(price, live) > MAX_PRICE_DIVERGENCE_PCT:
            reasons.append("bridge_vs_market_price_divergence")
        if str(market_row.get("market_warning") or "NONE").upper() not in {"NONE", ""}:
            reasons.append("market_warning_active")
    else:
        reasons.append("missing_full_market_crosscheck")

    return not reasons, reasons


def fast_track(row):
    m = row.get("metrics") or {}
    wp = row.get("winner_pattern") or {}
    return (
        f(wp.get("winner_edge"), -9) >= FAST_TRACK_MIN_EDGE
        and f(wp.get("positive_similarity")) >= FAST_TRACK_MIN_POS_SIM
        and f(row.get("change_24h_pct"), 999) <= 6.5
        and f(m.get("m15_vol_persistence_x")) >= 1.5
        and 0.95 <= f(m.get("buy_sell_ratio"), -1) <= 8.0
        and 0.35 <= f(m.get("bid_ask_depth_ratio"), -1) <= 5.0
        and f(m.get("price_change_per_5m_pct"), -999) >= 0.0
    )


def main():
    now = datetime.now(timezone.utc).isoformat()
    bridge = load_json(BRIDGE_FILE, {})
    market = load_json(MARKET_FILE, {})
    state = load_json(STATE_FILE, {})
    if not isinstance(state, dict): state = {}
    market_rows = market.get("rows") or {}

    prev_base = state.get("candidate_base")
    prev_runs = int(state.get("candidate_runs") or 0)
    promoted = bridge.get("promoted_top3") or []
    accepted, demoted, diagnostics = [], [], []

    for idx, row in enumerate(promoted):
        base = row.get("base")
        quality_ok, quality_reasons = data_quality(row, market_rows.get(base))
        same = base == prev_base
        runs = prev_runs + 1 if same else 1
        ft = fast_track(row)
        confirmed = quality_ok and (runs >= CONFIRM_RUNS or ft)

        out = dict(row)
        out["decision_integrity"] = {
            "version": VERSION,
            "quality_ok": quality_ok,
            "quality_reasons": quality_reasons,
            "confirmation_runs": runs,
            "confirmation_required": CONFIRM_RUNS,
            "fast_track": ft,
            "confirmed": confirmed,
        }

        diagnostics.append({
            "base": base,
            "quality_ok": quality_ok,
            "quality_reasons": quality_reasons,
            "confirmation_runs": runs,
            "fast_track": ft,
            "confirmed": confirmed,
        })

        if confirmed:
            accepted.append(out)
        else:
            out["execution_allowed"] = False
            out["promotion_grade"] = "WATCH_DATA_QUALITY_BLOCKED" if not quality_ok else "WATCH_CONFIRMATION_PENDING"
            demoted.append(out)

        # Track only the top fresh winner as the next confirmation candidate.
        if idx == 0:
            state["candidate_base"] = base
            state["candidate_runs"] = runs

    if not promoted:
        state["candidate_base"] = None
        state["candidate_runs"] = 0

    existing_watch = bridge.get("watch_top5") or []
    accepted_bases = {x.get("base") for x in accepted}
    merged_watch = demoted + [x for x in existing_watch if x.get("base") not in accepted_bases and x.get("base") not in {d.get("base") for d in demoted}]
    bridge["promoted_top3"] = accepted[:3]
    bridge["watch_top5"] = merged_watch[:5]
    bridge["top_candidates"] = (accepted + merged_watch)[:5]
    bridge["decision_integrity"] = {
        "generated_at_utc": now,
        "version": VERSION,
        "market_snapshot_healthy": bool(market.get("healthy")),
        "accepted": [x.get("base") for x in accepted],
        "demoted": [x.get("base") for x in demoted],
        "diagnostics": diagnostics,
        "principle": "fail closed on anomalous or unconfirmed fresh winners; continuity is handled downstream by v36",
    }
    bridge["version"] = f"{bridge.get('version','')}+{VERSION}"
    if not accepted and bridge.get("status") == "URGENT_WINNER_MATCH":
        bridge["status"] = "WATCH_ONLY"
    save_json(BRIDGE_FILE, bridge)

    state["updated_at_utc"] = now
    state["version"] = VERSION
    save_json(STATE_FILE, state)
    save_json(OUT_FILE, {
        "generated_at_utc": now,
        "version": VERSION,
        "market_snapshot_healthy": bool(market.get("healthy")),
        "accepted": [x.get("base") for x in accepted],
        "demoted": [x.get("base") for x in demoted],
        "diagnostics": diagnostics,
        "state": state,
    })
    print(json.dumps({"version": VERSION, "accepted": [x.get("base") for x in accepted], "demoted": [x.get("base") for x in demoted]}, ensure_ascii=False))


if __name__ == "__main__":
    main()
