import json
import os
import time
from datetime import datetime, timezone
from statistics import mean

import requests


# ============================================================
# FINAL CONFIG
# ============================================================

BINANCE_BASE = "https://data-api.binance.vision"
BITHUMB_BASE = "https://api.bithumb.com"

VERSION = "v4-execution-quality"

WATCHLIST = [
    "GWEI",
    "NEAR",
    "TAO",
    "BIO"
]

# 명백한 스테이블코인/달러성 자산
STABLE_BASES = {
    "USDT",
    "USDC",
    "FDUSD",
    "USDS",
    "TUSD",
    "DAI",
    "PYUSD",
    "USDP",
    "XUSD",
    "USD1",
    "USDG",
    "RLUSD",
    "USDE",
    "EURI",
    "AEUR",
    "BUSD",
    "UST",
    "USTC"
}

# 유동성 기준
MIN_BINANCE_QUOTE_USDT = 500_000
MIN_BITHUMB_TRADE_KRW = 300_000_000

# A등급은 실제 진입·청산이 가능한 유동성을 별도로 요구
MIN_ACTIONABLE_BINANCE_QUOTE_USDT = 2_000_000
MIN_ACTIONABLE_BITHUMB_TRADE_KRW = 1_000_000_000


session = requests.Session()

session.headers.update({
    "User-Agent":
        "Mozilla/5.0 crypto-prebreakout-scanner/3.0",

    "Accept":
        "application/json"
})


# ============================================================
# COMMON
# ============================================================

def get_json(
    url,
    params=None,
    retries=4,
    timeout=25
):

    last_error = None

    for i in range(retries):

        try:

            response = session.get(
                url,
                params=params,
                timeout=timeout
            )

            if response.status_code == 429:

                time.sleep(
                    2 + i * 2
                )

                continue

            response.raise_for_status()

            return response.json()

        except Exception as e:

            last_error = e

            time.sleep(
                1.5 + i * 1.5
            )

    raise last_error


def pct(a, b):

    if (
        a is None
        or b is None
        or b == 0
    ):
        return None

    return (
        a / b - 1.0
    ) * 100.0


def rnd(
    value,
    digits=2
):

    if value is None:
        return None

    return round(
        float(value),
        digits
    )


# ============================================================
# PREVIOUS RESULT
# ============================================================

def load_previous():

    if not os.path.exists(
        "scan_result.json"
    ):
        return {}

    try:

        with open(
            "scan_result.json",
            "r",
            encoding="utf-8"
        ) as f:

            return json.load(f)

    except Exception:

        return {}


# ============================================================
# BINANCE
# ============================================================

def get_binance_exchange_info():

    return get_json(
        BINANCE_BASE
        + "/api/v3/exchangeInfo"
    )


def get_binance_24h():

    rows = get_json(
        BINANCE_BASE
        + "/api/v3/ticker/24hr"
    )

    return {
        x["symbol"]: x
        for x in rows
        if "symbol" in x
    }


def closed_klines(
    symbol,
    interval,
    limit
):

    rows = get_json(
        BINANCE_BASE
        + "/api/v3/klines",

        {
            "symbol": symbol,
            "interval": interval,
            "limit": limit
        }
    )

    now_ms = int(
        time.time() * 1000
    )

    # 미완성 봉 제외
    return [
        row
        for row in rows
        if int(row[6]) < now_ms
    ]


# ============================================================
# BITHUMB
# ============================================================

def get_bithumb_markets(
    previous
):

    try:

        rows = get_json(
            BITHUMB_BASE
            + "/v1/market/all",

            {
                "isDetails":
                    "true"
            }
        )

        markets = {}

        for row in rows:

            market = row.get(
                "market",
                ""
            )

            if not market.startswith(
                "KRW-"
            ):
                continue

            base = market.split(
                "-",
                1
            )[1]

            markets[base] = {

                "market":
                    market,

                "korean_name":
                    row.get(
                        "korean_name"
                    ),

                "english_name":
                    row.get(
                        "english_name"
                    ),

                "market_warning":
                    row.get(
                        "market_warning",
                        "NONE"
                    )
            }

        return (
            markets,
            "live"
        )

    except Exception as e:

        # API 일시 실패 시
        # 직전 저장된 빗썸 목록 사용

        cached = previous.get(
            "bithumb_markets_cache",
            {}
        )

        if cached:

            return (
                cached,
                "previous_cache"
            )

        print(
            "Bithumb market list unavailable:",
            e
        )

        return (
            {},
            "unavailable"
        )


