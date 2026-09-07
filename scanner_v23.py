import json
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path

VERSION = "v23-realized-winner-recall"
SCAN_FILE = "beam_scan_result.json"
FIRST_SEEN_FILE = "beam_first_seen.json"
AUDIT_FILE = "beam_miss_audit.json"
ONE_PICK_FILE = "beam_one_pick_summary.json"
STATE_FILE = "winner_recall_state.json"
OUT_FILE = "beam_winner_recall.json"

MAX_CHANGE = 8.0
MAX_FIRST_CHANGE = 6.5
MIN_TRADE_KRW = 150_000_000
MIN_RECALL_SCORE = 100.0
MIN_SIGNAL_COUNT = 6
CONFIRMATIONS_REQUIRED = 2
LOW_BETA = {"BTC", "ETH", "DOGE", "XRP", "SOL", "BNB", "TRX", "LINK", "LTC", "BCH", "ADA", "XLM", "DOT"}


def load_json(path, default):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except Exception:
        return deepcopy(default)


def save_json(path, obj):
    Path(path).write_text(json.dumps(obj, ensure_ascii=False, indent=2), encoding="utf-8")


def fnum(v, default=0.0):
    try:
        return float(v)
    except Exception:
        return default


def pct(a, b):
    a, b = fnum(a), fnum(b)
    return ((a / b) - 1.0) * 100.0 if a > 0 and b > 0 else None


def recall_eval(row, first_seen):
    base = row.get("base")
    fs = first_seen.get(base) or {}
    ch = fnum(row.get("change_24h_pct"))
    price = fnum(row.get("price_krw"))
    trade = fnum(row.get("trade_24h_krw"))
    first_ch = fnum(fs.get("first_change_24h_pct"), 999.0)
    first_price = fnum(fs.get("first_price_krw"))
    first_score = fnum(fs.get("first_score"))
    beam = fnum(row.get("beam_score_v18"), fnum(row.get("beam_score")))
    sig = int(row.get("beam_signature_count") or 0)
    money = fnum(row.get("money_leads_price_score"))
    fast = row.get("fast_5m") or {}
    m15 = row.get("m15") or {}
    h1 = row.get("h1") or {}
    flow = row.get("trade_flow") or {}
    ob = row.get("orderbook") or {}
    ep = row.get("entry_plan") or {}

    turnover = fnum(fast.get("turnover_intensity_vs_even_5m_x"))
    p5 = fnum(fast.get("price_change_per_5m_pct"))
    vsp = fnum(m15.get("vol_spike_x"))
    vper = fnum(m15.get("vol_persistence_x"))
    bsr = fnum(flow.get("buy_sell_ratio"))
    obr = fnum(ob.get("bid_ask_depth_ratio"))
    since_first = pct(price, first_price)

    hard_failures = []
    if base in LOW_BETA:
        hard_failures.append("low_beta_major")
    if row.get("hard_track") != "PRE_IGNITION" and row.get("track") != "PRE_IGNITION":
        hard_failures.append("not_pre_ignition")
    if not (0 <= ch <= MAX_CHANGE):
        hard_failures.append("not_early_now")
    if first_ch > MAX_FIRST_CHANGE:
        hard_failures.append("first_detection_too_late")
    if trade < MIN_TRADE_KRW:
        hard_failures.append("turnover_too_small")
    if row.get("warning_flag"):
        hard_failures.append("market_warning")
    chase = fnum(ep.get("do_not_chase_above_krw"))
    if chase > 0 and price > chase:
        hard_failures.append("above_no_chase")
    if since_first is not None and since_first > 10:
        hard_failures.append("already_extended_from_first")

    signals = {
        "fast_turnover": turnover >= 1.5,
        "controlled_5m": -0.3 <= p5 <= 2.5,
        "m15_spike": vsp >= 1.4,
        "m15_persistence": vper >= 1.1,
        "m15_structure": bool(m15.get("low_rising")) or bool(m15.get("close_above_ma")),
        "h1_structure": bool(h1.get("low_rising")) or bool(h1.get("close_above_ma")),
        "buy_flow": bsr >= 0.8,
        "orderbook_not_bad": obr == 0 or obr >= 0.6,
        "signature": sig >= 3,
        "money_leads": money >= 12,
    }
    signal_count = sum(bool(x) for x in signals.values())

    score = 0.0
    if first_ch <= 2:
        score += 18
    elif first_ch <= 4:
        score += 13
    else:
        score += 8
    score += min(22.0, max(0.0, (beam - 80.0) * 0.22))
    score += min(15.0, max(-10.0, (beam - first_score) * 0.25))
    score += min(20.0, turnover * 3.0)
    score += 12 if vsp >= 2 else (7 if vsp >= 1.4 else 0)
    score += 12 if vper >= 1.5 else (7 if vper >= 1.1 else 0)
    score += 9 if signals["m15_structure"] else 0
    score += 9 if signals["h1_structure"] else 0
    score += min(12.0, max(0.0, (bsr - 0.7) * 10.0))
    score += min(8.0, max(0.0, (obr - 0.6) * 5.0)) if obr else 2.0
    score += min(12.0, money * 0.35)
    if 0 <= ch <= 4:
        score += 10
    elif ch <= 6.5:
        score += 7
    else:
        score += 3
    if since_first is not None and 0 <= since_first <= 6:
        score += 8
    if p5 > 3.5:
        score -= 12
    if bsr and bsr < 0.55:
        score -= 15
    score = round(score, 2)

    eligible = not hard_failures and score >= MIN_RECALL_SCORE and signal_count >= MIN_SIGNAL_COUNT
    return {
        "eligible": eligible,
        "recall_score": score,
        "signal_count": signal_count,
        "signals": signals,
        "hard_failures": hard_failures,
        "first_detected_at_utc": fs.get("first_detected_at_utc"),
        "first_price_krw": fs.get("first_price_krw"),
        "first_change_24h_pct": fs.get("first_change_24h_pct"),
        "first_score": fs.get("first_score"),
        "return_since_first_pct": round(since_first, 2) if since_first is not None else None,
        "beam_score": beam,
        "money_leads_price_score": money,
        "turnover_intensity": turnover,
        "buy_sell_ratio": bsr,
        "orderbook_ratio": obr,
    }


