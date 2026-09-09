import json
from copy import deepcopy
from datetime import datetime, timezone, timedelta
from pathlib import Path

VERSION = "v27-final-precision-risk-gate"
APLUS_FILE = "beam_a_plus_precision.json"
SCAN_FILE = "beam_scan_result.json"
FIRST_SEEN_FILE = "beam_first_seen.json"
APLUS_STATE_FILE = "a_plus_precision_state.json"
OUT_FILE = "beam_final_trade_gate.json"

MIN_REL_STRENGTH_PCT = 0.75
MIN_BUY_SELL = 1.35
MIN_M15_PERSISTENCE = 1.75
MAX_PEAK_GIVEBACK_PCT = 3.0
MAX_5M_PCT = 1.5
MIN_5M_PCT = -0.25
RECENT_STOP_COOLDOWN_HOURS = 24
MAX_INITIAL_PORTFOLIO_PCT = 20
MAX_TOTAL_POSITION_PCT = 25


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


def row_lookup(scan, base):
    pools = [
        scan.get("trade_candidates") or [],
        scan.get("pre_ignition_top5") or [],
        scan.get("stage2_ranked") or [],
    ]
    for pool in pools:
        for row in pool:
            if isinstance(row, dict) and row.get("base") == base:
                return row
    return {}


def recent_stop_loss(state, base, now):
    trials = (state or {}).get("prospective_trials") or []
    for t in reversed(trials):
        if t.get("base") != base or t.get("status") != "LOSS_STOP":
            continue
        ts = parse_ts(t.get("resolved_at_utc"))
        if ts and now - ts <= timedelta(hours=RECENT_STOP_COOLDOWN_HOURS):
            return True, ts.isoformat()
    return False, None