def get_bithumb_tickers(
    markets
):

    if not markets:

        return (
            {},
            "unavailable"
        )

    market_codes = [

        data["market"]

        for data
        in markets.values()
    ]

    result = {}

    failures = 0

    # URL 길이/부하 방지용
    # 40개씩 분할

    for i in range(
        0,
        len(market_codes),
        40
    ):

        chunk = market_codes[
            i:i + 40
        ]

        try:

            rows = get_json(
                BITHUMB_BASE
                + "/v1/ticker",

                {
                    "markets":
                        ",".join(chunk)
                }
            )

            for row in rows:

                market = row.get(
                    "market"
                )

                if market:

                    result[
                        market
                    ] = row

        except Exception as e:

            failures += 1

            print(
                "Bithumb ticker chunk failed:",
                e
            )

        time.sleep(
            0.08
        )

    if not result:

        return (
            {},
            "unavailable"
        )

    if failures:

        return (
            result,
            "partial"
        )

    return (
        result,
        "live"
    )


# ============================================================
# BINANCE ∩ BITHUMB KRW
# ============================================================

def build_intersection(
    exchange_info,
    bithumb_markets
):

    result = []

    for item in exchange_info.get(
        "symbols",
        []
    ):

        if (
            item.get("status")
            != "TRADING"
        ):
            continue

        if (
            item.get("quoteAsset")
            != "USDT"
        ):
            continue

        base = item.get(
            "baseAsset",
            ""
        )

        symbol = item.get(
            "symbol",
            ""
        )

        if not base:
            continue

        if base in STABLE_BASES:
            continue

        if base.endswith(
            (
                "UP",
                "DOWN",
                "BULL",
                "BEAR"
            )
        ):
            continue

        # 빗썸 시장 목록 취득 성공 시
        # KRW 미상장 종목 제거
        if (
            bithumb_markets
            and base
            not in bithumb_markets
        ):
            continue

        warning = (
            bithumb_markets
            .get(
                base,
                {}
            )
            .get(
                "market_warning",
                "NONE"
            )
        )

        # 유의종목 제외
        if warning not in (
            None,
            "",
            "NONE"
        ):
            continue

        result.append(
            (
                symbol,
                base
            )
        )

    return result


# ============================================================
# SYMBOL SCAN
# ============================================================

