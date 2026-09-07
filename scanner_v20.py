import json
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path

VERSION = "v20-one-pick-30-hysteresis"
SOURCE_SCAN = "beam_scan_result.json"
SOURCE_EXPERT = "beam_expert_summary.json"
SOURCE_TICKERS = "beam_state.json"
SUMMARY_FILE = "beam_one_pick_summary.json"
STATE_FILE = "one_pick_state.json"
HISTORY_FILE = "one_pick_history.json"
MAX_HISTORY = 500

MIN_SEEN_STREAK = 4
MIN_CONFIDENCE = 85.0
MIN_SIMILARITY = 75.0
MIN_EXPERT_SCORE = 170.0
MIN_BUY_SELL = 0.90
MIN_MONEY_LEADS = 20.0
MAX_PRE_CHANGE = 7.5
MAX_WEEK_CHANGE = 20.0
MAX_P5 = 1.5
MIN_P5 = -0.2
SOFT_FAILURES_TO_RELEASE = 3
CHALLENGER_CONFIRMATIONS = 2
REPLACEMENT_MARGIN = 25.0


def load_json(path, default):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except Exception:
        return deepcopy(default)


def save_json(path, obj):
    Path(path).write_text(json.dumps(obj, ensure_ascii=False, indent=2), encoding="utf-8")


def fnum(value, default=0.0):
    try:
        return float(value)
    except Exception:
        return default


def now_iso():
    return datetime.now(timezone.utc).isoformat()


def pct_target(price, pct):
    price = fnum(price)
    return round(price * (1.0 + pct / 100.0), 12) if price > 0 else None


def raw_map(scan):
    rows = scan.get("stage2_ranked") or []
    return {r.get("base"): r for r in rows if r.get("base")}


def expert_rows(expert):
    seen = set()
    out = []
    for bucket in ("locked_predictions", "expert_top10"):
        for row in (expert.get(bucket) or []):
            base = row.get("base")
            if not base or base in seen:
                continue
            seen.add(base)
            out.append(deepcopy(row))
    return out


def ticker_price(tickers, base):
    item = ((tickers.get("tickers") or {}).get(base) or {})
    return fnum(item.get("price")), fnum(item.get("change24_pct"))


def candidate_metrics(row, raw=None):
    raw = raw or {}
    ep = row.get("entry_plan") or {}
    fast = row.get("fast_5m") or {}
    m15 = row.get("m15") or {}
    h1 = row.get("h1") or {}
    flow = row.get("trade_flow") or {}
    ob = row.get("orderbook") or {}
    d1 = raw.get("d1") or {}
    return {
        "base": row.get("base"),
        "price": fnum(row.get("price_krw")),
        "change24": fnum(row.get("change_24h_pct")),
        "track": row.get("track"),
        "status": row.get("expert_status"),
        "expert_score": fnum(row.get("expert_score")),
        "similarity": fnum(row.get("pattern_similarity")),
        "confidence": fnum(row.get("expert_confidence")),
        "seen_streak": int(row.get("seen_streak") or 0),
        "buy_sell": fnum(flow.get("buy_sell_ratio")),
        "orderbook_ratio": fnum(ob.get("bid_ask_depth_ratio")),
        "p5": fnum(fast.get("price_change_per_5m_pct")),
        "turnover": fnum(fast.get("turnover_intensity_vs_even_5m_x")),
        "m15_low_rising": bool(m15.get("low_rising")),
        "m15_vol_persistence": fnum(m15.get("vol_persistence_x")),
        "h1_low_rising": bool(h1.get("low_rising")),
        "money_leads": fnum(raw.get("money_leads_price_score")),
        "week_change": fnum(d1.get("week_change_pct")),
        "warning": bool(raw.get("warning_flag")),
        "hard_invalidation": fnum(ep.get("invalidation_krw")),
        "do_not_chase": fnum(ep.get("do_not_chase_above_krw")),
    }


def quality_score(m):
    # Ranking score only; it is not a probability or expected return.
    return round(
        m["expert_score"]
        + m["confidence"] * 1.20
        + m["similarity"] * 0.90
        + min(50.0, m["money_leads"] * 1.5)
        + min(30.0, m["turnover"] * 3.0)
        + min(20.0, max(0.0, m["buy_sell"] - 0.8) * 20.0),
        2,
    )


