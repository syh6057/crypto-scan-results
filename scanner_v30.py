import json
from datetime import datetime, timezone
from pathlib import Path

import scanner_v29 as v29
import scanner_v28 as v28

VERSION = "v30-early-strong-promotion-audit"
SCAN_FILE = "beam_scan_result.json"
STATE_FILE = "beam_state.json"
FIRST_SEEN_FILE = "beam_first_seen.json"
MISS_AUDIT_FILE = "beam_miss_audit.json"
OUT_FILE = "beam_early_strong.json"
LOCK_FILE = "early_strong_state.json"
HISTORY_FILE = "early_strong_history.json"

MIN_TRADE_24H = 200_000_000
MAX_CURRENT_CHANGE = 6.0
MIN_CURRENT_CHANGE = -1.5
MAX_FIRST_CHANGE = 4.5
MAX_ANCHOR_LAG_MIN = 10.0
TEST_SCORE = 78.0
WATCH_SCORE = 70.0
MAX_TEST_PORTFOLIO_PCT = 5.0
STOP_PCT = 5.0
NO_CHASE_PCT = 2.5


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


def r(v, n=3):
    try:
        return round(float(v), n)
    except Exception:
        return None


def current_ticker(state, base):
    return ((state.get("tickers") or {}).get(base) or {})


def first_seen_row(first_seen, base):
    row = first_seen.get(base) if isinstance(first_seen, dict) else None
    return row if isinstance(row, dict) else {}


def sector_context(scan, row):
    sector = row.get("sector") or "OTHER"
    leader = ((scan.get("sector_leaders") or {}).get(sector) or {})
    return {
        "sector": sector,
        "leader_base": leader.get("base"),
        "leader_change_24h_pct": leader.get("change_24h_pct"),
        "leader_trade_24h_krw": leader.get("trade_24h_krw"),
        "candidate_is_sector_leader": leader.get("base") == row.get("base"),
    }


def prefilter(row, first_seen, btc, eth):
    base = row.get("base")
    fs = first_seen_row(first_seen, base)
    ch = fnum(row.get("change_24h_pct"), 999)
    trade = fnum(row.get("trade_24h_krw"))
    first_ch = fnum(fs.get("first_change_24h_pct"), 999)
    track = row.get("hard_track") or row.get("track")
    warning = bool(row.get("warning_flag"))
    rel = ch - max(btc, eth)
    reasons = []
    if trade < MIN_TRADE_24H:
        reasons.append("trade24_below_200m")
    if not (MIN_CURRENT_CHANGE <= ch <= MAX_CURRENT_CHANGE):
        reasons.append("current_change_outside_early_window")
    if first_ch > MAX_FIRST_CHANGE:
        reasons.append("first_detection_too_late")
    if warning:
        reasons.append("market_warning")
    if track == "CHASE_EXCLUDED":
        reasons.append("already_chase_excluded")
    if rel < -1.0:
        reasons.append("materially_lags_btc_eth")
    return not reasons, reasons


