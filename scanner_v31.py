import json
from datetime import datetime, timezone
from pathlib import Path

VERSION = "v31.1-episode-aware-early-winner-bridge"
SCAN_FILE = "beam_scan_result.json"
FIRST_SEEN_FILE = "beam_first_seen.json"
MARKET_STATE_FILE = "beam_state.json"
MISS_AUDIT_FILE = "beam_miss_audit.json"
STATE_FILE = "breakout_bridge_state.json"
HISTORY_FILE = "breakout_bridge_history.json"
OUT_FILE = "beam_breakout_bridge.json"

LOW_BETA = {"BTC", "ETH", "DOGE", "XRP", "SOL", "BNB", "TRX", "LINK", "LTC", "BCH", "ADA", "XLM", "DOT"}
MIN_TRADE_KRW = 150_000_000
STRONG_TRADE_KRW = 500_000_000
MAX_EPISODE_START_CHANGE = 8.0
MIN_CURRENT_CHANGE = -2.0
MAX_CURRENT_CHANGE = 12.5
IMMEDIATE_SCORE = 82.0
CONFIRMED_SCORE = 72.0
WATCH_SCORE = 64.0
MIN_SIGNAL_COUNT = 5
MIN_FLOW_CONFIRMATIONS = 2
CONFIRMATIONS_REQUIRED = 2
STATE_MEMORY_RUNS = 16
PROSPECTIVE_HISTORY_RUNS = 672  # 7 days at 15-minute cadence
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
    return (a / b - 1.0) * 100.0 if a > 0 and b > 0 else None


def first_seen_row(first_seen, base):
    row = first_seen.get(base) if isinstance(first_seen, dict) else None
    return row if isinstance(row, dict) else {}


def episode_anchor(fs):
    price = fnum(fs.get("episode_start_price_krw"), fnum(fs.get("first_price_krw")))
    change = fnum(fs.get("episode_start_change_24h_pct"), fnum(fs.get("first_change_24h_pct"), 999.0))
    return {
        "started_at_utc": fs.get("episode_started_at_utc") or fs.get("first_detected_at_utc"),
        "price_krw": price,
        "change_24h_pct": change,
        "runs": int(fs.get("episode_runs") or 0),
        "peak_price_krw": fnum(fs.get("episode_peak_price_krw"), price),
        "reset_reason": fs.get("episode_reset_reason"),
    }