def thirty_upside_eligible(m):
    if m["price"] <= 0:
        return False
    stop_pct = 999.0
    if m["hard_invalidation"] > 0 and m["hard_invalidation"] < m["price"]:
        stop_pct = (1.0 - m["hard_invalidation"] / m["price"]) * 100.0
    reward_risk_to_30 = 30.0 / stop_pct if stop_pct > 0 else 0.0
    return (
        m["track"] == "PRE_IGNITION"
        and 0 <= m["change24"] <= MAX_PRE_CHANGE
        and m["week_change"] <= MAX_WEEK_CHANGE
        and (m["do_not_chase"] <= 0 or m["price"] <= m["do_not_chase"])
        and reward_risk_to_30 >= 4.0
    )


def heavy_candidate(row, raw=None):
    m = candidate_metrics(row, raw)
    reasons = []
    failures = []
    checks = [
        (m["status"] == "PREDICT_BUY", "predict_buy", "not_predict_buy"),
        (m["track"] == "PRE_IGNITION", "pre_ignition", "not_pre_ignition"),
        (0 <= m["change24"] <= MAX_PRE_CHANGE, "still_early", "already_extended"),
        (m["confidence"] >= MIN_CONFIDENCE, "confidence>=85", "confidence_low"),
        (m["similarity"] >= MIN_SIMILARITY, "similarity>=75", "similarity_low"),
        (m["expert_score"] >= MIN_EXPERT_SCORE, "expert_score>=170", "expert_score_low"),
        (m["seen_streak"] >= MIN_SEEN_STREAK, "multi_scan>=4", "persistence_low"),
        (m["buy_sell"] >= MIN_BUY_SELL, "buy_flow_ok", "buy_flow_weak"),
        (m["money_leads"] >= MIN_MONEY_LEADS, "money_leads_price", "money_leads_low"),
        (m["m15_low_rising"], "15m_low_rising", "15m_structure_weak"),
        (m["m15_vol_persistence"] >= 1.2, "15m_volume_persists", "15m_volume_weak"),
        (m["h1_low_rising"], "1h_low_rising", "1h_structure_weak"),
        (MIN_P5 <= m["p5"] <= MAX_P5, "5m_not_vertical", "5m_bad_response"),
        (not m["warning"], "no_market_warning", "market_warning"),
        (thirty_upside_eligible(m), "30pct_scenario_eligible", "30pct_gate_fail"),
    ]
    for ok, yes, no in checks:
        (reasons if ok else failures).append(yes if ok else no)
    return {
        "eligible": not failures,
        "quality_score": quality_score(m),
        "metrics": m,
        "reasons": reasons,
        "failures": failures,
    }


def structural_weakness(current, expert_row, raw_row, state_pick):
    flags = []
    price = fnum(current.get("price"))
    hard_inv = fnum(state_pick.get("hard_invalidation_krw"))
    hard_price_breach = bool(price > 0 and hard_inv > 0 and price < hard_inv)

    if raw_row is None:
        flags.append("missing_from_stage2")
    else:
        fast = raw_row.get("fast_5m") or {}
        m15 = raw_row.get("m15") or {}
        h1 = raw_row.get("h1") or {}
        flow = raw_row.get("trade_flow") or {}
        if fnum(flow.get("buy_sell_ratio")) < 0.55:
            flags.append("buy_flow_below_0.55")
        if not bool(m15.get("low_rising")):
            flags.append("15m_low_not_rising")
        if not bool(h1.get("low_rising")):
            flags.append("1h_low_not_rising")
        if fnum(fast.get("price_change_per_5m_pct")) < -0.6:
            flags.append("5m_negative")
        if bool(raw_row.get("warning_flag")):
            flags.append("market_warning")

    if expert_row and expert_row.get("expert_status") == "REJECT":
        flags.append("expert_reject_observation")

    # A single noisy observation never revokes the one-pick.
    structural_fail = ("missing_from_stage2" in flags or len(flags) >= 3)
    return hard_price_breach, structural_fail, flags


