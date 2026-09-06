import json
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path

VERSION = "v19-expert-lock-predictive"
SOURCE_FILE = "beam_scan_result.json"
EXPERT_SUMMARY = "beam_expert_summary.json"
LOCK_STATE_FILE = "expert_lock_state.json"
PREDICTION_HISTORY_FILE = "expert_prediction_history.json"
MAX_HISTORY = 500

ALLOWED_TRACKS = {"PRE_IGNITION", "ACCELERATOR", "REACCELERATION_SPECIAL"}
TARGET_ARCHETYPE = "ARB/FLOCK/RAY/TEMCO-like early-beam"

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

def clamp(x, lo=0.0, hi=100.0):
    return max(lo, min(hi, x))

def now_iso():
    return datetime.now(timezone.utc).isoformat()

def current_metrics(row):
    fast = row.get("fast_5m") or {}
    m5 = row.get("m5") or {}
    m15 = row.get("m15") or {}
    h1 = row.get("h1") or {}
    h4 = row.get("h4") or {}
    ob = row.get("orderbook") or {}
    flow = row.get("trade_flow") or {}
    return {
        "price": fnum(row.get("price_krw")),
        "change24": fnum(row.get("change_24h_pct")),
        "v18_score": fnum(row.get("beam_score_v18") or row.get("beam_score")),
        "signature": int(row.get("beam_signature_count") or 0),
        "p5": fnum(fast.get("price_change_per_5m_pct")),
        "turnover_intensity": fnum(fast.get("turnover_intensity_vs_even_5m_x")),
        "trade_delta_5m": fnum(fast.get("trade_value_delta_per_5m_krw")),
        "m5_vol_spike": fnum(m5.get("vol_spike_x")),
        "m5_vol_persistence": fnum(m5.get("vol_persistence_x")),
        "m5_low_rising": bool(m5.get("low_rising")),
        "m15_vol_spike": fnum(m15.get("vol_spike_x")),
        "m15_vol_persistence": fnum(m15.get("vol_persistence_x")),
        "m15_low_rising": bool(m15.get("low_rising")),
        "m15_positive": int(m15.get("recent_positive_count") or 0),
        "h1_low_rising": bool(h1.get("low_rising")),
        "h1_above_ma": bool(h1.get("close_above_ma")),
        "h4_low_rising": bool(h4.get("low_rising")),
        "h4_above_ma": bool(h4.get("close_above_ma")),
        "orderbook_ratio": fnum(ob.get("bid_ask_depth_ratio")),
        "buy_sell_ratio": fnum(flow.get("buy_sell_ratio")),
        "track": row.get("hard_track") or row.get("track"),
        "warning": bool(row.get("warning_flag")),
        "v18_status": (row.get("entry_plan") or {}).get("status"),
    }

