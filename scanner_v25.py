import json
import math
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path

VERSION = "v25-a-plus-precision-30"
SCAN_FILE = "beam_scan_result.json"
FIRST_SEEN_FILE = "beam_first_seen.json"
STATE_FILE = "a_plus_precision_state.json"
OUT_FILE = "beam_a_plus_precision.json"
BEAM_STATE_FILE = "beam_state.json"
EXPERT_HISTORY_FILE = "expert_prediction_history.json"
SNAPSHOT_HISTORY_FILE = "policy_snapshot_history.json"

MAX_FIRST_CHANGE = 3.0
MAX_CURRENT_CHANGE = 6.0
MIN_TRADE_KRW = 200_000_000
MIN_TURNOVER_X = 5.0
MIN_MONEY_LEADS = 30.0
MIN_M15_PERSISTENCE = 1.5
MIN_BUY_SELL = 1.30
MIN_ORDERBOOK_RATIO = 0.60
MAX_5M_PCT = 2.0
MAX_WEEK_CHANGE = 15.0
MAX_STOP_PCT = 6.0
MIN_REWARD_RISK = 4.5
WINDOW = 5
MIN_HITS_IN_WINDOW = 3
MIN_CONSECUTIVE = 2
STOP_PCT = 5.0
TARGET_PCT = 30.0
MIN_CALIBRATION_RESOLVED = 50
TARGET_CALIBRATION_HIT_RATE = 0.80
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


def parse_ts(v):
    try:
        return datetime.fromisoformat(v.replace("Z", "+00:00"))
    except Exception:
        return None


def wilson_lower_bound(wins, n, z=1.96):
    if n <= 0:
        return None
    phat = wins / n
    denom = 1 + z * z / n
    centre = phat + z * z / (2 * n)
    margin = z * math.sqrt((phat * (1 - phat) + z * z / (4 * n)) / n)
    return (centre - margin) / denom


def market_headwind(beam_state):
    tickers = (beam_state or {}).get("tickers") or {}
    btc = fnum((tickers.get("BTC") or {}).get("change24_pct"))
    eth = fnum((tickers.get("ETH") or {}).get("change24_pct"))
    severe = btc <= -2.0 or eth <= -2.5 or (btc <= -1.5 and eth <= -1.5)
    return {"severe": severe, "btc_change_24h_pct": btc, "eth_change_24h_pct": eth}