def compact_pick(row, raw, state_pick=None):
    if not row:
        return None
    ev = heavy_candidate(row, raw)
    m = ev["metrics"]
    state_pick = state_pick or {}
    price = m["price"]
    designation_price = fnum(state_pick.get("designation_price_krw"), price)
    ret = ((price / designation_price) - 1.0) * 100.0 if price > 0 and designation_price > 0 else None
    return {
        "base": row.get("base"),
        "market": row.get("market"),
        "korean_name": row.get("korean_name"),
        "price_krw": row.get("price_krw"),
        "change_24h_pct": row.get("change_24h_pct"),
        "track": row.get("track"),
        "expert_status": row.get("expert_status"),
        "expert_score": row.get("expert_score"),
        "pattern_similarity": row.get("pattern_similarity"),
        "expert_confidence": row.get("expert_confidence"),
        "seen_streak": row.get("seen_streak"),
        "quality_score": ev["quality_score"],
        "heavy_eligible_now": ev["eligible"],
        "heavy_reasons": ev["reasons"],
        "heavy_failures": ev["failures"],
        "money_leads_price_score": m["money_leads"],
        "buy_sell_ratio": m["buy_sell"],
        "orderbook_ratio": m["orderbook_ratio"],
        "turnover_intensity": m["turnover"],
        "target_10_krw": pct_target(designation_price, 10),
        "target_15_krw": pct_target(designation_price, 15),
        "target_30_krw": pct_target(designation_price, 30),
        "target_40_krw": pct_target(designation_price, 40),
        "hard_invalidation_krw": state_pick.get("hard_invalidation_krw") or m["hard_invalidation"],
        "do_not_chase_above_krw": state_pick.get("do_not_chase_above_krw") or m["do_not_chase"],
        "return_since_designation_pct": round(ret, 2) if ret is not None else None,
        "entry_plan": row.get("entry_plan"),
        "fast_5m": row.get("fast_5m"),
        "m15": row.get("m15"),
        "h1": row.get("h1"),
        "h4": row.get("h4"),
        "orderbook": row.get("orderbook"),
        "trade_flow": row.get("trade_flow"),
    }


def update_challenger(state, eligible_rows, raw_by_base, exclude_base=None):
    ranked = []
    for row in eligible_rows:
        base = row.get("base")
        if exclude_base and base == exclude_base:
            continue
        ev = heavy_candidate(row, raw_by_base.get(base))
        if ev["eligible"]:
            ranked.append((ev["quality_score"], row))
    ranked.sort(key=lambda x: x[0], reverse=True)

    if not ranked:
        state["challenger"] = None
        return None

    score, row = ranked[0]
    base = row.get("base")
    old = state.get("challenger") or {}
    confirmations = int(old.get("confirmations") or 0) + 1 if old.get("base") == base else 1
    state["challenger"] = {
        "base": base,
        "confirmations": confirmations,
        "quality_score": score,
        "last_seen_at_utc": state.get("updated_at_utc"),
    }
    return row


def designate(state, row, raw, generated_at, reason):
    ev = heavy_candidate(row, raw)
    m = ev["metrics"]
    state["one_pick"] = {
        "base": row.get("base"),
        "market": row.get("market"),
        "korean_name": row.get("korean_name"),
        "designation_at_utc": generated_at,
        "designation_price_krw": m["price"],
        "designation_change_24h_pct": m["change24"],
        "designation_quality_score": ev["quality_score"],
        "designation_confidence": m["confidence"],
        "designation_similarity": m["similarity"],
        "designation_expert_score": m["expert_score"],
        "hard_invalidation_krw": m["hard_invalidation"],
        "do_not_chase_above_krw": m["do_not_chase"],
        "target_10_krw": pct_target(m["price"], 10),
        "target_15_krw": pct_target(m["price"], 15),
        "target_30_krw": pct_target(m["price"], 30),
        "target_40_krw": pct_target(m["price"], 40),
        "soft_failure_streak": 0,
        "peak_return_pct": 0.0,
        "hit_10pct": False,
        "hit_15pct": False,
        "hit_30pct": False,
        "hit_40pct": False,
        "designation_reason": reason,
    }