def score_candidate(scan, row, first_seen, miss_audit, points, now, btc, eth):
    base = row.get("base")
    fs = first_seen_row(first_seen, base)
    path = v28.summarize(points)
    m15 = row.get("m15") or {}
    h1 = row.get("h1") or {}
    h4 = row.get("h4") or {}
    flow = row.get("trade_flow") or {}
    ob = row.get("orderbook") or {}
    fast = row.get("fast_5m") or {}
    ch = fnum(row.get("change_24h_pct"))
    trade24 = fnum(row.get("trade_24h_krw"))
    first_ch = fnum(fs.get("first_change_24h_pct"), 999)
    rel = ch - max(btc, eth)
    bsr = fnum(flow.get("buy_sell_ratio"))
    obr = fnum(ob.get("bid_ask_depth_ratio"))
    lag = 999.0
    if points:
        lag = fnum(points[-1].get("anchor_lag_minutes"), 999.0)

    score = 0.0
    good = []
    weak = []

    exact = bool(path.get("exact_slots_complete"))
    fresh_anchor = lag <= MAX_ANCHOR_LAG_MIN
    if exact:
        score += 16; good.append("exact_5m_x6_complete")
    else:
        weak.append("incomplete_5m_x6")
    if fresh_anchor:
        score += 8; good.append("latest_closed_5m_fresh")
    else:
        weak.append("latest_closed_5m_stale")

    if MIN_CURRENT_CHANGE <= ch <= MAX_CURRENT_CHANGE:
        score += 8; good.append("still_early_not_vertical")
    if first_ch <= MAX_FIRST_CHANGE:
        score += 9; good.append("captured_early")
    if trade24 >= 500_000_000:
        score += 5; good.append("liquid_500m_plus")
    elif trade24 >= MIN_TRADE_24H:
        score += 3

    if m15.get("low_rising"):
        score += 8; good.append("15m_higher_low")
    else:
        weak.append("15m_higher_low_false")
    if m15.get("close_above_ma"):
        score += 5; good.append("15m_above_ma")
    else:
        weak.append("15m_below_ma")

    if h1.get("low_rising"):
        score += 6; good.append("1h_higher_low")
    else:
        weak.append("1h_higher_low_false")
    if h1.get("close_above_ma"):
        score += 4; good.append("1h_above_ma")
    else:
        weak.append("1h_below_ma")

    # 4h is a veto only when both trend checks are weak; early winners often ignite before full 4h confirmation.
    if h4.get("low_rising"):
        score += 3; good.append("4h_higher_low")
    if h4.get("close_above_ma"):
        score += 3; good.append("4h_above_ma")
    if not h4.get("low_rising") and not h4.get("close_above_ma"):
        score -= 4; weak.append("4h_structure_not_confirmed")

    if bsr >= 1.35:
        score += 9; good.append("buyers_dominate_1_35_plus")
    elif bsr >= 1.15:
        score += 6; good.append("buyers_dominate_1_15_plus")
    elif bsr < 1.0:
        score -= 10; weak.append("sell_flow_dominant")
    else:
        weak.append("buy_flow_not_strong")

    if obr >= 0.8:
        score += 5; good.append("orderbook_supportive")
    elif obr >= 0.6:
        score += 2
    elif 0 < obr < 0.6:
        score -= 5; weak.append("orderbook_ask_heavy")

    if rel >= 0.75:
        score += 7; good.append("relative_strength_vs_btc_eth")
    elif rel >= 0:
        score += 3
    else:
        score -= 5; weak.append("lags_btc_eth")

    if exact:
        if int(path.get("higher_low_steps_of_5") or 0) >= 3:
            score += 6; good.append("30m_higher_lows")
        else:
            weak.append("30m_higher_lows_insufficient")
        vacc = fnum(path.get("volume_acceleration_last2_vs_first2_x"))
        if vacc >= 1.15:
            score += 6; good.append("30m_trade_value_accelerating")
        elif vacc < 0.8:
            score -= 5; weak.append("30m_trade_value_fading")
        ret30 = fnum(path.get("return_30m_pct"), 99)
        if -0.5 <= ret30 <= 4.5:
            score += 4; good.append("30m_price_response_controlled")
        elif ret30 > 6:
            score -= 7; weak.append("30m_price_already_extended")
        if fnum(path.get("max_close_drawdown_pct"), 99) <= 2.5:
            score += 3
        else:
            score -= 4; weak.append("30m_giveback_too_large")

    pace = fnum(fast.get("price_change_per_5m_pct"), 0)
    if -0.5 <= pace <= 1.5:
        score += 3
    elif pace > 2.0:
        score -= 4; weak.append("5m_price_pace_too_hot")

    sec = sector_context(scan, row)
    if sec.get("candidate_is_sector_leader"):
        score += 3; good.append("sector_leader")
    elif fnum(sec.get("leader_change_24h_pct")) >= 3:
        score += 2; good.append("sector_flow_positive")

    failed_assets = {x.get("base") for x in (miss_audit.get("failed_acceleration_examples") or []) if x.get("base")}
    if base in failed_assets:
        score -= 4; weak.append("same_asset_recent_failed_acceleration_penalty")

    mandatory_test = {
        "exact_5m_x6": exact,
        "fresh_anchor": fresh_anchor,
        "early_current_window": MIN_CURRENT_CHANGE <= ch <= MAX_CURRENT_CHANGE,
        "early_first_detection": first_ch <= MAX_FIRST_CHANGE,
        "15m_higher_low": bool(m15.get("low_rising")),
        "15m_above_ma": bool(m15.get("close_above_ma")),
        "buy_sell_1_15_plus": bsr >= 1.15,
        "relative_strength_nonnegative": rel >= 0,
        "30m_higher_low_steps_3_plus": int(path.get("higher_low_steps_of_5") or 0) >= 3,
        "liquidity_200m_plus": trade24 >= MIN_TRADE_24H,
        "no_warning": not bool(row.get("warning_flag")),
    }
    mandatory_pass = all(mandatory_test.values())
    score = max(0.0, min(100.0, round(score, 2)))
    if mandatory_pass and score >= TEST_SCORE:
        grade = "EARLY_STRONG_TEST"
    elif score >= WATCH_SCORE:
        grade = "EARLY_STRONG_WATCH"
    else:
        grade = "REJECT"

    price = fnum(row.get("price_krw"))
    return {
        "base": base,
        "market": row.get("market"),
        "sector": row.get("sector"),
        "price_krw": price,
        "change_24h_pct": ch,
        "trade_24h_krw": trade24,
        "first_detected_price_krw": fs.get("first_price_krw"),
        "first_detected_change_24h_pct": first_ch,
        "first_detected_at_utc": fs.get("first_detected_at_utc"),
        "track": row.get("hard_track") or row.get("track"),
        "score": score,
        "grade": grade,
        "mandatory_test_checks": mandatory_test,
        "good_axes": good,
        "weak_axes": weak,
        "five_minute_series": points,
        "thirty_minute_path": path,
        "m15": m15,
        "h1": h1,
        "h4": h4,
        "trade_flow": flow,
        "orderbook": ob,
        "relative_strength_vs_stronger_major_pct": r(rel, 2),
        "sector_context": sec,
        "entry_plan": {
            "reference_price_krw": price,
            "do_not_chase_above_krw": r(price * (1 + NO_CHASE_PCT / 100.0), 8),
            "hard_stop_krw": r(price * (1 - STOP_PCT / 100.0), 8),
            "target_10_krw": r(price * 1.10, 8),
            "target_20_krw": r(price * 1.20, 8),
            "target_30_scenario_krw": r(price * 1.30, 8),
            "max_test_portfolio_pct": MAX_TEST_PORTFOLIO_PCT,
            "external_news_unlock_listing_delisting_tokenomics_check_required": True,
        },
    }