def evaluate(row, first_seen, failed_assets):
    base = row.get("base")
    fs = first_seen_row(first_seen, base)
    ep = episode_anchor(fs)
    ch = fnum(row.get("change_24h_pct"), 999.0)
    trade = fnum(row.get("trade_24h_krw"))
    price = fnum(row.get("price_krw"))
    episode_return = pct(price, ep["price_krw"])
    episode_giveback = pct(price, ep["peak_price_krw"])
    track = row.get("hard_track") or row.get("track")

    fast = row.get("fast_5m") or {}
    m15 = row.get("m15") or {}
    h1 = row.get("h1") or {}
    h4 = row.get("h4") or {}
    d1 = row.get("d1") or {}
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
    week = fnum(d1.get("week_change_pct"))

    hard_failures = []
    if base in LOW_BETA:
        hard_failures.append("low_beta_major")
    if bool(row.get("warning_flag")):
        hard_failures.append("market_warning")
    if trade < MIN_TRADE_KRW:
        hard_failures.append("turnover_below_150m")
    if ep["change_24h_pct"] > MAX_EPISODE_START_CHANGE:
        hard_failures.append("episode_started_above_8pct")
    if not (MIN_CURRENT_CHANGE <= ch <= MAX_CURRENT_CHANGE):
        hard_failures.append("current_change_outside_minus2_to_12_5")
    if track == "CHASE_EXCLUDED":
        hard_failures.append("already_chase_excluded")
    if pace > 4.0:
        hard_failures.append("five_minute_price_pace_over_4pct")
    if wick >= 3.0:
        hard_failures.append("large_15m_upper_wick")
    if episode_return is not None and episode_return > 15.0:
        hard_failures.append("already_over_15pct_from_episode_start")
    if episode_return is not None and episode_return < -2.5:
        hard_failures.append("episode_below_start_over_2_5pct")
    if episode_giveback is not None and episode_giveback <= -6.0 and ch <= 6.0:
        hard_failures.append("episode_giveback_over_6pct")

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
    flow_keys = ("m15_volume_spike", "m15_volume_persistence", "buy_flow", "money_leads_price", "fast_turnover")
    flow_confirmations = sum(bool(signals[k]) for k in flow_keys)

    raw_score = 18 if ep["change_24h_pct"] <= 2 else (14 if ep["change_24h_pct"] <= 4.5 else 8)
    raw_score += 16 if -1 <= ch <= 4 else (11 if ch <= 8 else 5)
    raw_score += 12 if trade >= 1_000_000_000 else (8 if trade >= STRONG_TRADE_KRW else 5)
    raw_score += 12 if vsp >= 2 else (8 if vsp >= 1.4 else 0)
    raw_score += 10 if vper >= 1.5 else (6 if vper >= 1.1 else 0)
    raw_score += 7 if signals["m15_structure"] else -3
    raw_score += 5 if signals["h1_structure"] else 0
    raw_score += 12 if bsr >= 1.35 else (8 if bsr >= 1.05 else (2 if bsr >= 0.85 else (-10 if bsr > 0 else 0)))
    raw_score += 6 if obr >= 1 else (3 if obr >= 0.7 else (-5 if 0 < obr < 0.5 else 0))
    raw_score += min(12.0, max(0.0, money * 0.35))
    raw_score += min(8.0, max(0.0, (turnover - 1.0) * 4.0))
    raw_score += min(10.0, signatures * 2.0)
    raw_score += min(8.0, max(0.0, (beam - 85.0) * 0.16))
    raw_score += 5 if -0.3 <= pace <= 1.5 else (2 if 1.5 < pace <= 2.5 else (-6 if pace < -0.5 else 0))
    raw_score += 4 if wick <= 0.8 else (-5 if wick >= 1.5 else 0)
    raw_score += 3 if h4.get("low_rising") or h4.get("close_above_ma") else 0
    raw_score += 3 if week >= 0 else (-3 if week <= -8 else 0)
    raw_score -= 5 if base in failed_assets else 0

    raw_score = round(raw_score, 2)
    score = max(0.0, min(100.0, raw_score))
    priority_score = round(raw_score + flow_confirmations * 2.0 + signal_count * 0.5 + min(5.0, max(0.0, ch)), 2)

    immediate = (
        not hard_failures
        and ch <= 8.0
        and (episode_return is None or episode_return <= 8.0)
        and raw_score >= IMMEDIATE_SCORE
        and signal_count >= MIN_SIGNAL_COUNT
        and flow_confirmations >= MIN_FLOW_CONFIRMATIONS
    )
    confirmed_quality = (
        not hard_failures
        and raw_score >= CONFIRMED_SCORE
        and signal_count >= MIN_SIGNAL_COUNT
        and flow_confirmations >= MIN_FLOW_CONFIRMATIONS
    )
    watch_quality = not hard_failures and raw_score >= WATCH_SCORE and flow_confirmations >= 1
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
        "raw_score": raw_score,
        "priority_score": priority_score,
        "signal_count": signal_count,
        "flow_confirmation_count": flow_confirmations,
        "signals": signals,
        "hard_failures": hard_failures,
        "immediate_quality": immediate,
        "confirmed_quality": confirmed_quality,
        "watch_quality": watch_quality,
        "lifetime_first_detected_at_utc": fs.get("first_detected_at_utc"),
        "lifetime_first_price_krw": fs.get("first_price_krw"),
        "lifetime_first_change_24h_pct": fs.get("first_change_24h_pct"),
        "episode": {
            "started_at_utc": ep["started_at_utc"],
            "start_price_krw": ep["price_krw"],
            "start_change_24h_pct": ep["change_24h_pct"],
            "runs": ep["runs"],
            "peak_price_krw": ep["peak_price_krw"],
            "return_pct": r(episode_return, 2) if episode_return is not None else None,
            "giveback_from_peak_pct": r(episode_giveback, 2) if episode_giveback is not None else None,
            "reset_reason": ep["reset_reason"],
        },
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
            "week_change_pct": r(week, 2),
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
    iterable = first_seen.items() if isinstance(first_seen, dict) else []
    for base, fs in iterable:
        if base in current_stage2 or base in LOW_BETA:
            continue
        t = tickers.get(base) or {}
        ch = fnum(t.get("change24_pct"), 999.0)
        trade = fnum(t.get("trade24_krw"))
        lifetime_first_ch = fnum((fs or {}).get("first_change_24h_pct"), 999.0)
        lifetime_first_price = fnum((fs or {}).get("first_price_krw"))
        price = fnum(t.get("price"))
        since_first = pct(price, lifetime_first_price)
        if lifetime_first_ch <= 4.5 and 3 <= ch <= 12.5 and trade >= STRONG_TRADE_KRW and since_first is not None and 2 <= since_first <= 15:
            out.append({
                "base": base,
                "price_krw": price,
                "change_24h_pct": ch,
                "trade_24h_krw": trade,
                "lifetime_first_change_24h_pct": lifetime_first_ch,
                "return_since_lifetime_first_pct": r(since_first, 2),
                "status": "REACTIVATE_FOR_FULL_CANDLE_REFRESH",
                "execution_allowed": False,
            })
    out.sort(key=lambda x: (x["change_24h_pct"], x["trade_24h_krw"]), reverse=True)
    return out[:10]