def main():
    now = datetime.now(timezone.utc)
    aplus = load_json(APLUS_FILE, {})
    scan = load_json(SCAN_FILE, {})
    first_seen = load_json(FIRST_SEEN_FILE, {})
    state = load_json(APLUS_STATE_FILE, {})

    health = aplus.get("health") or {}
    hard_blockers = list(health.get("hard_blockers") or [])
    warnings = list(health.get("warnings") or [])
    pick = aplus.get("a_plus_pick") or {}
    base = pick.get("base")

    checks = {
        "upstream_health_clean": bool(health.get("analysis_ready")) and not hard_blockers,
        "v25_a_plus_buy": aplus.get("status") == "A_PLUS_BUY" and bool(base),
    }
    metrics = {}
    reasons = []
    row = {}

    if base:
        row = row_lookup(scan, base)
        fs = first_seen.get(base) or {}
        price = fnum(pick.get("price_krw") or row.get("price_krw"))
        change24 = fnum(pick.get("change_24h_pct") or row.get("change_24h_pct"))
        btc = fnum((aplus.get("market_headwind") or {}).get("btc_change_24h_pct"))
        eth = fnum((aplus.get("market_headwind") or {}).get("eth_change_24h_pct"))
        market_best = max(btc, eth)
        relative_strength = change24 - market_best

        pmetrics = pick.get("metrics") or {}
        buy_sell = fnum(pmetrics.get("buy_sell_ratio") or (row.get("trade_flow") or {}).get("buy_sell_ratio"))
        m15_persist = fnum(pmetrics.get("m15_volume_persistence_x") or (row.get("m15") or {}).get("vol_persistence_x"))
        p5 = fnum(pmetrics.get("five_min_price_pct") or (row.get("fast_5m") or {}).get("price_change_per_5m_pct"))
        m15 = row.get("m15") or {}
        h1 = row.get("h1") or {}

        first_price = fnum(fs.get("first_price_krw"))
        peak_price = fnum(fs.get("peak_price_krw"))
        return_since_first = ((price / first_price - 1) * 100) if price > 0 and first_price > 0 else None
        peak_return = ((peak_price / first_price - 1) * 100) if peak_price > 0 and first_price > 0 else None
        giveback = (peak_return - return_since_first) if peak_return is not None and return_since_first is not None else None
        stopped_recently, stopped_at = recent_stop_loss(state, base, now)

        checks.update({
            "a_plus_ready": bool(pick.get("a_plus_ready")),
            "relative_strength_ge_0_75pct": relative_strength >= MIN_REL_STRENGTH_PCT,
            "buy_sell_ge_1_35": buy_sell >= MIN_BUY_SELL,
            "m15_volume_persistence_ge_1_75": m15_persist >= MIN_M15_PERSISTENCE,
            "m15_close_above_ma": bool(m15.get("close_above_ma")),
            "h1_close_above_ma": bool(h1.get("close_above_ma")),
            "five_min_controlled": MIN_5M_PCT <= p5 <= MAX_5M_PCT,
            "not_below_first_detection": return_since_first is not None and return_since_first >= -0.5,
            "peak_giveback_le_3pct": giveback is not None and giveback <= MAX_PEAK_GIVEBACK_PCT,
            "no_recent_stop_reentry": not stopped_recently,
        })
        metrics = {
            "price_krw": price,
            "change_24h_pct": round(change24, 2),
            "btc_change_24h_pct": round(btc, 2),
            "eth_change_24h_pct": round(eth, 2),
            "relative_strength_vs_stronger_major_pct": round(relative_strength, 2),
            "buy_sell_ratio": round(buy_sell, 2),
            "m15_volume_persistence_x": round(m15_persist, 2),
            "five_min_price_pct": round(p5, 3),
            "return_since_first_detection_pct": round(return_since_first, 2) if return_since_first is not None else None,
            "peak_return_since_first_detection_pct": round(peak_return, 2) if peak_return is not None else None,
            "giveback_from_first_seen_peak_pct": round(giveback, 2) if giveback is not None else None,
            "recent_stop_loss_at_utc": stopped_at,
        }

    failed = [k for k, ok in checks.items() if not ok]
    final_buy = bool(base) and not failed
    status = "FINAL_BUY" if final_buy else "NO_BUY"
    if not checks.get("v25_a_plus_buy"):
        reasons.append("v25 A+ gate did not authorize a buy")
    if failed:
        reasons.append("failed final precision checks: " + ", ".join(failed))

    out = {
        "generated_at_utc": now.isoformat(),
        "version": VERSION,
        "status": status,
        "final_pick": pick if final_buy else None,
        "source_a_plus_status": aplus.get("status"),
        "source_a_plus_pick": base,
        "health": {
            "analysis_ready": bool(health.get("analysis_ready")),
            "hard_blockers": hard_blockers,
            "warnings": warnings,
        },
        "checks": checks,
        "failed_checks": failed,
        "metrics": metrics,
        "reasons": reasons,
        "assistant_contract": {
            "new_buy_permitted_only_when_status_FINAL_BUY": True,
            "winner_recall_is_watch_only_never_a_buy_signal": True,
            "expert_score_or_confidence_alone_never_authorizes_buy": True,
            "scanner_score_is_not_probability": True,
            "do_not_claim_80pct_until_prospectively_calibrated": True,
            "if_NO_BUY_return_NO_BUY_without_forcing_a_pick": True,
        },
        "risk_policy": {
            "max_initial_portfolio_pct": MAX_INITIAL_PORTFOLIO_PCT,
            "max_total_position_pct": MAX_TOTAL_POSITION_PCT,
            "averaging_down": "PROHIBITED",
            "hard_stop": "use A+ hard stop; never widen after entry",
            "profit_protection": {
                "at_plus_10pct": "take about 25pct off and stop allowing a full round-trip",
                "at_plus_15pct": "take another 25pct off; protect remaining profit",
                "at_plus_30pct": "target zone for residual position, not guaranteed",
                "giveback_rule": "after +10 to +15, a 6 to 8 percentage-point giveback requires additional de-risking",
            },
            "replacement_rule": "do not rotate from a loser into a new coin unless the new coin is FINAL_BUY",
        },
        "change_log": [
            "recall is separated from execution: recall can only create a watch candidate",
            "added relative-strength veto so a coin lagging BTC/ETH cannot be promoted just because turnover spikes",
            "tightened live buy-flow and 15m persistence requirements",
            "requires 15m and 1h closes above their moving averages at execution time",
            "rejects candidates that already gave back more than 3 percentage points from their first-seen peak",
            "adds 24h re-entry cooldown after a prospective -5pct stop",
            "caps suggested position concentration and prohibits averaging down",
            "adds mandatory profit-protection checkpoints to avoid ARX-style giveback",
        ],
    }
    save_json(OUT_FILE, out)
    print(json.dumps(out, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