def update_trials(history, state):
    if not isinstance(history, list):
        history = []
    tickers = state.get("tickers") or {}
    for t in history:
        if t.get("resolved"):
            continue
        base = t.get("base")
        row = tickers.get(base) or {}
        p = fnum(row.get("price"))
        entry = fnum(t.get("entry_price_krw"))
        if p <= 0 or entry <= 0:
            continue
        ret = (p / entry - 1) * 100.0
        t["latest_return_pct"] = round(ret, 2)
        t["peak_return_pct"] = max(fnum(t.get("peak_return_pct"), ret), ret)
        t["trough_return_pct"] = min(fnum(t.get("trough_return_pct"), ret), ret)
        if t["peak_return_pct"] >= 30:
            t["resolved"] = True; t["outcome"] = "WIN_30"
        elif t["trough_return_pct"] <= -5:
            t["resolved"] = True; t["outcome"] = "LOSS_STOP"
    return history[-200:]


def retrospective_audit(miss_audit, history):
    movers = miss_audit.get("current_top_movers") or []
    out = []
    for m in movers[:20]:
        base = m.get("base")
        trials = [t for t in history if t.get("base") == base]
        out.append({
            "base": base,
            "current_change_24h_pct": m.get("change_24h_pct"),
            "base_scanner_capture": m.get("capture"),
            "first_detected_change_24h_pct": m.get("first_detected_change_24h_pct"),
            "v30_was_available_before_move": any(t.get("designated_at_utc") for t in trials),
            "v30_trial_count": len(trials),
            "note": "Legacy EARLY_CAPTURE proves detection only. It does not prove v30 would have authorized a test entry before v30 existed.",
        })
    return out


