import json
import math
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

import scanner_v18 as v18

VERSION = "v21-miss-recovery-source"
STAGE1_CAP = 200
STAGE2_CAP = 45
MIN_STAGE1_TRADE_KRW = 50_000_000

v17 = v18.v17
v16 = v17.v16


def current_change(item):
    return v16.safe(item[3].get("signed_change_rate")) * 100.0


def current_trade(item):
    return v16.safe(item[3].get("acc_trade_price_24h"))


def recall_pre_score(item):
    """Cheap ticker-only recall score used before candle requests.

    It deliberately gives a separate path to active 2~30% movers so the normal
    pre-ignition/liquidity ranking cannot consume the entire stage-1 budget.
    """
    ch = current_change(item)
    trade = current_trade(item)
    high = v16.safe(item[3].get("high_price"))
    price = v16.safe(item[3].get("trade_price"))
    dist = abs(v16.pct(price, high) or 0.0)
    score = min(26.0, math.log10(max(trade, 1) / 50_000_000 + 1) * 9.0)
    if 2 <= ch < 8:
        score += 20
    elif 8 <= ch <= 20:
        score += 24
    elif 20 < ch <= 30:
        score += 16
    elif 0 <= ch < 2:
        score += 8
    if dist <= 4:
        score += 10
    elif dist <= 8:
        score += 6
    return score


def breakout_recall_score(row):
    """Stage-1 candle score for the winners we previously missed.

    Screenshots showed many eventual +12~28% names with meaningful turnover.
    This score rewards the transition itself: rising/persistent 15m flow,
    positive 1h response, proximity to the day high, and fast turnover, without
    requiring the candidate to win the original blended score first.
    """
    ch = v16.safe(row.get("change_24h_pct"))
    trade = v16.safe(row.get("trade_24h_krw"))
    dist = v16.safe(row.get("distance_from_day_high_pct"), 99)
    m15 = row.get("m15") or {}
    h1 = row.get("h1") or {}
    d1 = row.get("d1") or {}
    fast = row.get("fast_5m") or {}

    score = min(24.0, math.log10(max(trade, 1) / 50_000_000 + 1) * 8.0)
    if 2 <= ch < 8:
        score += 22
    elif 8 <= ch <= 20:
        score += 24
    elif 20 < ch <= 30:
        score += 12
    elif 0 <= ch < 2:
        score += 8

    vsp = v16.safe(m15.get("vol_spike_x"))
    vper = v16.safe(m15.get("vol_persistence_x"))
    p15 = v16.safe(m15.get("last_price_pct"))
    p1 = v16.safe(h1.get("last_price_pct"))
    if vsp >= 3:
        score += 16
    elif vsp >= 2:
        score += 12
    elif vsp >= 1.4:
        score += 7
    if vper >= 2:
        score += 15
    elif vper >= 1.3:
        score += 10
    elif vper >= 1.05:
        score += 5
    if m15.get("low_rising"):
        score += 10
    if m15.get("close_above_ma"):
        score += 6
    if 0 <= p15 <= 3.5:
        score += 7
    if 0 <= p1 <= 7:
        score += 7
    if dist <= 5:
        score += 8
    elif dist <= 10:
        score += 4

    pdvx = v16.safe(d1.get("previous_day_volume_x"))
    if pdvx >= 2:
        score += 8
    elif pdvx >= 1.3:
        score += 4

    if fast.get("available"):
        intensity = v16.safe(fast.get("turnover_intensity_vs_even_5m_x"))
        p5 = v16.safe(fast.get("price_change_per_5m_pct"))
        if intensity >= 6:
            score += 16
        elif intensity >= 3:
            score += 11
        elif intensity >= 1.5:
            score += 5
        if 0.05 <= p5 <= 2.5:
            score += 7
        elif p5 > 4:
            score -= 8
    return round(score, 2)


def add_unique(dst, seen, group, quota, cap):
    added = 0
    for item in group:
        if len(dst) >= cap or added >= quota:
            break
        market = item[1]
        if market in seen:
            continue
        dst.append(item)
        seen.add(market)
        added += 1


