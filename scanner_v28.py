import json
from concurrent.futures import ThreadPoolExecutor, as_completed
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path

import scanner_v16 as v16

VERSION = "v28-30m-six-point-expert-gate"
SCAN_FILE = "beam_scan_result.json"
FINAL_GATE_FILE = "beam_final_trade_gate.json"
OUT_FILE = "beam_30m_expert.json"
HISTORY_FILE = "beam_30m_expert_history.json"
MAX_CANDIDATES = 45
POINTS_REQUIRED = 6
HISTORY_RUNS = 96


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
    if a is None or b in (None, 0):
        return None
    return (float(a) / float(b) - 1.0) * 100.0


def avg(vals):
    vals = [float(x) for x in vals if x is not None]
    return sum(vals) / len(vals) if vals else None


def r(v, n=3):
    return None if v is None else round(float(v), n)


def pull_six_closed_5m(market):
    rows = v16.candles(market, 5, 9)[-POINTS_REQUIRED:]
    if len(rows) < POINTS_REQUIRED:
        return []
    out = []
    for i, row in enumerate(rows):
        out.append({
            "minutes_ago": (POINTS_REQUIRED - i) * 5,
            "candle_time_utc": row.get("candle_date_time_utc"),
            "open": fnum(row.get("opening_price")),
            "high": fnum(row.get("high_price")),
            "low": fnum(row.get("low_price")),
            "close": fnum(row.get("trade_price")),
            "trade_krw": fnum(row.get("candle_acc_trade_price")),
        })
    return out


def summarize(points):
    if len(points) != POINTS_REQUIRED:
        return {"complete": False, "data_points": len(points)}
    opens = [x["open"] for x in points]
    highs = [x["high"] for x in points]
    lows = [x["low"] for x in points]
    closes = [x["close"] for x in points]
    values = [x["trade_krw"] for x in points]
    start = opens[0] if opens[0] > 0 else closes[0]
    higher_lows = sum(lows[i] >= lows[i - 1] for i in range(1, 6))
    higher_highs = sum(highs[i] >= highs[i - 1] for i in range(1, 6))
    positive = sum(closes[i] >= opens[i] for i in range(6))
    first2 = avg(values[:2])
    last2 = avg(values[-2:])
    volume_accel = last2 / first2 if first2 and last2 is not None else None
    peak = closes[0]
    worst = 0.0
    for c in closes:
        peak = max(peak, c)
        worst = min(worst, (c / peak - 1.0) * 100.0 if peak else 0.0)
    return {
        "complete": True,
        "data_points": 6,
        "return_30m_pct": r(pct(closes[-1], start)),
        "higher_low_steps_of_5": higher_lows,
        "higher_high_steps_of_5": higher_highs,
        "positive_candles_of_6": positive,
        "trade_value_30m_krw": round(sum(values), 0),
        "volume_acceleration_last2_vs_first2_x": r(volume_accel, 2),
        "max_close_drawdown_pct": r(abs(worst)),
    }


def prior_for(history, base):
    if not isinstance(history, list):
        return None
    for run in reversed(history):
        cand = (run.get("candidates") or {}).get(base)
        if cand:
            return cand
    return None


