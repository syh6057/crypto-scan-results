import json
import math
import os
import time
from copy import deepcopy
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
SUPPORTED_POLICY_VERSION = "2026-09-02.3"
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
PROBE_TRADE_KRW = 300_000_000
FORCE_SCAN_TRADE_KRW = 300_000_000
SIGNAL_STABILITY_RUNS = 3
WATCH_STABILITY_RUNS = 2
TRACKING_MINUTES = (15, 60, 240, 1440)
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
    return [{
        "ts": int(r[0]),
        "open": float(r[1]),
        "high": float(r[2]),
        "low": float(r[3]),
        "close": float(r[4]),
        "volume": float(r[5]),
        "quote_volume": float(r[7]),
    } for r in rows if int(r[6]) < now_ms]


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
        out.append({
            "ts": ts,
            "open": float(row["opening_price"]),
            "high": float(row["high_price"]),
            "low": float(row["low_price"]),
            "close": float(row["trade_price"]),
            "volume": float(row["candle_acc_trade_volume"]),
            "quote_volume": float(row.get("candle_acc_trade_price") or 0),
        })
    return sorted(out, key=lambda x: x["ts"])


def candle_metrics(k15, k1, k4):
    if len(k15) < 8 or len(k1) < 21 or len(k4) < 4:
        return None
    latest, previous = k1[-1], k1[-2]
    avg20 = mean(r["volume"] for r in k1[-21:-1])
    old15 = mean(r["volume"] for r in k15[-8:-4])
    new15 = mean(r["volume"] for r in k15[-4:])
    old15_turnover = mean(r.get("quote_volume", 0) for r in k15[-8:-4])
    new15_turnover = mean(r.get("quote_volume", 0) for r in k15[-4:])
    avg20_turnover = mean(r.get("quote_volume", 0) for r in k1[-21:-1])
    lows = [r["low"] for r in k4[-3:]]
    pos_seq = [1 if r["close"] >= r["open"] else 0 for r in k15[-4:]]
    return {
        "vol_1h_vs_prev_pct": pct(latest["volume"], previous["volume"]),
        "vol_1h_vs_20h_x": latest["volume"] / avg20 if avg20 else None,
        "price_1h_pct": pct(latest["close"], latest["open"]),
        "price_4h_pct": pct(k4[-1]["close"], k4[-1]["open"]),
        "vol_15m_persistence_x": new15 / old15 if old15 else None,
        "turnover_15m_persistence_x": new15_turnover / old15_turnover if old15_turnover else None,
        "turnover_1h_vs_20h_x": latest.get("quote_volume", 0) / avg20_turnover if avg20_turnover else None,
        "price_last_60m_pct": pct(k15[-1]["close"], k15[-4]["open"]),
        "recent_15m_positive_count": sum(pos_seq),
        "positive_15m_sequence": pos_seq,
        "upper_wick_1h_pct": pct(latest["high"], max(latest["open"], latest["close"])),
        "four_hour_low_rising": lows[-1] >= lows[-2] >= lows[-3],
        "four_hour_low_transition": lows[-1] >= lows[-2] and lows[-2] < lows[-3],
        "recent_4h_max_body_pct": max(abs(pct(r["close"], r["open"]) or 0) for r in k4[-3:]),
    }


def recent_rows(snapshot_history, base, count=6, current_time=None):
    out = []
    for snap in snapshot_history[-count:]:
        if current_time and snap.get("generated_at_utc"):
            try:
                age_minutes = (
                    datetime.fromisoformat(current_time)
                    - datetime.fromisoformat(snap["generated_at_utc"])
                ).total_seconds() / 60
                if age_minutes < 0 or age_minutes > 30:
                    continue
            except Exception:
                continue
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
    "turnover_1h_vs_20h_x",
    "turnover_15m_persistence_x",
    "price_last_60m_pct",
    "recent_15m_positive_count",
    "failure_similarity_score",
    "four_hour_low_rising",
    "upper_wick_1h_pct",
    "price_change_since_scan_pct",
    "trade_value_delta_since_scan_krw",
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


