import json
from datetime import datetime, timezone
from pathlib import Path

VERSION = "v24.1-episode-aware-first-seen"
SCAN_FILE = "beam_scan_result.json"
FIRST_SEEN_FILE = "beam_first_seen.json"
EPISODE_GAP_MINUTES = 60
EPISODE_STALE_MINUTES = 360
EPISODE_PEAK_DRAWDOWN_RESET_PCT = 8.0


def load_json(path, default):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except Exception:
        return default


def fnum(value, default=0.0):
    try:
        return float(value)
    except Exception:
        return default


def parse_dt(value):
    try:
        return datetime.fromisoformat(value) if value else None
    except Exception:
        return None


def pct(a, b):
    a, b = fnum(a), fnum(b)
    return ((a / b) - 1.0) * 100.0 if a > 0 and b > 0 else None


def should_reset_episode(item, generated_at, price, chg):
    start_price = fnum(item.get("episode_start_price_krw"))
    peak_price = fnum(item.get("episode_peak_price_krw"))
    started = parse_dt(item.get("episode_started_at_utc"))
    last_seen = parse_dt(item.get("episode_last_seen_at_utc") or item.get("last_seen_at_utc"))
    now = parse_dt(generated_at) or datetime.now(timezone.utc)

    if start_price <= 0 or started is None:
        return True, "episode_missing"
    if last_seen is None:
        return True, "episode_last_seen_missing"

    gap_minutes = (now - last_seen).total_seconds() / 60.0
    if gap_minutes > EPISODE_GAP_MINUTES:
        return True, "stage2_gap_over_60m"

    age_minutes = (now - started).total_seconds() / 60.0
    current_from_start = pct(price, start_price)
    if age_minutes > EPISODE_STALE_MINUTES and current_from_start is not None and current_from_start <= 1.0 and chg <= 4.0:
        return True, "stale_episode_without_expansion"

    drawdown = pct(price, peak_price) if peak_price > 0 else None
    if drawdown is not None and drawdown <= -EPISODE_PEAK_DRAWDOWN_RESET_PCT and chg <= 6.0:
        return True, "old_episode_peak_drawdown"

    return False, None


def start_episode(item, generated_at, price, chg, score, row, reason):
    item["episode_started_at_utc"] = generated_at
    item["episode_start_price_krw"] = price
    item["episode_start_change_24h_pct"] = chg
    item["episode_start_score"] = score
    item["episode_start_track"] = row.get("hard_track") or row.get("track")
    item["episode_peak_price_krw"] = price
    item["episode_peak_change_24h_pct"] = chg
    item["episode_runs"] = 1
    item["episode_reset_reason"] = reason


def update_stage2_first_seen():
    scan = load_json(SCAN_FILE, {})
    hist = load_json(FIRST_SEEN_FILE, {})
    if not isinstance(hist, dict):
        hist = {}

    generated_at = scan.get("generated_at_utc") or datetime.now(timezone.utc).isoformat()
    rows = scan.get("stage2_ranked") or []
    tracked, created, episode_resets = [], [], []

    for row in rows:
        base = row.get("base")
        price = fnum(row.get("price_krw"))
        if not base or price <= 0:
            continue

        chg = fnum(row.get("change_24h_pct"))
        score = fnum(row.get("beam_score_v18"), fnum(row.get("beam_score")))
        item = hist.get(base)
        if not isinstance(item, dict):
            item = {
                "first_detected_at_utc": generated_at,
                "first_price_krw": price,
                "first_change_24h_pct": chg,
                "first_score": score,
                "first_track": row.get("hard_track") or row.get("track"),
                "first_detection_source": "RAW_STAGE2",
                "first_beam_eligible_v18": bool(row.get("beam_eligible_v18")),
            }
            created.append(base)
        else:
            item.setdefault("first_detection_source", "V18_ELIGIBLE_OR_LEGACY")
            item.setdefault("first_beam_eligible_v18", bool(row.get("beam_eligible_v18")))

        reset, reset_reason = should_reset_episode(item, generated_at, price, chg)
        if reset:
            start_episode(item, generated_at, price, chg, score, row, reset_reason)
            episode_resets.append({"base": base, "reason": reset_reason})
        else:
            item["episode_runs"] = int(item.get("episode_runs") or 0) + 1
            item["episode_peak_price_krw"] = max(fnum(item.get("episode_peak_price_krw")), price)
            item["episode_peak_change_24h_pct"] = max(fnum(item.get("episode_peak_change_24h_pct"), -999), chg)

        item["episode_last_seen_at_utc"] = generated_at
        ep_start = fnum(item.get("episode_start_price_krw"))
        ep_peak = fnum(item.get("episode_peak_price_krw"))
        item["episode_return_pct"] = round(pct(price, ep_start), 2) if pct(price, ep_start) is not None else None
        item["episode_peak_return_pct"] = round(pct(ep_peak, ep_start), 2) if pct(ep_peak, ep_start) is not None else None
        item["episode_giveback_from_peak_pct"] = round(pct(price, ep_peak), 2) if pct(price, ep_peak) is not None else None

        item["last_seen_at_utc"] = generated_at
        item["last_price_krw"] = price
        item["last_change_24h_pct"] = chg
        item["last_score"] = score
        item["last_track"] = row.get("hard_track") or row.get("track")
        item["last_detection_source"] = "RAW_STAGE2"
        item["last_beam_eligible_v18"] = bool(row.get("beam_eligible_v18"))
        item["peak_price_krw"] = max(fnum(item.get("peak_price_krw")), price)
        item["peak_change_24h_pct"] = max(fnum(item.get("peak_change_24h_pct"), -999), chg)

        first_price = fnum(item.get("first_price_krw"))
        if first_price > 0:
            item["return_since_first_pct"] = round((price / first_price - 1.0) * 100.0, 2)
            item["peak_return_since_first_pct"] = round((item["peak_price_krw"] / first_price - 1.0) * 100.0, 2)
        else:
            item["return_since_first_pct"] = None
            item["peak_return_since_first_pct"] = None

        hist[base] = item
        tracked.append(base)

    Path(FIRST_SEEN_FILE).write_text(json.dumps(hist, ensure_ascii=False, indent=2), encoding="utf-8")

    scan.setdefault("logic", {})["first_seen_recall_patch"] = {
        "version": VERSION,
        "policy": "preserve lifetime first detection for audit, plus resettable episode anchors for current ignition/promotion decisions",
        "tracked_stage2_count": len(tracked),
        "new_stage2_first_seen_count": len(created),
        "new_stage2_first_seen": created[:50],
        "episode_reset_count": len(episode_resets),
        "episode_resets": episode_resets[:50],
        "episode_gap_minutes": EPISODE_GAP_MINUTES,
        "episode_stale_minutes": EPISODE_STALE_MINUTES,
        "episode_peak_drawdown_reset_pct": EPISODE_PEAK_DRAWDOWN_RESET_PCT,
    }
    Path(SCAN_FILE).write_text(json.dumps(scan, ensure_ascii=False, indent=2), encoding="utf-8")

    out = {
        "generated_at_utc": generated_at,
        "version": VERSION,
        "stage2_rows": len(rows),
        "tracked_stage2_count": len(tracked),
        "new_stage2_first_seen_count": len(created),
        "new_stage2_first_seen": created,
        "episode_reset_count": len(episode_resets),
        "episode_resets": episode_resets,
    }
    print(json.dumps(out, ensure_ascii=False, indent=2))
    return out


def main():
    update_stage2_first_seen()


if __name__ == "__main__":
    main()