def select_stage1(base_rows):
    by_pre = sorted(base_rows, key=lambda x: x[0], reverse=True)
    by_change = sorted(
        [x for x in base_rows if 0 <= current_change(x) <= 30],
        key=lambda x: (recall_pre_score(x), current_change(x), current_trade(x)),
        reverse=True,
    )
    by_liq = sorted(base_rows, key=current_trade, reverse=True)
    by_early_midcap = sorted(
        [x for x in base_rows if 0 <= current_change(x) <= 12 and current_trade(x) >= 100_000_000],
        key=lambda x: (recall_pre_score(x), current_trade(x)),
        reverse=True,
    )

    selected, seen = [], set()
    # Old logic often filled MAX_STAGE1 before the top-change group was reached.
    # Reserve explicit capacity for each lane so momentum names cannot be crowded out.
    add_unique(selected, seen, by_pre, 70, STAGE1_CAP)
    add_unique(selected, seen, by_change, 70, STAGE1_CAP)
    add_unique(selected, seen, by_liq, 35, STAGE1_CAP)
    add_unique(selected, seen, by_early_midcap, 25, STAGE1_CAP)
    add_unique(selected, seen, by_pre, STAGE1_CAP, STAGE1_CAP)
    return selected


def select_stage2(stage1_ok):
    base_ranked = sorted(
        stage1_ok,
        key=lambda r: max(v16.safe(r.get("pre_ignition_score_raw")), v16.safe(r.get("accelerator_score_raw")))
        + v16.safe(r.get("sector_rotation_bonus")),
        reverse=True,
    )
    breakout = sorted(
        [
            r for r in stage1_ok
            if 2 <= v16.safe(r.get("change_24h_pct")) <= 30
            and v16.safe(r.get("trade_24h_krw")) >= 250_000_000
        ],
        key=breakout_recall_score,
        reverse=True,
    )
    early = sorted(
        [
            r for r in stage1_ok
            if 0 <= v16.safe(r.get("change_24h_pct")) < 8
            and v16.safe(r.get("trade_24h_krw")) >= 100_000_000
        ],
        key=breakout_recall_score,
        reverse=True,
    )

    finalists, seen = [], set()

    def add_rows(group, quota):
        added = 0
        for row in group:
            if len(finalists) >= STAGE2_CAP or added >= quota:
                break
            base = row.get("base")
            if not base or base in seen:
                continue
            finalists.append(row)
            seen.add(base)
            added += 1

    add_rows(base_ranked, 24)
    add_rows(breakout, 14)
    add_rows(early, 7)
    add_rows(base_ranked, STAGE2_CAP)
    return finalists, breakout[:10], early[:10]