def consecutive_signal_runs(hist, current, max_runs=SIGNAL_STABILITY_RUNS):
    """Count consecutive scans that retain a usable early-momentum structure."""
    rows = (hist + [current])[-max_runs:]
    count = 0
    for row in reversed(rows):
        if (
            row.get("momentum_stage") not in {"pre_ignition", "acceleration"}
            or (row.get("failure_similarity_score") or 0) >= 40
            or row.get("four_hour_low_rising") is not True
            or (row.get("price_last_60m_pct") or 0) < -0.2
        ):
            break
        count += 1
    return count


def success_pattern_similarity(m, change24):
    """Compare features, not ticker names, with the stored pre-breakout pattern."""
    checks = [
        bool(m.get("four_hour_low_rising")),
        (m.get("recent_15m_positive_count") or 0) >= 2,
        (m.get("vol_15m_persistence_x") or 0) >= 0.8,
        0 <= (m.get("price_last_60m_pct") or 0) <= 2.5,
        (m.get("upper_wick_1h_pct") or 99) <= 0.8,
        0 <= change24 <= 8,
        max(m.get("vol_1h_vs_20h_x") or 0, m.get("turnover_1h_vs_20h_x") or 0) >= 1.0,
    ]
    return sum(checks) / len(checks) * 100


def classify_stage(change24, m, hist, fast):
    p60 = m.get("price_last_60m_pct") or 0
    wick = m.get("upper_wick_1h_pct") or 0
    persistence = m.get("vol_15m_persistence_x") or 0
    positives = m.get("recent_15m_positive_count") or 0
    hl = bool(m.get("four_hour_low_rising"))
    prev_p60 = hist[-1].get("price_last_60m_pct") if hist else None
    turning = prev_p60 is not None and prev_p60 <= 0 < p60
    fast_price = normalized_fast_price(fast)
    fast_trade = normalized_fast_trade(fast)
    exhaustion_signals = sum([
        wick >= 1.5,
        persistence < 0.55,
        p60 < -0.5,
        positives <= 1,
        fast_price < -0.5,
    ])
    # A strong scan-to-scan recovery with intact 4h structure is reacceleration,
    # not exhaustion.  Two correlated price-only weakness flags (p60/positive
    # candle count) cannot override positive fast price and persistent turnover.
    reaccelerating = (
        8 <= change24 < 25
        and hl
        and persistence >= 0.8
        and wick < 1.5
        and fast_price >= 0.5
        and fast_trade >= 0
    )
    if reaccelerating:
        return "acceleration"
    structural_exhaustion = (
        exhaustion_signals >= 2
        and (wick >= 1.5 or persistence < 0.55 or fast_price < -0.5)
    )
    # Price extension alone is never enough to declare exhaustion.  Require at
    # least one supply/flow failure in addition to multiple exhaustion signals.
    if change24 >= 50 or (change24 >= 10 and structural_exhaustion):
        return "exhaustion"
    if 25 <= change24 < 50:
        return "late"
    if 0 <= change24 < 8 and hl and positives >= 2 and p60 >= 0 and (turning or persistence >= 0.8 or fast_price >= 0.4):
        return "pre_ignition"
    if 8 <= change24 < 25 and positives >= 2 and persistence >= 0.8 and (p60 > 0.25 or fast_price >= 0.4):
        return "acceleration"
    if 0 <= change24 < 25 and p60 > 0.4 and positives >= 2 and persistence >= 1.0:
        return "acceleration"
    return "developing"


