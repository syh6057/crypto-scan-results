import json
from datetime import datetime, timezone
from pathlib import Path

VERSION = "v41-pre-ignition-precursor"
BRIDGE_FILE = "beam_breakout_bridge.json"
OUT_FILE = "beam_precursor_alert.json"

def load(path, default):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except Exception:
        return default

def f(v, default=0.0):
    try: return float(v)
    except Exception: return default

def main():
    bridge = load(BRIDGE_FILE, {})
    rows = []
    seen = set()
    for key in ("watch_top5", "top_candidates", "persistent_candidate_pool"):
        for r in bridge.get(key) or []:
            if not isinstance(r, dict) or not r.get("base") or r["base"] in seen: continue
            seen.add(r["base"]); rows.append(r)

    picks = []
    late = []
    for r in rows:
        ch = f(r.get("change_24h_pct"))
        m = r.get("metrics") or {}
        sig = r.get("signals") or {}
        vol = f(m.get("m15_vol_spike_x"))
        persist = f(m.get("m15_vol_persistence_x"))
        flow = f(m.get("buy_sell_ratio"))
        depth = f(m.get("bid_ask_depth_ratio"))
        money = f(m.get("money_leads_price_score"))
        pace = f(m.get("fast_turnover_intensity_x"))
        upper = f(m.get("m15_upper_wick_pct"))
        m15hl = bool(m.get("m15_low_rising"))
        m15ma = bool(m.get("m15_close_above_ma"))
        h1hl = bool(m.get("h1_low_rising"))
        h1ma = bool(m.get("h1_close_above_ma"))

        # Explicit anti-late gate: PRL-like moves are not precursor entries.
        if ch >= 7.0:
            late.append({"base":r["base"],"price_krw":r.get("price_krw"),"change_24h_pct":ch,"reason":"change24_ge_7_no_chase"})
            continue
        # 5-7% is allowed only as observation, never as a precursor buy candidate.
        if ch >= 5.0:
            late.append({"base":r["base"],"price_krw":r.get("price_krw"),"change_24h_pct":ch,"reason":"change24_ge_5_late_watch"})
            continue

        checks = {
            "price_early_0_to_4pct": 0.0 <= ch < 4.0,
            "m15_volume_leads": vol >= 1.5,
            "m15_persistence": persist >= 1.2,
            "buy_flow": flow >= 1.15,
            "m15_higher_low": m15hl,
            "no_exhaustion_wick": upper <= 1.5,
        }
        # 1h structure is confirmation, not a hard precursor requirement.
        confirmation = {"m15_above_ma":m15ma,"h1_higher_low":h1hl,"h1_above_ma":h1ma,"orderbook_support":depth>=0.8}
        score = sum(checks.values())*12 + sum(confirmation.values())*5 + min(money,30)*0.4 + min(pace,3)*3
        hard = all(checks.values())
        if hard:
            picks.append({
                "base":r["base"],"market":r.get("market"),"price_krw":r.get("price_krw"),
                "change_24h_pct":ch,"precursor_score":round(score,2),
                "metrics":{"m15_vol_spike_x":vol,"m15_vol_persistence_x":persist,"buy_sell_ratio":flow,
                           "bid_ask_depth_ratio":depth,"money_leads_price_score":money,
                           "fast_turnover_intensity_x":pace,"m15_low_rising":m15hl,
                           "m15_close_above_ma":m15ma,"h1_low_rising":h1hl,"h1_close_above_ma":h1ma},
                "checks":checks,"confirmation":confirmation,
                "permission":"EARLY_WATCH_ONLY",
                "note":"Precursor detection only. External news/unlock/listing/tokenomics check required before any money."
            })

    picks.sort(key=lambda x:x["precursor_score"], reverse=True)
    out={"generated_at_utc":datetime.now(timezone.utc).isoformat(),"version":VERSION,
         "status":"PRECURSOR_FOUND" if picks else "NO_PRECURSOR",
         "policy":{"goal":"detect money/volume before vertical price move","preferred_change24_pct":"0 to <4",
                   "change24_ge_5":"late watch only","change24_ge_7":"no chase",
                   "h1_structure":"confirmation, not hard precursor gate","execution":"never automatic"},
         "precursors":picks[:5],"late_no_chase":late[:10]}
    Path(OUT_FILE).write_text(json.dumps(out,ensure_ascii=False,indent=2),encoding="utf-8")
    print(json.dumps(out,ensure_ascii=False,indent=2))

if __name__ == "__main__":
    main()
