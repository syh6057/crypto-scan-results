import json
from datetime import datetime, timezone
from pathlib import Path

VERSION = "v40-persistent-candidate-lifecycle"
BRIDGE_FILE = "beam_breakout_bridge.json"
MARKET_FILE = "all_market_snapshot.json"
STATE_FILE = "persistent_candidate_pool.json"
OUT_FILE = "persistent_candidate_summary.json"

MAX_AGE_RUNS = 12
MAX_POOL = 60
HARD_FAIL_PCT = -4.0
SUCCESS_PCT = 10.0


def load(path, default):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except Exception:
        return default


def save(path, obj):
    Path(path).write_text(json.dumps(obj, ensure_ascii=False, indent=2), encoding="utf-8")


def f(v, default=0.0):
    try:
        return float(v)
    except Exception:
        return default


def market_prices(market):
    out = {}
    rows = market.get("tickers") or market.get("markets") or market.get("rows") or []
    if isinstance(rows, dict):
        rows = list(rows.values())
    for r in rows if isinstance(rows, list) else []:
        if not isinstance(r, dict):
            continue
        base = r.get("base") or r.get("symbol") or str(r.get("market") or "").replace("KRW-", "")
        price = r.get("price_krw") or r.get("trade_price") or r.get("closing_price") or r.get("price")
        if base and f(price) > 0:
            out[str(base).upper()] = f(price)
    return out


def current_rows(bridge):
    out = {}
    # These are display/ranking lists only. The persistent pool below is never truncated to top5.
    for key in ("promoted_top3", "watch_top5", "top_candidates"):
        for row in bridge.get(key) or []:
            if isinstance(row, dict) and row.get("base"):
                out[row["base"]] = row
    return out


def is_early_or_urgent(row):
    grade = str(row.get("promotion_grade") or "")
    tier = str(row.get("tier") or "")
    return bool(row.get("execution_gate")) or grade.startswith("A_") or "URGENT" in grade or "EARLY" in grade or tier == "URGENT_EARLY"


def main():
    now = datetime.now(timezone.utc).isoformat()
    bridge = load(BRIDGE_FILE, {})
    market = load(MARKET_FILE, {})
    state = load(STATE_FILE, {})
    pool = state.get("pool") if isinstance(state, dict) else {}
    if not isinstance(pool, dict):
        pool = {}
    rows = current_rows(bridge)
    prices = market_prices(market)

    # Register every early/urgent candidate. Ranking loss must never equal deletion.
    for base, row in rows.items():
        if not is_early_or_urgent(row):
            continue
        rec = pool.get(base) or {}
        if not rec:
            rec = {
                "base": base,
                "first_seen_utc": now,
                "first_price_krw": f(row.get("price_krw")),
                "first_change_24h_pct": f(row.get("change_24h_pct")),
                "runs": 0,
                "mfe_pct": 0.0,
                "mae_pct": 0.0,
                "status": "TRACKING",
                "reason": "registered_early_or_urgent",
            }
        rec["last_row"] = row
        rec["last_rank_seen_utc"] = now
        pool[base] = rec

    removed = []
    for base in list(pool):
        rec = pool[base]
        rec["runs"] = int(rec.get("runs") or 0) + 1
        px = prices.get(str(base).upper()) or f((rows.get(base) or {}).get("price_krw"))
        first = f(rec.get("first_price_krw"))
        if px > 0:
            rec["last_price_krw"] = px
            rec["last_price_seen_utc"] = now
            if first > 0:
                ret = (px / first - 1.0) * 100.0
                rec["current_return_pct"] = round(ret, 4)
                rec["mfe_pct"] = round(max(f(rec.get("mfe_pct")), ret), 4)
                rec["mae_pct"] = round(min(f(rec.get("mae_pct")), ret), 4)
                if ret >= SUCCESS_PCT:
                    rec["status"] = "SUCCESS_TRACKED"
                    rec["reason"] = "forward_gain_ge_10pct"
                elif ret <= HARD_FAIL_PCT:
                    rec["status"] = "INVALIDATED"
                    rec["reason"] = "forward_loss_le_minus_4pct"
        # Refresh full metrics only when upstream currently observes the candidate.
        if base in rows:
            rec["last_row"] = rows[base]
            rec["observable_this_run"] = True
        else:
            rec["observable_this_run"] = False
        if rec.get("status") == "TRACKING" and int(rec.get("runs") or 0) > MAX_AGE_RUNS:
            rec["status"] = "EXPIRED"
            rec["reason"] = "max_tracking_age"
        if rec.get("status") in {"INVALIDATED", "EXPIRED"}:
            removed.append({"base": base, "status": rec.get("status"), "reason": rec.get("reason")})

    # Keep completed winners for learning, but bound the state deterministically.
    ordered = sorted(pool.items(), key=lambda kv: (kv[1].get("status") in {"TRACKING", "SUCCESS_TRACKED"}, kv[1].get("last_price_seen_utc") or ""), reverse=True)
    pool = dict(ordered[:MAX_POOL])

    # Expose the complete lifecycle pool separately from UI top5. Downstream continuity can inspect this key.
    bridge["persistent_candidate_pool"] = [
        {
            **(rec.get("last_row") or {"base": base}),
            "persistent_tracking": {
                "version": VERSION,
                "status": rec.get("status"),
                "runs": rec.get("runs"),
                "first_price_krw": rec.get("first_price_krw"),
                "last_price_krw": rec.get("last_price_krw"),
                "current_return_pct": rec.get("current_return_pct"),
                "mfe_pct": rec.get("mfe_pct"),
                "mae_pct": rec.get("mae_pct"),
                "observable_this_run": rec.get("observable_this_run"),
                "reason": rec.get("reason"),
            },
        }
        for base, rec in pool.items() if rec.get("status") in {"TRACKING", "SUCCESS_TRACKED"}
    ]
    bridge["persistent_candidate_lifecycle"] = {
        "generated_at_utc": now,
        "version": VERSION,
        "pool_size": len(pool),
        "active": [b for b, r in pool.items() if r.get("status") == "TRACKING"],
        "success_tracked": [b for b, r in pool.items() if r.get("status") == "SUCCESS_TRACKED"],
        "principle": "top5 is display only; once early/urgent is detected it remains in lifecycle state until explicit invalidation, success, or expiry",
    }
    save(BRIDGE_FILE, bridge)
    save(STATE_FILE, {"updated_at_utc": now, "version": VERSION, "pool": pool})
    save(OUT_FILE, bridge["persistent_candidate_lifecycle"] | {"removed_this_run": removed})
    print(json.dumps(bridge["persistent_candidate_lifecycle"], ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
