import json
from datetime import datetime, timezone
from pathlib import Path

import scanner_v18 as v18

VERSION = "v37-all-market-observer"
OUT_FILE = "all_market_snapshot.json"

v17 = v18.v17
v16 = v17.v16


def f(v, default=0.0):
    try:
        return float(v)
    except Exception:
        return default


def main():
    now = datetime.now(timezone.utc).isoformat()
    errors = []
    try:
        markets = v16.get_markets()
    except Exception as e:
        markets = {}
        errors.append(f"market_list_failed:{e}")
    try:
        tickers = v16.get_tickers(markets) if markets else {}
    except Exception as e:
        tickers = {}
        errors.append(f"ticker_failed:{e}")

    rows = {}
    for market, info in markets.items():
        t = tickers.get(market) or {}
        price = f(t.get("trade_price"))
        if not price:
            continue
        base = info.get("base") or market.split("-", 1)[-1]
        rows[base] = {
            "base": base,
            "market": market,
            "price_krw": price,
            "change_24h_pct": round(f(t.get("signed_change_rate")) * 100.0, 4),
            "trade_24h_krw": f(t.get("acc_trade_price_24h")),
            "market_warning": info.get("market_warning") or "NONE",
        }

    out = {
        "generated_at_utc": now,
        "version": VERSION,
        "market_count": len(markets),
        "ticker_count": len(rows),
        "healthy": bool(rows) and len(rows) >= max(1, int(len(markets) * 0.80)),
        "errors": errors,
        "rows": rows,
        "principle": "Track every open outcome from the full Bithumb KRW ticker universe even after it disappears from the ranking bridge.",
    }
    Path(OUT_FILE).write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({k: out[k] for k in ("version", "market_count", "ticker_count", "healthy", "errors")}, ensure_ascii=False))


if __name__ == "__main__":
    main()
