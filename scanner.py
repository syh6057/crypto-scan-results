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
RESULT_FILE = "policy_scan_result.json"
SUMMARY_FILE = "policy_latest_summary.json"
HISTORY_FILE = "policy_recommendation_history.json"
SNAPSHOT_HISTORY_FILE = "policy_snapshot_history.json"
SNAPSHOT_HISTORY_LIMIT = 6
SUPPORTED_POLICY_SCHEMA = 1
SUPPORTED_POLICY_VERSION = "2026-08-28.1"
GDELT_DOC_API = "https://api.gdeltproject.org/api/v2/doc/doc"
RISK_LOOKBACK = "3d"
MIN_MARKET_BREADTH_PCT = 35.0
NEGATIVE_TERMS = [
    "hack", "hacked", "exploit", "breach", "attack", "stolen",
    "delist", "delisting", "suspension", "suspended", "warning",
    "lawsuit", "investigation", "fraud", "scam", "insolvency",
    "shutdown", "closure", "terminate", "termination",
    "token unlock", "unlock", "vesting", "dump", "rug pull",
]
MAX_CANDLE_UNIVERSE = 260
ACTIONABLE_TRADE_KRW = 1_000_000_000
FORCE_SCAN_TRADE_KRW = 300_000_000
MAJOR_LOW_BETA = {"BTC", "ETH", "DOGE", "XRP", "SOL", "BNB", "TRX", "LINK", "LTC", "BCH", "ADA", "XLM", "DOT"}
STABLES = {"USDT", "USDC", "FDUSD", "USDS", "TUSD", "DAI", "PYUSD", "USDP", "USD1", "USDE"}

session = requests.Session()
session.headers.update({"User-Agent": "coin-project-scanner/7.0", "Accept": "application/json"})


def get_json(url, params=None, retries=4, timeout=25):
    error = None
    for attempt in range(retries):
        try:
            r = session.get(url, params=params, timeout=timeout)
            if r.status_code == 429:
                time.sleep(2 + attempt * 2)
                continue
            r.raise_for_status()
            return r.json()
        except Exception as exc:
            error = exc
            time.sleep(1 + attempt)
    raise error


def external_risk_check(base, english_name):
    """Fail closed: any lookup failure or negative event blocks actual_buy."""
    name = (english_name or "").strip()
    identity = f'"{name}"' if name else f'"{base} crypto"'
    negative_query = " OR ".join(f'"{term}"' for term in NEGATIVE_TERMS)
    query = f'{identity} ({negative_query})'
    try:
        payload = get_json(
            GDELT_DOC_API,
            {
                "query": query,
                "mode": "ArtList",
                "maxrecords": 25,
                "timespan": RISK_LOOKBACK,
                "format": "json",
                "sort": "DateDesc",
            },
            retries=3,
            timeout=30,
        )
        articles = payload.get("articles", []) if isinstance(payload, dict) else []
        hits = [
            {
                "title": str(article.get("title") or "").strip(),
                "url": article.get("url"),
                "seen_date": article.get("seendate"),
            }
            for article in articles
            if str(article.get("title") or "").strip()
        ][:10]
        return {
            "status": "negative_found" if hits else "clear",
            "checked": True,
            "buy_allowed": not bool(hits),
            "lookback": RISK_LOOKBACK,
            "query": query,
            "hits": hits,
            "reason": "recent_negative_event_found" if hits else "no_recent_negative_event_found",
        }
    except Exception as exc:
        return {
            "status": "unavailable",
            "checked": False,
            "buy_allowed": False,
            "lookback": RISK_LOOKBACK,
            "query": query,
            "hits": [],
            "reason": "risk_lookup_failed",
            "error": str(exc),
        }


def load_json(path, default):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


def save_json(path, value):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(value, f, ensure_ascii=False, indent=2)


def load_policy():
    p = load_json(POLICY_FILE, None)
    if not p:
        raise RuntimeError("POLICY_MISSING")
    if p.get("schema_version") != SUPPORTED_POLICY_SCHEMA:
        raise RuntimeError("POLICY_SCHEMA_MISMATCH")
    if p.get("policy_version") != SUPPORTED_POLICY_VERSION:
        raise RuntimeError("POLICY_VERSION_MISMATCH")
    return p


def pct(a, b):
    if a is None or b in (None, 0):
        return None
    return (float(a) / float(b) - 1) * 100


def rnd(v, digits=2):
    return None if v is None else round(float(v), digits)