def track_pick_outcome(state, current_price):
    pick = state.get("one_pick")
    if not pick:
        return
    entry = fnum(pick.get("designation_price_krw"))
    if entry <= 0 or current_price <= 0:
        return
    ret = (current_price / entry - 1.0) * 100.0
    peak = max(fnum(pick.get("peak_return_pct"), -999), ret)
    pick["current_return_pct"] = round(ret, 2)
    pick["peak_return_pct"] = round(peak, 2)
    pick["hit_10pct"] = bool(pick.get("hit_10pct")) or peak >= 10
    pick["hit_15pct"] = bool(pick.get("hit_15pct")) or peak >= 15
    pick["hit_30pct"] = bool(pick.get("hit_30pct")) or peak >= 30
    pick["hit_40pct"] = bool(pick.get("hit_40pct")) or peak >= 40


def append_history(summary):
    hist = load_json(HISTORY_FILE, [])
    if not isinstance(hist, list):
        hist = []
    pick = summary.get("one_pick") or {}
    hist.append({
        "generated_at_utc": summary.get("generated_at_utc"),
        "version": VERSION,
        "status": summary.get("status"),
        "base": pick.get("base"),
        "price_krw": pick.get("price_krw"),
        "return_since_designation_pct": pick.get("return_since_designation_pct"),
        "soft_failure_streak": summary.get("soft_failure_streak"),
        "challenger": summary.get("challenger"),
    })
    save_json(HISTORY_FILE, hist[-MAX_HISTORY:])


