import json
import math
from copy import deepcopy

import scanner

VERSION = "v14-full-market-breakout-radar"
RADAR_CANDLE_FORCE_LIMIT = 60
RADAR_OUTPUT_LIMIT = 20

_original_select_candle_universe = scanner.select_candle_universe
_capture = {}


def _fast_values(fast):
    fast_price = scanner.normalized_fast_price(fast)
    fast_change = fast.get("change24_delta_since_scan_pct") or 0
    fast_trade = scanner.normalized_fast_trade(fast)
    return float(fast_price or 0), float(fast_change or 0), float(fast_trade or 0)


def _is_radar_candidate(change24, trade24, fast):
    """Cheap full-market trigger used before candle eligibility.

    It deliberately has lower turnover requirements than order eligibility.
    The purpose is detection, not permission to buy.
    """
    change24 = float(change24 or 0)
    trade24 = float(trade24 or 0)
    fast_price, fast_change, fast_trade = _fast_values(fast)
    if trade24 < 10_000_000 or change24 < -2 or change24 >= 25:
        return False
    fast_trigger = (
        fast_price >= 0.25
        or fast_change >= 0.40
        or (fast_price >= 0.10 and fast_trade >= 20_000_000)
    )
    early_band = 1.0 <= change24 < 8.0 and trade24 >= 30_000_000 and fast_price >= 0.10
    first_scan_fallback = 2.0 <= change24 < 8.0 and trade24 >= 100_000_000
    acceleration_band = 8.0 <= change24 < 25.0 and fast_price >= 0.20
    return fast_trigger or early_band or first_scan_fallback or acceleration_band


def _radar_score(change24, trade24, fast):
    change24 = float(change24 or 0)
    trade24 = float(trade24 or 0)
    fast_price, fast_change, fast_trade = _fast_values(fast)
    score = 0.0
    score += max(min(fast_price, 4.0), -2.0) * 35.0
    score += max(min(fast_change, 4.0), -2.0) * 15.0
    score += min(max(fast_trade, 0.0) / 20_000_000, 10.0) * 2.0
    if 2.0 <= change24 < 8.0:
        score += 14.0
    elif 8.0 <= change24 < 15.0:
        score += 9.0
    elif 15.0 <= change24 < 25.0:
        score += 3.0
    score += max(0.0, min(math.log10(max(trade24, 1.0)) - 7.0, 3.0)) * 3.0
    return round(score, 2)


def _patched_select_candle_universe(markets, tickers, current_market, previous_market):
    selected = set(_original_select_candle_universe(markets, tickers, current_market, previous_market))
    ranked = []
    for base, info in markets.items():
        ticker = tickers.get(info["market"], {})
        change24 = float(ticker.get("signed_change_rate") or 0) * 100
        trade24 = float(ticker.get("acc_trade_price_24h") or 0)
        fast = scanner.fast_market_metrics(base, current_market, previous_market)
        if _is_radar_candidate(change24, trade24, fast):
            ranked.append((_radar_score(change24, trade24, fast), base))
    ranked.sort(reverse=True)
    selected.update(base for _, base in ranked[:RADAR_CANDLE_FORCE_LIMIT])
    _capture.clear()
    _capture.update({
        "markets": markets,
        "tickers": tickers,
        "current_market": current_market,
        "previous_market": previous_market,
        "forced_radar_bases": [base for _, base in ranked[:RADAR_CANDLE_FORCE_LIMIT]],
    })
    return selected


