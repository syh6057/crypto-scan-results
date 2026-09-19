import json
from datetime import datetime, timezone
from pathlib import Path
import requests

VERSION="v43-real-h4-expansion-gate"
BRIDGE_FILE="beam_breakout_bridge.json"
OUT_FILE="beam_precursor_alert.json"
API="https://api.bithumb.com"

def load(p,d):
    try:return json.loads(Path(p).read_text(encoding="utf-8"))
    except Exception:return d
def f(v,d=0.0):
    try:return float(v)
    except Exception:return d
def h4_expansion(market,count=18):
    try:
        r=requests.get(API+"/v1/candles/minutes/240",params={"market":market,"count":count},timeout=8,headers={"User-Agent":"crypto-beam-scanner/43","Accept":"application/json"})
        r.raise_for_status(); a=r.json()
        lows=[f(x.get("low_price")) for x in a if f(x.get("low_price"))>0]
        highs=[f(x.get("high_price")) for x in a if f(x.get("high_price"))>0]
        if not lows or not highs:return None
        lo=min(lows); hi=max(highs)
        return {"bars":len(a),"low":lo,"high":hi,"low_to_high_pct":round((hi/lo-1)*100,2)}
    except Exception as e:return {"error":str(e)[:120]}

def main():
    b=load(BRIDGE_FILE,{})
    rows=[]; seen=set()
    for key in ("watch_top5","top_candidates","persistent_candidate_pool"):
        for r in b.get(key) or []:
            if isinstance(r,dict) and r.get("base") and r["base"] not in seen:
                seen.add(r["base"]); rows.append(r)
    picks=[]; late=[]
    for r in rows:
        ch=f(r.get("change_24h_pct")); m=r.get("metrics") or {}
        vol=f(m.get("m15_vol_spike_x")); persist=f(m.get("m15_vol_persistence_x")); flow=f(m.get("buy_sell_ratio"))
        depth=f(m.get("bid_ask_depth_ratio")); money=f(m.get("money_leads_price_score")); pace=f(m.get("fast_turnover_intensity_x"))
        upper=f(m.get("m15_upper_wick_pct")); week=f(m.get("week_change_pct"))
        m15hl=bool(m.get("m15_low_rising")); m15ma=bool(m.get("m15_close_above_ma")); h1hl=bool(m.get("h1_low_rising")); h1ma=bool(m.get("h1_close_above_ma"))
        market=r.get("market") or ("KRW-"+r["base"])
        x=h4_expansion(market); exp=f((x or {}).get("low_to_high_pct"),999 if x and x.get("error") else 0)
        if x and x.get("error"):
            late.append({"base":r["base"],"price_krw":r.get("price_krw"),"reason":"h4_data_unavailable_fail_closed","h4_expansion":x}); continue
        if exp>=15:
            late.append({"base":r["base"],"price_krw":r.get("price_krw"),"change_24h_pct":ch,"week_change_pct":week,"h4_18bar_expansion_pct":exp,"reason":"real_h4_already_expanded"}); continue
        if week>=12:
            late.append({"base":r["base"],"price_krw":r.get("price_krw"),"change_24h_pct":ch,"week_change_pct":week,"h4_18bar_expansion_pct":exp,"reason":"week_already_expanded"}); continue
        if ch>=7:
            late.append({"base":r["base"],"price_krw":r.get("price_krw"),"change_24h_pct":ch,"reason":"change24_ge_7_no_chase"}); continue
        if ch>=5:
            late.append({"base":r["base"],"price_krw":r.get("price_krw"),"change_24h_pct":ch,"reason":"change24_ge_5_late_watch"}); continue
        checks={"price_early_0_to_4pct":0<=ch<4,"m15_volume_leads":vol>=1.5,"m15_persistence":persist>=1.2,"buy_flow":flow>=1.15,"m15_higher_low":m15hl,"no_exhaustion_wick":upper<=1.5,"real_h4_not_expanded":exp<15}
        conf={"m15_above_ma":m15ma,"h1_higher_low":h1hl,"h1_above_ma":h1ma,"orderbook_support":depth>=0.8}
        if all(checks.values()):
            score=sum(checks.values())*12+sum(conf.values())*5+min(money,30)*.4+min(pace,3)*3
            picks.append({"base":r["base"],"market":market,"price_krw":r.get("price_krw"),"change_24h_pct":ch,"week_change_pct":week,"h4_18bar_expansion":x,"precursor_score":round(score,2),"metrics":{"m15_vol_spike_x":vol,"m15_vol_persistence_x":persist,"buy_sell_ratio":flow,"bid_ask_depth_ratio":depth,"money_leads_price_score":money,"fast_turnover_intensity_x":pace},"confirmation":conf,"permission":"EARLY_WATCH_ONLY"})
    picks.sort(key=lambda x:x["precursor_score"],reverse=True)
    out={"generated_at_utc":datetime.now(timezone.utc).isoformat(),"version":VERSION,"status":"PRECURSOR_FOUND" if picks else "NO_PRECURSOR","policy":{"real_h4_18bar_low_to_high_ge_15":"NO_CHASE","week_ge_12":"NO_CHASE","24h_ge_5":"LATE","execution":"never automatic"},"precursors":picks[:5],"late_no_chase":late[:20]}
    Path(OUT_FILE).write_text(json.dumps(out,ensure_ascii=False,indent=2),encoding="utf-8"); print(json.dumps(out,ensure_ascii=False,indent=2))
if __name__=="__main__":main()
