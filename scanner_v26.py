import json
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path

VERSION = "v26-a-plus-validation-coverage"
EXPERT_HISTORY_FILE = "expert_prediction_history.json"
SNAPSHOT_HISTORY_FILE = "policy_snapshot_history.json"
ONE_PICK_HISTORY_FILE = "one_pick_history.json"
FIRST_SEEN_FILE = "beam_first_seen.json"
V25_FILE = "beam_a_plus_precision.json"
OUT_FILE = "beam_a_plus_validation.json"

STOP_PCT = 5.0
TARGET_PCT = 30.0


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
        return datetime.fromisoformat(str(v).replace("Z", "+00:00"))
    except Exception:
        return None


def iter_expert_rows(expert_hist):
    for item in expert_hist if isinstance(expert_hist, list) else []:
        ts = parse_ts(item.get("generated_at_utc"))
        if not ts:
            continue
        for p in item.get("locked_predictions") or []:
            base = p.get("base")
            px = fnum(p.get("price_krw"))
            if base and px > 0:
                yield ts, base, px, p


def extract_one_pick_observations(one_pick_hist):
    out = []
    for item in one_pick_hist if isinstance(one_pick_hist, list) else []:
        ts = parse_ts(item.get("generated_at_utc") or item.get("ts") or item.get("updated_at_utc"))
        if not ts:
            continue
        candidates = []
        if isinstance(item.get("one_pick"), dict):
            candidates.append(item.get("one_pick"))
        if isinstance(item.get("pick"), dict):
            candidates.append(item.get("pick"))
        for p in candidates:
            base = p.get("base")
            px = fnum(p.get("price_krw") or p.get("current_price_krw") or p.get("designation_price_krw"))
            if base and px > 0:
                out.append((ts, base, px, "one_pick_history"))
    return out


def build_observation_index(expert_hist, snapshot_hist, one_pick_hist):
    by_base = {}
    source_counts = {"expert_history": 0, "policy_snapshot_history": 0, "one_pick_history": 0}

    def add(ts, base, px, source):
        if not ts or not base or px <= 0:
            return
        by_base.setdefault(base, []).append((ts, px, source))
        source_counts[source] = source_counts.get(source, 0) + 1

    for ts, base, px, _ in iter_expert_rows(expert_hist):
        add(ts, base, px, "expert_history")

    for item in snapshot_hist if isinstance(snapshot_hist, list) else []:
        ts = parse_ts(item.get("generated_at_utc"))
        snap = item.get("snapshot") or {}
        if not ts or not isinstance(snap, dict):
            continue
        for base, row in snap.items():
            if not isinstance(row, dict):
                continue
            px = fnum(row.get("bithumb_krw_price") or row.get("price_krw") or row.get("price"))
            add(ts, base, px, "policy_snapshot_history")

    for ts, base, px, source in extract_one_pick_observations(one_pick_hist):
        add(ts, base, px, source)

    for base in by_base:
        # collapse exact duplicate timestamp/price pairs while keeping chronological order
        seen = set()
        clean = []
        for ts, px, source in sorted(by_base[base], key=lambda x: x[0]):
            key = (ts.isoformat(), px)
            if key in seen:
                continue
            seen.add(key)
            clean.append((ts, px, source))
        by_base[base] = clean
    return by_base, source_counts


def build_signals(expert_hist):
    signals = {}
    for ts, base, px, p in iter_expert_rows(expert_hist):
        if base in signals:
            continue
        if p.get("expert_status") != "PREDICT_BUY":
            continue
        if fnum(p.get("expert_confidence")) < 85:
            continue
        if fnum(p.get("pattern_similarity")) < 75:
            continue
        ch = fnum(p.get("change_24h_pct"), 999)
        if not (0 <= ch <= 6):
            continue
        signals[base] = {
            "base": base,
            "signal_at_utc": ts.isoformat(),
            "entry_price": px,
            "change_24h_pct": ch,
            "expert_confidence": fnum(p.get("expert_confidence")),
            "pattern_similarity": fnum(p.get("pattern_similarity")),
        }
    return signals


def resolve_signal(sig, observations):
    entry = fnum(sig.get("entry_price"))
    signal_ts = parse_ts(sig.get("signal_at_utc"))
    future = [(ts, px, src) for ts, px, src in observations if ts > signal_ts]
    if not future:
        return {
            **sig,
            "outcome": "INSUFFICIENT_PATH",
            "future_observation_count": 0,
            "first_future_at_utc": None,
            "last_future_at_utc": None,
            "max_seen_return_pct": None,
            "min_seen_return_pct": None,
            "resolution_source": None,
        }

    max_ret = -999.0
    min_ret = 999.0
    outcome = "UNRESOLVED"
    resolution_source = None
    resolution_ts = None
    for ts, px, src in future:
        ret = (px / entry - 1.0) * 100.0
        max_ret = max(max_ret, ret)
        min_ret = min(min_ret, ret)
        if ret >= TARGET_PCT:
            outcome = "WIN_30"
            resolution_source = src
            resolution_ts = ts.isoformat()
            break
        if ret <= -STOP_PCT:
            outcome = "LOSS_STOP"
            resolution_source = src
            resolution_ts = ts.isoformat()
            break

    return {
        **sig,
        "outcome": outcome,
        "future_observation_count": len(future),
        "first_future_at_utc": future[0][0].isoformat(),
        "last_future_at_utc": future[-1][0].isoformat(),
        "max_seen_return_pct": round(max_ret, 2),
        "min_seen_return_pct": round(min_ret, 2),
        "resolved_at_utc": resolution_ts,
        "resolution_source": resolution_source,
    }