def technical_grade(row, path, btc, eth):
    m15 = row.get("m15") or {}
    h1 = row.get("h1") or {}
    h4 = row.get("h4") or {}
    flow = row.get("trade_flow") or {}
    ob = row.get("orderbook") or {}
    fast = row.get("fast_5m") or {}
    d1 = row.get("d1") or {}
    score = 0.0
    failures = []

    for label, tf, weight in (("15m", m15, 10), ("1h", h1, 12), ("4h", h4, 14)):
        if tf.get("low_rising"):
            score += weight * 0.55
        else:
            failures.append(label + "_higher_low_false")
        if tf.get("close_above_ma"):
            score += weight * 0.45
        else:
            failures.append(label + "_close_above_ma_false")

    if path.get("complete"):
        ret30 = fnum(path.get("return_30m_pct"), -99)
        vacc = fnum(path.get("volume_acceleration_last2_vs_first2_x"))
        if -0.25 <= ret30 <= 4.5:
            score += 8
        else:
            failures.append("30m_price_path_not_controlled")
        if int(path.get("higher_low_steps_of_5") or 0) >= 3:
            score += 8
        else:
            failures.append("30m_higher_lows_insufficient")
        if int(path.get("positive_candles_of_6") or 0) >= 3:
            score += 5
        else:
            failures.append("30m_positive_candles_insufficient")
        if vacc >= 1.15:
            score += 8
        elif vacc < 0.8:
            score -= 5
            failures.append("30m_trade_value_fading")
        if fnum(path.get("max_close_drawdown_pct"), 99) <= 2.5:
            score += 5
        else:
            failures.append("30m_giveback_too_large")
    else:
        score -= 15
        failures.append("six_point_5m_path_incomplete")

    bsr = fnum(flow.get("buy_sell_ratio"))
    obr = fnum(ob.get("bid_ask_depth_ratio"))
    ch24 = fnum(row.get("change_24h_pct"))
    rel = ch24 - max(btc, eth)
    if bsr >= 1.35:
        score += 8
    elif bsr < 1.0:
        score -= 7
        failures.append("sell_flow_dominant")
    if obr >= 0.8:
        score += 5
    elif 0 < obr < 0.55:
        score -= 5
        failures.append("orderbook_ask_heavy")
    if rel >= 0.75:
        score += 6
    elif rel < 0:
        score -= 5
        failures.append("lags_btc_eth")
    if 0 <= ch24 <= 6:
        score += 5
    elif ch24 > 10:
        score -= 8
        failures.append("24h_extended")
    if fnum(d1.get("week_change_pct")) <= 15:
        score += 4
    if -0.25 <= fnum(fast.get("price_change_per_5m_pct")) <= 1.5:
        score += 3
    if row.get("warning_flag"):
        score -= 20
        failures.append("market_warning")

    score = max(0.0, min(100.0, round(score, 2)))
    grade = "A" if score >= 85 else "B+" if score >= 78 else "B" if score >= 70 else "C_OR_LOWER"
    return {
        "score": score,
        "grade": grade,
        "failed_or_weak_axes": failures,
        "relative_strength_vs_stronger_major_pct": round(rel, 2),
        "buy_sell_ratio": round(bsr, 2),
        "orderbook_ratio": round(obr, 2),
    }