def evaluate_row(row, first_seen, headwind):
    base = row.get("base")
    fs = first_seen.get(base) or {}
    price = fnum(row.get("price_krw"))
    ch = fnum(row.get("change_24h_pct"))
    trade = fnum(row.get("trade_24h_krw"))
    first_ch = fnum(fs.get("first_change_24h_pct"), 999.0)
    fast = row.get("fast_5m") or {}
    m15 = row.get("m15") or {}
    h1 = row.get("h1") or {}
    d1 = row.get("d1") or {}
    flow = row.get("trade_flow") or {}
    ob = row.get("orderbook") or {}
    ep = row.get("entry_plan") or {}
    turnover = fnum(fast.get("turnover_intensity_vs_even_5m_x"))
    p5 = fnum(fast.get("price_change_per_5m_pct"))
    m15_persist = fnum(m15.get("vol_persistence_x"))
    bsr = fnum(flow.get("buy_sell_ratio"))
    obr = fnum(ob.get("bid_ask_depth_ratio"))
    money = fnum(row.get("money_leads_price_score"))
    week = fnum(d1.get("week_change_pct"))
    invalidation = fnum(ep.get("invalidation_krw"))
    stop_pct = ((price - invalidation) / price * 100.0) if price > 0 and invalidation > 0 and invalidation < price else 999.0
    reward_risk = TARGET_PCT / stop_pct if stop_pct > 0 else 0.0
    checks = {
        "not_low_beta_major": base not in LOW_BETA,
        "pre_ignition": (row.get("hard_track") or row.get("track")) == "PRE_IGNITION",
        "first_detected_0_to_3pct": 0.0 <= first_ch <= MAX_FIRST_CHANGE,
        "current_0_to_6pct": 0.0 <= ch <= MAX_CURRENT_CHANGE,
        "trade_24h_ge_200m": trade >= MIN_TRADE_KRW,
        "turnover_ge_5x": turnover >= MIN_TURNOVER_X,
        "money_leads_ge_30": money >= MIN_MONEY_LEADS,
        "m15_low_rising": bool(m15.get("low_rising")),
        "m15_volume_persistence_ge_1_5": m15_persist >= MIN_M15_PERSISTENCE,
        "h1_low_rising": bool(h1.get("low_rising")),
        "buy_sell_ge_1_3": bsr >= MIN_BUY_SELL,
        "orderbook_not_bad": obr == 0 or obr >= MIN_ORDERBOOK_RATIO,
        "five_min_not_vertical": -0.5 <= p5 <= MAX_5M_PCT,
        "week_not_extended": week <= MAX_WEEK_CHANGE,
        "no_market_warning": not bool(row.get("warning_flag")),
        "no_severe_market_headwind": not bool(headwind.get("severe")),
        "stop_within_6pct": stop_pct <= MAX_STOP_PCT,
        "reward_risk_ge_4_5": reward_risk >= MIN_REWARD_RISK,
    }
    failed = [k for k, ok in checks.items() if not ok]
    quality = 0.0
    quality += min(25.0, turnover * 2.5)
    quality += min(20.0, money * 0.45)
    quality += min(15.0, max(0.0, (bsr - 1.0) * 15.0))
    quality += min(10.0, max(0.0, (obr - 0.6) * 6.0)) if obr else 3.0
    quality += min(10.0, m15_persist * 3.0)
    quality += 8.0 if bool(m15.get("low_rising")) else 0.0
    quality += 8.0 if bool(h1.get("low_rising")) else 0.0
    quality += 8.0 if first_ch <= 1.5 else (4.0 if first_ch <= 3.0 else 0.0)
    quality += 6.0 if ch <= 3.0 else (3.0 if ch <= 6.0 else 0.0)
    quality -= max(0.0, week - 10.0)
    quality = round(quality, 2)
    return {
        "base": base,
        "market": row.get("market"),
        "korean_name": row.get("korean_name"),
        "price_krw": row.get("price_krw"),
        "change_24h_pct": row.get("change_24h_pct"),
        "first_change_24h_pct": fs.get("first_change_24h_pct"),
        "first_price_krw": fs.get("first_price_krw"),
        "trade_24h_krw": row.get("trade_24h_krw"),
        "quality_score": quality,
        "all_checks_pass": not failed,
        "failed_checks": failed,
        "checks": checks,
        "metrics": {
            "turnover_intensity_x": round(turnover, 2),
            "money_leads_price_score": round(money, 2),
            "m15_volume_persistence_x": round(m15_persist, 2),
            "buy_sell_ratio": round(bsr, 2),
            "orderbook_ratio": round(obr, 2),
            "five_min_price_pct": round(p5, 3),
            "week_change_pct": round(week, 2),
            "stop_distance_pct": round(stop_pct, 2) if stop_pct < 900 else None,
            "reward_risk_to_30pct": round(reward_risk, 2),
        },
        "entry_plan": {
            "reference_price_krw": price,
            "hard_stop_krw": round(price * (1.0 - STOP_PCT / 100.0), 12) if price else None,
            "target_10_krw": round(price * 1.10, 12) if price else None,
            "target_15_krw": round(price * 1.15, 12) if price else None,
            "target_30_krw": round(price * 1.30, 12) if price else None,
            "do_not_chase_above_krw": round(price * 1.025, 12) if price else None,
        },
    }


def update_observation_state(state, evaluations, ts):
    obs = state.setdefault("observations", {})
    current = {x["base"]: bool(x["all_checks_pass"]) for x in evaluations if x.get("base")}
    for base in set(obs) | set(current):
        hist = list(obs.get(base) or [])
        hist.append({"ts": ts, "pass": bool(current.get(base, False))})
        obs[base] = hist[-WINDOW:]
    return obs


def persistence_for(base, observations):
    hist = observations.get(base) or []
    vals = [bool(x.get("pass")) for x in hist]
    hits = sum(vals)
    consecutive = 0
    for v in reversed(vals):
        if v:
            consecutive += 1
        else:
            break
    return {"window_size": len(vals), "hits": hits, "consecutive": consecutive}


def update_forward_calibration(state, current_prices, ts):
    trials = state.setdefault("prospective_trials", [])
    for trial in trials:
        if trial.get("status") != "OPEN":
            continue
        price = fnum(current_prices.get(trial.get("base")))
        entry = fnum(trial.get("entry_price"))
        if price <= 0 or entry <= 0:
            continue
        ret = (price / entry - 1.0) * 100.0
        trial["last_price"] = price
        trial["last_return_pct"] = round(ret, 2)
        trial["peak_return_pct"] = round(max(fnum(trial.get("peak_return_pct"), -999), ret), 2)
        trial["trough_return_pct"] = round(min(fnum(trial.get("trough_return_pct"), 999), ret), 2)
        if ret >= TARGET_PCT:
            trial["status"] = "WIN_30"
            trial["resolved_at_utc"] = ts
        elif ret <= -STOP_PCT:
            trial["status"] = "LOSS_STOP"
            trial["resolved_at_utc"] = ts
    return trials