def compact(row, ev, confirmations=0):
    price = fnum(row.get("price_krw"))
    return {
        "base": row.get("base"),
        "market": row.get("market"),
        "korean_name": row.get("korean_name"),
        "price_krw": row.get("price_krw"),
        "change_24h_pct": row.get("change_24h_pct"),
        "trade_24h_krw": row.get("trade_24h_krw"),
        "track": row.get("hard_track") or row.get("track"),
        "recall_score": ev["recall_score"],
        "signal_count": ev["signal_count"],
        "signals": ev["signals"],
        "hard_failures": ev["hard_failures"],
        "confirmations": confirmations,
        "first_detected_at_utc": ev["first_detected_at_utc"],
        "first_price_krw": ev["first_price_krw"],
        "first_change_24h_pct": ev["first_change_24h_pct"],
        "return_since_first_pct": ev["return_since_first_pct"],
        "beam_score": ev["beam_score"],
        "money_leads_price_score": ev["money_leads_price_score"],
        "turnover_intensity": ev["turnover_intensity"],
        "buy_sell_ratio": ev["buy_sell_ratio"],
        "orderbook_ratio": ev["orderbook_ratio"],
        "scenario_target_10_krw": round(price * 1.10, 12) if price else None,
        "scenario_target_15_krw": round(price * 1.15, 12) if price else None,
        "scenario_target_30_krw": round(price * 1.30, 12) if price else None,
        "entry_plan": row.get("entry_plan"),
    }


def realized_winner_cohort(audit):
    out = []
    for x in (audit.get("current_top_movers") or []):
        if x.get("capture") == "EARLY_CAPTURE" and fnum(x.get("change_24h_pct")) >= 10:
            out.append({
                "base": x.get("base"),
                "current_change_24h_pct": x.get("change_24h_pct"),
                "first_detected_change_24h_pct": x.get("first_detected_change_24h_pct"),
                "first_detected_price_krw": x.get("first_detected_price_krw"),
            })
    return out


def main():
    scan = load_json(SCAN_FILE, {})
    first_seen = load_json(FIRST_SEEN_FILE, {})
    audit = load_json(AUDIT_FILE, {})
    one_pick = load_json(ONE_PICK_FILE, {})
    state = load_json(STATE_FILE, {"confirmations": {}, "history": []})

    rows = scan.get("stage2_ranked") or []
    evaluated = []
    for row in rows:
        ev = recall_eval(row, first_seen)
        if ev["eligible"]:
            evaluated.append((ev["recall_score"], row, ev))
    evaluated.sort(key=lambda x: x[0], reverse=True)

    prev_counts = state.get("confirmations") or {}
    new_counts = {}
    for _, row, _ in evaluated[:10]:
        base = row.get("base")
        new_counts[base] = int(prev_counts.get(base) or 0) + 1
    state["confirmations"] = new_counts
    state["updated_at_utc"] = scan.get("generated_at_utc") or datetime.now(timezone.utc).isoformat()
    state["version"] = VERSION

    watchlist = [compact(row, ev, new_counts.get(row.get("base"), 0)) for _, row, ev in evaluated[:5]]
    confirmed = [x for x in watchlist if int(x.get("confirmations") or 0) >= CONFIRMATIONS_REQUIRED]
    confirmed_pick = confirmed[0] if confirmed else None

    stable_pick = one_pick.get("one_pick") or None
    profit_protection = audit.get("profit_protection") or {}
    state.setdefault("history", []).append({
        "generated_at_utc": state["updated_at_utc"],
        "confirmed_recall_pick": (confirmed_pick or {}).get("base"),
        "watch_top": (watchlist[0] if watchlist else {}).get("base"),
    })
    state["history"] = state["history"][-300:]
    save_json(STATE_FILE, state)

    out = {
        "generated_at_utc": state["updated_at_utc"],
        "version": VERSION,
        "health": scan.get("health"),
        "purpose": "surface early movers the scanner detected but downstream expert gates failed to recommend",
        "learned_from_realized_winners": realized_winner_cohort(audit),
        "policy": {
            "current_change_max_pct": MAX_CHANGE,
            "first_detection_change_max_pct": MAX_FIRST_CHANGE,
            "min_trade_24h_krw": MIN_TRADE_KRW,
            "min_recall_score": MIN_RECALL_SCORE,
            "min_signal_count": MIN_SIGNAL_COUNT,
            "confirmations_required": CONFIRMATIONS_REQUIRED,
            "not_a_guarantee": true,
            "note": "30pct is an upside scenario only; +10/+15 checkpoints are used to avoid ARX-style profit giveback.",
        },
        "stable_one_pick": stable_pick,
        "stable_one_pick_profit_protection": profit_protection,
        "confirmed_recall_pick": confirmed_pick,
        "winner_recall_watchlist": watchlist,
        "feedback": {
            "detection_bottleneck": "some eventual winners were first detected below 5pct but never surfaced as the recommendation",
            "fix": "parallel raw-stage2 recall scoring bypasses v19/v20 all-of expert thresholds, while requiring two observations",
            "exit_control": "a +10 to +15 peak followed by 6 to 8 percentage-point giveback triggers profit protection",
        },
    }
    save_json(OUT_FILE, out)
    print(json.dumps(out, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
