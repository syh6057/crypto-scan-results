import json
from datetime import datetime, timezone
from pathlib import Path

VERSION = "v31-early-winner-promotion-bridge"
SCAN_FILE = "beam_scan_result.json"
FIRST_SEEN_FILE = "beam_first_seen.json"
MARKET_STATE_FILE = "beam_state.json"
MISS_AUDIT_FILE = "beam_miss_audit.json"
STATE_FILE = "breakout_bridge_state.json"
OUT_FILE = "beam_breakout_bridge.json"

LOW_BETA = {"BTC", "ETH", "DOGE", "XRP", "SOL", "BNB", "TRX", "LINK", "LTC", "BCH", "ADA", "XLM", "DOT"}
MIN_TRADE_KRW = 150_000_000
STRONG_TRADE_KRW = 500_000_000
MAX_FIRST_CHANGE = 8.0
MIN_CURRENT_CHANGE = -2.0
MAX_CURRENT_CHANGE = 12.5
IMMEDIATE_SCORE = 82.0
CONFIRMED_SCORE = 72.0
WATCH_SCORE = 64.0
MIN_SIGNAL_COUNT = 5
MIN_FLOW_CONFIRMATIONS = 2
CONFIRMATIONS_REQUIRED = 2
MEMORY_RUNS = 8
NO_CHASE_PCT = 2.5
STOP_PCT = 5.0


def load_json(path, default):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except Exception:
        return default


def save_json(path, value):
    Path(path).write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")


def fnum(v, default=0.0):
    try:
        return float(v)
    except Exception:
        return default


def r(v, n=3):
    try:
        return round(float(v), n)
    except Exception:
        return None


def pct(a, b):
    a, b = fnum(a), fnum(b)
    if a <= 0 or b <= 0:
        return None
    return (a / b - 1.0) * 100.0


def first_seen_row(first_seen, base):
    row = first_seen.get(base) if isinstance(first_seen, dict) else None
    return row if isinstance(row, dict) else {}


