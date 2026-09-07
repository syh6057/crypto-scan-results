import json
from datetime import datetime, timezone
from pathlib import Path

VERSION = "v24-raw-stage2-first-seen"
SCAN_FILE = "beam_scan_result.json"
FIRST_SEEN_FILE = "beam_first_seen.json"


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


def update_stage2_first_seen():
    scan = load_json(SCAN_FILE, {})
    hist = load_json(FIRST_SEEN_FILE, {})
    if not isinstance(hist, dict):
        hist = {}

    generated_at = scan.get("generated_at_utc") or datetime.now(timezone.utc).isoformat()
    rows = scan.get("stage2_ranked") or []
    tracked = []
    created = []

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
        "policy": "every valid raw stage2 row is recorded immediately, even if v18 eligibility rejects it",
        "tracked_stage2_count": len(tracked),
        "new_stage2_first_seen_count": len(created),
        "new_stage2_first_seen": created[:50],
    }
    Path(SCAN_FILE).write_text(json.dumps(scan, ensure_ascii=False, indent=2), encoding="utf-8")

    out = {
        "generated_at_utc": generated_at,
        "version": VERSION,
        "stage2_rows": len(rows),
        "tracked_stage2_count": len(tracked),
        "new_stage2_first_seen_count": len(created),
        "new_stage2_first_seen": created,
    }
    print(json.dumps(out, ensure_ascii=False, indent=2))
    return out


def main():
    update_stage2_first_seen()


if __name__ == "__main__":
    main()