def snapshot_coverage(snapshot_hist):
    timestamps = []
    for item in snapshot_hist if isinstance(snapshot_hist, list) else []:
        ts = parse_ts(item.get("generated_at_utc"))
        if ts:
            timestamps.append(ts)
    timestamps.sort()
    return {
        "snapshot_count": len(timestamps),
        "first_snapshot_at_utc": timestamps[0].isoformat() if timestamps else None,
        "last_snapshot_at_utc": timestamps[-1].isoformat() if timestamps else None,
    }


def eventual_30_sanity(first_seen):
    rows = []
    for base, fs in (first_seen or {}).items():
        first_ch = fnum((fs or {}).get("first_change_24h_pct"), 999)
        first_px = fnum((fs or {}).get("first_price_krw"))
        peak_px = fnum((fs or {}).get("peak_price_krw"))
        if first_px <= 0 or peak_px <= 0 or not (0 <= first_ch <= 3):
            continue
        peak_ret = (peak_px / first_px - 1.0) * 100.0
        if peak_ret >= TARGET_PCT:
            rows.append({
                "base": base,
                "first_price_krw": first_px,
                "first_change_24h_pct": first_ch,
                "peak_price_krw": peak_px,
                "peak_return_since_first_pct": round(peak_ret, 2),
                "note": "shows eventual +30 reach only; cannot establish +30-before--5 ordering",
            })
    rows.sort(key=lambda x: x["peak_return_since_first_pct"], reverse=True)
    return rows


def main():
    expert_hist = load_json(EXPERT_HISTORY_FILE, [])
    snapshot_hist = load_json(SNAPSHOT_HISTORY_FILE, [])
    one_pick_hist = load_json(ONE_PICK_HISTORY_FILE, [])
    first_seen = load_json(FIRST_SEEN_FILE, {})
    v25 = load_json(V25_FILE, {})

    observations, source_counts = build_observation_index(expert_hist, snapshot_hist, one_pick_hist)
    signals = build_signals(expert_hist)
    outcomes = [resolve_signal(sig, observations.get(base, [])) for base, sig in signals.items()]

    resolved = [x for x in outcomes if x["outcome"] in {"WIN_30", "LOSS_STOP"}]
    wins = sum(x["outcome"] == "WIN_30" for x in resolved)
    losses = len(resolved) - wins
    hit_rate = wins / len(resolved) if resolved else None
    insufficient = sum(x["outcome"] == "INSUFFICIENT_PATH" for x in outcomes)
    open_unresolved = sum(x["outcome"] == "UNRESOLVED" for x in outcomes)
    covered = len(outcomes) - insufficient
    coverage_ratio = covered / len(outcomes) if outcomes else 0.0

    cov = snapshot_coverage(snapshot_hist)
    sanity = eventual_30_sanity(first_seen)
    prospective = v25.get("prospective_calibration") or {}

    out = {
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "version": VERSION,
        "purpose": "honest validation coverage for the A+ +30 gate; do not fabricate historical hit rate when price paths were not stored",
        "v25_status": v25.get("status"),
        "v25_pick": (v25.get("a_plus_pick") or {}).get("base"),
        "historical_proxy": {
            "signal_rule": "first historical v19 PREDICT_BUY per asset with confidence>=85, similarity>=75, 0<=24h_change<=6",
            "outcome_rule": "+30pct observed before -5pct observed in persisted chronological prices",
            "signals": len(outcomes),
            "signals_with_any_future_path": covered,
            "coverage_ratio": round(coverage_ratio, 4),
            "resolved": len(resolved),
            "wins_30_before_stop": wins,
            "losses_stop_before_30": losses,
            "hit_rate": round(hit_rate, 4) if hit_rate is not None else None,
            "insufficient_path": insufficient,
            "unresolved_with_path": open_unresolved,
            "source_observation_counts": source_counts,
            "policy_snapshot_coverage": cov,
            "probability_usable": bool(len(resolved) >= 50 and coverage_ratio >= 0.8),
            "outcomes": outcomes[-100:],
        },
        "eventual_30_sanity_check": {
            "count": len(sanity),
            "assets": sanity[:50],
            "warning": "This is recall sanity only. Peak return lacks chronological trough ordering, so it is excluded from hit-rate/probability calculations.",
        },
        "prospective_a_plus_calibration": prospective,
        "validation_conclusion": {
            "may_claim_80pct_now": bool((prospective or {}).get("may_claim_80pct_probability")),
            "reason": "Only prospective A+ trials with +30-before--5 ordering can certify the 80pct claim. Historical proxy is reported only when persisted paths exist.",
        },
    }
    save_json(OUT_FILE, out)
    print(json.dumps(out, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