def evaluate(row, first_seen, failed_assets):
    base = row.get("base")
    fs = first_seen_row(first_seen, base)
    ch = fnum(row.get("change_24h_pct"), 999.0)
    trade = fnum(row.get("trade_24h_krw"))
    first_ch = fnum(fs.get("first_change_24h_pct"), 999.0)
    first_price = fnum(fs.get("first_price_krw"))
    price = fnum(row.get("price_krw"))
    since_first = pct(price, first_price)
    warning = bool(row.get("warning_flag"))
    track = row.get("hard_track") or row.get("track")

    fast = row.get("fast_5m") or {}
    m15 = row.get("m15") or {}
    h1 = row.get("h1") or {}
    h4 = row.get("h4") or {}
    flow = row.get("trade_flow") or {}
    ob = row.get("orderbook") or {}

    beam = fnum(row.get("beam_score_v18"), fnum(row.get("beam_score")))
    signatures = int(row.get("beam_signature_count") or 0)
    money = fnum(row.get("money_leads_price_score"))
    turnover = fnum(fast.get("turnover_intensity_vs_even_5m_x"))
    pace = fnum(fast.get("price_change_per_5m_pct"))
    vsp = fnum(m15.get("vol_spike_x"))
    vper = fnum(m15.get("vol_persistence_x"))
    bsr = fnum(flow.get("buy_sell_ratio"))
    obr = fnum(ob.get("bid_ask_depth_ratio"))
    wick = fnum(m15.get("upper_wick_pct"))

    hard_failures = []
    if base in LOW_BETA:
        hard_failures.append("low_beta_major")
    if warning:
        hard_failures.append("market_warning")
    if trade < MIN_TRADE_KRW:
        hard_failures.append("turnover_below_150m")
    if first_ch > MAX_FIRST_CHANGE:
        hard_failures.append("first_detection_above_8pct")
    if not (MIN_CURRENT_CHANGE <= ch <= MAX_CURRENT_CHANGE):
        hard_failures.append("current_change_outside_minus2_to_12_5")
    if track == "CHASE_EXCLUDED":
        hard_failures.append("already_chase_excluded")
    if pace > 4.0:
        hard_failures.append("five_minute_price_pace_over_4pct")
    if wick >= 3.0:
        hard_failures.append("large_15m_upper_wick")
    if since_first is not None and since_first > 15.0:
        hard_failures.append("already_over_15pct_from_first_detection")

    signals = {
        "m15_volume_spike": vsp >= 1.4,
        "m15_volume_persistence": vper >= 1.1,
        "m15_structure": bool(m15.get("low_rising")) or bool(m15.get("close_above_ma")),
        "h1_structure": bool(h1.get("low_rising")) or bool(h1.get("close_above_ma")),
        "buy_flow": bsr >= 1.05,
        "orderbook_support": obr >= 0.7,
        "money_leads_price": money >= 8.0,
        "fast_turnover": turnover >= 1.2,
        "beam_signature": signatures >= 3,
        "beam_score": beam >= 90.0,
    }
    signal_count = sum(bool(v) for v in signals.values())
    flow_confirmations = sum(bool(signals[k]) for k in (
        "m15_volume_spike", "m15_volume_persistence", "buy_flow", "money_leads_price", "fast_turnover"
    ))

    score = 0.0
    if first_ch <= 2:
        score += 18
    elif first_ch <= 4.5:
        score += 14
    else:
        score += 8

    if -1.0 <= ch <= 4.0:
        score += 16
    elif 4.0 < ch <= 8.0:
        score += 11
    else:
        score += 5

    score += 12 if trade >= 1_000_000_000 else (8 if trade >= STRONG_TRADE_KRW else 5)
    score += 12 if vsp >= 2.0 else (8 if vsp >= 1.4 else 0)
    score += 10 if vper >= 1.5 else (6 if vper >= 1.1 else 0)
    score += 7 if signals["m15_structure"] else -3
    score += 5 if signals["h1_structure"] else 0

    if bsr >= 1.35:
        score += 12
    elif bsr >= 1.05:
        score += 8
    elif bsr >= 0.85:
        score += 2
    elif bsr > 0:
        score -= 10

    if obr >= 1.0:
        score += 6
    elif obr >= 0.7:
        score += 3
    elif 0 < obr < 0.5:
        score -= 5

    score += min(12.0, max(0.0, money * 0.35))
    score += min(8.0, max(0.0, (turnover - 1.0) * 4.0))
    score += min(10.0, signatures * 2.0)
    score += min(8.0, max(0.0, (beam - 85.0) * 0.16))

    if -0.3 <= pace <= 1.5:
        score += 5
    elif 1.5 < pace <= 2.5:
        score += 2
    elif pace < -0.5:
        score -= 6

    if wick <= 0.8:
        score += 4
    elif wick >= 1.5:
        score -= 5

    if h4.get("low_rising") or h4.get("close_above_ma"):
        score += 3
    # 4h structure is intentionally not a veto. Many realized winners ignited before 4h confirmation.

    if base in failed_assets:
        score -= 5

    score = max(0.0, min(100.0, round(score, 2)))
    immediate = (
        not hard_failures
        and score >= IMMEDIATE_SCORE
        and signal_count >= MIN_SIGNAL_COUNT
        and flow_confirmations >= MIN_FLOW_CONFIRMATIONS
    )
    confirmed_quality = (
        not hard_failures
        and score >= CONFIRMED_SCORE
        and signal_count >= MIN_SIGNAL_COUNT
        and flow_confirmations >= MIN_FLOW_CONFIRMATIONS
    )
    watch_quality = not hard_failures and score >= WATCH_SCORE and flow_confirmations >= 1

    lane = "QUIET_FLOW" if ch <= 4 else ("EARLY_ACCELERATOR" if ch <= 8 else "CONTROLLED_ACCELERATOR")
    return {
        "base": base,
        "market": row.get("market"),
        "korean_name": row.get("korean_name"),
        "price_krw": price,
        "change_24h_pct": ch,
        "trade_24h_krw": trade,
        "lane": lane,
        "score": score,
        "signal_count": signal_count,
        "flow_confirmation_count": flow_confirmations,
        "signals": signals,
        "hard_failures": hard_failures,
        "immediate_quality": immediate,
        "confirmed_quality": confirmed_quality,
        "watch_quality": watch_quality,
        "first_detected_at_utc": fs.get("first_detected_at_utc"),
        "first_price_krw": fs.get("first_price_krw"),
        "first_change_24h_pct": fs.get("first_change_24h_pct"),
        "return_since_first_pct": r(since_first, 2) if since_first is not None else None,
        "track": track,
        "metrics": {
            "beam_score": r(beam, 2),
            "beam_signature_count": signatures,
            "money_leads_price_score": r(money, 2),
            "fast_turnover_intensity_x": r(turnover, 2),
            "price_change_per_5m_pct": r(pace, 2),
            "m15_vol_spike_x": r(vsp, 2),
            "m15_vol_persistence_x": r(vper, 2),
            "m15_low_rising": bool(m15.get("low_rising")),
            "m15_close_above_ma": bool(m15.get("close_above_ma")),
            "h1_low_rising": bool(h1.get("low_rising")),
            "h1_close_above_ma": bool(h1.get("close_above_ma")),
            "buy_sell_ratio": r(bsr, 2),
            "bid_ask_depth_ratio": r(obr, 2),
            "m15_upper_wick_pct": r(wick, 2),
        },
        "entry_plan": {
            "reference_price_krw": price,
            "do_not_chase_above_krw": r(price * (1 + NO_CHASE_PCT / 100.0), 8) if price else None,
            "hard_stop_krw": r(price * (1 - STOP_PCT / 100.0), 8) if price else None,
            "target_10_krw": r(price * 1.10, 8) if price else None,
            "target_20_krw": r(price * 1.20, 8) if price else None,
            "external_news_unlock_listing_delisting_tokenomics_check_required": True,
            "automatic_full_size_buy": False,
        },
    }