def prospective_stats(trials):
    resolved = [x for x in trials if x.get("status") in {"WIN_30", "LOSS_STOP"}]
    wins = sum(x.get("status") == "WIN_30" for x in resolved)
    losses = sum(x.get("status") == "LOSS_STOP" for x in resolved)
    n = len(resolved)
    rate = wins / n if n else None
    lb = wilson_lower_bound(wins, n)
    calibrated_80 = bool(n >= MIN_CALIBRATION_RESOLVED and rate is not None and rate >= TARGET_CALIBRATION_HIT_RATE)
    return {
        "resolved": n,
        "wins_30_before_stop": wins,
        "losses_stop_before_30": losses,
        "hit_rate": round(rate, 4) if rate is not None else None,
        "wilson_95pct_lower_bound": round(lb, 4) if lb is not None else None,
        "min_resolved_required_before_probability_claim": MIN_CALIBRATION_RESOLVED,
        "target_hit_rate": TARGET_CALIBRATION_HIT_RATE,
        "may_claim_80pct_probability": calibrated_80,
    }


def build_proxy_backtest():
    expert_hist = load_json(EXPERT_HISTORY_FILE, [])
    snap_hist = load_json(SNAPSHOT_HISTORY_FILE, [])
    snapshots = []
    for s in snap_hist if isinstance(snap_hist, list) else []:
        ts = parse_ts(s.get("generated_at_utc", ""))
        snap = s.get("snapshot") or {}
        if ts and isinstance(snap, dict):
            snapshots.append((ts, snap))
    snapshots.sort(key=lambda x: x[0])
    signals = {}
    for item in expert_hist if isinstance(expert_hist, list) else []:
        ts = parse_ts(item.get("generated_at_utc", ""))
        if not ts:
            continue
        for p in item.get("locked_predictions") or []:
            base = p.get("base")
            if not base or base in signals:
                continue
            if p.get("expert_status") != "PREDICT_BUY":
                continue
            if fnum(p.get("expert_confidence")) < 85:
                continue
            if fnum(p.get("pattern_similarity")) < 75:
                continue
            if not (0 <= fnum(p.get("change_24h_pct")) <= 6):
                continue
            entry = fnum(p.get("price_krw"))
            if entry > 0:
                signals[base] = {"base": base, "ts": ts, "entry": entry}
    outcomes = []
    for sig in signals.values():
        outcome = "UNRESOLVED"
        max_ret = -999.0
        min_ret = 999.0
        for ts, snap in snapshots:
            if ts < sig["ts"]:
                continue
            row = snap.get(sig["base"]) or {}
            px = fnum(row.get("bithumb_krw_price"))
            if px <= 0:
                continue
            ret = (px / sig["entry"] - 1.0) * 100.0
            max_ret = max(max_ret, ret)
            min_ret = min(min_ret, ret)
            if ret >= TARGET_PCT:
                outcome = "WIN_30"
                break
            if ret <= -STOP_PCT:
                outcome = "LOSS_STOP"
                break
        outcomes.append({
            "base": sig["base"],
            "entry_price": sig["entry"],
            "outcome": outcome,
            "max_seen_return_pct": None if max_ret < -900 else round(max_ret, 2),
            "min_seen_return_pct": None if min_ret > 900 else round(min_ret, 2),
        })
    resolved = [x for x in outcomes if x["outcome"] != "UNRESOLVED"]
    wins = sum(x["outcome"] == "WIN_30" for x in resolved)
    rate = wins / len(resolved) if resolved else None
    return {
        "kind": "historical_proxy_not_full_v25_backtest",
        "rule": "first v19 PREDICT_BUY per asset with confidence>=85, similarity>=75, 0<=24h_change<=6; +30 before -5 using later policy snapshots",
        "signals": len(outcomes),
        "resolved": len(resolved),
        "wins": wins,
        "losses": len(resolved) - wins,
        "hit_rate": round(rate, 4) if rate is not None else None,
        "outcomes": outcomes[-80:],
        "limitations": [
            "historical prediction rows do not store all v25 A+ features, so this is not a backtest of the full gate",
            "snapshot cadence can miss intrainterval stop/target touches",
            "no probability claim is allowed from this proxy",
        ],
    }


