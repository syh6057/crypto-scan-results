import json
import math
from datetime import datetime, timezone
from pathlib import Path

VERSION = "v34-urgent-winner-pattern-selector"
BRIDGE_FILE = "beam_breakout_bridge.json"
HISTORY_FILE = "breakout_bridge_history.json"
AUDIT_FILE = "beam_miss_audit.json"
OUT_FILE = "urgent_winner_selector.json"
STATE_FILE = "urgent_winner_state.json"

MIN_POSITIVE_EXAMPLES = 3
MIN_POS_SIM = 0.58
MIN_EDGE = 0.06
MAX_ENTRY_CHANGE_PCT = 8.5
MIN_VOL_PERSISTENCE = 1.10
MIN_BUY_SELL_RATIO = 0.90
MAX_UPPER_WICK_PCT = 2.0
MIN_SHORT_PACE = -0.05


def load_json(path, default):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except Exception:
        return default


def save_json(path, value):
    Path(path).write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")


def f(v, default=0.0):
    try:
        return float(v)
    except Exception:
        return default


def clip(x, lo, hi):
    return max(lo, min(hi, x))


def feature_vector(row):
    m = row.get("metrics") or {}
    ch = f(row.get("change_24h_pct"))
    trade = max(1.0, f(row.get("trade_24h_krw"), 1.0))
    vsp = max(0.0, f(m.get("m15_vol_spike_x")))
    vper = max(0.0, f(m.get("m15_vol_persistence_x")))
    bsr = max(0.0, f(m.get("buy_sell_ratio")))
    depth = max(0.0, f(m.get("bid_ask_depth_ratio")))
    pace = f(m.get("price_change_per_5m_pct"))
    wick = max(0.0, f(m.get("m15_upper_wick_pct")))
    money = max(0.0, f(m.get("money_leads_price_score")))
    turnover = max(0.0, f(m.get("fast_turnover_intensity_x")))
    beam = max(0.0, f(m.get("beam_score")))
    return [
        clip((ch + 2.0) / 12.0, 0.0, 1.0),
        clip((math.log10(trade) - 8.0) / 3.0, 0.0, 1.0),
        clip(math.log1p(vsp) / math.log(61.0), 0.0, 1.0),
        clip(math.log1p(vper) / math.log(31.0), 0.0, 1.0),
        1.0 if m.get("m15_close_above_ma") else 0.0,
        1.0 if m.get("h1_close_above_ma") else 0.0,
        1.0 if m.get("m15_low_rising") else 0.0,
        1.0 if m.get("h1_low_rising") else 0.0,
        clip(bsr / 3.0, 0.0, 1.0),
        clip(depth / 3.0, 0.0, 1.0),
        clip((pace + 1.0) / 3.0, 0.0, 1.0),
        clip(1.0 - wick / 3.0, 0.0, 1.0),
        clip(money / 40.0, 0.0, 1.0),
        clip(math.log1p(turnover) / math.log(21.0), 0.0, 1.0),
        clip(beam / 180.0, 0.0, 1.0),
        clip(f(row.get("signal_count")) / 10.0, 0.0, 1.0),
        clip(f(row.get("flow_confirmation_count")) / 5.0, 0.0, 1.0),
    ]


def distance(a, b):
    if not a or not b or len(a) != len(b):
        return 9.0
    return math.sqrt(sum((x-y)**2 for x, y in zip(a, b)) / len(a))


def similarity(vec, bank):
    if not bank:
        return 0.0
    ds = sorted(distance(vec, x) for x in bank)
    k = min(3, len(ds))
    d = sum(ds[:k]) / k
    return math.exp(-4.0 * d * d)


def iter_history_rows(history):
    for snap in history if isinstance(history, list) else []:
        for key in ("promoted", "watch"):
            for row in snap.get(key) or []:
                if isinstance(row, dict) and row.get("base"):
                    yield row


def earliest_rows_by_base(history):
    out = {}
    for row in iter_history_rows(history):
        base = row.get("base")
        if base not in out:
            out[base] = row
    return out


def raw_urgent_candidates(bridge):
    rows = []
    seen = set()
    for row in (bridge.get("promoted_top3") or []) + (bridge.get("watch_top5") or []):
        if not isinstance(row, dict):
            continue
        base = row.get("base")
        if not base or base in seen:
            continue
        # v33 attaches execution_gate only to rows that were URGENT/promoted before its gate.
        was_urgent = bool(row.get("execution_gate")) or str(row.get("promotion_grade") or "").startswith("A_")
        if not was_urgent:
            continue
        seen.add(base)
        rows.append(row)
    return rows


def hard_live_checks(row):
    m = row.get("metrics") or {}
    checks = {
        "still_early_not_already_pumped": -1.0 <= f(row.get("change_24h_pct"), 999.0) <= MAX_ENTRY_CHANGE_PCT,
        "persistent_15m_volume": f(m.get("m15_vol_persistence_x")) >= MIN_VOL_PERSISTENCE,
        "price_above_15m_ma": bool(m.get("m15_close_above_ma")),
        "buy_flow_not_sell_dominant": f(m.get("buy_sell_ratio")) >= MIN_BUY_SELL_RATIO,
        "short_pace_not_rolling_over": f(m.get("price_change_per_5m_pct"), -999.0) >= MIN_SHORT_PACE,
        "upper_wick_not_exhausted": f(m.get("m15_upper_wick_pct"), 999.0) <= MAX_UPPER_WICK_PCT,
    }
    return checks, [k for k, ok in checks.items() if not ok]


