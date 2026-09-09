import json
from datetime import datetime, timezone, timedelta
from pathlib import Path

import scanner_v28 as v28

VERSION = "v29-latest-closed-anchor-coverage-gate"
OUT_FILE = v28.OUT_FILE
MIN_LIQUID_TRADE_24H_KRW = 200_000_000
MIN_LIQUID_COVERAGE_PCT = 70.0
MAX_ANCHOR_LAG_MIN = 10.0


def parse_utc(v):
    try:
        dt = datetime.fromisoformat(str(v).replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc).replace(second=0, microsecond=0)
    except Exception:
        return None


def fnum(v, default=0.0):
    try:
        return float(v)
    except Exception:
        return default


def pull_six_from_latest_closed(market, now):
    """Return six exact consecutive 5m buckets anchored to the latest ACTUAL closed candle.

    The old v28 wall-clock anchor could point at a just-closed bucket that Bithumb had
    not published yet, making the newest slot missing for every market. This function
    first asks Bithumb for closed 5m candles, finds the newest candle actually returned,
    then walks backward in exact 5m steps. Sparse/no-trade buckets stay explicitly
    missing; we never substitute an older unrelated candle.
    """
    rows = v28.v16.candles(market, 5, 72)
    by_start = {}
    for row in rows:
        ts = parse_utc(row.get("candle_date_time_utc"))
        if ts:
            by_start[ts] = row

    if not by_start:
        return []

    anchor = max(by_start)
    anchor_end = anchor + timedelta(minutes=5)
    lag_min = max(0.0, (now.astimezone(timezone.utc) - anchor_end).total_seconds() / 60.0)

    # Oldest -> newest, labelled exactly as the user's 30/25/20/15/10/5-minute slots.
    targets = []
    for idx, minutes_ago in enumerate((30, 25, 20, 15, 10, 5)):
        target = anchor - timedelta(minutes=25 - idx * 5)
        targets.append((minutes_ago, target))

    out = []
    for minutes_ago, target in targets:
        row = by_start.get(target)
        common = {
            "minutes_ago": minutes_ago,
            "expected_candle_time_utc": target.isoformat(),
            "anchor_candle_start_utc": anchor.isoformat(),
            "anchor_lag_minutes": round(lag_min, 2),
        }
        if row is None:
            out.append({
                **common,
                "candle_time_utc": None,
                "missing": True,
                "open": None,
                "high": None,
                "low": None,
                "close": None,
                "trade_krw": 0.0,
            })
        else:
            out.append({
                **common,
                "candle_time_utc": row.get("candle_date_time_utc"),
                "missing": False,
                "open": fnum(row.get("opening_price")),
                "high": fnum(row.get("high_price")),
                "low": fnum(row.get("low_price")),
                "close": fnum(row.get("trade_price")),
                "trade_krw": fnum(row.get("candle_acc_trade_price")),
            })
    return out