def _build_full_market_radar(current_market, previous_market, markets, snapshot):
    radar = []
    for base, current in current_market.get("tickers", {}).items():
        change24 = float(current.get("change24_pct") or 0)
        trade24 = float(current.get("trade24_krw") or 0)
        fast = scanner.fast_market_metrics(base, current_market, previous_market)
        if not _is_radar_candidate(change24, trade24, fast):
            continue
        warning = (markets.get(base) or {}).get("market_warning", "NONE")
        context = snapshot.get(base) or {}
        fast_price, fast_change, fast_trade = _fast_values(fast)
        item = {
            "base": base,
            "market": current.get("market") or (markets.get(base) or {}).get("market"),
            "bithumb_krw_price": current.get("price"),
            "bithumb_24h_change_pct": round(change24, 2),
            "bithumb_24h_trade_krw": round(trade24),
            "price_change_per_5m_pct": round(fast_price, 2),
            "change24_delta_since_scan_pct": round(fast_change, 2),
            "trade_value_delta_per_5m_krw": round(fast_trade),
            "radar_score": _radar_score(change24, trade24, fast),
            "radar_stage": "ticker_acceleration" if change24 >= 8 else "ticker_early_breakout",
            "market_warning": warning,
            "high_risk_market": warning not in (None, "", "NONE"),
            "candle_scanned": bool(context),
            "momentum_stage": context.get("momentum_stage"),
            "future_expansion_score": context.get("future_expansion_score"),
            "failure_similarity_score": context.get("failure_similarity_score"),
            "four_hour_low_rising": context.get("four_hour_low_rising"),
            "recent_15m_positive_count": context.get("recent_15m_positive_count"),
            "upper_wick_1h_pct": context.get("upper_wick_1h_pct"),
        }
        if item["high_risk_market"]:
            item["detection_status"] = "HIGH_RISK_BREAKOUT_VISIBLE_NO_EXECUTION_PERMISSION"
        elif item["candle_scanned"]:
            item["detection_status"] = "CANDLE_CONFIRMED_OR_EVALUATED"
        else:
            item["detection_status"] = "TICKER_BREAKOUT_NEEDS_CANDLE_CONFIRMATION"
        radar.append(item)
    radar.sort(key=lambda r: (r["radar_score"], r["bithumb_24h_trade_krw"]), reverse=True)
    return radar[:RADAR_OUTPUT_LIMIT]


def _candidate_bases_from_result(result):
    bases = set()
    for key in ("pre_ignition_top5", "acceleration_top5", "fast_breakout_alerts", "late_top5"):
        for row in result.get(key) or []:
            if row and row.get("base"):
                bases.add(row["base"])
    for key in ("best_available_pick", "actual_buy", "probe_buy", "watch_pick", "fast_breakout_leader"):
        row = result.get(key) or {}
        if row.get("base"):
            bases.add(row["base"])
    return bases


def _build_continuity_signals(previous_result, current_result):
    previous_bases = _candidate_bases_from_result(previous_result)
    current_top = _candidate_bases_from_result(current_result)
    snapshot = current_result.get("snapshot") or {}
    out = []
    for base in sorted(previous_bases):
        row = snapshot.get(base)
        if not row:
            continue
        still_valid = (
            row.get("momentum_stage") in {"pre_ignition", "acceleration"}
            and (row.get("failure_similarity_score") or 100) < 60
            and row.get("four_hour_low_rising") is True
            and scanner.normalized_fast_price(row) >= -0.5
        )
        if not still_valid:
            continue
        out.append({
            "base": base,
            "still_valid": True,
            "in_current_top_lists": base in current_top,
            "rank_churn_only": base not in current_top,
            "momentum_stage": row.get("momentum_stage"),
            "bithumb_krw_price": row.get("bithumb_krw_price"),
            "future_expansion_score": row.get("future_expansion_score"),
            "failure_similarity_score": row.get("failure_similarity_score"),
            "four_hour_low_rising": row.get("four_hour_low_rising"),
            "price_change_per_5m_pct": row.get("price_change_per_5m_pct"),
            "reason": "structure_intact_despite_rank_churn" if base not in current_top else "structure_intact_and_ranked",
        })
    out.sort(key=lambda r: (r["rank_churn_only"], r.get("future_expansion_score") or -999), reverse=True)
    return out[:20]


def _enrich_additional(additional, current_market, previous_market):
    enriched = []
    for row in additional or []:
        item = deepcopy(row)
        base = item.get("base")
        fast = scanner.fast_market_metrics(base, current_market, previous_market)
        change24 = float(item.get("bithumb_24h_change_pct") or 0)
        trade24 = float(item.get("bithumb_24h_trade_krw") or 0)
        fast_price, fast_change, fast_trade = _fast_values(fast)
        item.update({
            "price_change_per_5m_pct": round(fast_price, 2),
            "change24_delta_since_scan_pct": round(fast_change, 2),
            "trade_value_delta_per_5m_krw": round(fast_trade),
            "breakout_radar_candidate": _is_radar_candidate(change24, trade24, fast),
            "breakout_priority_score": _radar_score(change24, trade24, fast),
        })
        enriched.append(item)
    enriched.sort(
        key=lambda r: (
            bool(r.get("breakout_radar_candidate")),
            r.get("breakout_priority_score") or -999,
            r.get("bithumb_24h_trade_krw") or 0,
        ),
        reverse=True,
    )
    return enriched[:30]