def get_bithumb_markets():
    rows = get_json(BITHUMB_BASE + "/v1/market/all", {"isDetails": "true"})
    out = {}
    for row in rows:
        market = row.get("market", "")
        if market.startswith("KRW-"):
            base = market.split("-", 1)[1]
            out[base] = {
                "market": market,
                "korean_name": row.get("korean_name"),
                "english_name": row.get("english_name"),
                "market_warning": row.get("market_warning", "NONE"),
            }
    return out


def get_bithumb_tickers(markets):
    codes = [v["market"] for v in markets.values()]
    out = {}
    for i in range(0, len(codes), 40):
        rows = get_json(BITHUMB_BASE + "/v1/ticker", {"markets": ",".join(codes[i:i + 40])})
        for row in rows:
            out[row["market"]] = row
        time.sleep(0.06)
    return out


def get_binance_universe():
    info = get_json(BINANCE_BASE + "/api/v3/exchangeInfo")
    tickers = get_json(BINANCE_BASE + "/api/v3/ticker/24hr")
    ticker_map = {r["symbol"]: r for r in tickers if r.get("symbol")}
    pairs = {}
    for row in info.get("symbols", []):
        base = row.get("baseAsset")
        if row.get("status") == "TRADING" and row.get("quoteAsset") == "USDT" and base and base not in STABLES:
            pairs[base] = row.get("symbol")
    return pairs, ticker_map


def binance_bars(symbol, interval, limit):
    rows = get_json(BINANCE_BASE + "/api/v3/klines", {"symbol": symbol, "interval": interval, "limit": limit})
    now_ms = int(time.time() * 1000)
    return [{"ts": int(r[0]), "open": float(r[1]), "high": float(r[2]), "low": float(r[3]), "close": float(r[4]), "volume": float(r[5])} for r in rows if int(r[6]) < now_ms]


def bithumb_bars(market, unit, limit):
    rows = get_json(BITHUMB_BASE + f"/v1/candles/minutes/{unit}", {"market": market, "count": limit})
    now_ms = int(time.time() * 1000)
    interval_ms = unit * 60_000
    out = []
    for row in rows:
        text = row.get("candle_date_time_utc")
        if not text:
            continue
        ts = int(datetime.fromisoformat(text).replace(tzinfo=timezone.utc).timestamp() * 1000)
        if ts + interval_ms > now_ms:
            continue
        out.append({"ts": ts, "open": float(row["opening_price"]), "high": float(row["high_price"]), "low": float(row["low_price"]), "close": float(row["trade_price"]), "volume": float(row["candle_acc_trade_volume"])})
    return sorted(out, key=lambda x: x["ts"])


def candle_metrics(k15, k1, k4):
    if len(k15) < 8 or len(k1) < 21 or len(k4) < 4:
        return None
    latest, previous = k1[-1], k1[-2]
    avg20 = mean(r["volume"] for r in k1[-21:-1])
    old15 = mean(r["volume"] for r in k15[-8:-4])
    new15 = mean(r["volume"] for r in k15[-4:])
    lows = [r["low"] for r in k4[-3:]]
    pos_seq = [1 if r["close"] >= r["open"] else 0 for r in k15[-4:]]
    return {
        "vol_1h_vs_prev_pct": pct(latest["volume"], previous["volume"]),
        "vol_1h_vs_20h_x": latest["volume"] / avg20 if avg20 else None,
        "price_1h_pct": pct(latest["close"], latest["open"]),
        "price_4h_pct": pct(k4[-1]["close"], k4[-1]["open"]),
        "vol_15m_persistence_x": new15 / old15 if old15 else None,
        "price_last_60m_pct": pct(k15[-1]["close"], k15[-4]["open"]),
        "recent_15m_positive_count": sum(pos_seq),
        "positive_15m_sequence": pos_seq,
        "upper_wick_1h_pct": pct(latest["high"], max(latest["open"], latest["close"])),
        "four_hour_low_rising": lows[-1] >= lows[-2] >= lows[-3],
        "four_hour_low_transition": lows[-1] >= lows[-2] and lows[-2] < lows[-3],
        "recent_4h_max_body_pct": max(abs(pct(r["close"], r["open"]) or 0) for r in k4[-3:]),
    }


def recent_rows(snapshot_history, base, count=6):
    out = []
    for snap in snapshot_history[-count:]:
        row = snap.get("snapshot", {}).get(base)
        if row:
            out.append(row)
    return out