def reactivation_watch(first_seen, market_state, current_stage2):
    tickers = market_state.get("tickers") or {}
    out = []
    for base, fs in first_seen.items() if isinstance(first_seen, dict) else []:
        if base in current_stage2 or base in LOW_BETA:
            continue
        t = tickers.get(base) or {}
        ch = fnum(t.get("change24_pct"), 999.0)
        trade = fnum(t.get("trade24_krw"))
        first_ch = fnum((fs or {}).get("first_change_24h_pct"), 999.0)
        first_price = fnum((fs or {}).get("first_price_krw"))
        price = fnum(t.get("price"))
        since_first = pct(price, first_price)
        if first_ch <= 4.5 and 3.0 <= ch <= 12.5 and trade >= STRONG_TRADE_KRW and since_first is not None and 2.0 <= since_first <= 15.0:
            out.append({
                "base": base,
                "price_krw": price,
                "change_24h_pct": ch,
                "trade_24h_krw": trade,
                "first_change_24h_pct": first_ch,
                "return_since_first_pct": r(since_first, 2),
                "status": "REACTIVATE_FOR_FULL_CANDLE_REFRESH",
            })
    out.sort(key=lambda x: (x["change_24h_pct"], x["trade_24h_krw"]), reverse=True)
    return out[:10]