def main():
    now = datetime.now(timezone.utc)
    scan = load_json(SCAN_FILE, {})
    state = load_json(STATE_FILE, {})
    first_seen = load_json(FIRST_SEEN_FILE, {})
    miss_audit = load_json(MISS_AUDIT_FILE, {})
    lock = load_json(LOCK_FILE, {})
    history = update_trials(load_json(HISTORY_FILE, []), state)

    health = scan.get("health") or {}
    blockers = list(health.get("hard_blockers") or [])
    tickers = state.get("tickers") or {}
    btc = fnum((tickers.get("BTC") or {}).get("change24_pct"))
    eth = fnum((tickers.get("ETH") or {}).get("change24_pct"))

    rows = [x for x in (scan.get("stage2_ranked") or []) if x.get("base") and x.get("market")]
    pre = []
    rejected_prefilter = []
    for row in rows:
        ok, reasons = prefilter(row, first_seen, btc, eth)
        if ok:
            pre.append(row)
        else:
            rejected_prefilter.append({"base": row.get("base"), "reasons": reasons})

    scored = []
    pull_errors = []
    for row in pre:
        try:
            points = v29.pull_six_from_latest_closed(row["market"], now)
            scored.append(score_candidate(scan, row, first_seen, miss_audit, points, now, btc, eth))
        except Exception as e:
            pull_errors.append({"base": row.get("base"), "error": str(e)[:200]})

    scored.sort(key=lambda x: (0 if x["grade"] == "REJECT" else 1 if x["grade"] == "EARLY_STRONG_WATCH" else 2, x["score"]), reverse=True)
    test_candidates = [x for x in scored if x["grade"] == "EARLY_STRONG_TEST"]
    watch_candidates = [x for x in scored if x["grade"] == "EARLY_STRONG_WATCH"]

    active = lock.get("active") if isinstance(lock, dict) else None
    active_status = None
    if active:
        t = current_ticker(state, active.get("base"))
        p = fnum(t.get("price"))
        entry = fnum(active.get("entry_price_krw"))
        ret = (p / entry - 1) * 100.0 if p > 0 and entry > 0 else None
        if ret is None:
            active_status = "ACTIVE_PRICE_UNAVAILABLE"
        elif ret <= -STOP_PCT:
            active_status = "STOP_HIT_RELEASED"
            active = None
        elif ret >= 30:
            active_status = "TARGET_30_HIT_RELEASED"
            active = None
        elif fnum(t.get("change24_pct")) > 15:
            active_status = "NO_LONGER_EARLY_PROFIT_PROTECT"
        elif ret >= 10:
            active_status = "PROFIT_PROTECT"
        else:
            active_status = "ACTIVE_EARLY_STRONG"
        if active:
            active["current_price_krw"] = p
            active["current_return_pct"] = r(ret, 2)
            active["status"] = active_status

    promoted = None
    if not blockers and active is None and test_candidates:
        promoted = test_candidates[0]
        entry = fnum(promoted.get("price_krw"))
        active = {
            "base": promoted.get("base"),
            "designated_at_utc": now.isoformat(),
            "entry_price_krw": entry,
            "score_at_designation": promoted.get("score"),
            "grade_at_designation": promoted.get("grade"),
            "hard_stop_krw": r(entry * 0.95, 8),
            "do_not_chase_above_krw": r(entry * 1.025, 8),
            "max_test_portfolio_pct": MAX_TEST_PORTFOLIO_PCT,
            "status": "ACTIVE_EARLY_STRONG",
        }
        history.append({
            "base": promoted.get("base"),
            "designated_at_utc": now.isoformat(),
            "entry_price_krw": entry,
            "score": promoted.get("score"),
            "grade": promoted.get("grade"),
            "peak_return_pct": 0.0,
            "trough_return_pct": 0.0,
            "latest_return_pct": 0.0,
            "resolved": False,
            "outcome": "OPEN",
        })

    resolved = [t for t in history if t.get("resolved")]
    wins = sum(1 for t in resolved if t.get("outcome") == "WIN_30")
    losses = sum(1 for t in resolved if t.get("outcome") == "LOSS_STOP")

    if blockers:
        status = "BLOCKED"
    elif promoted:
        status = "EARLY_STRONG_TEST_CANDIDATE"
    elif active:
        status = active_status or "ACTIVE_EARLY_STRONG"
    elif watch_candidates:
        status = "WATCH_ONLY"
    else:
        status = "NO_EARLY_STRONG"

    out = {
        "generated_at_utc": now.isoformat(),
        "version": VERSION,
        "health": {
            "analysis_ready": bool(health.get("analysis_ready")) and not blockers,
            "hard_blockers": blockers,
            "warnings": list(health.get("warnings") or []),
            "stage2_candidates": len(rows),
            "early_prefilter_candidates": len(pre),
            "six_point_pull_errors": pull_errors,
        },
        "market": {"btc_change_24h_pct": btc, "eth_change_24h_pct": eth},
        "status": status,
        "active_early_strong": active,
        "newly_promoted": promoted,
        "best_test_candidate": test_candidates[0] if test_candidates else None,
        "best_watch_candidate": watch_candidates[0] if watch_candidates else None,
        "ranked_candidates": scored[:12],
        "prefilter_rejections": rejected_prefilter[:20],
        "policy": {
            "purpose": "Do not bury genuine early winners behind the A+ execution gate. Surface one stable early-strong candidate while keeping it separate from full-size FINAL_BUY.",
            "not_full_buy_signal": True,
            "test_entry_requires_external_check_in_chat": True,
            "max_test_portfolio_pct": MAX_TEST_PORTFOLIO_PCT,
            "no_averaging_down": True,
            "hard_stop_pct": STOP_PCT,
            "no_chase_pct": NO_CHASE_PCT,
            "current_change_window_pct": [MIN_CURRENT_CHANGE, MAX_CURRENT_CHANGE],
            "max_first_detection_change_pct": MAX_FIRST_CHANGE,
            "min_trade_24h_krw": MIN_TRADE_24H,
            "test_score_min": TEST_SCORE,
            "watch_score_min": WATCH_SCORE,
            "stable_pick_rule": "keep the active early-strong pick until stop, +30 target, or it is no longer an early setup; do not flip just because ranking changes",
            "external_axes_required_before_money": [
                "official news/announcements",
                "unlock/tokenomics/large-supply risk",
                "listing/delisting/trading-warning risk",
                "fresh Bithumb execution price",
            ],
        },
        "prospective_calibration": {
            "resolved": len(resolved),
            "wins_30_before_stop": wins,
            "losses_stop_before_30": losses,
            "hit_rate": round(wins / len(resolved), 4) if resolved else None,
            "probability_claim_allowed": False,
            "note": "No numeric win probability is claimed from heuristic score. Prospective samples must accumulate first.",
        },
        "retrospective_top_mover_audit": retrospective_audit(miss_audit, history),
    }

    save_json(LOCK_FILE, {"version": VERSION, "active": active, "updated_at_utc": now.isoformat()})
    save_json(HISTORY_FILE, history[-200:])
    save_json(OUT_FILE, out)
    print(json.dumps(out, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