SNAPSHOT_HISTORY_FIELDS = (
    "bithumb_krw_price",
    "bithumb_24h_trade_krw",
    "bithumb_24h_change_pct",
    "momentum_stage",
    "future_expansion_score",
    "high_risk_market",
    "vol_1h_vs_20h_x",
    "price_last_60m_pct",
    "recent_15m_positive_count",
)


def compact_snapshot(snapshot):
    """Keep only fields used by cross-run scoring and monitoring.

    Full candidate rows remain in policy_scan_result.json.  Repeating them in
    every history entry made the history file several megabytes large and some
    GitHub readers returned an empty body for it.
    """
    return {
        base: {key: row.get(key) for key in SNAPSHOT_HISTORY_FIELDS if key in row}
        for base, row in snapshot.items()
    }


def compact_snapshot_history(history):
    compacted = []
    for item in history[-SNAPSHOT_HISTORY_LIMIT:]:
        if not isinstance(item, dict):
            continue
        snapshot = item.get("snapshot")
        if not isinstance(snapshot, dict):
            continue
        compacted.append({
            "generated_at_utc": item.get("generated_at_utc"),
            "snapshot": compact_snapshot(snapshot),
        })
    return compacted


def slope(values):
    vals = [float(v) for v in values if v is not None]
    if len(vals) < 2:
        return 0.0
    return vals[-1] - vals[0]


def classify_stage(change24, m, hist, fast):
    p60 = m.get("price_last_60m_pct") or 0
    wick = m.get("upper_wick_1h_pct") or 0
    persistence = m.get("vol_15m_persistence_x") or 0
    positives = m.get("recent_15m_positive_count") or 0
    hl = bool(m.get("four_hour_low_rising"))
    prev_p60 = hist[-1].get("price_last_60m_pct") if hist else None
    turning = prev_p60 is not None and prev_p60 <= 0 < p60
    fast_price = fast.get("price_change_since_scan_pct") or 0
    exhaustion_signals = sum([
        wick >= 1.5,
        persistence < 0.55,
        p60 < -0.5,
        positives <= 1,
        fast_price < -0.5,
    ])
    # Price extension alone is never enough to declare exhaustion.  This keeps
    # the policy's 8-25% acceleration band observable instead of deleting it.
    if change24 >= 50 or (change24 >= 10 and exhaustion_signals >= 2):
        return "exhaustion"
    if 25 <= change24 < 50:
        return "late"
    if 0 <= change24 < 8 and hl and positives >= 2 and p60 >= 0 and (turning or persistence >= 0.8 or fast_price >= 0.4):
        return "pre_ignition"
    if 8 <= change24 < 25 and positives >= 2 and persistence >= 0.8 and (p60 > 0.25 or fast_price >= 0.4):
        return "acceleration"
    if change24 < 25 and p60 > 0.4 and positives >= 2 and persistence >= 1.0:
        return "acceleration"
    return "developing"


def failure_similarity(m, change24):
    volume_spike_no_price = (m.get("vol_1h_vs_20h_x") or 0) >= 1.5 and (m.get("price_last_60m_pct") or 0) <= 0.1
    weak4h = not bool(m.get("four_hour_low_rising"))
    long_wick = (m.get("upper_wick_1h_pct") or 0) >= 1.5
    overheated = change24 >= 12
    no_persistence = (m.get("recent_15m_positive_count") or 0) <= 1 or (m.get("vol_15m_persistence_x") or 0) < 0.55
    flags = [volume_spike_no_price, weak4h, long_wick, overheated, no_persistence]
    return sum(1 for x in flags if x) / len(flags) * 100, {
        "volume_spike_without_price_confirmation": volume_spike_no_price,
        "weak_4h_structure": weak4h,
        "long_upper_wick": long_wick,
        "overheated": overheated,
        "no_15m_persistence": no_persistence,
    }


def twenty_pct_path(krw_price, k4):
    if not krw_price or not k4:
        return {"target_price": None, "candle_reference_price": None, "resistance_levels_pct": [], "path_open": False}
    reference_price = float(k4[-1]["close"])
    reference_target = reference_price * 1.2
    highs = sorted({float(r["high"]) for r in k4[-8:] if reference_price < float(r["high"]) < reference_target})
    resistance_pcts = sorted({round((high / reference_price - 1) * 100, 2) for high in highs})
    path_open = len(resistance_pcts) <= 3
    return {
        "target_price": rnd(krw_price * 1.2, 8),
        "candle_reference_price": rnd(reference_price, 8),
        "resistance_levels_pct": resistance_pcts[:5],
        "path_open": path_open,
    }


