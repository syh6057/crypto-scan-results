import json
import time
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone

BINANCE = "https://data-api.binance.vision"
BITHUMB = "https://api.bithumb.com"
USER_AGENT = "crypto-scan-results/1.0"


def get_json(url, timeout=20):
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def f(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0


def pct(new, old):
    return ((new / old) - 1.0) * 100.0 if old else 0.0


def closed_klines(symbol, interval, limit):
    query = urllib.parse.urlencode(
        {"symbol": symbol, "interval": interval, "limit": limit + 2}
    )
    rows = get_json(f"{BINANCE}/api/v3/klines?{query}")
    now_ms = int(time.time() * 1000)
    closed = [row for row in rows if int(row[6]) < now_ms]
    return closed[-limit:]


def get_bithumb_markets():
    rows = get_json(f"{BITHUMB}/v1/market/all?isDetails=true")
    result = {}
    for row in rows:
        market = row.get("market", "")
        if market.startswith("KRW-"):
            result[market[4:]] = row
    return result


def get_bithumb_tickers(symbols):
    result = {}
    symbols = list(symbols)

    # Bithumb v1 ticker accepts comma-separated market codes.
    for i in range(0, len(symbols), 80):
        chunk = symbols[i:i + 80]
        markets = ",".join(f"KRW-{s}" for s in chunk)
        query = urllib.parse.urlencode({"markets": markets})
        try:
            rows = get_json(f"{BITHUMB}/v1/ticker?{query}")
        except Exception:
            continue

        if isinstance(rows, list):
            for row in rows:
                market = row.get("market", "")
                if market.startswith("KRW-"):
                    result[market[4:]] = row
    return result


def load_previous():
    try:
        with open("scan_result.json", "r", encoding="utf-8") as fp:
            data = json.load(fp)
            return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def scan_one(base, pair, bithumb_ticker, binance_24h, btc_1h_pct, previous_row):
    k1 = closed_klines(pair, "1h", 21)
    k4 = closed_klines(pair, "4h", 2)
    k15 = closed_klines(pair, "15m", 12)

    if len(k1) < 21 or len(k4) < 1 or len(k15) < 8:
        raise RuntimeError("not enough closed candles")

    cur = k1[-1]
    prev = k1[-2]
    hist20 = k1[-21:-1]

    cur_vol = f(cur[5])
    prev_vol = f(prev[5])
    avg20 = sum(f(x[5]) for x in hist20) / len(hist20)

    vol_vs_prev_pct = pct(cur_vol, prev_vol)
    vol_vs_20h_x = cur_vol / avg20 if avg20 else 0.0
    price_1h_pct = pct(f(cur[4]), f(cur[1]))
    price_4h_pct = pct(f(k4[-1][4]), f(k4[-1][1]))

    last15_vol = f(k15[-1][5])
    prev4_15m_avg = sum(f(x[5]) for x in k15[-5:-1]) / 4.0
    vol_15m_persistence_x = last15_vol / prev4_15m_avg if prev4_15m_avg else 0.0

    btc_relative_1h_pct = price_1h_pct - btc_1h_pct

    b24 = binance_24h or {}
    binance_price = f(b24.get("lastPrice"))
    binance_quote_volume = f(b24.get("quoteVolume"))
    binance_24h_change_pct = f(b24.get("priceChangePercent"))
    binance_high = f(b24.get("highPrice"))
    distance_from_24h_high_pct = pct(binance_price, binance_high) if binance_high else 0.0

    bt = bithumb_ticker or {}
    bithumb_price_krw = f(bt.get("trade_price"))
    bithumb_24h_trade_value_krw = f(bt.get("acc_trade_price_24h"))
    bithumb_high = f(bt.get("high_price"))
    bithumb_prev_close = f(bt.get("prev_closing_price"))
    bithumb_day_high_pct = pct(bithumb_high, bithumb_prev_close) if bithumb_prev_close else 0.0

    overheated = (
        binance_24h_change_pct >= 10.0
        or bithumb_day_high_pct >= 10.0
        or price_1h_pct >= 6.0
    )

    prev_vol20 = f(previous_row.get("vol_vs_20h_x")) if previous_row else 0.0
    prev_rel = f(previous_row.get("btc_relative_1h_pct")) if previous_row else 0.0
    repeated_confirmation = bool(
        previous_row
        and prev_vol20 >= 1.2
        and vol_vs_20h_x >= 1.2
        and prev_rel > -1.0
        and btc_relative_1h_pct > -1.0
    )

    # Recovery-focused score:
    # reward volume leading price, persistence, relative strength, and repeated confirmation.
    score = 0.0
    score += min(max(vol_vs_20h_x - 1.0, 0.0), 5.0) * 3.0
    score += min(max(vol_vs_prev_pct, 0.0) / 100.0, 5.0) * 1.5
    score += min(max(vol_15m_persistence_x - 1.0, 0.0), 4.0) * 1.5
    score += max(min(btc_relative_1h_pct, 4.0), -4.0) * 0.8

    if -1.0 <= price_1h_pct <= 4.0:
        score += 3.0
    elif 4.0 < price_1h_pct <= 6.0:
        score += 1.0
    elif price_1h_pct > 6.0:
        score -= 4.0

    if repeated_confirmation:
        score += 3.0

    if overheated:
        score -= 6.0

    if binance_quote_volume < 1_000_000:
        score -= 1.5

    if score >= 10.0 and not overheated and vol_vs_20h_x >= 1.5:
        grade = "A"
    elif score >= 5.0:
        grade = "B"
    else:
        grade = "C"

    previous_summary = None
    if previous_row:
        previous_summary = {
            "score": previous_row.get("score"),
            "grade": previous_row.get("grade"),
            "vol_vs_20h_x": previous_row.get("vol_vs_20h_x"),
            "vol_vs_prev_pct": previous_row.get("vol_vs_prev_pct"),
            "price_1h_pct": previous_row.get("price_1h_pct"),
            "btc_relative_1h_pct": previous_row.get("btc_relative_1h_pct"),
        }

    return {
        "symbol": base,
        "pair": pair,
        "bithumb_market": f"KRW-{base}",
        "bithumb_price_krw": bithumb_price_krw,
        "bithumb_24h_trade_value_krw": bithumb_24h_trade_value_krw,
        "binance_price_usdt": binance_price,
        "binance_24h_quote_volume_usdt": binance_quote_volume,
        "vol_vs_prev_pct": round(vol_vs_prev_pct, 2),
        "vol_vs_20h_x": round(vol_vs_20h_x, 3),
        "price_1h_pct": round(price_1h_pct, 3),
        "price_4h_pct": round(price_4h_pct, 3),
        "vol_15m_persistence_x": round(vol_15m_persistence_x, 3),
        "btc_relative_1h_pct": round(btc_relative_1h_pct, 3),
        "binance_24h_change_pct": round(binance_24h_change_pct, 3),
        "bithumb_day_high_pct": round(bithumb_day_high_pct, 3),
        "distance_from_24h_high_pct": round(distance_from_24h_high_pct, 3),
        "repeated_confirmation": repeated_confirmation,
        "overheated": overheated,
        "score": round(score, 3),
        "grade": grade,
        "previous": previous_summary,
    }


def main():
    previous = load_previous()
    previous_generated_at_utc = previous.get("generated_at_utc")

    previous_map = {}
    for row in previous.get("all_candidates", []):
        if isinstance(row, dict) and row.get("symbol"):
            previous_map[row["symbol"]] = row

    bithumb_markets = get_bithumb_markets()

    exchange_info = get_json(f"{BINANCE}/api/v3/exchangeInfo")
    binance_spot = {}
    for row in exchange_info.get("symbols", []):
        if (
            row.get("status") == "TRADING"
            and row.get("quoteAsset") == "USDT"
            and row.get("isSpotTradingAllowed") is True
        ):
            binance_spot[row["baseAsset"]] = row["symbol"]

    symbols = sorted(set(bithumb_markets).intersection(binance_spot))
    bithumb_tickers = get_bithumb_tickers(symbols)

    all_24h = get_json(f"{BINANCE}/api/v3/ticker/24hr")
    allowed_pairs = set(binance_spot.values())
    binance_24h = {
        row["symbol"]: row
        for row in all_24h
        if row.get("symbol") in allowed_pairs
    }

    btc = closed_klines("BTCUSDT", "1h", 2)
    btc_1h_pct = pct(f(btc[-1][4]), f(btc[-1][1])) if btc else 0.0

    candidates = []
    errors = []

    def worker(base):
        pair = binance_spot[base]
        return scan_one(
            base=base,
            pair=pair,
            bithumb_ticker=bithumb_tickers.get(base, {}),
            binance_24h=binance_24h.get(pair, {}),
            btc_1h_pct=btc_1h_pct,
            previous_row=previous_map.get(base, {}),
        )

    # Conservative concurrency to stay comfortably inside public API rate limits.
    with ThreadPoolExecutor(max_workers=8) as pool:
        future_map = {pool.submit(worker, base): base for base in symbols}
        for future in as_completed(future_map):
            base = future_map[future]
            try:
                candidates.append(future.result())
            except Exception as exc:
                errors.append({"symbol": base, "error": str(exc)[:200]})

    candidates.sort(key=lambda x: x["score"], reverse=True)
    clean = [x for x in candidates if not x["overheated"]]

    result = {
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "version": "v4-hourly-recovery-focus",
        "previous_generated_at_utc": previous_generated_at_utc,
        "data_sources": {
            "binance": "official_public_market_data",
            "bithumb_market_list": "live",
            "bithumb_ticker": "live",
        },
        "scan_scope": {
            "bithumb_binance_spot_intersection_count": len(symbols),
            "success_count": len(candidates),
            "error_count": len(errors),
        },
        "btc_1h_pct": round(btc_1h_pct, 3),
        "top5": candidates[:5],
        "clean_top10": clean[:10],
        "all_candidates": candidates,
        "errors": errors[:100],
    }

    with open("scan_result.json", "w", encoding="utf-8") as fp:
        json.dump(result, fp, ensure_ascii=False, indent=2)

    print(
        json.dumps(
            {
                "generated_at_utc": result["generated_at_utc"],
                "scope": result["scan_scope"],
                "top5": [
                    {
                        "symbol": x["symbol"],
                        "score": x["score"],
                        "grade": x["grade"],
                        "vol_vs_20h_x": x["vol_vs_20h_x"],
                        "price_1h_pct": x["price_1h_pct"],
                    }
                    for x in result["top5"]
                ],
            },
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    main()