def failure_similarity(m, change24):
    volume_spike_no_price = (m.get("vol_1h_vs_20h_x") or 0) >= 1.5 and (m.get("price_last_60m_pct") or 0) <= 0.1
    weak4h = not bool(m.get("four_hour_low_rising"))
    long_wick = (m.get("upper_wick_1h_pct") or 0) >= 1.5
    overheated = change24 >= 25 or (change24 >= 12 and (m.get("price_last_60m_pct") or 0) >= 6 and (m.get("upper_wick_1h_pct") or 0) >= 1.5)
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
    fast_price = normalized_fast_price(fast)
    fast_change = fast.get("change24_delta_since_scan_pct") or 0
    fast_trade = normalized_fast_trade(fast)
    fes += max(min(fast_price, 3), -3) * 8
    fes += max(min(fast_change, 3), -3) * 4
    if fast_trade >= 500_000_000:
        fes += 10
    elif fast_trade >= 100_000_000:
        fes += 5
    if base in MAJOR_LOW_BETA:
        fes -= 12
    # Success examples are feature templates. Merely reusing a historical
    # winner's ticker must not create a bonus.
    pattern_similarity = success_pattern_similarity(m, change)
    fes += max(0, pattern_similarity - 50) * 0.16
    if base in failure_reference:
        fes -= 30
    failure, _ = failure_similarity(m, change)
    fes -= failure * 0.22
    return rnd(fes), failure


def market_snapshot(markets, tickers, generated_at):
    snapshot = {}
    for base, info in markets.items():
        t = tickers.get(info["market"], {})
        ticker_timestamp = t.get("timestamp")
        observed_at = None
        if ticker_timestamp:
            try:
                observed_at = datetime.fromtimestamp(
                    float(ticker_timestamp) / 1000, timezone.utc
                ).isoformat()
            except Exception:
                observed_at = None
        snapshot[base] = {
            "market": info["market"],
            "price": rnd(t.get("trade_price"), 8),
            "change24_pct": rnd(float(t.get("signed_change_rate") or 0) * 100),
            "trade24_krw": rnd(t.get("acc_trade_price_24h"), 0),
            "price_observed_at_utc": observed_at,
        }
    return {"generated_at_utc": generated_at, "tickers": snapshot}


def fast_market_metrics(base, current_market, previous_market):
    current = current_market.get("tickers", {}).get(base, {})
    previous = previous_market.get("tickers", {}).get(base, {})
    current_trade = float(current.get("trade24_krw") or 0)
    previous_trade = float(previous.get("trade24_krw") or 0)
    interval_minutes = None
    try:
        interval_minutes = (
            datetime.fromisoformat(current_market["generated_at_utc"])
            - datetime.fromisoformat(previous_market["generated_at_utc"])
        ).total_seconds() / 60
    except Exception:
        pass
    price_change = rnd(pct(current.get("price"), previous.get("price")))
    trade_delta = rnd(current_trade - previous_trade, 0) if previous else None
    normalizer = 5 / interval_minutes if interval_minutes and interval_minutes > 0 else None
    return {
        "price_change_since_scan_pct": price_change,
        "change24_delta_since_scan_pct": rnd((current.get("change24_pct") or 0) - (previous.get("change24_pct") or 0)) if previous else None,
        "trade_value_delta_since_scan_krw": trade_delta,
        "scan_interval_minutes": rnd(interval_minutes),
        "price_change_per_5m_pct": rnd(price_change * normalizer) if price_change is not None and normalizer else None,
        "trade_value_delta_per_5m_krw": rnd(trade_delta * normalizer, 0) if trade_delta is not None and normalizer else None,
        "previous_market_scan_utc": previous_market.get("generated_at_utc") if previous else None,
    }


def normalized_fast_price(fast):
    value = fast.get("price_change_per_5m_pct")
    return value if value is not None else (fast.get("price_change_since_scan_pct") or 0)


def normalized_fast_trade(fast):
    value = fast.get("trade_value_delta_per_5m_krw")
    return value if value is not None else (fast.get("trade_value_delta_since_scan_krw") or 0)