def future_expansion_score(base, ticker, m, hist, path, success_reference, failure_reference, fast):
    trade = float(ticker.get("acc_trade_price_24h") or 0)
    change = float(ticker.get("signed_change_rate") or 0) * 100
    p60 = m.get("price_last_60m_pct") or 0
    positives = m.get("recent_15m_positive_count") or 0
    persistence = m.get("vol_15m_persistence_x") or 0
    wick = m.get("upper_wick_1h_pct") or 0
    hl = bool(m.get("four_hour_low_rising"))
    transition = bool(m.get("four_hour_low_transition"))
    hist_p60 = [r.get("price_last_60m_pct") for r in hist] + [p60]
    hist_pos = [r.get("recent_15m_positive_count") for r in hist] + [positives]
    hist_vol = [r.get("vol_1h_vs_20h_x") for r in hist] + [m.get("vol_1h_vs_20h_x")]
    fes = 0.0
    fes += 18 if hl else -18
    fes += 8 if transition else 0
    fes += positives * 4
    fes += max(min(slope(hist_pos), 3), -3) * 4
    fes += max(min(p60, 3), -3) * 6
    if len(hist_p60) >= 2 and hist_p60[-2] is not None and hist_p60[-2] <= 0 < p60:
        fes += 12
    fes += max(min(slope(hist_vol), 2), -2) * 5
    fes += min(persistence, 3) * 4
    fes += max(6 - abs(change), -6) if -2 <= change <= 6 else -abs(change - 4) * 0.8
    fes += 5 if wick <= 0.6 else -min(wick, 3) * 3
    fes += min(math.log10(max(trade, 1)) - 8, 3) * 2
    fes += 6 if path.get("path_open") else -6
    # Full-market scan-to-scan change is the fast path.  Completed 15m/1h bars
    # remain structural confirmation, but a new mover no longer waits for them.
    fast_price = fast.get("price_change_since_scan_pct") or 0
    fast_change = fast.get("change24_delta_since_scan_pct") or 0
    fast_trade = fast.get("trade_value_delta_since_scan_krw") or 0
    fes += max(min(fast_price, 3), -3) * 8
    fes += max(min(fast_change, 3), -3) * 4
    if fast_trade >= 500_000_000:
        fes += 10
    elif fast_trade >= 100_000_000:
        fes += 5
    if base in MAJOR_LOW_BETA:
        fes -= 12
    if base in success_reference:
        fes += 8
    if base in failure_reference:
        fes -= 30
    failure, _ = failure_similarity(m, change)
    fes -= failure * 0.22
    return rnd(fes), failure


def market_snapshot(markets, tickers, generated_at):
    snapshot = {}
    for base, info in markets.items():
        t = tickers.get(info["market"], {})
        snapshot[base] = {
            "market": info["market"],
            "price": rnd(t.get("trade_price"), 8),
            "change24_pct": rnd(float(t.get("signed_change_rate") or 0) * 100),
            "trade24_krw": rnd(t.get("acc_trade_price_24h"), 0),
        }
    return {"generated_at_utc": generated_at, "tickers": snapshot}


def fast_market_metrics(base, current_market, previous_market):
    current = current_market.get("tickers", {}).get(base, {})
    previous = previous_market.get("tickers", {}).get(base, {})
    current_trade = float(current.get("trade24_krw") or 0)
    previous_trade = float(previous.get("trade24_krw") or 0)
    return {
        "price_change_since_scan_pct": rnd(pct(current.get("price"), previous.get("price"))),
        "change24_delta_since_scan_pct": rnd((current.get("change24_pct") or 0) - (previous.get("change24_pct") or 0)) if previous else None,
        "trade_value_delta_since_scan_krw": rnd(current_trade - previous_trade, 0) if previous else None,
        "previous_market_scan_utc": previous_market.get("generated_at_utc") if previous else None,
    }