def main():
    now = datetime.now(timezone.utc)
    scan = load_json(SCAN_FILE, {})
    final_gate = load_json(FINAL_GATE_FILE, {})
    history = load_json(HISTORY_FILE, [])
    health = scan.get("health") or {}
    rows = [x for x in (scan.get("stage2_ranked") or [])[:MAX_CANDIDATES] if x.get("market") and x.get("base")]
    tickers = (load_json("beam_state.json", {}) or {}).get("tickers") or {}
    btc = fnum((tickers.get("BTC") or {}).get("change24_pct"))
    eth = fnum((tickers.get("ETH") or {}).get("change24_pct"))

    series = {}
    with ThreadPoolExecutor(max_workers=8) as ex:
        fs = {ex.submit(pull_six_closed_5m, row["market"]): row["base"] for row in rows}
        for fut in as_completed(fs):
            base = fs[fut]
            try:
                series[base] = fut.result()
            except Exception:
                series[base] = []

    candidates = []
    compact = {}
    complete_count = 0
    for row in rows:
        base = row["base"]
        points = series.get(base) or []
        path = summarize(points)
        if path.get("complete"):
            complete_count += 1
        grade = technical_grade(row, path, btc, eth)
        prior = prior_for(history, base) or {}
        bsr = fnum((row.get("trade_flow") or {}).get("buy_sell_ratio"))
        obr = fnum((row.get("orderbook") or {}).get("bid_ask_depth_ratio"))
        previous_price = fnum(prior.get("price_krw"))
        item = {
            "base": base,
            "market": row.get("market"),
            "price_krw": row.get("price_krw"),
            "change_24h_pct": row.get("change_24h_pct"),
            "trade_24h_krw": row.get("trade_24h_krw"),
            "track": row.get("hard_track") or row.get("track"),
            "five_minute_series": points,
            "thirty_minute_path": path,
            "m15": row.get("m15"),
            "h1": row.get("h1"),
            "h4": row.get("h4"),
            "trade_flow": row.get("trade_flow"),
            "orderbook": row.get("orderbook"),
            "delta_vs_previous_30m_run": {
                "price_pct": r(pct(fnum(row.get("price_krw")), previous_price)) if previous_price > 0 else None,
                "buy_sell_ratio": r(bsr - fnum(prior.get("buy_sell_ratio")), 2) if prior else None,
                "orderbook_ratio": r(obr - fnum(prior.get("orderbook_ratio")), 2) if prior else None,
            },
            "professional_technical_grade": grade,
        }
        candidates.append(item)
        compact[base] = {"price_krw": row.get("price_krw"), "buy_sell_ratio": bsr, "orderbook_ratio": obr, "grade": grade["grade"], "score": grade["score"]}

    rank = {"A": 3, "B+": 2, "B": 1, "C_OR_LOWER": 0}
    candidates.sort(key=lambda x: (rank.get(x["professional_technical_grade"]["grade"], 0), x["professional_technical_grade"]["score"]), reverse=True)
    a_or_bplus = [x for x in candidates if x["professional_technical_grade"]["grade"] in {"A", "B+"}]
    best = a_or_bplus[0] if a_or_bplus else None
    v27_pick = ((final_gate.get("final_pick") or {}).get("base"))
    technical_a = bool(best and best["professional_technical_grade"]["grade"] == "A" and final_gate.get("status") == "FINAL_BUY" and best.get("base") == v27_pick)
    blockers = list(health.get("hard_blockers") or [])
    status = "BLOCKED" if blockers else "A_TECHNICAL_EXTERNAL_CHECK_REQUIRED" if technical_a else "WATCH_ONLY" if best else "NO_BUY"

    out = {
        "generated_at_utc": now.isoformat(),
        "version": VERSION,
        "schedule_design": {
            "github_workflow_cadence_minutes": 30,
            "single_run_points_minutes": [5, 10, 15, 20, 25, 30],
            "method": "Each 30-minute workflow run backfills the six most recent closed Bithumb 5-minute candles for every stage2 finalist, so one extraction contains the full 30-minute time path.",
            "orderbook_tradeflow_method": "Use current live orderbook/trade flow and compare them with the persisted prior 30-minute run; public historical 5-minute orderbooks are not available for retroactive backfill."
        },
        "health": {
            "analysis_ready": bool(health.get("analysis_ready")) and not blockers,
            "hard_blockers": blockers,
            "warnings": list(health.get("warnings") or []),
            "stage2_candidates": len(rows),
            "six_point_paths_complete": complete_count,
            "six_point_coverage_pct": round(complete_count / max(1, len(rows)) * 100.0, 1)
        },
        "upstream_v27_status": final_gate.get("status"),
        "upstream_v27_pick": v27_pick,
        "status": status,
        "best_a_or_bplus_candidate": best,
        "candidates": candidates[:12],
        "professional_decision_contract": {
            "separate_upside_possibility_from_entry_permission": True,
            "no_forced_candidate": True,
            "if_no_a_or_bplus_return_cash": True,
            "bplus_is_watch_only_not_automatic_buy": True,
            "real_money_requires_all": [
                "v27_FINAL_BUY",
                "v28_grade_A",
                "six_5m_time_flow_confirmation",
                "15m_1h_4h_structure",
                "live_volume_trade_value_and_execution_flow",
                "BTC_ETH_relative_strength",
                "sector_flow",
                "orderbook_and_volatility_overheat_check",
                "support_resistance_and_invalidation",
                "news_official_announcements_unlock_listing_delisting_tokenomics_large_supply_risk_market_risk_clear",
                "fresh_bithumb_execution_price"
            ],
            "recommendation_output_fields": ["entry_price", "split_entry", "hard_stop", "target_1", "target_2", "target_30_scenario", "expected_holding_time", "grade", "invalidation_price_and_conditions"],
            "probability_rule": "Scores and grades are not calibrated probabilities; numeric probability claims require prospective calibration."
        }
    }
    if not isinstance(history, list):
        history = []
    history.append({"generated_at_utc": out["generated_at_utc"], "status": status, "best_base": (best or {}).get("base"), "candidates": compact})
    save_json(HISTORY_FILE, history[-HISTORY_RUNS:])
    save_json(OUT_FILE, out)
    print(json.dumps(out, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
