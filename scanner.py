import json
import math
import os
import time
from datetime import datetime, timezone
from statistics import mean

import requests


BINANCE_BASE = "https://data-api.binance.vision"
BITHUMB_BASE = "https://api.bithumb.com"
POLICY_FILE = "crypto_decision_policy.json"
RESULT_FILE = "scan_result.json"
SUMMARY_FILE = "latest_summary.json"
HISTORY_FILE = "recommendation_history.json"
SUPPORTED_POLICY_SCHEMA = 1
SUPPORTED_POLICY_VERSION = "2026-08-27.1"
MAX_CANDLE_UNIVERSE = 180
MIN_TRADE_KRW = 300_000_000
ACTIONABLE_TRADE_KRW = 1_000_000_000
MAJOR_LOW_BETA = {"BTC", "ETH", "DOGE", "XRP", "SOL", "BNB"}
STABLES = {"USDT", "USDC", "FDUSD", "USDS", "TUSD", "DAI", "PYUSD", "USDP", "USD1", "USDE"}

session = requests.Session()
session.headers.update({"User-Agent": "coin-project-scanner/6.0", "Accept": "application/json"})


def get_json(url, params=None, retries=4, timeout=25):
    error = None
    for attempt in range(retries):
        try:
            response = session.get(url, params=params, timeout=timeout)
            if response.status_code == 429:
                time.sleep(2 + attempt * 2)
                continue
            response.raise_for_status()
            return response.json()
        except Exception as exc:
            error = exc
            time.sleep(1 + attempt)
    raise error


def load_json(path, default):
    try:
        with open(path, "r", encoding="utf-8") as handle:
            return json.load(handle)
    except Exception:
        return default


def save_json(path, value):
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(value, handle, ensure_ascii=False, indent=2)


def load_policy():
    policy = load_json(POLICY_FILE, None)
    if not policy:
        raise RuntimeError("POLICY_MISSING: crypto_decision_policy.json is required")
    if policy.get("schema_version") != SUPPORTED_POLICY_SCHEMA:
        raise RuntimeError("POLICY_SCHEMA_MISMATCH: stop analysis before producing recommendations")
    if policy.get("policy_version") != SUPPORTED_POLICY_VERSION:
        raise RuntimeError("POLICY_VERSION_MISMATCH: scanner and policy must be updated together")
    required = ["objective", "data_priority", "universes", "momentum_stages", "success_examples", "failure_examples"]
    missing = [key for key in required if key not in policy]
    if missing:
        raise RuntimeError(f"POLICY_FIELDS_MISSING: {missing}")
    return policy


def pct(a, b):
    if a is None or b in (None, 0):
        return None
    return (float(a) / float(b) - 1) * 100


def rnd(value, digits=2):
    return None if value is None else round(float(value), digits)


def get_bithumb_markets():
    rows = get_json(BITHUMB_BASE + "/v1/market/all", {"isDetails": "true"})
    output = {}
    for row in rows:
        market = row.get("market", "")
        if not market.startswith("KRW-"):
            continue
        base = market.split("-", 1)[1]
        output[base] = {
            "market": market,
            "korean_name": row.get("korean_name"),
            "english_name": row.get("english_name"),
            "market_warning": row.get("market_warning", "NONE"),
        }
    return output


def get_bithumb_tickers(markets):
    codes = [value["market"] for value in markets.values()]
    output = {}
    for index in range(0, len(codes), 40):
        rows = get_json(BITHUMB_BASE + "/v1/ticker", {"markets": ",".join(codes[index:index + 40])})
        for row in rows:
            output[row["market"]] = row
        time.sleep(0.06)
    return output


def get_binance_universe():
    info = get_json(BINANCE_BASE + "/api/v3/exchangeInfo")
    tickers = get_json(BINANCE_BASE + "/api/v3/ticker/24hr")
    ticker_map = {row["symbol"]: row for row in tickers if row.get("symbol")}
    pairs = {}
    for row in info.get("symbols", []):
        base = row.get("baseAsset")
        if row.get("status") == "TRADING" and row.get("quoteAsset") == "USDT" and base and base not in STABLES:
            pairs[base] = row.get("symbol")
    return pairs, ticker_map


def binance_bars(symbol, interval, limit):
    rows = get_json(BINANCE_BASE + "/api/v3/klines", {"symbol": symbol, "interval": interval, "limit": limit})
    now_ms = int(time.time() * 1000)
    return [
        {"ts": int(row[0]), "open": float(row[1]), "high": float(row[2]), "low": float(row[3]), "close": float(row[4]), "volume": float(row[5])}
        for row in rows if int(row[6]) < now_ms
    ]


