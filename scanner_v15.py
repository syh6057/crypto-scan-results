import json
from copy import deepcopy

import scanner
import scanner_v14 as v14

VERSION = "v15-gap-breakout-catcher"
RADAR_OUTPUT_LIMIT = 50
GAP_MIN_CHANGE24 = 20.0
GAP_MAX_CHANGE24 = 60.0

_original_is_radar_candidate = v14._is_radar_candidate
_original_radar_score = v14._radar_score


def _is_gap_jump(change24, trade24, fast):
    change24 = float(change24 or 0)
    trade24 = float(trade24 or 0)
    fast_price, fast_change, fast_trade = v14._fast_values(fast)
    if trade24 < 10_000_000 or not (GAP_MIN_CHANGE24 <= change24 < GAP_MAX_CHANGE24):
        return False
    return (
        fast_price >= 0.75
        or fast_change >= 2.0
        or fast_trade >= 50_000_000
    )


def _is_radar_candidate(change24, trade24, fast):
    # v14 intentionally hid >=25% movers from the ticker radar. That caused
    # names that crossed the threshold between delayed scans (e.g. FLOCK-like
    # gaps) to disappear from detection entirely. Keep normal v14 rules for
    # early movers, but allow a fresh gap jump to remain visible.
    if _is_gap_jump(change24, trade24, fast):
        return True
    return _original_is_radar_candidate(change24, trade24, fast)


def _radar_score(change24, trade24, fast):
    score = float(_original_radar_score(change24, trade24, fast))
    if _is_gap_jump(change24, trade24, fast):
        fast_price, fast_change, fast_trade = v14._fast_values(fast)
        score += 25.0
        score += min(max(fast_price, 0.0), 5.0) * 8.0
        score += min(max(fast_change, 0.0), 10.0) * 3.0
        score += min(max(fast_trade, 0.0) / 50_000_000, 5.0) * 2.0
    return round(score, 2)


def _build_gap_breakout_alerts(current_market, previous_market, markets):
    alerts = []
    for base, current in current_market.get("tickers", {}).items():
        change24 = float(current.get("change24_pct") or 0)
        trade24 = float(current.get("trade24_krw") or 0)
        fast = scanner.fast_market_metrics(base, current_market, previous_market)
        if not _is_gap_jump(change24, trade24, fast):
            continue
        fast_price, fast_change, fast_trade = v14._fast_values(fast)
        warning = (markets.get(base) or {}).get("market_warning", "NONE")
        alerts.append({
            "base": base,
            "market": current.get("market") or (markets.get(base) or {}).get("market"),
            "bithumb_krw_price": current.get("price"),
            "bithumb_24h_change_pct": round(change24, 2),
            "bithumb_24h_trade_krw": round(trade24),
            "price_change_per_5m_pct": round(fast_price, 2),
            "change24_delta_since_scan_pct": round(fast_change, 2),
            "trade_value_delta_per_5m_krw": round(fast_trade),
            "radar_score": _radar_score(change24, trade24, fast),
            "market_warning": warning,
            "high_risk_market": warning not in (None, "", "NONE"),
            "detection_status": "GAP_BREAKOUT_VISIBLE_NO_CHASE",
            "chase_allowed": False,
            "reason": "crossed_overheat_band_between_scans_but_fresh_acceleration_is_visible",
        })
    alerts.sort(key=lambda r: (r["radar_score"], r["bithumb_24h_trade_krw"]), reverse=True)
    return alerts[:20]


def _post_v15():
    result = scanner.load_json(scanner.RESULT_FILE, {})
    summary = scanner.load_json(scanner.SUMMARY_FILE, {})
    current_market = result.get("market_snapshot") or {}
    previous_result = getattr(v14, "_v15_previous_result", {}) or {}
    previous_market = previous_result.get("market_snapshot") or {}
    markets = v14._capture.get("markets") or {}

    gap_alerts = _build_gap_breakout_alerts(current_market, previous_market, markets)
    result["version"] = VERSION
    result["gap_breakout_alerts"] = gap_alerts
    diagnostics = deepcopy(result.get("scanner_diagnostics") or {})
    diagnostics.update({
        "gap_breakout_overheat_visibility": True,
        "gap_breakout_no_chase": True,
        "radar_output_limit": RADAR_OUTPUT_LIMIT,
        "stale_run_backlog_should_be_cancelled": True,
    })
    result["scanner_diagnostics"] = diagnostics

    # Recompute audit after v15 radar visibility so fast >25% jumps are no
    # longer incorrectly labelled MISSED merely because they skipped the band.
    radar = result.get("full_market_breakout_radar") or []
    radar_bases = {row.get("base") for row in radar}
    radar_bases.update(row.get("base") for row in gap_alerts)
    ranked_bases = v14._candidate_bases_from_result(result)
    movers = []
    for base, row in current_market.get("tickers", {}).items():
        change = float(row.get("change24_pct") or 0)
        trade = float(row.get("trade24_krw") or 0)
        if not (0 < change < 60) or trade < 10_000_000:
            continue
        detected = base in radar_bases or base in ranked_bases
        movers.append({
            "base": base,
            "bithumb_24h_change_pct": round(change, 2),
            "bithumb_24h_trade_krw": round(trade),
            "detected": detected,
            "detection_channel": (
                "gap_breakout_alerts" if base in {x.get("base") for x in gap_alerts}
                else "full_market_breakout_radar" if base in {x.get("base") for x in radar}
                else "ranked_candle_signal" if base in ranked_bases
                else "MISSED"
            ),
        })
    movers.sort(key=lambda r: r["bithumb_24h_change_pct"], reverse=True)
    result["mover_detection_audit"] = movers[:40]

    for key in ("version", "gap_breakout_alerts", "mover_detection_audit", "scanner_diagnostics"):
        summary[key] = deepcopy(result.get(key))

    scanner.assert_public_output_safe(result)
    scanner.assert_public_output_safe(summary)
    scanner.save_json(scanner.RESULT_FILE, result)
    scanner.save_json(scanner.SUMMARY_FILE, summary)


def main():
    previous_result = scanner.load_json(scanner.RESULT_FILE, {})
    v14._v15_previous_result = previous_result
    v14._is_radar_candidate = _is_radar_candidate
    v14._radar_score = _radar_score
    v14.RADAR_OUTPUT_LIMIT = RADAR_OUTPUT_LIMIT
    v14.main()
    _post_v15()
    result = scanner.load_json(scanner.RESULT_FILE, {})
    print(json.dumps({
        "version": VERSION,
        "technical_breakout_leader": result.get("technical_breakout_leader"),
        "gap_breakout_alerts": result.get("gap_breakout_alerts"),
        "mover_detection_audit": result.get("mover_detection_audit"),
        "scanner_diagnostics": result.get("scanner_diagnostics"),
    }, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