def main():
    scan = load_json(SCAN_FILE, {})
    first_seen = load_json(FIRST_SEEN_FILE, {})
    state = load_json(STATE_FILE, {"observations": {}, "prospective_trials": [], "designation_history": []})
    beam_state = load_json(BEAM_STATE_FILE, {})
    ts = scan.get("generated_at_utc") or datetime.now(timezone.utc).isoformat()
    headwind = market_headwind(beam_state)
    rows = scan.get("stage2_ranked") or []
    evaluations = [evaluate_row(row, first_seen, headwind) for row in rows]
    evaluations.sort(key=lambda x: x["quality_score"], reverse=True)
    observations = update_observation_state(state, evaluations, ts)
    tickers = (beam_state or {}).get("tickers") or {}
    current_prices = {base: fnum(v.get("price")) for base, v in tickers.items() if isinstance(v, dict)}
    trials = update_forward_calibration(state, current_prices, ts)
    precision_watch = []
    for ev in evaluations:
        p = persistence_for(ev["base"], observations)
        ev["persistence"] = p
        ev["a_plus_ready"] = bool(ev["all_checks_pass"] and p["hits"] >= MIN_HITS_IN_WINDOW and p["consecutive"] >= MIN_CONSECUTIVE)
        if ev["all_checks_pass"] or p["hits"] > 0:
            precision_watch.append(ev)
    ready = [x for x in precision_watch if x.get("a_plus_ready")]
    pick = ready[0] if ready else None
    if pick:
        open_trial = next((x for x in trials if x.get("status") == "OPEN" and x.get("base") == pick["base"]), None)
        if not open_trial:
            trial = {
                "base": pick["base"],
                "designated_at_utc": ts,
                "entry_price": fnum(pick["price_krw"]),
                "status": "OPEN",
                "peak_return_pct": 0.0,
                "trough_return_pct": 0.0,
                "gate_version": VERSION,
            }
            trials.append(trial)
            state.setdefault("designation_history", []).append({
                "ts": ts,
                "base": pick["base"],
                "entry_price": pick["price_krw"],
                "quality_score": pick["quality_score"],
            })
    state["prospective_trials"] = trials[-500:]
    state["designation_history"] = (state.get("designation_history") or [])[-500:]
    state["updated_at_utc"] = ts
    state["version"] = VERSION
    proxy = build_proxy_backtest()
    pstats = prospective_stats(trials)
    save_json(STATE_FILE, state)
    out = {
        "generated_at_utc": ts,
        "version": VERSION,
        "health": scan.get("health"),
        "market_headwind": headwind,
        "goal": "maximize precision for early setups with a +30pct upside path; do not claim 80pct until prospectively calibrated",
        "gate": {
            "first_detection_change_pct_max": MAX_FIRST_CHANGE,
            "current_change_pct_max": MAX_CURRENT_CHANGE,
            "min_trade_24h_krw": MIN_TRADE_KRW,
            "min_turnover_intensity_x": MIN_TURNOVER_X,
            "min_money_leads_price_score": MIN_MONEY_LEADS,
            "min_m15_volume_persistence_x": MIN_M15_PERSISTENCE,
            "min_buy_sell_ratio": MIN_BUY_SELL,
            "min_orderbook_ratio": MIN_ORDERBOOK_RATIO,
            "max_5m_price_change_pct": MAX_5M_PCT,
            "max_week_change_pct": MAX_WEEK_CHANGE,
            "max_stop_distance_pct": MAX_STOP_PCT,
            "min_reward_risk_to_30pct": MIN_REWARD_RISK,
            "persistence": f"at least {MIN_HITS_IN_WINDOW} passes in last {WINDOW} observations and {MIN_CONSECUTIVE} consecutive",
            "hard_stop_pct": STOP_PCT,
            "target_pct": TARGET_PCT,
        },
        "status": "A_PLUS_BUY" if pick else "NO_BUY",
        "a_plus_pick": pick,
        "watchlist": precision_watch[:10],
        "prospective_calibration": pstats,
        "historical_proxy_backtest": proxy,
        "probability_label_policy": {
            "current_label": "UNCALIBRATED" if not pstats["may_claim_80pct_probability"] else "CALIBRATED_80_PLUS",
            "rule": "never translate heuristic confidence/similarity into probability; 80pct language requires >=50 prospective resolved A+ trials and observed hit rate >=80pct",
        },
    }
    save_json(OUT_FILE, out)
    print(json.dumps(out, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