def pattern_similarity(row, prev_state):
    """Heuristic similarity to the target early-beam archetype; not a historical backtest."""
    m = current_metrics(row)
    score = 0.0
    reasons = []

    if m["turnover_intensity"] >= 8:
        score += 18; reasons.append("turnover>=8x")
    elif m["turnover_intensity"] >= 4:
        score += 14; reasons.append("turnover>=4x")
    elif m["turnover_intensity"] >= 2:
        score += 8

    if 0.02 <= m["p5"] <= 1.4:
        score += 12; reasons.append("controlled_5m_price_response")
    elif 1.4 < m["p5"] <= 2.2:
        score += 6
    elif m["p5"] < -0.35 or m["p5"] > 3:
        score -= 8

    if m["m5_vol_spike"] >= 2:
        score += 8
    if m["m15_vol_spike"] >= 1.5:
        score += 8
    if m["m15_vol_persistence"] >= 1.2:
        score += 8; reasons.append("15m_volume_persists")

    if m["m5_low_rising"]:
        score += 7
    if m["m15_low_rising"]:
        score += 10; reasons.append("15m_low_rising")
    if m["h1_low_rising"]:
        score += 5
    if m["h4_low_rising"]:
        score += 4

    if m["buy_sell_ratio"] >= 1.2:
        score += 10; reasons.append("buyers_dominate")
    elif m["buy_sell_ratio"] >= 0.85:
        score += 6
    elif m["buy_sell_ratio"] < 0.5:
        score -= 14; reasons.append("sell_flow_warning")

    if m["orderbook_ratio"] >= 1.3:
        score += 7
    elif m["orderbook_ratio"] >= 1.0:
        score += 4
    elif 0 < m["orderbook_ratio"] < 0.75:
        score -= 6

    if m["track"] == "PRE_IGNITION" and 0 <= m["change24"] < 6:
        score += 8; reasons.append("early_stage")
    elif m["track"] == "PRE_IGNITION":
        score += 4
    elif m["track"] == "ACCELERATOR":
        score += 1

    if prev_state:
        prev_score = fnum(prev_state.get("last_v18_score"))
        score_delta = m["v18_score"] - prev_score
        if score_delta >= 10:
            score += 8; reasons.append("score_strengthening")
        elif score_delta <= -20:
            score -= 10; reasons.append("score_fading")
        prev_ob = fnum(prev_state.get("last_orderbook_ratio"))
        if prev_ob > 0 and m["orderbook_ratio"] >= prev_ob * 1.08:
            score += 4; reasons.append("bid_pressure_improving")
        seen = int(prev_state.get("seen_streak") or 0)
        if seen >= 2:
            score += 7; reasons.append("multi_scan_persistence")

    return round(clamp(score), 2), reasons

def expert_evaluate(row, prev_state):
    r = deepcopy(row)
    m = current_metrics(r)
    similarity, sim_reasons = pattern_similarity(r, prev_state)

    score = m["v18_score"]
    reasons = list(sim_reasons)
    penalties = []

    if m["buy_sell_ratio"] >= 1.25:
        score += 16
    elif m["buy_sell_ratio"] >= 0.9:
        score += 8
    elif m["buy_sell_ratio"] >= 0.65:
        score -= 6
    elif m["buy_sell_ratio"] >= 0.4:
        score -= 18; penalties.append("weak_buy_flow")
    else:
        score -= 38; penalties.append("distribution_risk")

    if 0.05 <= m["p5"] <= 1.5:
        score += 10
    elif 1.5 < m["p5"] <= 2.5:
        score += 3
    elif m["p5"] < -0.4:
        score -= 18; penalties.append("negative_5m_momentum")
    elif m["p5"] > 3:
        score -= 10; penalties.append("too_vertical")

    if m["orderbook_ratio"] >= 1.25:
        score += 8
    elif 0 < m["orderbook_ratio"] < 0.75:
        score -= 10; penalties.append("ask_depth_dominates")

    if m["m15_low_rising"]:
        score += 10
    if m["m5_low_rising"]:
        score += 6
    if m["h1_low_rising"]:
        score += 4
    if m["m15_vol_persistence"] >= 1.2:
        score += 6
    if m["signature"] >= 5:
        score += 8
    elif m["signature"] >= 4:
        score += 4

    prev_v18 = fnum((prev_state or {}).get("last_v18_score"))
    score_trend = round(m["v18_score"] - prev_v18, 2) if prev_state else None
    if prev_state:
        if score_trend >= 12:
            score += 10
        elif score_trend <= -20:
            score -= 14; penalties.append("score_decay")
        prev_price = fnum(prev_state.get("last_price"))
        if prev_price and m["price"] >= prev_price and m["buy_sell_ratio"] >= 0.85:
            score += 5

    seen_streak = int((prev_state or {}).get("seen_streak") or 0) + 1
    if seen_streak >= 3:
        score += 12
    elif seen_streak >= 2:
        score += 7

    hard_fail = (
        m["warning"]
        or m["track"] not in ALLOWED_TRACKS
        or m["change24"] > 35
        or m["buy_sell_ratio"] < 0.28
        or m["p5"] < -1.0
    )

    qualified = (
        not hard_fail
        and similarity >= 52
        and score >= 115
        and m["buy_sell_ratio"] >= 0.5
        and m["p5"] >= -0.4
        and m["v18_status"] in {"ENTER_CANDIDATE", "WAIT_CONFIRMATION"}
    )

    high_conviction = (
        qualified
        and similarity >= 64
        and score >= 145
        and m["buy_sell_ratio"] >= 0.75
        and (
            seen_streak >= 2
            or (m["m15_low_rising"] and m["turnover_intensity"] >= 4 and m["p5"] >= 0.05)
        )
    )

    if high_conviction:
        expert_status = "PREDICT_BUY"
    elif qualified:
        expert_status = "PREDICT_WAIT"
    else:
        expert_status = "REJECT"

    confidence = clamp(
        similarity * 0.55
        + min(100.0, max(0.0, score - 80.0)) * 0.25
        + min(20.0, seen_streak * 5.0),
        0, 100
    )

    r["expert_score"] = round(score, 2)
    r["pattern_similarity"] = similarity
    r["expert_confidence"] = round(confidence, 2)
    r["expert_status"] = expert_status
    r["expert_reasons"] = reasons[:10]
    r["expert_penalties"] = penalties[:10]
    r["score_trend_vs_prev"] = score_trend
    r["seen_streak"] = seen_streak
    return r