def compact_candidate(x):
    return {
        "base": x.get("base"),
        "price_krw": x.get("price_krw"),
        "change_24h_pct": x.get("change_24h_pct"),
        "trade_24h_krw": x.get("trade_24h_krw"),
        "lane": x.get("lane"),
        "score": x.get("score"),
        "raw_score": x.get("raw_score"),
        "priority_score": x.get("priority_score"),
        "signal_count": x.get("signal_count"),
        "flow_confirmation_count": x.get("flow_confirmation_count"),
        "promotion_grade": x.get("promotion_grade"),
        "episode": x.get("episode"),
        "metrics": x.get("metrics"),
    }


def main():
    now = datetime.now(timezone.utc)
    scan = load_json(SCAN_FILE, {})
    first_seen = load_json(FIRST_SEEN_FILE, {})
    market_state = load_json(MARKET_STATE_FILE, {})
    audit = load_json(MISS_AUDIT_FILE, {})
    state = load_json(STATE_FILE, {"confirmations": {}, "history": []})
    prospective_history = load_json(HISTORY_FILE, [])
    if not isinstance(prospective_history, list):
        prospective_history = []

    health = scan.get("health") or {}
    blockers = list(health.get("hard_blockers") or [])
    rows = [x for x in (scan.get("stage2_ranked") or []) if x.get("base")]
    failed_assets = {x.get("base") for x in (audit.get("failed_acceleration_examples") or []) if x.get("base")}

    evaluated = [evaluate(row, first_seen, failed_assets) for row in rows]
    evaluated.sort(key=lambda x: (x["priority_score"], x["trade_24h_krw"]), reverse=True)
    prev = state.get("confirmations") or {}
    counts = {x["base"]: int(prev.get(x["base"]) or 0) + 1 for x in evaluated if x["confirmed_quality"]}

    for item in evaluated:
        item["confirmations"] = int(counts.get(item["base"]) or 0)
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
    reactivation = reactivation_watch(first_seen, market_state, {x.get("base") for x in rows})

    state["version"] = VERSION
    state["updated_at_utc"] = now.isoformat()
    state["confirmations"] = counts
    state.setdefault("history", []).append({
        "generated_at_utc": now.isoformat(),
        "promoted": [x["base"] for x in promoted[:3]],
        "watch": [x["base"] for x in watches[:5]],
        "reactivation": [x["base"] for x in reactivation[:5]],
    })
    state["history"] = state["history"][-STATE_MEMORY_RUNS:]

    prospective_history.append({
        "generated_at_utc": now.isoformat(),
        "promoted": [compact_candidate(x) for x in promoted[:5]],
        "watch": [compact_candidate(x) for x in watches[:8]],
        "reactivation": reactivation[:10],
    })
    prospective_history = prospective_history[-PROSPECTIVE_HISTORY_RUNS:]

    status = "BLOCKED" if blockers else ("EARLY_WINNER_CANDIDATE" if promoted else ("WATCH_ONLY" if watches or reactivation else "NO_SIGNAL"))
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
        "top_candidates": (promoted + watches)[:5],
        "reactivation_watch": reactivation,
        "policy": {
            "purpose": "bridge raw early detection to user-visible promotion before the move is obvious, using resettable ignition episodes rather than stale lifetime first-seen anchors",
            "scan_all_bithumb_source": True,
            "episode_aware_anchor": True,
            "lifetime_first_seen_is_audit_only": True,
            "no_single_pick_lock": True,
            "four_hour_structure_is_soft_context_not_veto": True,
            "fifteen_minute_higher_low_is_not_single_veto": True,
            "thirty_minute_three_higher_lows_not_required": True,
            "episode_start_max_change_pct": MAX_EPISODE_START_CHANGE,
            "current_change_window_pct": [MIN_CURRENT_CHANGE, MAX_CURRENT_CHANGE],
            "immediate_lane_current_change_max_pct": 8.0,
            "min_trade_24h_krw": MIN_TRADE_KRW,
            "immediate_score_min": IMMEDIATE_SCORE,
            "confirmed_score_min": CONFIRMED_SCORE,
            "confirmations_required": CONFIRMATIONS_REQUIRED,
            "minimum_signal_count": MIN_SIGNAL_COUNT,
            "minimum_flow_confirmations": MIN_FLOW_CONFIRMATIONS,
            "prospective_feature_history_runs": PROSPECTIVE_HISTORY_RUNS,
            "external_news_unlock_listing_delisting_tokenomics_check_required_before_money": True,
            "not_a_full_size_buy_signal": True,
            "no_averaging_down": True,
        },
        "miss_feedback": {
            "realized_early_captures": [
                {"base": x.get("base"), "first_detected_change_24h_pct": x.get("first_detected_change_24h_pct"), "current_change_24h_pct": x.get("change_24h_pct")}
                for x in (audit.get("current_top_movers") or []) if x.get("capture") == "EARLY_CAPTURE"
            ][:15],
            "design_change": "weighted flow/volume promotion + controlled accelerator lane + resettable episode anchors + reactivation refresh + top3 surfacing + 7-day prospective feature history",
        },
    }
    save_json(STATE_FILE, state)
    save_json(HISTORY_FILE, prospective_history)
    save_json(OUT_FILE, out)
    print(json.dumps(out, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