def recall_source_main():
    started = time.time()
    hard_blockers, warnings = [], []
    try:
        markets = v16.get_markets()
    except Exception as e:
        markets = {}
        hard_blockers.append(f"bithumb_market_list_failed:{e}")
    tickers = v16.get_tickers(markets) if markets else {}
    if not tickers:
        hard_blockers.append("bithumb_ticker_unavailable")
    binance24 = v16.get_binance_24h()
    if not binance24:
        warnings.append("binance_optional_crosscheck_unavailable")

    base_rows = []
    for market, info in markets.items():
        t = tickers.get(market)
        if not t or v16.safe(t.get("acc_trade_price_24h")) < MIN_STAGE1_TRADE_KRW:
            continue
        base_rows.append((v16.prelim_score(t), market, info, t))

    selected = select_stage1(base_rows)
    stage1 = []
    with ThreadPoolExecutor(max_workers=v16.MAX_WORKERS) as ex:
        fs = [ex.submit(v16.fetch_stage1, m, i, t, binance24) for _, m, i, t in selected]
        for f in as_completed(fs):
            try:
                stage1.append(f.result())
            except Exception:
                pass

    stage1_ok = [r for r in stage1 if r.get("stage1_ok")]
    success_ratio = len(stage1_ok) / max(1, len(selected))
    if success_ratio < 0.70:
        hard_blockers.append(f"stage1_data_coverage_low:{success_ratio:.1%}")
    elif success_ratio < 0.85:
        warnings.append(f"stage1_data_coverage_partial:{success_ratio:.1%}")

    leaders = v16.add_sector_rotation(stage1_ok)
    finalists, breakout_lane, early_lane = select_stage2(stage1_ok)
    enriched = []
    with ThreadPoolExecutor(max_workers=6) as ex:
        fs = [ex.submit(v16.enrich_stage2, dict(r)) for r in finalists]
        for f in as_completed(fs):
            try:
                enriched.append(v16.final_score(f.result()))
            except Exception:
                pass
    enriched.sort(key=lambda r: v16.safe(r.get("beam_score")), reverse=True)

    normal = [r for r in enriched if not r.get("warning_flag")]
    speculative = [r for r in enriched if r.get("warning_flag")]
    enter = [r for r in normal if r.get("entry_plan", {}).get("status") == "ENTER_CANDIDATE"]
    confirm = [r for r in normal if r.get("entry_plan", {}).get("status") == "WAIT_CONFIRMATION"]
    pool = enter + [r for r in confirm if r not in enter] + [r for r in normal if r not in enter and r not in confirm]
    trade_candidates = pool[:2]
    if len(trade_candidates) < 2:
        hard_blockers.append("fewer_than_two_rankable_candidates")

    output = {
        "generated_at_utc": v16.now_iso(),
        "source_version": VERSION,
        "data_sources": {
            "bithumb_market_list": "live",
            "bithumb_ticker": "live" if tickers else "unavailable",
            "bithumb_candles": "live",
            "bithumb_orderbook": "live_for_finalists",
            "bithumb_recent_trades": "live_for_finalists",
            "binance_24h": "optional_crosscheck" if binance24 else "unavailable",
            "news_catalyst": "DEFERRED_TO_CHAT_WEB_CHECK",
        },
        "health": {
            "analysis_ready": not hard_blockers,
            "hard_blockers": hard_blockers,
            "warnings": warnings,
            "markets_total": len(markets),
            "tickers_loaded": len(tickers),
            "stage1_requested": len(selected),
            "stage1_success": len(stage1_ok),
            "stage1_success_ratio": v16.rnd(success_ratio * 100),
            "stage2_success": len(enriched),
            "runtime_sec": v16.rnd(time.time() - started, 1),
        },
        "logic": {
            "universe": "ALL_BITHUMB_KRW; Binance intersection is NOT a gate",
            "tracks": ["PRE_IGNITION", "ACCELERATOR"],
            "patterns_learned_and_applied": v16.PATTERNS_LEARNED,
            "warning_assets": "scanned but penalized and separated; never silently discarded",
            "risk_news": "not a hard gate in GitHub runtime; ChatGPT must web-check top2 before trade conclusion",
            "miss_recovery_patch": {
                "version": VERSION,
                "root_cause_fixed": [
                    "reserve stage1 capacity for top-change names before cap is filled",
                    "reserve stage2 capacity for breakout/early lanes instead of blended-score top30 only",
                    "lower stage1 turnover floor to catch flow before the visible pump",
                ],
                "stage1_cap": STAGE1_CAP,
                "stage2_cap": STAGE2_CAP,
                "stage1_turnover_floor_krw": MIN_STAGE1_TRADE_KRW,
                "stage1_lane_quotas": {"core_pre": 70, "top_change": 70, "liquidity": 35, "early_midcap": 25},
                "stage2_lane_quotas": {"core": 24, "breakout": 14, "early": 7},
            },
        },
        "sector_leaders": leaders,
        "trade_candidates": trade_candidates,
        "pre_ignition_top5": sorted(normal, key=lambda r: v16.safe(r.get("pre_ignition_score")), reverse=True)[:5],
        "accelerator_top5": sorted(normal, key=lambda r: v16.safe(r.get("accelerator_score")), reverse=True)[:5],
        "speculative_warning_top3": speculative[:3],
        "stage2_ranked": enriched,
        "recall_stage1_breakout_top10": [
            {"base": r.get("base"), "change_24h_pct": r.get("change_24h_pct"), "trade_24h_krw": r.get("trade_24h_krw"), "recall_score": breakout_recall_score(r)}
            for r in breakout_lane
        ],
        "recall_stage1_early_top10": [
            {"base": r.get("base"), "change_24h_pct": r.get("change_24h_pct"), "trade_24h_krw": r.get("trade_24h_krw"), "recall_score": breakout_recall_score(r)}
            for r in early_lane
        ],
    }

    with open("beam_scan_result.json", "w", encoding="utf-8") as f:
        json.dump(output, f, ensure_ascii=False, indent=2)
    with open("beam_latest_summary.json", "w", encoding="utf-8") as f:
        json.dump({
            "generated_at_utc": output["generated_at_utc"],
            "source_version": VERSION,
            "health": output["health"],
            "logic": output["logic"],
            "trade_candidates": [
                {"base": r.get("base"), "price_krw": r.get("price_krw"), "beam_score": r.get("beam_score")}
                for r in trade_candidates
            ],
        }, f, ensure_ascii=False, indent=2)


# v17 patches get_tickers/base_row/score_stage1 immediately before calling v16.main.
# Replacing v16.main here lets the calibrated fast-state metrics flow through the
# recall lanes without duplicating the downstream v17/v18 logic.
v16.main = recall_source_main


def main():
    v18.main()


if __name__ == "__main__":
    main()