def scan_symbol(
    symbol,
    base,
    binance_24h,
    bithumb_market,
    bithumb_ticker
):

    # ========================================================
    # 1H — 핵심 탐지봉
    # ========================================================

    k1 = closed_klines(
        symbol,
        "1h",
        22
    )

    if len(k1) < 21:
        return None

    latest = k1[-1]
    previous = k1[-2]

    prior20 = k1[
        -21:-1
    ]

    latest_vol = float(
        latest[5]
    )

    previous_vol = float(
        previous[5]
    )

    avg20 = mean(
        float(x[5])
        for x in prior20
    )

    vol_vs_prev = pct(
        latest_vol,
        previous_vol
    )

    vol_vs_20h = (
        latest_vol / avg20
        if avg20
        else None
    )

    o1 = float(
        latest[1]
    )

    h1 = float(
        latest[2]
    )

    l1 = float(
        latest[3]
    )

    c1 = float(
        latest[4]
    )

    price_1h = pct(
        c1,
        o1
    )

    upper_wick = pct(
        h1,
        max(o1, c1)
    )


    # ========================================================
    # 4H
    # ========================================================

    k4 = closed_klines(
        symbol,
        "4h",
        7
    )

    price_4h = None

    low_rising = False

    if len(k4) >= 3:

        latest4 = k4[-1]

        price_4h = pct(
            float(
                latest4[4]
            ),
            float(
                latest4[1]
            )
        )

        lows = [
            float(x[3])
            for x in k4[-3:]
        ]

        low_rising = (
            lows[-1]
            >= lows[-2]
            >= lows[-3]
        )


    # ========================================================
    # 15M
    # ========================================================

    k15 = closed_klines(
        symbol,
        "15m",
        9
    )

    persistence_15m = None

    price_60m = None

    positive_15m = 0

    if len(k15) >= 8:

        volumes = [
            float(x[5])
            for x in k15[-8:]
        ]

        old_avg = mean(
            volumes[:4]
        )

        new_avg = mean(
            volumes[4:]
        )

        if old_avg:

            persistence_15m = (
                new_avg
                / old_avg
            )

        price_60m = pct(
            float(
                k15[-1][4]
            ),
            float(
                k15[-4][1]
            )
        )

        positive_15m = sum(

            1

            for x
            in k15[-4:]

            if float(x[4])
            >= float(x[1])
        )


    # ========================================================
    # BINANCE 24H
    # ========================================================

    bt = binance_24h.get(
        symbol,
        {}
    )

    last_usdt = (

        float(
            bt["lastPrice"]
        )

        if bt.get(
            "lastPrice"
        )

        else None
    )

    price_24h = (

        float(
            bt[
                "priceChangePercent"
            ]
        )

        if bt.get(
            "priceChangePercent"
        )

        else None
    )

    quote_24h = (

        float(
            bt["quoteVolume"]
        )

        if bt.get(
            "quoteVolume"
        )

        else None
    )


    # ========================================================
    # BITHUMB
    # ========================================================

    krw_price = None

    krw_trade_24h = None

    krw_high_vs_prevclose = None

    krw_change_24h = None

    krw_day_high = None

    if bithumb_ticker:

        krw_price = (
            bithumb_ticker
            .get(
                "trade_price"
            )
        )

        krw_trade_24h = (
            bithumb_ticker
            .get(
                "acc_trade_price_24h"
            )
        )

        prev_close = (
            bithumb_ticker
            .get(
                "prev_closing_price"
            )
        )

        high_price = (
            bithumb_ticker
            .get(
                "high_price"
            )
        )

        if high_price is not None:
            krw_day_high = float(high_price)

        if (
            prev_close
            and high_price
        ):

            krw_high_vs_prevclose = pct(
                float(
                    high_price
                ),
                float(
                    prev_close
                )
            )

        change_rate = (
            bithumb_ticker
            .get(
                "signed_change_rate"
            )
        )

        if change_rate is not None:

            krw_change_24h = (
                float(
                    change_rate
                )
                * 100.0
            )


    # ========================================================
    # OVERHEAT
    # ========================================================

    # 우선적으로 빗썸 당일 고가 /
    # 전일 종가 사용

    high_metric = (
        krw_high_vs_prevclose
    )

    # 빗썸 데이터 없으면
    # Binance rolling 24h 이용

    if high_metric is None:

        open24 = (

            float(
                bt["openPrice"]
            )

            if bt.get(
                "openPrice"
            )

            else None
        )

        high24 = (

            float(
                bt["highPrice"]
            )

            if bt.get(
                "highPrice"
            )

            else None
        )

        if (
            open24
            and high24
        ):

            high_metric = pct(
                high24,
                open24
            )


    overheated = bool(

        (
            high_metric
            is not None
            and
            high_metric >= 15.0
        )

        or

        (
            price_1h
            is not None
            and
            price_1h >= 10.0
        )
    )


    warm = bool(

        high_metric
        is not None

        and

        10.0
        <= high_metric
        < 15.0
    )


    # ========================================================
    # DUMPING VOLUME
    # ========================================================

    dumping = bool(

        (
            price_1h
            is not None

            and

            price_1h < -1.0

            and

            vol_vs_20h
            is not None

            and

            vol_vs_20h >= 2.0
        )

        or

        (
            price_4h
            is not None

            and

            price_4h < -4.0

            and

            vol_vs_20h
            is not None

            and

            vol_vs_20h >= 2.0
        )
    )


    # ========================================================
    # LIQUIDITY
    # ========================================================

    thin_binance = (

        quote_24h
        is None

        or

        quote_24h
        < MIN_BINANCE_QUOTE_USDT
    )


    thin_bithumb = (

        krw_trade_24h
        is None

        or

        float(
            krw_trade_24h
        )
        < MIN_BITHUMB_TRADE_KRW
    )


    # 둘 다 얇을 때만
    # C 처리

    low_liquidity = (
        thin_binance
        and
        thin_bithumb
    )

    # A등급은 한쪽 시장이라도 충분한 체결 유동성을 요구한다.
    execution_liquidity = (
        (
            quote_24h is not None
            and quote_24h >= MIN_ACTIONABLE_BINANCE_QUOTE_USDT
        )
        or
        (
            krw_trade_24h is not None
            and float(krw_trade_24h) >= MIN_ACTIONABLE_BITHUMB_TRADE_KRW
        )
    )

    distance_from_day_high_pct = (
        pct(krw_price, krw_day_high)
        if krw_price is not None and krw_day_high
        else None
    )

    near_day_high = (
        distance_from_day_high_pct is not None
        and distance_from_day_high_pct >= -1.0
    )

    chase_risk = bool(
        near_day_high
        and (
            (price_1h is not None and price_1h > 2.0)
            or
            (price_60m is not None and price_60m > 2.0)
        )
    )


    # ========================================================
    # SCORE
    # ========================================================

    score = 0.0


    # 거래량 직전봉 대비
    if vol_vs_prev is not None:

        score += (

            max(
                min(
                    vol_vs_prev,
                    500.0
                ),
                -100.0
            )

            / 100.0
        )


    # 20시간 평균 대비
    if vol_vs_20h is not None:

        score += (

            min(
                vol_vs_20h,
                10.0
            )

            * 1.6
        )


    # 1H 가격
    if price_1h is not None:

        if (
            0.0
            <= price_1h
            <= 2.0
        ):

            score += 4.0

        elif (
            2.0
            < price_1h
            <= 3.0
        ):

            score += 1.0

        elif (
            3.0
            < price_1h
            <= 5.0
        ):

            score -= 3.0

        elif price_1h < -0.5:

            score -= 2.5

        elif price_1h > 5.0:

            score -= 6.0


    # 4H 가격 구조
    if price_4h is not None:

        if (
            0.0
            <= price_4h
            <= 5.0
        ):

            score += 2.0

        elif price_4h < -2.0:

            score -= 2.5


    # 4h 저점상승
    if low_rising:

        score += 1.5


    # 15m 거래량 지속
    if persistence_15m is not None:

        if persistence_15m >= 1.5:

            score += 2.5

        elif persistence_15m >= 1.0:

            score += 1.0

        elif persistence_15m < 0.6:

            score -= 1.5


    if positive_15m >= 3:

        score += 1.0


    # 1h 긴 윗꼬리
    if (
        upper_wick
        is not None
        and
        upper_wick >= 1.5
    ):

        score -= 2.0

    if chase_risk:
        score -= 5.0

    if not execution_liquidity:
        score -= 3.0


    # 당일 고가
    if high_metric is not None:

        if high_metric >= 15.0:

            score -= 10.0

        elif high_metric >= 10.0:

            score -= 4.0

        elif high_metric < 8.0:

            score += 1.0


    # Binance 거래대금
    if quote_24h is not None:

        if quote_24h >= 50_000_000:

            score += 2.0

        elif quote_24h >= 10_000_000:

            score += 1.5

        elif quote_24h >= 2_000_000:

            score += 0.8


    # 빗썸 KRW 거래대금
    if krw_trade_24h is not None:

        krw_trade_float = float(
            krw_trade_24h
        )

        if (
            krw_trade_float
            >= 10_000_000_000
        ):

            score += 2.0

        elif (
            krw_trade_float
            >= 3_000_000_000
        ):

            score += 1.5

        elif (
            krw_trade_float
            >= 1_000_000_000
        ):

            score += 1.0

        elif (
            krw_trade_float
            < 100_000_000
        ):

            score -= 2.0


    if warm:

        score -= 2.0


    if dumping:

        score -= 8.0


    if low_liquidity:

        score -= 6.0


    # ========================================================
    # A GRADE
    # ========================================================

    volume_signal = (

        (
            vol_vs_prev
            is not None

            and

            vol_vs_prev >= 60

            and

            vol_vs_20h
            is not None

            and

            vol_vs_20h >= 2.0
        )

        or

        (
            vol_vs_prev
            is not None

            and

            vol_vs_prev >= 30

            and

            vol_vs_20h
            is not None

            and

            vol_vs_20h >= 3.0
        )
    )


    grade = "B"


    if (

        not overheated

        and
        not warm

        and
        not dumping

        and
        not low_liquidity

        and
        execution_liquidity

        and
        not chase_risk

        and
        volume_signal

        and
        price_1h
        is not None

        and
        0.0
        <= price_1h
        <= 2.0

        and
        price_4h
        is not None

        and
        price_4h >= 0.0

        and
        price_60m
        is not None

        and
        price_60m <= 2.0

        and
        upper_wick
        is not None

        and
        upper_wick <= 1.5

        and
        persistence_15m
        is not None

        and
        persistence_15m >= 1.2
    ):

        grade = "A"


    if (

        overheated

        or
        dumping

        or
        low_liquidity
    ):

        grade = "C"


    return {

        "symbol":
            symbol,

        "base":
            base,

        "bithumb_market":
            bithumb_market,

        "bithumb_krw_price":
            rnd(
                krw_price,
                8
            ),

        "bithumb_24h_trade_krw":
            rnd(
                krw_trade_24h,
                0
            ),

        "bithumb_24h_change_pct":
            rnd(
                krw_change_24h,
                2
            ),

        "bithumb_day_high_vs_prevclose_pct":
            rnd(
                krw_high_vs_prevclose,
                2
            ),

        "binance_last_usdt":
            rnd(
                last_usdt,
                10
            ),

        "binance_24h_quote_usdt":
            rnd(
                quote_24h,
                0
            ),

        "binance_24h_change_pct":
            rnd(
                price_24h,
                2
            ),

        "vol_1h_vs_prev_pct":
            rnd(
                vol_vs_prev,
                1
            ),

        "vol_1h_vs_20h_x":
            rnd(
                vol_vs_20h,
                2
            ),

        "price_1h_pct":
            rnd(
                price_1h,
                2
            ),

        "price_4h_pct":
            rnd(
                price_4h,
                2
            ),

        "vol_15m_persistence_x":
            rnd(
                persistence_15m,
                2
            ),

        "price_last_60m_pct":
            rnd(
                price_60m,
                2
            ),

        "upper_wick_1h_pct":
            rnd(
                upper_wick,
                2
            ),

        "four_hour_low_rising":
            low_rising,

        "recent_15m_positive_count":
            positive_15m,

        "warm_10_to_15":
            warm,

        "overheated_15_plus":
            overheated,

        "dumping_volume":
            dumping,

        "low_liquidity":
            low_liquidity,

        "execution_liquidity":
            execution_liquidity,

        "distance_from_day_high_pct":
            rnd(
                distance_from_day_high_pct,
                2
            ),

        "near_day_high":
            near_day_high,

        "chase_risk":
            chase_risk,

        "setup_type":
            (
                "actionable_prebreakout"
                if grade == "A"
                else "volume_alert_only"
            ),

        "grade":
            grade,

        "score":
            rnd(
                score,
                2
            )
    }