def bithumb_bars(market, unit, limit):
    rows = get_json(BITHUMB_BASE + f"/v1/candles/minutes/{unit}", {"market": market, "count": limit})
    interval_ms = unit * 60_000
    now_ms = int(time.time() * 1000)
    bars = []
    for row in rows:
        text = row.get("candle_date_time_utc")
        if not text:
            continue
        ts = int(datetime.fromisoformat(text).replace(tzinfo=timezone.utc).timestamp() * 1000)
        if ts + interval_ms > now_ms:
            continue
        bars.append({
            "ts": ts,
            "open": float(row["opening_price"]),
            "high": float(row["high_price"]),
            "low": float(row["low_price"]),
            "close": float(row["trade_price"]),
            "volume": float(row["candle_acc_trade_volume"]),
        })
    return sorted(bars, key=lambda value: value["ts"])


def candle_metrics(k15, k1, k4):
    if len(k15) < 8 or len(k1) < 21 or len(k4) < 3:
        return None
    latest, previous = k1[-1], k1[-2]
    avg20 = mean(row["volume"] for row in k1[-21:-1])
    old15 = mean(row["volume"] for row in k15[-8:-4])
    new15 = mean(row["volume"] for row in k15[-4:])
    lows = [row["low"] for row in k4[-3:]]
    return {
        "vol_1h_vs_prev_pct": pct(latest["volume"], previous["volume"]),
        "vol_1h_vs_20h_x": latest["volume"] / avg20 if avg20 else None,
        "price_1h_pct": pct(latest["close"], latest["open"]),
        "price_4h_pct": pct(k4[-1]["close"], k4[-1]["open"]),
        "vol_15m_persistence_x": new15 / old15 if old15 else None,
        "price_last_60m_pct": pct(k15[-1]["close"], k15[-4]["open"]),
        "recent_15m_positive_count": sum(1 for row in k15[-4:] if row["close"] >= row["open"]),
        "upper_wick_1h_pct": pct(latest["high"], max(latest["open"], latest["close"])),
        "four_hour_low_rising": lows[-1] >= lows[-2] >= lows[-3],
    }


def determine_stage(change24, metrics, distance_high):
    change24 = change24 or 0
    wick = metrics.get("upper_wick_1h_pct") or 0
    p60 = metrics.get("price_last_60m_pct") or 0
    persistence = metrics.get("vol_15m_persistence_x") or 0
    exhausted = (
        (change24 >= 8 and wick >= 1.5 and persistence < 0.8)
        or (change24 >= 8 and p60 <= -1)
        or (change24 >= 15 and distance_high is not None and distance_high <= -8)
        or (change24 >= 15 and persistence < 0.55)
    )
    if exhausted:
        return "exhaustion"
    if change24 < 8:
        return "ignition"
    if change24 < 25:
        return "acceleration"
    if change24 < 50:
        return "late"
    return "exhaustion"