def select_candle_universe(markets, tickers, current_market, previous_market):
    ranked = []
    forced = set()
    for base, info in markets.items():
        t = tickers.get(info["market"], {})
        trade = float(t.get("acc_trade_price_24h") or 0)
        signed_change = float(t.get("signed_change_rate") or 0) * 100
        change = abs(signed_change)
        fast = fast_market_metrics(base, current_market, previous_market)
        fast_price = fast.get("price_change_since_scan_pct") or 0
        fast_change = fast.get("change24_delta_since_scan_pct") or 0
        fast_trade = fast.get("trade_value_delta_since_scan_krw") or 0
        ignition_bonus = 2_000_000_000 if 0 <= signed_change < 8 else 0
        acceleration_bonus = 4_000_000_000 if 8 <= signed_change < 25 else 0
        fast_bonus = max(fast_price, 0) * 2_000_000_000 + max(fast_change, 0) * 1_000_000_000
        fast_trade_bonus = max(fast_trade, 0) * 4
        ranked.append((trade + change * 100_000_000 + ignition_bonus + acceleration_bonus + fast_bonus + fast_trade_bonus, base))
        # A CUDIS-type candidate (+9%, sub-1bn turnover) must receive candles.
        if FORCE_SCAN_TRADE_KRW <= trade and 4 <= signed_change < 25:
            forced.add(base)
        if fast_price >= 0.5 or fast_change >= 0.5 or fast_trade >= 100_000_000:
            forced.add(base)
    selected = {b for _, b in sorted(ranked, reverse=True)[:MAX_CANDLE_UNIVERSE]}
    selected.update(forced)
    selected.update({"HOME", "PROM", "BICO", "TAO", "BIO", "SUI", "ONT", "TRX", "BANK", "ACE", "ME", "INIT"})
    return {b for b in selected if b in markets}


def update_scorecard(history, tickers, candle_cache, generated_at):
    checkpoints = [60, 180, 360, 720, 1440]
    for item in history:
        ticker = tickers.get(item.get("market"))
        if not ticker or item.get("closed"):
            continue
        bars = candle_cache.get(item["base"], {}).get("15m", [])
        start_ms = int(datetime.fromisoformat(item["recommended_at_utc"]).timestamp() * 1000)
        after = [b for b in bars if b["ts"] >= start_ms]
        current = float(ticker.get("trade_price") or item["entry_price"])
        current_return = rnd(pct(current, item["entry_price"]))
        # Never compare Binance USDT candles with a Bithumb KRW entry.  Candle
        # excursions are measured from a candle-native reference and merged
        # with the synchronized KRW current return only as percentages.
        candle_entry = item.get("candle_entry_price")
        if candle_entry is None and after:
            candle_entry = after[0]["open"]
            item["candle_entry_price"] = rnd(candle_entry, 8)
        if candle_entry and after:
            candle_mfe = pct(max(b["high"] for b in after), candle_entry)
            candle_mae = pct(min(b["low"] for b in after), candle_entry)
            item["mfe_pct"] = rnd(max(current_return, candle_mfe))
            item["mae_pct"] = rnd(min(current_return, candle_mae))
        else:
            item["mfe_pct"] = current_return
            item["mae_pct"] = current_return
        item["current_return_pct"] = current_return
        elapsed = (datetime.fromisoformat(generated_at) - datetime.fromisoformat(item["recommended_at_utc"])).total_seconds() / 60
        item.setdefault("checkpoints", {})
        # Quarantine legacy scorecard values created by cross-currency candle
        # comparisons.  They must never flow into ranking or user reports.
        invalid = item.setdefault("invalid_checkpoints", {})
        for key in list(invalid):
            invalid[key] = {"reason": "legacy_unit_mismatch_quarantined"}
        for key, value in list(item["checkpoints"].items()):
            values = [value.get("return_pct"), value.get("mfe_pct"), value.get("mae_pct")]
            if any(v is not None and abs(float(v)) >= 50 for v in values):
                invalid[key] = {"reason": "legacy_unit_mismatch_quarantined"}
                del item["checkpoints"][key]
        if not invalid:
            item.pop("invalid_checkpoints", None)
        for minute in checkpoints:
            if elapsed >= minute and str(minute) not in item["checkpoints"]:
                item["checkpoints"][str(minute)] = {"return_pct": item["current_return_pct"], "mfe_pct": item["mfe_pct"], "mae_pct": item["mae_pct"]}
        if elapsed >= 1440:
            item["closed"] = True
    return history