# ============================================================
# PREVIOUS RUN COMPARISON
# ============================================================

def compare_with_previous(
    row,
    previous_snapshot
):

    previous = previous_snapshot.get(
        row["base"]
    )

    if not previous:

        return {

            "base":
                row["base"],

            "status":
                "no_previous_measurement"
        }


    def delta(key):

        current_value = row.get(
            key
        )

        previous_value = previous.get(
            key
        )

        if (
            current_value
            is None

            or

            previous_value
            is None
        ):

            return None

        return rnd(

            float(
                current_value
            )

            -

            float(
                previous_value
            ),

            2
        )


    return {

        "base":
            row["base"],

        "status":
            "compared",

        "vol_1h_vs_prev_pct": {

            "previous":
                previous.get(
                    "vol_1h_vs_prev_pct"
                ),

            "current":
                row.get(
                    "vol_1h_vs_prev_pct"
                ),

            "delta":
                delta(
                    "vol_1h_vs_prev_pct"
                )
        },

        "vol_1h_vs_20h_x": {

            "previous":
                previous.get(
                    "vol_1h_vs_20h_x"
                ),

            "current":
                row.get(
                    "vol_1h_vs_20h_x"
                ),

            "delta":
                delta(
                    "vol_1h_vs_20h_x"
                )
        },

        "price_1h_pct": {

            "previous":
                previous.get(
                    "price_1h_pct"
                ),

            "current":
                row.get(
                    "price_1h_pct"
                ),

            "delta":
                delta(
                    "price_1h_pct"
                )
        },

        "price_4h_pct": {

            "previous":
                previous.get(
                    "price_4h_pct"
                ),

            "current":
                row.get(
                    "price_4h_pct"
                ),

            "delta":
                delta(
                    "price_4h_pct"
                )
        },

        "score": {

            "previous":
                previous.get(
                    "score"
                ),

            "current":
                row.get(
                    "score"
                ),

            "delta":
                delta(
                    "score"
                )
        }
    }