def main():
    scan = load_json(SOURCE_SCAN, {})
    expert = load_json(SOURCE_EXPERT, {})
    tickers = load_json(SOURCE_TICKERS, {})
    generated_at = scan.get("generated_at_utc") or expert.get("generated_at_utc") or now_iso()
    state = load_json(STATE_FILE, {"one_pick": None, "challenger": None, "released": []})
    state["updated_at_utc"] = generated_at
    state["version"] = VERSION

    raw_by_base = raw_map(scan)
    rows = expert_rows(expert)
    expert_by_base = {r.get("base"): r for r in rows if r.get("base")}

    current_pick = state.get("one_pick")
    current_base = (current_pick or {}).get("base")
    challenger_row = update_challenger(state, rows, raw_by_base, exclude_base=current_base)
    released_this_run = None

    if current_pick:
        base = current_pick.get("base")
        expert_row = expert_by_base.get(base)
        raw_row = raw_by_base.get(base)
        price, change24 = ticker_price(tickers, base)
        if price <= 0 and raw_row:
            price = fnum(raw_row.get("price_krw"))
            change24 = fnum(raw_row.get("change_24h_pct"))

        hard_breach, structural_fail, flags = structural_weakness(
            {"price": price, "change24": change24}, expert_row, raw_row, current_pick
        )
        soft = int(current_pick.get("soft_failure_streak") or 0)
        soft = soft + 1 if structural_fail else 0
        current_pick["soft_failure_streak"] = soft
        current_pick["last_structural_flags"] = flags
        current_pick["last_price_krw"] = price
        current_pick["last_change_24h_pct"] = change24
        track_pick_outcome(state, price)

        if hard_breach or soft >= SOFT_FAILURES_TO_RELEASE:
            released_this_run = deepcopy(current_pick)
            released_this_run["released_at_utc"] = generated_at
            released_this_run["release_reason"] = (
                "hard_price_invalidation" if hard_breach else "three_consecutive_structural_failures"
            )
            state.setdefault("released", []).append(released_this_run)
            state["released"] = state["released"][-50:]
            state["one_pick"] = None
            current_pick = None

    challenger = state.get("challenger") or {}
    challenger_ready = (
        challenger_row is not None
        and int(challenger.get("confirmations") or 0) >= CHALLENGER_CONFIRMATIONS
    )

    if state.get("one_pick") is None and challenger_ready:
        ev = heavy_candidate(challenger_row, raw_by_base.get(challenger_row.get("base")))
        released_score = fnum((released_this_run or {}).get("designation_quality_score"))
        margin_ok = (
            released_this_run is None
            or ev["quality_score"] >= released_score + REPLACEMENT_MARGIN
            or (released_this_run or {}).get("release_reason") == "hard_price_invalidation"
        )
        if margin_ok:
            designate(
                state,
                challenger_row,
                raw_by_base.get(challenger_row.get("base")),
                generated_at,
                "two_scan_heavy_confirmation_after_release" if released_this_run else "two_scan_heavy_confirmation",
            )

    current_pick = state.get("one_pick")
    pick_row = expert_by_base.get((current_pick or {}).get("base"))
    pick_raw = raw_by_base.get((current_pick or {}).get("base"))

    if current_pick and pick_row:
        one_pick_view = compact_pick(pick_row, pick_raw, current_pick)
    elif current_pick:
        price, change24 = ticker_price(tickers, current_pick.get("base"))
        one_pick_view = {
            "base": current_pick.get("base"),
            "market": current_pick.get("market"),
            "korean_name": current_pick.get("korean_name"),
            "price_krw": price,
            "change_24h_pct": change24,
            "designation_price_krw": current_pick.get("designation_price_krw"),
            "hard_invalidation_krw": current_pick.get("hard_invalidation_krw"),
            "target_10_krw": current_pick.get("target_10_krw"),
            "target_15_krw": current_pick.get("target_15_krw"),
            "target_30_krw": current_pick.get("target_30_krw"),
            "target_40_krw": current_pick.get("target_40_krw"),
            "return_since_designation_pct": current_pick.get("current_return_pct"),
            "heavy_eligible_now": False,
            "heavy_failures": ["not_in_current_expert_top10_but_lock_persists"],
        }
    else:
        one_pick_view = None

    provisional = None
    if challenger_row:
        provisional = compact_pick(challenger_row, raw_by_base.get(challenger_row.get("base")), None)
        provisional["confirmations"] = int((state.get("challenger") or {}).get("confirmations") or 0)

    status = "DESIGNATED" if current_pick else ("PROVISIONAL" if provisional else "NO_PICK")
    summary = {
        "generated_at_utc": generated_at,
        "source_version": scan.get("source_version"),
        "expert_version": expert.get("expert_version"),
        "one_pick_version": VERSION,
        "health": scan.get("health"),
        "status": status,
        "policy": {
            "goal": "ARX-like early one-pick with 30pct upside scenario, not a return guarantee",
            "designation_requires": {
                "expert_status": "PREDICT_BUY",
                "track": "PRE_IGNITION",
                "seen_streak_min": MIN_SEEN_STREAK,
                "confidence_min": MIN_CONFIDENCE,
                "similarity_min": MIN_SIMILARITY,
                "expert_score_min": MIN_EXPERT_SCORE,
                "buy_sell_min": MIN_BUY_SELL,
                "money_leads_price_min": MIN_MONEY_LEADS,
                "m15_low_rising": True,
                "m15_volume_persistence_min": 1.2,
                "h1_low_rising": True,
                "change24_max": MAX_PRE_CHANGE,
                "week_change_max": MAX_WEEK_CHANGE,
                "two_scan_confirmation": True,
            },
            "hold_hysteresis": {
                "instant_release": "hard price invalidation only",
                "soft_release": f"{SOFT_FAILURES_TO_RELEASE} consecutive structural failures",
                "single_REJECT_does_not_release": True,
                "replacement_margin": REPLACEMENT_MARGIN,
            },
            "profit_protection": {
                "track_hit_10pct": True,
                "track_hit_15pct": True,
                "track_hit_30pct": True,
                "track_hit_40pct": True,
                "note": "10-15pct is a profit-protection checkpoint; 30pct is an upside scenario target, not guaranteed.",
            },
        },
        "one_pick": one_pick_view,
        "soft_failure_streak": int((current_pick or {}).get("soft_failure_streak") or 0),
        "challenger": provisional,
        "released_this_run": released_this_run,
    }

    save_json(STATE_FILE, state)
    save_json(SUMMARY_FILE, summary)
    append_history(summary)
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