def recommendation_feedback_adjustment(base, history):
    """Feed realized recommendation quality back into future ranking.

    Only evaluated checkpoints count, so a newly-created recommendation cannot
    penalize or reward itself. Loss/MAE penalties are applied before any bonus.
    """
    adjustment = 0.0
    for item in history[-30:]:
        if item.get("base") != base or not item.get("checkpoints"):
            continue
        current = float(item.get("current_return_pct") or 0)
        mfe = float(item.get("mfe_pct") or 0)
        mae = float(item.get("mae_pct") or 0)
        if current < 0:
            adjustment -= min(15.0, -current * 2.0)
        if mae < -3:
            adjustment -= min(10.0, (-mae - 3) * 1.5)
        if mfe < 1:
            adjustment -= 3.0
        if current >= 3 and mfe >= 5 and mae > -3:
            adjustment += min(8.0, current * 0.5)
    return rnd(max(-30.0, min(8.0, adjustment)))


def main():
    policy = load_policy()
    success_reference = set(policy.get("reference_tickers", {}).get("success", []))
    failure_reference = set(policy.get("reference_tickers", {}).get("failure", []))
    previous = load_json(RESULT_FILE, {})
    history = load_json(HISTORY_FILE, [])
    snapshot_history = compact_snapshot_history(load_json(SNAPSHOT_HISTORY_FILE, []))
    generated_at = datetime.now(timezone.utc).isoformat()
    markets = get_bithumb_markets()
    tickers = get_bithumb_tickers(markets)
    current_market = market_snapshot(markets, tickers, generated_at)
    previous_market = previous.get("market_snapshot", {})
    binance_pairs, binance_tickers = get_binance_universe()
    candle_bases = select_candle_universe(markets, tickers, current_market, previous_market)
    btc_ticker = tickers.get("KRW-BTC", {})
    btc_change = float(btc_ticker.get("signed_change_rate") or 0) * 100
    rows, failures, candle_cache = [], [], {}

    for index, base in enumerate(sorted(candle_bases), 1):
        market = markets[base]["market"]
        ticker = tickers.get(market, {})
        symbol = binance_pairs.get(base)
        try:
            if symbol:
                k15 = binance_bars(symbol, "15m", 12)
                k1 = binance_bars(symbol, "1h", 22)
                k4 = binance_bars(symbol, "4h", 10)
                candle_source = "binance_official"
            else:
                k15 = bithumb_bars(market, 15, 100)
                k1 = bithumb_bars(market, 60, 22)
                k4 = bithumb_bars(market, 240, 10)
                candle_source = "bithumb_official"
            candle_cache[base] = {"15m": k15, "1h": k1, "4h": k4}
            m = candle_metrics(k15, k1, k4)
            if not m:
                raise ValueError("insufficient completed candles")
            price = float(ticker.get("trade_price") or 0)
            trade = float(ticker.get("acc_trade_price_24h") or 0)
            change = float(ticker.get("signed_change_rate") or 0) * 100
            fast = fast_market_metrics(base, current_market, previous_market)
            hist = recent_rows(snapshot_history, base, 6)
            path = twenty_pct_path(price, k4)
            fes, failure = future_expansion_score(base, ticker, m, hist, path, success_reference, failure_reference, fast)
            feedback_adjustment = recommendation_feedback_adjustment(base, history)
            fes = rnd(fes + feedback_adjustment)
            market_warning = markets[base].get("market_warning", "NONE")
            if market_warning not in (None, "", "NONE"):
                fes = rnd(fes - 8)
            failure_score, failure_flags = failure_similarity(m, change)
            stage = classify_stage(change, m, hist, fast)
            row = {
                "base": base,
                "bithumb_market": market,
                "bithumb_krw_price": rnd(price, 8),
                "bithumb_24h_trade_krw": rnd(trade, 0),
                "bithumb_24h_change_pct": rnd(change),
                "btc_relative_strength_24h_pct": rnd(change - btc_change),
                "momentum_stage": stage,
                "future_expansion_score": fes,
                "failure_similarity_score": rnd(failure_score),
                "failure_flags": failure_flags,
                "reference_success_pattern": base in success_reference,
                "reference_failure_pattern": base in failure_reference,
                "recommendation_feedback_adjustment": feedback_adjustment,
                "market_warning": market_warning,
                "high_risk_market": market_warning not in (None, "", "NONE"),
                "twenty_pct_path": path,
                "execution_liquidity": trade >= ACTIONABLE_TRADE_KRW,
                "high_beta_target_eligible": base not in MAJOR_LOW_BETA,
                "candle_source": candle_source,
                "in_binance_bithumb_intersection": bool(symbol),
                **fast,
                **{k: rnd(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else v for k, v in m.items()},
            }
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

    rows.sort(key=lambda r: r["future_expansion_score"], reverse=True)
    snapshot = {r["base"]: r for r in rows}
    pre = [r for r in rows if r["momentum_stage"] == "pre_ignition" and r["failure_similarity_score"] < 60 and not r["reference_failure_pattern"]][:5]
    accel = [r for r in rows if r["momentum_stage"] == "acceleration" and r["failure_similarity_score"] < 60 and not r["reference_failure_pattern"]][:5]
    late = [r for r in rows if r["momentum_stage"] == "late" and r["failure_similarity_score"] < 60 and not r["reference_failure_pattern"]][:5]
    exhausted = [r for r in rows if r["momentum_stage"] == "exhaustion"][:10]
    path_candidates = [r for r in rows if r["twenty_pct_path"]["path_open"] and r["momentum_stage"] in {"pre_ignition", "acceleration"} and r["failure_similarity_score"] < 60 and not r["reference_failure_pattern"]][:10]
    fast_breakout = [r for r in rows if r["momentum_stage"] in {"pre_ignition", "acceleration"} and ((r.get("price_change_since_scan_pct") or 0) >= 0.8 or (r.get("change24_delta_since_scan_pct") or 0) >= 0.8) and not r["reference_failure_pattern"]][:10]

    scanned = set(snapshot)
    additional = []
    for base, info in markets.items():
        if base in scanned:
            continue
        t = tickers.get(info["market"], {})
        additional.append({"base": base, "market": info["market"], "bithumb_krw_price": t.get("trade_price"), "bithumb_24h_change_pct": rnd(float(t.get("signed_change_rate") or 0) * 100), "bithumb_24h_trade_krw": rnd(t.get("acc_trade_price_24h"), 0), "status": "additional_data_required_not_deleted"})
    additional.sort(key=lambda r: r["bithumb_24h_trade_krw"] or 0, reverse=True)

    def volume_confirmed(r):
        return (r.get("vol_1h_vs_20h_x") or 0) >= 1.0 or (r.get("trade_value_delta_since_scan_krw") or 0) >= 100_000_000

    def not_fading(r):
        fast = r.get("price_change_since_scan_pct")
        return fast is None or fast >= 0

    breadth_pct = rnd(sum(1 for t in tickers.values() if float(t.get("signed_change_rate") or 0) > 0) / max(len(tickers), 1) * 100, 1)

    # Full technical hard gates. Missing data is a failure, never an implicit pass.
    technical_candidates = [
        r for r in path_candidates
        if breadth_pct is not None and breadth_pct >= MIN_MARKET_BREADTH_PCT
        and r.get("execution_liquidity") is True
        and r.get("high_beta_target_eligible") is True
        and r.get("market_warning") in (None, "", "NONE")
        and r.get("reference_failure_pattern") is False
        and r.get("failure_similarity_score") is not None and r["failure_similarity_score"] < 40
        and r.get("bithumb_24h_change_pct") is not None and 0 <= r["bithumb_24h_change_pct"] <= 5
        and r.get("binance_24h_change_pct") is not None and 0 <= r["binance_24h_change_pct"] <= 8
        and r.get("four_hour_low_rising") is True
        and r.get("vol_15m_persistence_x") is not None and r["vol_15m_persistence_x"] >= 1.2
        and r.get("vol_1h_vs_20h_x") is not None and r["vol_1h_vs_20h_x"] >= 1.2
        and r.get("price_last_60m_pct") is not None and 0 <= r["price_last_60m_pct"] <= 2
        and r.get("upper_wick_1h_pct") is not None and r["upper_wick_1h_pct"] <= 1.5
        and r.get("recent_4h_max_body_pct") is not None and r["recent_4h_max_body_pct"] <= 4
        and r.get("future_expansion_score") is not None and r["future_expansion_score"] >= 35
        and volume_confirmed(r)
        and not_fading(r)
    ]

    # Risk verification is applied only after every technical gate passes.
    # It is deliberately fail-closed: unavailable/negative/not-checked => no buy.
    risk_scan = {
        "provider": "GDELT_DOC_API",
        "lookback": RISK_LOOKBACK,
        "fail_closed": True,
        "market_breadth_required_pct": MIN_MARKET_BREADTH_PCT,
        "market_breadth_actual_pct": breadth_pct,
        "checked": [],
    }
    blocked_buy_candidates = []
    verified_candidates = []
    for candidate in technical_candidates[:10]:
        risk = external_risk_check(
            candidate["base"],
            markets.get(candidate["base"], {}).get("english_name"),
        )
        candidate["risk_verification"] = risk
        candidate["buy_alert_allowed"] = bool(risk.get("checked") and risk.get("buy_allowed"))
        risk_scan["checked"].append({
            "base": candidate["base"],
            "status": risk.get("status"),
            "buy_allowed": candidate["buy_alert_allowed"],
            "reason": risk.get("reason"),
        })
        if candidate["buy_alert_allowed"]:
            verified_candidates.append(candidate)
        else:
            blocked_buy_candidates.append({
                "base": candidate["base"],
                "reason": risk.get("reason"),
                "risk_verification": risk,
            })
        time.sleep(0.15)

    actual_pick = verified_candidates[0] if verified_candidates else None
    watch_pick = next(
        (
            r for r in path_candidates
            if r is not actual_pick
            and r.get("bithumb_24h_trade_krw", 0) >= 100_000_000
            and r.get("failure_similarity_score", 100) < 40
            and r.get("market_warning") in (None, "", "NONE")
            and volume_confirmed(r)
            and not_fading(r)
        ),
        None,
    )

    history = update_scorecard(history, tickers, candle_cache, generated_at)
    if actual_pick:
        last = history[-1] if history else None
        if not last or last.get("base") != actual_pick["base"] or last.get("closed"):
            pick_bars = candle_cache.get(actual_pick["base"], {}).get("15m", [])
            history.append({"base": actual_pick["base"], "market": actual_pick["bithumb_market"], "recommended_at_utc": generated_at, "entry_price": actual_pick["bithumb_krw_price"], "candle_source": actual_pick.get("candle_source"), "candle_entry_price": rnd(pick_bars[-1]["close"], 8) if pick_bars else None, "policy_version": policy["policy_version"], "closed": False, "checkpoints": {}})

    all_changes = [float(t.get("signed_change_rate") or 0) * 100 for t in tickers.values()]
    output = {
        "generated_at_utc": generated_at,
        "schema_version": policy["schema_version"],
        "version": "v9-negative-event-fail-closed",
        "policy_version": policy["policy_version"],
        "policy_file": POLICY_FILE,
        "universe": {"bithumb_krw_total": len(markets), "binance_bithumb_intersection_total": len(set(markets) & set(binance_pairs)), "candidate_candles_scanned": len(rows), "additional_data_required_count": len(additional), "failed": len(failures)},
        "market_regime": {"btc_bithumb_24h_change_pct": rnd(btc_change), "bithumb_positive_breadth_pct": rnd(sum(1 for v in all_changes if v > 0) / max(len(all_changes), 1) * 100, 1)},
        "pre_ignition_top5": pre,
        "acceleration_top3": accel,
        "acceleration_top5": accel,
        "late_top5": late,
        "exhaustion_no_chase": exhausted,
        "fast_breakout_alerts": fast_breakout,
        "twenty_pct_path_candidates": path_candidates,
        "additional_data_required": additional[:30],
        "actual_buy": actual_pick,
        "watch_pick": watch_pick,
        "risk_scan": risk_scan,
        "blocked_buy_candidates": blocked_buy_candidates,
        "recommendation_scorecard": history[-20:],
        "market_snapshot": current_market,
        "snapshot": snapshot,
        "failed_sample": failures[:30],
    }
    summary_keys = ["generated_at_utc", "schema_version", "version", "policy_version", "universe", "market_regime", "pre_ignition_top5", "acceleration_top5", "late_top5", "exhaustion_no_chase", "fast_breakout_alerts", "twenty_pct_path_candidates", "additional_data_required", "actual_buy", "watch_pick", "risk_scan", "blocked_buy_candidates", "recommendation_scorecard"]
    summary = {k: output[k] for k in summary_keys}
    save_json(RESULT_FILE, output)
    save_json(SUMMARY_FILE, summary)
    save_json(HISTORY_FILE, history)
    snapshot_history.append({"generated_at_utc": generated_at, "snapshot": compact_snapshot(snapshot)})
    with open(SNAPSHOT_HISTORY_FILE, "w", encoding="utf-8") as f:
        json.dump(snapshot_history[-SNAPSHOT_HISTORY_LIMIT:], f, ensure_ascii=False, separators=(",", ":"))
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