def weak_now(row, prev_state):
    m = current_metrics(row)
    score_drop = 0.0
    if prev_state:
        score_drop = fnum(prev_state.get("last_v18_score")) - m["v18_score"]
    return (
        row.get("expert_status") == "REJECT"
        or m["buy_sell_ratio"] < 0.5
        or m["p5"] < -0.45
        or score_drop > 22
    )

def invalidated(row, lock_item):
    if not row:
        return True
    if row.get("expert_status") == "REJECT":
        return True
    price = fnum(row.get("price_krw"))
    inv = fnum((lock_item or {}).get("invalidation_krw"))
    if inv > 0 and price < inv:
        return True
    return False

def row_rank(row):
    status_rank = {"PREDICT_BUY": 3, "PREDICT_WAIT": 2, "REJECT": 0}.get(row.get("expert_status"), 0)
    track_rank = {"PRE_IGNITION": 3, "ACCELERATOR": 2, "REACCELERATION_SPECIAL": 1}.get(row.get("hard_track") or row.get("track"), 0)
    return (
        status_rank,
        track_rank,
        fnum(row.get("expert_confidence")),
        fnum(row.get("pattern_similarity")),
        fnum(row.get("expert_score")),
    )

def compact(row, lock_meta=None):
    if not row:
        return None
    return {
        "base": row.get("base"),
        "market": row.get("market"),
        "korean_name": row.get("korean_name"),
        "price_krw": row.get("price_krw"),
        "change_24h_pct": row.get("change_24h_pct"),
        "track": row.get("hard_track") or row.get("track"),
        "expert_status": row.get("expert_status"),
        "expert_score": row.get("expert_score"),
        "pattern_similarity": row.get("pattern_similarity"),
        "expert_confidence": row.get("expert_confidence"),
        "score_trend_vs_prev": row.get("score_trend_vs_prev"),
        "seen_streak": row.get("seen_streak"),
        "expert_reasons": row.get("expert_reasons"),
        "expert_penalties": row.get("expert_penalties"),
        "entry_plan": row.get("entry_plan"),
        "fast_5m": row.get("fast_5m"),
        "m5": row.get("m5"),
        "m15": row.get("m15"),
        "h1": row.get("h1"),
        "h4": row.get("h4"),
        "orderbook": row.get("orderbook"),
        "trade_flow": row.get("trade_flow"),
        "lock": lock_meta,
    }