def select_candle_universe(markets, tickers, current_market, previous_market):
    ranked = []
    forced = set()
    for base, info in markets.items():
        t = tickers.get(info["market"], {})
        trade = float(t.get("acc_trade_price_24h") or 0)
        signed_change = float(t.get("signed_change_rate") or 0) * 100
        change = abs(signed_change)
        fast = fast_market_metrics(base, current_market, previous_market)
        fast_price = normalized_fast_price(fast)
        fast_change = fast.get("change24_delta_since_scan_pct") or 0
        fast_trade = normalized_fast_trade(fast)
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
    checkpoints = TRACKING_MINUTES
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
        if item.get("policy_version") != SUPPORTED_POLICY_VERSION:
            if item["checkpoints"]:
                item["legacy_checkpoints"] = {
                    key: {**value, "reason": "legacy_tracking_window_not_comparable"}
                    for key, value in item["checkpoints"].items()
                }
                item["checkpoints"] = {}
            if elapsed >= 1440:
                item["closed"] = True
            continue
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
            key = str(minute)
            if elapsed < minute or key in item["checkpoints"]:
                continue
            cutoff_ms = start_ms + minute * 60_000
            window = [b for b in after if b["ts"] < cutoff_ms]
            if candle_entry and window:
                checkpoint_return = rnd(pct(window[-1]["close"], candle_entry))
                checkpoint_mfe = rnd(pct(max(b["high"] for b in window), candle_entry))
                checkpoint_mae = rnd(pct(min(b["low"] for b in window), candle_entry))
                quality = "exact_candle_window"
            else:
                checkpoint_return = item["current_return_pct"]
                checkpoint_mfe = item["mfe_pct"]
                checkpoint_mae = item["mae_pct"]
                quality = "synchronized_price_fallback"
            item["checkpoints"][key] = {
                "return_pct": checkpoint_return,
                "mfe_pct": checkpoint_mfe,
                "mae_pct": checkpoint_mae,
                "quality": quality,
            }
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
        if item.get("policy_version") != SUPPORTED_POLICY_VERSION:
            continue
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


def bithumb_execution_confirmation(candidate):
    """Order eligibility is always confirmed on the execution venue."""
    try:
        market = candidate["bithumb_market"]
        metrics = candle_metrics(
            bithumb_bars(market, 15, 100),
            bithumb_bars(market, 60, 22),
            bithumb_bars(market, 240, 10),
        )
        if not metrics:
            raise ValueError("insufficient_bithumb_candles")
        confirmed = (
            metrics.get("four_hour_low_rising") is True
            and (metrics.get("price_last_60m_pct") or 0) >= -0.2
            and (metrics.get("recent_15m_positive_count") or 0) >= 2
        )
        return {
            "checked": True,
            "confirmed": confirmed,
            "source": "bithumb_official",
            "four_hour_low_rising": metrics.get("four_hour_low_rising"),
            "price_last_60m_pct": rnd(metrics.get("price_last_60m_pct")),
            "recent_15m_positive_count": metrics.get("recent_15m_positive_count"),
            "reason": "confirmed" if confirmed else "execution_venue_structure_not_confirmed",
        }
    except Exception as exc:
        return {
            "checked": False,
            "confirmed": False,
            "source": "bithumb_official",
            "reason": "execution_venue_check_failed",
            "error": str(exc),
        }


def decorate_action(row, action_class, execution_allowed, max_position_fraction):
    if not row:
        return None
    result = deepcopy(row)
    result["action_class"] = action_class
    result["execution_plan_allowed"] = execution_allowed
    result["max_position_fraction"] = max_position_fraction
    return result


def append_signal_history(history, selected, signal_type, generated_at, candle_cache):
    if not selected:
        return history
    for item in reversed(history[-30:]):
        if item.get("base") == selected["base"] and item.get("signal_type", "actual_buy") == signal_type and not item.get("closed"):
            return history
    pick_bars = candle_cache.get(selected["base"], {}).get("15m", [])
    history.append({
        "base": selected["base"],
        "market": selected["bithumb_market"],
        "signal_type": signal_type,
        "recommended_at_utc": generated_at,
        "entry_price": selected["bithumb_krw_price"],
        "candle_source": selected.get("candle_source"),
        "candle_entry_price": rnd(pick_bars[-1]["close"], 8) if pick_bars else None,
        "policy_version": SUPPORTED_POLICY_VERSION,
        "closed": False,
        "checkpoints": {},
    })
    return history