def _build_mover_detection_audit(current_result, radar):
    radar_bases = {row["base"] for row in radar}
    ranked_bases = _candidate_bases_from_result(current_result)
    current = current_result.get("market_snapshot", {}).get("tickers", {})
    movers = []
    for base, row in current.items():
        change = float(row.get("change24_pct") or 0)
        trade = float(row.get("trade24_krw") or 0)
        if not (0 < change < 50) or trade < 10_000_000:
            continue
        detected = base in radar_bases or base in ranked_bases
        movers.append({
            "base": base,
            "bithumb_24h_change_pct": round(change, 2),
            "bithumb_24h_trade_krw": round(trade),
            "detected": detected,
            "detection_channel": (
                "full_market_breakout_radar" if base in radar_bases
                else "ranked_candle_signal" if base in ranked_bases
                else "MISSED"
            ),
        })
    movers.sort(key=lambda r: r["bithumb_24h_change_pct"], reverse=True)
    return movers[:20]


def _postprocess(previous_result):
    result = scanner.load_json(scanner.RESULT_FILE, {})
    summary = scanner.load_json(scanner.SUMMARY_FILE, {})
    current_market = result.get("market_snapshot") or _capture.get("current_market") or {}
    previous_market = previous_result.get("market_snapshot") or _capture.get("previous_market") or {}
    markets = _capture.get("markets") or {}
    snapshot = result.get("snapshot") or {}

    radar = _build_full_market_radar(current_market, previous_market, markets, snapshot)
    safe_radar = [r for r in radar if not r.get("high_risk_market")]
    high_risk = [r for r in radar if r.get("high_risk_market")]
    continuity = _build_continuity_signals(previous_result, result)
    enriched_additional = _enrich_additional(result.get("additional_data_required"), current_market, previous_market)

    result["version"] = VERSION
    result["full_market_breakout_radar"] = radar
    result["technical_breakout_leader"] = safe_radar[0] if safe_radar else None
    result["high_risk_breakout_alerts"] = high_risk[:10]
    result["high_risk_breakout_leader"] = high_risk[0] if high_risk else None
    result["continuity_signals"] = continuity
    result["additional_data_required"] = enriched_additional
    result["mover_detection_audit"] = _build_mover_detection_audit(result, radar)
    result["scanner_diagnostics"] = {
        "full_market_ticker_radar": True,
        "forced_candle_confirmation_limit": RADAR_CANDLE_FORCE_LIMIT,
        "market_warning_detection_separated_from_execution": True,
        "rank_churn_does_not_invalidate_intact_structure": True,
        "additional_data_sorted_by_breakout_priority": True,
        "risk_gate_still_fail_closed_for_actual_orders": True,
    }

    for key in (
        "version",
        "full_market_breakout_radar",
        "technical_breakout_leader",
        "high_risk_breakout_alerts",
        "high_risk_breakout_leader",
        "continuity_signals",
        "additional_data_required",
        "mover_detection_audit",
        "scanner_diagnostics",
    ):
        summary[key] = deepcopy(result.get(key))

    scanner.assert_public_output_safe(result)
    scanner.assert_public_output_safe(summary)
    scanner.save_json(scanner.RESULT_FILE, result)
    scanner.save_json(scanner.SUMMARY_FILE, summary)
    print(json.dumps({
        "version": VERSION,
        "technical_breakout_leader": result.get("technical_breakout_leader"),
        "high_risk_breakout_leader": result.get("high_risk_breakout_leader"),
        "continuity_signals": result.get("continuity_signals"),
        "mover_detection_audit": result.get("mover_detection_audit"),
    }, ensure_ascii=False, indent=2))


def main():
    previous_result = scanner.load_json(scanner.RESULT_FILE, {})
    scanner.select_candle_universe = _patched_select_candle_universe
    scanner.main()
    _postprocess(previous_result)


if __name__ == "__main__":
    main()