# ============================================================
# MARKET CONTEXT
# ============================================================

def build_market_context(
    snapshot
):

    btc = snapshot.get(
        "BTC"
    )

    eth = snapshot.get(
        "ETH"
    )


    alt_rows = [

        row

        for base, row
        in snapshot.items()

        if base
        not in (
            "BTC",
            "ETH"
        )
    ]


    def breadth(key):

        values = [

            row.get(key)

            for row
            in alt_rows

            if row.get(key)
            is not None
        ]

        if not values:

            return None

        positive = sum(

            1

            for value
            in values

            if float(value) > 0
        )

        return rnd(

            positive
            / len(values)
            * 100.0,

            1
        )


    return {

        "BTC":
            btc,

        "ETH":
            eth,

        "alt_breadth_positive_1h_pct":
            breadth(
                "price_1h_pct"
            ),

        "alt_breadth_positive_4h_pct":
            breadth(
                "price_4h_pct"
            ),

        "alt_breadth_positive_24h_pct":
            breadth(
                "binance_24h_change_pct"
            )
    }


# ============================================================
# MAIN
# ============================================================

def main():

    print(
        "================================"
    )

    print(
        "CRYPTO PREBREAKOUT SCANNER FINAL"
    )

    print(
        "================================"
    )


    previous = load_previous()

    previous_snapshot = (
        previous.get(
            "snapshot",
            {}
        )
    )


    # Binance
    exchange_info = (
        get_binance_exchange_info()
    )

    binance_24h = (
        get_binance_24h()
    )


    # Bithumb
    (
        bithumb_markets,
        market_source
    ) = get_bithumb_markets(
        previous
    )


    (
        bithumb_tickers,
        ticker_source
    ) = get_bithumb_tickers(
        bithumb_markets
    )


    # 교집합
    symbols = build_intersection(
        exchange_info,
        bithumb_markets
    )


    print(
        "Intersection symbols:",
        len(symbols)
    )

    print(
        "Bithumb market source:",
        market_source
    )

    print(
        "Bithumb ticker source:",
        ticker_source
    )


    results = []

    failed = []


    for i, (
        symbol,
        base
    ) in enumerate(
        symbols,
        1
    ):

        try:

            market_info = (
                bithumb_markets
                .get(
                    base,
                    {}
                )
            )

            market = (
                market_info
                .get(
                    "market"
                )
            )

            ticker = (

                bithumb_tickers
                .get(
                    market,
                    {}
                )

                if market

                else {}
            )


            row = scan_symbol(

                symbol,
                base,

                binance_24h,

                market,

                ticker
            )


            if row:

                results.append(
                    row
                )


        except Exception as e:

            failed.append({

                "symbol":
                    symbol,

                "base":
                    base,

                "error":
                    str(e)
            })


        if (
            i % 25 == 0

            or

            i == len(symbols)
        ):

            print(

                f"{i}/{len(symbols)} scanned "
                f"| ok={len(results)} "
                f"failed={len(failed)}"
            )


        time.sleep(
            0.04
        )


    # ========================================================
    # SORT
    # ========================================================

    grade_order = {

        "A": 0,
        "B": 1,
        "C": 2
    }


    results.sort(

        key=lambda row: (

            grade_order.get(
                row["grade"],
                9
            ),

            -row["score"]
        )
    )


    # ========================================================
    # CLEAN
    # ========================================================

    clean = [

        row

        for row
        in results

        if (
            row["grade"]
            != "C"

            and
            not row[
                "overheated_15_plus"
            ]

            and
            not row[
                "dumping_volume"
            ]

            and
            not row[
                "low_liquidity"
            ]
        )
    ]


    snapshot = {

        row["base"]:
            row

        for row
        in results
    }


    clean_top10 = (
        clean[:10]
    )


    # ========================================================
    # PREVIOUS COMPARISON
    # ========================================================

    comparisons = [

        compare_with_previous(
            row,
            previous_snapshot
        )

        for row
        in clean_top10
    ]


    # ========================================================
    # WATCHLIST
    # ========================================================

    watchlist = {}


    for base in WATCHLIST:

        if base in snapshot:

            watchlist[
                base
            ] = snapshot[
                base
            ]

        else:

            watchlist[
                base
            ] = {

                "status":
                    "not_in_verified_binance_bithumb_intersection"
            }


    # ========================================================
    # OUTPUT
    # ========================================================

    output = {

        "generated_at_utc":

            datetime.now(
                timezone.utc
            ).isoformat(),


        "version":
            VERSION,


        "data_sources": {

            "binance":
                "official_public_market_data",

            "bithumb_market_list":
                market_source,

            "bithumb_ticker":
                ticker_source
        },


        "bithumb_markets_cache":
            bithumb_markets,


        "universe": {

            "binance_bithumb_krw_intersection_count":
                len(symbols),

            "success":
                len(results),

            "failed":
                len(failed)
        },


        "market_context":
            build_market_context(
                snapshot
            ),


        "top20":
            results[:20],


        "clean_top10":
            clean_top10,


        "changes_vs_previous_run":
            comparisons,


        "watchlist":
            watchlist,


        # 다음 회차와
        # 모든 동일 종목 비교용
        "snapshot":
            snapshot,


        "failed_sample":
            failed[:20]
    }


    # ========================================================
    # SAVE
    # ========================================================

    with open(

        "scan_result.json",

        "w",

        encoding="utf-8"

    ) as f:

        json.dump(

            output,

            f,

            ensure_ascii=False,

            indent=2
        )


    # ========================================================
    # LOG
    # ========================================================

    print(
        "\nSCAN COMPLETE"
    )

    print(
        "Version:",
        VERSION
    )

    print(
        "Success:",
        len(results)
    )

    print(
        "Failed:",
        len(failed)
    )

    print(
        "\nCLEAN TOP 10"
    )

    print(

        json.dumps(

            clean_top10,

            ensure_ascii=False,

            indent=2
        )
    )


    print(
        "\nCHANGES VS PREVIOUS"
    )

    print(

        json.dumps(

            comparisons,

            ensure_ascii=False,

            indent=2
        )
    )


    print(
        "\nWATCHLIST"
    )

    print(

        json.dumps(

            watchlist,

            ensure_ascii=False,

            indent=2
        )
    )


if __name__ == "__main__":

    main()