def update_candidate_state(state, evaluated, generated_at):
    old = state.get("candidates") or {}
    new = {}
    current_bases = {r.get("base") for r in evaluated if r.get("base")}
    for r in evaluated:
        base = r.get("base")
        if not base:
            continue
        prev = old.get(base) or {}
        weak_streak = int(prev.get("weak_streak") or 0)
        weak_streak = weak_streak + 1 if weak_now(r, prev) else 0
        snap = {
            "at": generated_at,
            "price": r.get("price_krw"),
            "change24": r.get("change_24h_pct"),
            "v18_score": r.get("beam_score_v18") or r.get("beam_score"),
            "expert_score": r.get("expert_score"),
            "similarity": r.get("pattern_similarity"),
            "confidence": r.get("expert_confidence"),
            "status": r.get("expert_status"),
            "buy_sell_ratio": (r.get("trade_flow") or {}).get("buy_sell_ratio"),
            "orderbook_ratio": (r.get("orderbook") or {}).get("bid_ask_depth_ratio"),
            "p5": (r.get("fast_5m") or {}).get("price_change_per_5m_pct"),
        }
        snaps = list(prev.get("snapshots") or [])[-5:] + [snap]
        new[base] = {
            "seen_streak": int(r.get("seen_streak") or 1),
            "weak_streak": weak_streak,
            "last_seen_at": generated_at,
            "last_price": r.get("price_krw"),
            "last_v18_score": r.get("beam_score_v18") or r.get("beam_score"),
            "last_expert_score": r.get("expert_score"),
            "last_similarity": r.get("pattern_similarity"),
            "last_confidence": r.get("expert_confidence"),
            "last_status": r.get("expert_status"),
            "last_orderbook_ratio": (r.get("orderbook") or {}).get("bid_ask_depth_ratio"),
            "last_buy_sell_ratio": (r.get("trade_flow") or {}).get("buy_sell_ratio"),
            "snapshots": snaps,
        }

    for base, prev in old.items():
        if base in current_bases:
            continue
        if base in (state.get("locked") or []):
            kept = deepcopy(prev)
            kept["seen_streak"] = 0
            kept["weak_streak"] = int(kept.get("weak_streak") or 0) + 1
            new[base] = kept
    state["candidates"] = new

def choose_locked(state, evaluated, generated_at):
    by_base = {r.get("base"): r for r in evaluated if r.get("base")}
    old_locked = list(state.get("locked") or [])[:2]
    lock_meta = state.get("lock_meta") or {}
    kept = []

    for base in old_locked:
        row = by_base.get(base)
        prev = (state.get("candidates") or {}).get(base) or {}
        meta = lock_meta.get(base) or {}
        if row and not invalidated(row, meta) and int(prev.get("weak_streak") or 0) < 2:
            kept.append(base)
        else:
            meta["unlocked_at"] = generated_at
            meta["unlock_reason"] = "invalidation_or_two_weak_observations"
            lock_meta[base] = meta

    ranked = sorted(
        [r for r in evaluated if r.get("expert_status") in {"PREDICT_BUY", "PREDICT_WAIT"}],
        key=row_rank,
        reverse=True,
    )

    for r in ranked:
        base = r.get("base")
        if base in kept:
            continue
        if len(kept) >= 2:
            break
        kept.append(base)
        ep = r.get("entry_plan") or {}
        lock_meta[base] = {
            "locked_at": generated_at,
            "lock_price_krw": r.get("price_krw"),
            "lock_change_24h_pct": r.get("change_24h_pct"),
            "lock_expert_score": r.get("expert_score"),
            "lock_similarity": r.get("pattern_similarity"),
            "invalidation_krw": ep.get("invalidation_krw"),
            "do_not_chase_above_krw": ep.get("do_not_chase_above_krw"),
        }

    state["locked"] = kept[:2]
    state["lock_meta"] = lock_meta
    return state["locked"]