def main():
    now = datetime.now(timezone.utc)
    scan = load_json(SCAN_FILE, {})
    first_seen = load_json(FIRST_SEEN_FILE, {})
    market_state = load_json(MARKET_STATE_FILE, {})
    audit = load_json(MISS_AUDIT_FILE, {})
    state = load_json(STATE_FILE, {"confirmations": {}, "history": []})

    health = scan.get("health") or {}
    blockers = list(health.get("hard_blockers") or [])
    rows = [r for r in (scan.get("stage2_ranked") or []) if r.get("base")]
    failed_assets = {x.get("base") for x in (audit.get("failed_acceleration_examples") or []) if x.get("base")}

    evaluated = [evaluate(row, first_seen, failed_assets) for row in rows]
    evaluated.sort(key=lambda x: (x["score"], x["trade_24h_krw"]), reverse=True)

    previous_counts = state.get("confirmations") or {}
    new_counts = {}
    for item in evaluated:
        if item["confirmed_quality"]:
            new_counts[item["base"]] = int(previous_counts.get(item["base"]) or 0) + 1

    for item in evaluated:
        item["confirmations"] = int(new_counts.get(item["base"]) or 0)
        if item["immediate_quality"]:
            item["promotion_grade"] = "A_EARLY_IMMEDIATE"
        elif item["confirmed_quality"] and item["confirmations"] >= CONFIRMATIONS_REQUIRED:
            item["promotion_grade"] = "BPLUS_EARLY_CONFIRMED"
        elif item["watch_quality"]:
            item["promotion_grade"] = "WATCH"
        else:
            item["promotion_grade"] = "REJECT"

    promoted = [x for x in evaluated if x["promotion_grade"] in {"A_EARLY_IMMEDIATE", "BPLUS_EARLY_CONFIRMED"}]
    watches = [x for x in evaluated if x["promotion_grade"] == "WATCH"]
    current_stage2 = {x.get("base") for x in rows}
    reactivation = reactivation_watch(first_seen, market_state, current_stage2)

    state["version"] = VERSION
    state["updated_at_utc"] = now.isoformat()
    state["confirmations"] = new_counts
    state.setdefault("history", []).append({
        "generated_at_utc": now.isoformat(),
        "promoted": [x["base"] for x in promoted[:3]],
        "watch": [x["base"] for x in watches[:3]],
        "reactivation": [x["base"] for x in reactivation[:3]],
    })
    state["history"] = state["history"][-MEMORY_RUNS:]

    if blockers:
        status = "BLOCKED"
    elif promoted:
        status = "EARLY_WINNER_CANDIDATE"
    elif watches or reactivation:
        status = "WATCH_ONLY"
    else:
        status = "NO_SIGNAL"

    top_candidates = (promoted + watches)[:5]
    out = {
        "generated_at_utc": now.isoformat(),
        "version": VERSION,
        "status": status,
        "health": {
            "analysis_ready": bool(health.get("analysis_ready")) and not blockers,
            "hard_blockers": blockers,
            "stage2_candidates": len(rows),
            "evaluated": len(evaluated),
        },
        "promoted_top3": promoted[:3],
        "watch_top5": watches[:5],
        "top_candidates": top_candidates,
        "reactivation_watch": reactivation,
        "policy": {
            "purpose": "bridge raw early detection to user-visible promotion before the move is obvious",
            "scan_all_bithumb_source": True,
            "no_single_pick_lock": True,
            "four_hour_structure_is_soft_context_not_veto": True,
            "fifteen_minute_higher_low_is_not_single_veto": True,
            "thirty_minute_three_higher_lows_not_required": True,
            "first_detection_max_change_pct": MAX_FIRST_CHANGE,
            "current_change_window_pct": [MIN_CURRENT_CHANGE, MAX_CURRENT_CHANGE],
            "min_trade_24h_krw": MIN_TRADE_KRW,
            "immediate_score_min": IMMEDIATE_SCORE,
            "confirmed_score_min": CONFIRMED_SCORE,
            "confirmations_required": CONFIRMATIONS_REQUIRED,
            "minimum_signal_count": MIN_SIGNAL_COUNT,
            "minimum_flow_confirmations": MIN_FLOW_CONFIRMATIONS,
            "external_news_unlock_listing_delisting_tokenomics_check_required_before_money": True,
            "not_a_full_size_buy_signal": True,
            "no_averaging_down": True,
        },
        "miss_feedback": {
            "realized_early_captures": [
                {"base": x.get("base"), "first_detected_change_24h_pct": x.get("first_detected_change_24h_pct"), "current_change_24h_pct": x.get("change_24h_pct")}
                for x in (audit.get("current_top_movers") or [])
                if x.get("capture") == "EARLY_CAPTURE"
            ][:15],
            "design_change": "replace all-of structure vetoes with weighted flow/volume confirmation, add controlled accelerator lane, emit top3 instead of hiding behind one locked pick",
        },
    }
    save_json(STATE_FILE, state)
    save_json(OUT_FILE, out)
    print(json.dumps(out, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