def postprocess():
    p = Path(OUT_FILE)
    out = json.loads(p.read_text(encoding="utf-8"))
    candidates = out.get("candidates") or []
    upstream_health = out.get("health") or {}

    liquid = 0
    anchored_complete = 0
    stale_anchor_count = 0
    for c in candidates:
        trade24 = fnum(c.get("trade_24h_krw"))
        if trade24 < MIN_LIQUID_TRADE_24H_KRW:
            continue
        liquid += 1
        points = c.get("five_minute_series") or []
        path = c.get("thirty_minute_path") or {}
        lag = None
        if points:
            lag = fnum(points[-1].get("anchor_lag_minutes"), 999.0)
        recent_anchor = lag is not None and lag <= MAX_ANCHOR_LAG_MIN
        if not recent_anchor:
            stale_anchor_count += 1
            grade = c.get("professional_technical_grade") or {}
            weak = list(grade.get("failed_or_weak_axes") or [])
            if "latest_closed_5m_anchor_stale" not in weak:
                weak.append("latest_closed_5m_anchor_stale")
            grade["failed_or_weak_axes"] = weak
            if grade.get("grade") in {"A", "B+"}:
                grade["grade"] = "B"
            c["professional_technical_grade"] = grade
        if path.get("exact_slots_complete") and recent_anchor:
            anchored_complete += 1

    coverage = round(anchored_complete / max(1, liquid) * 100.0, 1) if liquid else 0.0
    blockers = [x for x in list(upstream_health.get("hard_blockers") or []) if not str(x).startswith("v29_anchored_5m_coverage_low")]
    if liquid >= 5 and coverage < MIN_LIQUID_COVERAGE_PCT:
        blockers.append(f"v29_anchored_5m_coverage_low:{coverage:.1f}%")

    out["version"] = VERSION
    out["schedule_design"] = {
        "github_workflow_cadence_minutes": 30,
        "single_run_points_minutes": [5, 10, 15, 20, 25, 30],
        "method": "One GitHub run every 30 minutes. For each finalist, anchor to the latest 5-minute candle that Bithumb actually returned as closed, then map the preceding five exact 5-minute buckets. Missing/no-trade buckets stay missing; no stale-candle substitution.",
        "anchor_freshness_rule": f"latest actual closed 5m candle must be no more than {MAX_ANCHOR_LAG_MIN:.0f} minutes stale for A/B+ eligibility",
        "orderbook_tradeflow_method": "Current live orderbook/trade flow plus persisted prior 30-minute-run deltas; historical 5-minute orderbooks cannot be retroactively backfilled from the public API.",
    }
    out["health"] = {
        **upstream_health,
        "analysis_ready": bool(upstream_health.get("analysis_ready")) and not blockers,
        "hard_blockers": blockers,
        "liquid_candidates_200m_plus": liquid,
        "anchored_exact_six_complete_liquid": anchored_complete,
        "anchored_exact_six_liquid_coverage_pct": coverage,
        "stale_anchor_liquid_count": stale_anchor_count,
        "anchor_max_lag_minutes": MAX_ANCHOR_LAG_MIN,
        "coverage_gate_min_pct": MIN_LIQUID_COVERAGE_PCT,
    }

    # Re-rank after stale-anchor caps, then recompute best candidate and status.
    rank = {"A": 3, "B+": 2, "B": 1, "C_OR_LOWER": 0}
    candidates.sort(key=lambda x: (
        rank.get((x.get("professional_technical_grade") or {}).get("grade"), 0),
        fnum((x.get("professional_technical_grade") or {}).get("score")),
    ), reverse=True)
    eligible = [x for x in candidates if (x.get("professional_technical_grade") or {}).get("grade") in {"A", "B+"}]
    best = eligible[0] if eligible else None
    out["candidates"] = candidates
    out["best_a_or_bplus_candidate"] = best

    v27_status = out.get("upstream_v27_status")
    v27_pick = out.get("upstream_v27_pick")
    technical_a = bool(best and (best.get("professional_technical_grade") or {}).get("grade") == "A" and v27_status == "FINAL_BUY" and best.get("base") == v27_pick)
    if blockers:
        out["status"] = "BLOCKED"
        out["best_a_or_bplus_candidate"] = None
    elif technical_a:
        out["status"] = "A_TECHNICAL_EXTERNAL_CHECK_REQUIRED"
    elif best:
        out["status"] = "WATCH_ONLY"
    else:
        out["status"] = "NO_BUY"

    contract = out.get("professional_decision_contract") or {}
    contract["anchored_5m_coverage_required"] = True
    contract["coverage_below_threshold_blocks_trade_recommendation"] = True
    contract["latest_closed_candle_anchor_not_wall_clock"] = True
    out["professional_decision_contract"] = contract

    p.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({
        "version": VERSION,
        "status": out.get("status"),
        "liquid_candidates": liquid,
        "anchored_complete": anchored_complete,
        "coverage_pct": coverage,
        "blockers": blockers,
        "best": (out.get("best_a_or_bplus_candidate") or {}).get("base"),
    }, ensure_ascii=False, indent=2))


def main():
    # v28.main resolves this symbol at runtime, so replacing it fixes the data
    # extraction without duplicating the entire scoring engine.
    v28.pull_exact_six_5m = pull_six_from_latest_closed
    v28.main()
    postprocess()


if __name__ == "__main__":
    main()