def score_row(base, ticker, metrics, btc_change):
    trade = float(ticker.get("acc_trade_price_24h") or 0)
    change = float(ticker.get("signed_change_rate") or 0) * 100
    price = float(ticker.get("trade_price") or 0)
    high = float(ticker.get("high_price") or 0)
    distance = pct(price, high) if high else None
    stage = determine_stage(change, metrics, distance)
    volume20 = metrics.get("vol_1h_vs_20h_x") or 0
    persistence = metrics.get("vol_15m_persistence_x") or 0
    p60 = metrics.get("price_last_60m_pct") or 0
    p4 = metrics.get("price_4h_pct") or 0
    wick = metrics.get("upper_wick_1h_pct") or 0
    positives = metrics.get("recent_15m_positive_count") or 0
    higher_low = bool(metrics.get("four_hour_low_rising"))
    relative = change - (btc_change or 0)
    score = min(math.log10(max(trade, 1)) - 8, 3) * 2
    score += min(volume20, 5) * 2
    score += min(persistence, 3) * 2
    score += positives * 1.2
    score += 5 if higher_low else -5
    score += max(min(p60, 5), -5) * 1.2
    score += max(min(p4, 12), -6) * 0.5
    score += max(min(relative, 25), -10) * 0.25
    score -= max(wick - 0.8, 0) * 2
    if stage == "acceleration":
        score += 5
    elif stage == "late":
        score += 1
    elif stage == "exhaustion":
        score -= 14
    if base in MAJOR_LOW_BETA:
        score -= 7
    failure_like = volume20 >= 1.5 and (not higher_low or p60 <= 0)
    success_like = stage in {"ignition", "acceleration"} and higher_low and p60 > 0 and persistence >= 1 and wick <= 1.5
    if failure_like:
        score -= 8
    if success_like:
        score += 6
    return {
        "base": base,
        "bithumb_market": ticker.get("market"),
        "bithumb_krw_price": rnd(price, 8),
        "bithumb_ticker_timestamp_ms": ticker.get("timestamp"),
        "bithumb_24h_trade_krw": rnd(trade, 0),
        "bithumb_24h_change_pct": rnd(change),
        "distance_from_day_high_pct": rnd(distance),
        "btc_relative_strength_24h_pct": rnd(relative),
        "momentum_stage": stage,
        "success_pattern_like": success_like,
        "failure_pattern_like": failure_like,
        "execution_liquidity": trade >= ACTIONABLE_TRADE_KRW,
        "high_beta_target_eligible": base not in MAJOR_LOW_BETA,
        "score": rnd(score),
        **{key: rnd(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else value for key, value in metrics.items()},
    }


def select_candle_universe(markets, tickers, previous):
    ranked = []
    for base, info in markets.items():
        if info.get("market_warning") not in (None, "", "NONE"):
            continue
        ticker = tickers.get(info["market"], {})
        trade = float(ticker.get("acc_trade_price_24h") or 0)
        change = abs(float(ticker.get("signed_change_rate") or 0) * 100)
        ranked.append((trade + change * 100_000_000, base))
    selected = {base for _, base in sorted(ranked, reverse=True)[:MAX_CANDLE_UNIVERSE]}
    selected.update(previous.get("snapshot", {}).keys())
    selected.update({"PROM", "ONT", "BICO", "BIOT", "NESTRY", "PLUME", "CYS"})
    return {base for base in selected if base in markets}


def update_scorecard(history, tickers, candle_cache, generated_at):
    checkpoints = [15, 60, 240, 1440]
    for item in history:
        market = item.get("market")
        ticker = tickers.get(market)
        if not ticker or item.get("closed"):
            continue
        base = item["base"]
        bars = candle_cache.get(base, {}).get("15m", [])
        start_ms = int(datetime.fromisoformat(item["recommended_at_utc"]).timestamp() * 1000)
        after = [bar for bar in bars if bar["ts"] >= start_ms]
        current = float(ticker.get("trade_price") or item["entry_price"])
        highs = [bar["high"] for bar in after] + [current]
        lows = [bar["low"] for bar in after] + [current]
        item["current_return_pct"] = rnd(pct(current, item["entry_price"]))
        item["mfe_pct"] = rnd(pct(max(highs), item["entry_price"]))
        item["mae_pct"] = rnd(pct(min(lows), item["entry_price"]))
        elapsed = (datetime.fromisoformat(generated_at) - datetime.fromisoformat(item["recommended_at_utc"])).total_seconds() / 60
        item["checkpoints"] = item.get("checkpoints", {})
        for minute in checkpoints:
            if elapsed >= minute and str(minute) not in item["checkpoints"]:
                item["checkpoints"][str(minute)] = {
                    "return_pct": item["current_return_pct"],
                    "mfe_pct": item["mfe_pct"],
                    "mae_pct": item["mae_pct"],
                }
        if elapsed >= 1440:
            item["closed"] = True
    return history


def main():
    policy = load_policy()
    previous = load_json(RESULT_FILE, {})
    history = load_json(HISTORY_FILE, [])
    generated_at = datetime.now(timezone.utc).isoformat()
    markets = get_bithumb_markets()
    tickers = get_bithumb_tickers(markets)
    binance_pairs, binance_tickers = get_binance_universe()
    candle_bases = select_candle_universe(markets, tickers, previous)
    btc_ticker = tickers.get("KRW-BTC", {})
    btc_change = float(btc_ticker.get("signed_change_rate") or 0) * 100
    rows, failures, candle_cache = [], [], {}

    for index, base in enumerate(sorted(candle_bases), 1):
        market = markets[base]["market"]
        ticker = tickers.get(market, {})
        symbol = binance_pairs.get(base)
        try:
            if symbol:
                k15 = binance_bars(symbol, "15m", 10)
                k1 = binance_bars(symbol, "1h", 22)
                k4 = binance_bars(symbol, "4h", 8)
                candle_source = "binance_official"
            else:
                k15 = bithumb_bars(market, 15, 100)
                k1 = bithumb_bars(market, 60, 22)
                k4 = bithumb_bars(market, 240, 8)
                candle_source = "bithumb_official"
            candle_cache[base] = {"15m": k15, "1h": k1, "4h": k4}
            metrics = candle_metrics(k15, k1, k4)
            if not metrics:
                raise ValueError("insufficient completed candles")
            row = score_row(base, ticker, metrics, btc_change)
            row["candle_source"] = candle_source
            row["in_binance_bithumb_intersection"] = bool(symbol)
            if symbol:
                bt = binance_tickers.get(symbol, {})
                row["binance_symbol"] = symbol
                row["binance_24h_change_pct"] = rnd(bt.get("priceChangePercent"))
                row["binance_24h_quote_usdt"] = rnd(bt.get("quoteVolume"), 0)
            rows.append(row)
        except Exception as exc:
            failures.append({"base": base, "market": market, "error": str(exc)})
        if index % 20 == 0:
            print(f"{index}/{len(candle_bases)} candidate candles processed")
        time.sleep(0.04)

    rows.sort(key=lambda row: row["score"], reverse=True)
    snapshot = {row["base"]: row for row in rows}
    exhausted = [row for row in rows if row["momentum_stage"] == "exhaustion"][:5]
    acceleration = [row for row in rows if row["momentum_stage"] in {"acceleration", "late"} and not row["failure_pattern_like"]][:5]
    next_rotation = [row for row in rows if row["momentum_stage"] == "ignition" and row["success_pattern_like"]][:5]
    scanned = set(snapshot)
    additional = []
    for base, info in markets.items():
        if base in scanned:
            continue
        ticker = tickers.get(info["market"], {})
        additional.append({
            "base": base,
            "market": info["market"],
            "bithumb_krw_price": ticker.get("trade_price"),
            "bithumb_24h_change_pct": rnd(float(ticker.get("signed_change_rate") or 0) * 100),
            "bithumb_24h_trade_krw": rnd(ticker.get("acc_trade_price_24h"), 0),
            "status": "additional_data_required_not_deleted",
        })
    additional.sort(key=lambda row: row["bithumb_24h_trade_krw"] or 0, reverse=True)
    actual_candidates = [
        row for row in (acceleration + next_rotation)
        if row["execution_liquidity"] and row["high_beta_target_eligible"] and not row["failure_pattern_like"] and row["score"] >= 18
    ]
    actual_pick = actual_candidates[0] if actual_candidates else None
    watch_pick = next((row for row in rows if row is not actual_pick and row["high_beta_target_eligible"] and not row["failure_pattern_like"]), None)

    history = update_scorecard(history, tickers, candle_cache, generated_at)
    if actual_pick:
        last = history[-1] if history else None
        if not last or last.get("base") != actual_pick["base"] or last.get("closed"):
            history.append({
                "base": actual_pick["base"],
                "market": actual_pick["bithumb_market"],
                "recommended_at_utc": generated_at,
                "entry_price": actual_pick["bithumb_krw_price"],
                "policy_version": policy["policy_version"],
                "closed": False,
                "checkpoints": {},
            })

    all_changes = [float(t.get("signed_change_rate") or 0) * 100 for t in tickers.values()]
    output = {
        "generated_at_utc": generated_at,
        "version": "v6-coin-project-policy-driven",
        "policy_version": policy["policy_version"],
        "policy_file": POLICY_FILE,
        "data_sources": {"primary": "user_generated_github_scan", "bithumb": "official_public_market_data", "binance": "official_public_market_data"},
        "universe": {
            "bithumb_krw_total": len(markets),
            "binance_bithumb_intersection_total": len(set(markets) & set(binance_pairs)),
            "candidate_candles_scanned": len(rows),
            "additional_data_required_count": len(additional),
            "failed": len(failures),
        },
        "market_context": {
            "btc_bithumb_24h_change_pct": rnd(btc_change),
            "bithumb_positive_breadth_pct": rnd(sum(1 for value in all_changes if value > 0) / max(len(all_changes), 1) * 100, 1),
        },
        "already_pumped_or_exhausted_top5": exhausted,
        "acceleration_continuation_top5": acceleration,
        "next_rotation_top5": next_rotation,
        "additional_data_required": additional[:30],
        "actual_buy": actual_pick,
        "watch_pick": watch_pick,
        "recommendation_scorecard": history[-20:],
        "snapshot": snapshot,
        "failed_sample": failures[:30],
    }
    summary = {key: output[key] for key in [
        "generated_at_utc", "version", "policy_version", "data_sources", "universe", "market_context",
        "already_pumped_or_exhausted_top5", "acceleration_continuation_top5", "next_rotation_top5",
        "additional_data_required", "actual_buy", "watch_pick", "recommendation_scorecard"
    ]}
    save_json(RESULT_FILE, output)
    save_json(SUMMARY_FILE, summary)
    save_json(HISTORY_FILE, history)
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