def main():
    now = datetime.now(timezone.utc).isoformat()
    bridge = load_json(BRIDGE_FILE, {})
    history = load_json(HISTORY_FILE, [])
    audit = load_json(AUDIT_FILE, {})
    earliest = earliest_rows_by_base(history)

    positive_bases = [
        x.get("base") for x in (audit.get("current_top_movers") or [])
        if x.get("capture") == "EARLY_CAPTURE" and f(x.get("change_24h_pct")) >= 10.0 and x.get("base")
    ]
    negative_bases = [x.get("base") for x in (audit.get("failed_acceleration_examples") or []) if x.get("base")]

    positive_rows = [earliest[b] for b in positive_bases if b in earliest]
    negative_rows = [earliest[b] for b in negative_bases if b in earliest]
    pos_bank = [feature_vector(x) for x in positive_rows]
    neg_bank = [feature_vector(x) for x in negative_rows]

    scored = []
    for row in raw_urgent_candidates(bridge):
        vec = feature_vector(row)
        pos_sim = similarity(vec, pos_bank)
        neg_sim = similarity(vec, neg_bank)
        edge = pos_sim - neg_sim if neg_bank else pos_sim - 0.50
        checks, failed = hard_live_checks(row)
        winner_match = (
            len(pos_bank) >= MIN_POSITIVE_EXAMPLES
            and pos_sim >= MIN_POS_SIM
            and edge >= MIN_EDGE
            and not failed
        )
        out = dict(row)
        out["winner_pattern"] = {
            "version": VERSION,
            "positive_examples": [x.get("base") for x in positive_rows],
            "negative_examples": [x.get("base") for x in negative_rows],
            "positive_similarity": round(pos_sim, 4),
            "negative_similarity": round(neg_sim, 4),
            "winner_edge": round(edge, 4),
            "live_checks": checks,
            "failed_live_checks": failed,
            "winner_match": winner_match,
            "logic": "select only among URGENT_EARLY detections by resemblance to historically early-captured movers; failed accelerations are contrast examples, not the target class",
        }
        out["execution_allowed"] = bool(winner_match)
        out["promotion_grade"] = "A_URGENT_WINNER_MATCH" if winner_match else "WATCH_URGENT_NONWINNER_PATTERN"
        scored.append(out)

    scored.sort(key=lambda x: (
        1 if (x.get("winner_pattern") or {}).get("winner_match") else 0,
        f((x.get("winner_pattern") or {}).get("winner_edge")),
        f((x.get("winner_pattern") or {}).get("positive_similarity")),
        f(x.get("priority_score")),
    ), reverse=True)

    winners = [x for x in scored if (x.get("winner_pattern") or {}).get("winner_match")]
    nonwinners = [x for x in scored if not (x.get("winner_pattern") or {}).get("winner_match")]

    # Preserve ordinary watches, but only winner-pattern URGENTs may remain promoted/actionable.
    ordinary_watch = []
    urgent_bases = {x.get("base") for x in scored}
    for row in bridge.get("watch_top5") or []:
        if row.get("base") not in urgent_bases:
            ordinary_watch.append(row)

    bridge["promoted_top3"] = winners[:3]
    bridge["watch_top5"] = (nonwinners + ordinary_watch)[:5]
    bridge["top_candidates"] = (winners + nonwinners + ordinary_watch)[:5]
    bridge["winner_pattern_selector"] = {
        "generated_at_utc": now,
        "version": VERSION,
        "positive_bases_requested": positive_bases,
        "positive_bases_with_history": [x.get("base") for x in positive_rows],
        "negative_bases_with_history": [x.get("base") for x in negative_rows],
        "winner_matches": [x.get("base") for x in winners],
        "rejected_urgent": [x.get("base") for x in nonwinners],
        "minimum_positive_examples": MIN_POSITIVE_EXAMPLES,
        "principle": "URGENT is candidate pool; actionable means it looks like the URGENT alerts that actually became >=10% movers, not merely that it avoids past failures.",
    }
    bridge["version"] = f"{bridge.get('version','')}+{VERSION}"
    bridge["status"] = "URGENT_WINNER_MATCH" if winners else ("WATCH_ONLY" if bridge.get("watch_top5") else "NO_SIGNAL")
    save_json(BRIDGE_FILE, bridge)

    result = {
        "generated_at_utc": now,
        "version": VERSION,
        "status": bridge["status"],
        "winner_matches": [
            {
                "base": x.get("base"),
                "price_krw": x.get("price_krw"),
                "change_24h_pct": x.get("change_24h_pct"),
                "positive_similarity": (x.get("winner_pattern") or {}).get("positive_similarity"),
                "negative_similarity": (x.get("winner_pattern") or {}).get("negative_similarity"),
                "winner_edge": (x.get("winner_pattern") or {}).get("winner_edge"),
            } for x in winners[:3]
        ],
        "rejected_urgent": [
            {
                "base": x.get("base"),
                "failed_live_checks": (x.get("winner_pattern") or {}).get("failed_live_checks"),
                "positive_similarity": (x.get("winner_pattern") or {}).get("positive_similarity"),
                "negative_similarity": (x.get("winner_pattern") or {}).get("negative_similarity"),
                "winner_edge": (x.get("winner_pattern") or {}).get("winner_edge"),
            } for x in nonwinners
        ],
        "positive_examples": [x.get("base") for x in positive_rows],
        "negative_examples": [x.get("base") for x in negative_rows],
    }
    save_json(OUT_FILE, result)
    save_json(STATE_FILE, result)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