def track_outcomes(state, evaluated):
    by_base = {r.get("base"): r for r in evaluated if r.get("base")}
    meta = state.get("lock_meta") or {}
    for base, item in meta.items():
        row = by_base.get(base)
        if not row:
            continue
        lock_price = fnum(item.get("lock_price_krw"))
        price = fnum(row.get("price_krw"))
        if lock_price <= 0 or price <= 0:
            continue
        ret = (price / lock_price - 1) * 100
        peak = max(fnum(item.get("peak_return_pct"), -999), ret)
        item["current_return_pct"] = round(ret, 2)
        item["peak_return_pct"] = round(peak, 2)
        item["hit_8pct"] = bool(item.get("hit_8pct")) or peak >= 8
        item["hit_15pct"] = bool(item.get("hit_15pct")) or peak >= 15
        item["hit_25pct"] = bool(item.get("hit_25pct")) or peak >= 25
    state["lock_meta"] = meta

def append_history(summary):
    hist = load_json(PREDICTION_HISTORY_FILE, [])
    if not isinstance(hist, list):
        hist = []
    hist.append({
        "generated_at_utc": summary.get("generated_at_utc"),
        "version": VERSION,
        "locked_predictions": [
            {
                "base": r.get("base"),
                "price_krw": r.get("price_krw"),
                "change_24h_pct": r.get("change_24h_pct"),
                "expert_status": r.get("expert_status"),
                "expert_confidence": r.get("expert_confidence"),
                "pattern_similarity": r.get("pattern_similarity"),
            }
            for r in (summary.get("locked_predictions") or [])
        ],
    })
    save_json(PREDICTION_HISTORY_FILE, hist[-MAX_HISTORY:])

def main():
    source = load_json(SOURCE_FILE, {})
    generated_at = source.get("generated_at_utc") or now_iso()
    rows = source.get("stage2_ranked") or []
    state = load_json(LOCK_STATE_FILE, {"locked": [], "lock_meta": {}, "candidates": {}})
    prev_candidates = state.get("candidates") or {}

    evaluated = []
    for row in rows:
        base = row.get("base")
        evaluated.append(expert_evaluate(row, prev_candidates.get(base) or {}))

    evaluated.sort(key=row_rank, reverse=True)
    update_candidate_state(state, evaluated, generated_at)
    locked = choose_locked(state, evaluated, generated_at)
    track_outcomes(state, evaluated)
    state["updated_at_utc"] = generated_at
    state["version"] = VERSION
    save_json(LOCK_STATE_FILE, state)

    by_base = {r.get("base"): r for r in evaluated if r.get("base")}
    locked_rows = [by_base.get(base) for base in locked if by_base.get(base)]
    rejected_top = [r for r in evaluated if r.get("expert_status") == "REJECT"][:5]

    summary = {
        "generated_at_utc": generated_at,
        "source_version": source.get("source_version"),
        "expert_version": VERSION,
        "health": source.get("health"),
        "target_archetype": TARGET_ARCHETYPE,
        "method_note": "Pattern similarity is a rule-based expert heuristic, not a historical backtest probability.",
        "lock_policy": {
            "purpose": "prevent recommendation churn",
            "unlock_only_if": "price invalidation OR two consecutive weak expert observations",
            "replacement": "only after a lock slot is released",
        },
        "locked_predictions": [
            compact(r, (state.get("lock_meta") or {}).get(r.get("base"))) for r in locked_rows
        ],
        "expert_top10": [compact(r, (state.get("lock_meta") or {}).get(r.get("base"))) for r in evaluated[:10]],
        "expert_rejected_top5": [compact(r) for r in rejected_top],
    }
    save_json(EXPERT_SUMMARY, summary)
    append_history(summary)
    print(json.dumps(summary, ensure_ascii=False, indent=2))

if __name__ == "__main__":
    main()