PRIVATE_OUTPUT_KEYS = {
    "holdings", "portfolio", "average_buy_price", "avg_buy_price", "purchase_amount",
    "asset_balance", "account_balance", "available_cash", "user_id", "email", "phone",
    "보유수량", "평균매수가", "매수금액", "총자산", "가용현금",
}


def assert_public_output_safe(value):
    if isinstance(value, dict):
        forbidden = PRIVATE_OUTPUT_KEYS.intersection(value)
        if forbidden:
            raise RuntimeError(f"PRIVATE_OUTPUT_FIELD_BLOCKED:{','.join(sorted(forbidden))}")
        for nested in value.values():
            assert_public_output_safe(nested)
    elif isinstance(value, list):
        for nested in value:
            assert_public_output_safe(nested)


def main():
    policy = load_policy()
    success_reference = set(policy.get("reference_tickers", {}).get("success", []))
    failure_reference = set(policy.get("reference_tickers", {}).get("failure", []))
    previous = load_json(RESULT_FILE, {})
    history = load_json(HISTORY_FILE, [])
    snapshot_history = compact_snapshot_history(load_json(SNAPSHOT_HISTORY_FILE, []))
    scan_started_at = datetime.now(timezone.utc).isoformat()
    markets = get_bithumb_markets()
    tickers = get_bithumb_tickers(markets)
    # Timestamp the market snapshot after the batched Bithumb ticker reads.
    # Per-ticker exchange timestamps are preserved inside market_snapshot.
    generated_at = datetime.now(timezone.utc).isoformat()
    current_market = market_snapshot(markets, tickers, generated_at)
    previous_market = previous.get("market_snapshot", {})
    binance_pairs, binance_tickers = get_binance_universe()
    candle_bases = select_candle_universe(markets, tickers, current_market, previous_market)
    # Keep exact 15m/1h/4h/24h evaluation available even after a prior signal
    # drops out of the current ranking universe.
    candle_bases.update({
        item.get("base") for item in history
        if not item.get("closed") and item.get("base") in markets
    })
    btc_ticker = tickers.get("KRW-BTC", {})
    btc_change = float(btc_ticker.get("signed_change_rate") or 0) * 100
    rows, failures, candle_cache = [], [], {}

    for index, base in enumerate(sorted(candle_bases), 1):
        market = markets[base]["market"]
        ticker = tickers.get(market, {})
        symbol = binance_pairs.get(base)
        try:
            # All KRW execution structure and order levels must use Bithumb
            # candles. Binance remains an auxiliary 24h cross-check only; using
            # USDT candles here caused unit/time mismatches against KRW prices.
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
            hist = recent_rows(snapshot_history, base, 6, generated_at)
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
            row["success_pattern_similarity_score"] = rnd(success_pattern_similarity(m, change))
            row["signal_stability_runs"] = consecutive_signal_runs(hist, row)
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
    fast_breakout = [
        r for r in rows
        if r["momentum_stage"] in {"pre_ignition", "acceleration"}
        and (
            r.get("signal_stability_runs", 0) >= WATCH_STABILITY_RUNS
            or (
                normalized_fast_price(r) >= 0.8
                and normalized_fast_trade(r) >= 100_000_000
            )
        )
        and not r["reference_failure_pattern"]
    ][:10]

    scanned = set(snapshot)
    additional = []
    for base, info in markets.items():
        if base in scanned:
            continue
        t = tickers.get(info["market"], {})
        additional.append({"base": base, "market": info["market"], "bithumb_krw_price": t.get("trade_price"), "bithumb_24h_change_pct": rnd(float(t.get("signed_change_rate") or 0) * 100), "bithumb_24h_trade_krw": rnd(t.get("acc_trade_price_24h"), 0), "status": "additional_data_required_not_deleted"})
    additional.sort(key=lambda r: r["bithumb_24h_trade_krw"] or 0, reverse=True)

    def volume_confirmed(r):
        return (
            max(r.get("vol_1h_vs_20h_x") or 0, r.get("turnover_1h_vs_20h_x") or 0) >= 1.0
            or normalized_fast_trade(r) >= 100_000_000
        )

    def not_fading(r):
        return normalized_fast_price(r) >= 0

    breadth_pct = rnd(sum(1 for t in tickers.values() if float(t.get("signed_change_rate") or 0) > 0) / max(len(tickers), 1) * 100, 1)

    # Full technical hard gates. Missing data is a failure, never an implicit pass.
    # The execution gate follows the policy's four measurable requirements:
    # >= KRW 1bn turnover, failure similarity < 40, confirmed volume or
    # scan-to-scan turnover acceleration, and non-negative scan-to-scan price.
    # Breadth, a narrow 24h band and three-run stability remain ranking context;
    # they must not silently veto a liquid early accelerator.
    technical_candidates = [
        r for r in rows
        if r.get("momentum_stage") in {"pre_ignition", "acceleration"}
        and r.get("bithumb_24h_trade_krw", 0) >= ACTIONABLE_TRADE_KRW
        and r.get("high_beta_target_eligible") is True
        and r.get("market_warning") in (None, "", "NONE")
        and r.get("reference_failure_pattern") is False
        and r.get("failure_similarity_score") is not None and r["failure_similarity_score"] < 40
        and volume_confirmed(r)
        and not_fading(r)
    ]

    # A probe is explicitly not a full recommendation. It permits only a small
    # first tranche after the same stability and execution-venue checks.
    probe_candidates = [
        r for r in rows
        if r.get("momentum_stage") == "pre_ignition"
        and r.get("bithumb_24h_trade_krw", 0) >= PROBE_TRADE_KRW
        and 0 <= (r.get("bithumb_24h_change_pct") or 0) <= 8
        and r.get("high_beta_target_eligible") is True
        and r.get("market_warning") in (None, "", "NONE")
        and r.get("reference_failure_pattern") is False
        and r.get("failure_similarity_score", 100) < 40
        and r.get("signal_stability_runs", 0) >= WATCH_STABILITY_RUNS
        and r.get("four_hour_low_rising") is True
        and 1.2 <= (r.get("vol_15m_persistence_x") or 0) <= 5.0
        and 0 <= (r.get("price_last_60m_pct") or 0) <= 3.0
        and 0 <= normalized_fast_price(r) <= 2.0
        and (r.get("upper_wick_1h_pct") or 0) <= 1.5
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
    verified_actual = []
    verified_probe = []
    candidate_classes = []
    seen = set()
    for action_class, candidates in (("actual_buy", technical_candidates), ("probe_buy", probe_candidates)):
        for candidate in candidates[:10]:
            key = (candidate["base"], action_class)
            if key not in seen:
                candidate_classes.append((action_class, candidate))
                seen.add(key)
    risk_cache = {}
    execution_cache = {}
    for action_class, candidate in candidate_classes:
        base = candidate["base"]
        execution = execution_cache.get(base)
        if execution is None:
            execution = bithumb_execution_confirmation(candidate)
            execution_cache[base] = execution
        candidate["bithumb_execution_confirmation"] = execution
        if not execution.get("confirmed"):
            blocked_buy_candidates.append({
                "base": base,
                "action_class": action_class,
                "reason": execution.get("reason"),
                "bithumb_execution_confirmation": execution,
            })
            continue
        risk = risk_cache.get(base)
        if risk is None:
            risk = external_risk_check(base, markets.get(base, {}).get("english_name"))
            risk_cache[base] = risk
        candidate["risk_verification"] = risk
        candidate["buy_alert_allowed"] = bool(risk.get("checked") and risk.get("buy_allowed"))
        risk_scan["checked"].append({
            "base": base,
            "action_class": action_class,
            "status": risk.get("status"),
            "buy_allowed": candidate["buy_alert_allowed"],
            "reason": risk.get("reason"),
        })
        if candidate["buy_alert_allowed"]:
            if action_class == "actual_buy":
                verified_actual.append(candidate)
            else:
                verified_probe.append(candidate)
        else:
            blocked_buy_candidates.append({
                "base": base,
                "action_class": action_class,
                "reason": risk.get("reason"),
                "risk_verification": risk,
            })
        time.sleep(0.15)

    actual_pick = decorate_action(verified_actual[0] if verified_actual else None, "actual_buy", True, 0.30)
    probe_source = next((r for r in verified_probe if not actual_pick or r["base"] != actual_pick["base"]), None)
    probe_pick = decorate_action(probe_source, "probe_buy", True, 0.10)

    watch_pool = [
        r for r in rows
        if r.get("twenty_pct_path", {}).get("path_open") is True
        and r.get("momentum_stage") in {"pre_ignition", "acceleration"}
        and r.get("bithumb_24h_trade_krw", 0) >= 100_000_000
        and r.get("failure_similarity_score", 100) < 40
        and r.get("market_warning") in (None, "", "NONE")
        and r.get("reference_failure_pattern") is False
        and r.get("signal_stability_runs", 0) >= WATCH_STABILITY_RUNS
        and volume_confirmed(r)
        and not_fading(r)
        and (not actual_pick or r["base"] != actual_pick["base"])
        and (not probe_pick or r["base"] != probe_pick["base"])
    ]
    watch_source = watch_pool[0] if watch_pool else None
    previous_watch_base = (previous.get("watch_pick") or {}).get("base")
    previous_watch = next((r for r in watch_pool if r["base"] == previous_watch_base), None)
    if previous_watch and watch_source and previous_watch["future_expansion_score"] >= watch_source["future_expansion_score"] - 15:
        watch_source = previous_watch
    watch_pick = decorate_action(watch_source, "watch_only", False, 0.0)

    # Selection and execution are deliberately separate. The scanner must always
    # preserve one best candidate so opportunity-cost misses remain visible even
    # when no order is permitted by the hard gates.
    def selection_blockers(r):
        blockers = []
        if r.get("bithumb_24h_trade_krw", 0) < ACTIONABLE_TRADE_KRW:
            blockers.append("actual_buy_trade_value_below_1bn_krw")
        if r.get("failure_similarity_score", 100) >= 40:
            blockers.append("failure_similarity_at_or_above_40")
        if not volume_confirmed(r):
            blockers.append("volume_confirmation_missing")
        if not not_fading(r):
            blockers.append("scan_to_scan_price_negative")
        if r.get("four_hour_low_rising") is not True:
            blockers.append("four_hour_higher_low_missing")
        if r.get("market_warning") not in (None, "", "NONE"):
            blockers.append("market_warning")
        if r.get("reference_failure_pattern") is True:
            blockers.append("reference_failure_pattern")
        return blockers

    fallback_pool = [
        r for r in rows
        if r.get("momentum_stage") in {"pre_ignition", "acceleration"}
        and r.get("market_warning") in (None, "", "NONE")
        and r.get("reference_failure_pattern") is False
        and r.get("failure_similarity_score", 100) < 60
        and 0 <= (r.get("bithumb_24h_change_pct") or 0) <= 25
        and (r.get("price_last_60m_pct") or 0) >= 0
        and r.get("four_hour_low_rising") is True
    ]
    broad_fallback_pool = [
        r for r in rows
        if r.get("momentum_stage") in {"pre_ignition", "acceleration"}
        and r.get("reference_failure_pattern") is False
        and r.get("failure_similarity_score", 100) < 60
    ]
    selected_source = actual_pick or probe_pick or watch_pick
    if selected_source:
        best_available_pick = deepcopy(selected_source)
        best_available_pick["selection_source"] = selected_source.get("action_class")
    else:
        fallback_source = (fallback_pool or broad_fallback_pool or rows or [None])[0]
        best_available_pick = decorate_action(fallback_source, "best_available_only", False, 0.0)
        if best_available_pick:
            best_available_pick["selection_source"] = "ranked_fallback"
    if best_available_pick:
        best_available_pick["selection_rank"] = 1
        best_available_pick["selection_status"] = (
            "order_allowed" if best_available_pick.get("execution_plan_allowed")
            else "rank_1_but_no_buy"
        )
        best_available_pick["blocked_by"] = selection_blockers(best_available_pick)

    # Realized-loss replacement trades are intentionally stricter than watch or
    # probe signals. Public scan outputs never know the user's cash or holdings;
    # they only state whether deployment is technically allowed.
    deploy_pick = actual_pick or probe_pick
    cash_deployment_decision = {
        "allowed": deploy_pick is not None,
        "required_signal_class": "actual_buy_or_strict_early_probe",
        "selected_base": deploy_pick.get("base") if deploy_pick else None,
        "selected_signal_class": deploy_pick.get("action_class") if deploy_pick else None,
        "max_cash_fraction": deploy_pick.get("max_position_fraction") if deploy_pick else 0.0,
        "probe_buy_is_replacement_permission": probe_pick is not None,
        "watch_pick_is_replacement_permission": False,
        "available_cash_source": "latest_user_screen_or_explicit_value_only",
        "reason": "verified_deployment_signal" if deploy_pick else "no_verified_deployment_signal",
    }
    portfolio_exit_guardrails = {
        "full_exit_requires_explicit_action_price": True,
        "minimum_independent_weakness_signals": 2,
        "weakness_signals": [
            "vol_15m_persistence_below_0_8",
            "price_last_60m_negative",
            "scan_price_change_at_or_below_minus_0_5",
            "trade_value_decrease",
            "long_upper_wick_or_four_pct_high_drawdown",
            "four_hour_structure_break",
        ],
        "single_signal_full_exit_forbidden": True,
        "invent_new_action_price_forbidden": True,
        "averaging_down_requires_actual_buy_gates_and_four_hour_higher_low": True,
    }

    history = update_scorecard(history, tickers, candle_cache, generated_at)
    history = append_signal_history(history, actual_pick, "actual_buy", generated_at, candle_cache)
    history = append_signal_history(history, probe_pick, "probe_buy", generated_at, candle_cache)
    history = append_signal_history(history, watch_pick, "watch_pick", generated_at, candle_cache)
    history = append_signal_history(history, best_available_pick, "best_available_pick", generated_at, candle_cache)

    all_changes = [float(t.get("signed_change_rate") or 0) * 100 for t in tickers.values()]
    output = {
        "generated_at_utc": generated_at,
        "scan_started_at_utc": scan_started_at,
        "schema_version": policy["schema_version"],
        "version": "v12-always-one-best-pick",
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
        "best_available_pick": best_available_pick,
        "actual_buy": actual_pick,
        "probe_buy": probe_pick,
        "watch_pick": watch_pick,
        "risk_scan": risk_scan,
        "blocked_buy_candidates": blocked_buy_candidates,
        "cash_deployment_decision": cash_deployment_decision,
        "portfolio_exit_guardrails": portfolio_exit_guardrails,
        "recommendation_scorecard": history[-20:],
        "market_snapshot": current_market,
        "snapshot": snapshot,
        "failed_sample": failures[:30],
    }
    summary_keys = ["generated_at_utc", "schema_version", "version", "policy_version", "universe", "market_regime", "pre_ignition_top5", "acceleration_top5", "late_top5", "exhaustion_no_chase", "fast_breakout_alerts", "twenty_pct_path_candidates", "additional_data_required", "best_available_pick", "actual_buy", "probe_buy", "watch_pick", "risk_scan", "blocked_buy_candidates", "cash_deployment_decision", "portfolio_exit_guardrails", "recommendation_scorecard"]
    summary = {k: output[k] for k in summary_keys}
    assert_public_output_safe(output)
    assert_public_output_safe(summary)
    assert_public_output_safe(history)
    save_json(RESULT_FILE, output)
    save_json(SUMMARY_FILE, summary)
    save_json(HISTORY_FILE, history)
    snapshot_history.append({"generated_at_utc": generated_at, "snapshot": compact_snapshot(snapshot)})
    with open(SNAPSHOT_HISTORY_FILE, "w", encoding="utf-8") as f:
        json.dump(snapshot_history[-SNAPSHOT_HISTORY_LIMIT:], f, ensure_ascii=False, separators=(",", ":"))
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
