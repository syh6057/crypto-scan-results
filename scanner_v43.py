import json
from datetime import datetime, timezone
from pathlib import Path
import requests

VERSION="v44-live-bithumb-ticker-refresh"
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

def live_ticker(markets):
    """Fetch fresh Bithumb KRW ticker data. Fail closed if unavailable."""
    out={}
    try:
        # Bithumb public v1 ticker accepts comma-separated markets.
        for i in range(0,len(markets),100):
            chunk=markets[i:i+100]
            r=requests.get(API+"/v1/ticker",params={"markets":",".join(chunk)},timeout=8,
                headers={"User-Agent":"crypto-beam-scanner/44","Accept":"application/json"})
            r.raise_for_status()
            for x in r.json():
                m=x.get("market")
                if not m: continue
                px=f(x.get("trade_price"),0)
                ch=f(x.get("signed_change_rate"),0)*100
                if px>0: out[m]={"price_krw":px,"change_24h_pct":round(ch,4),"timestamp":x.get("timestamp")}
        return out,None
    except Exception as e:
        return {},str(e)[:160]

def main():
    b=load(BRIDGE_FILE,{})
    rows=[]; seen=set()
    for key in ("watch_top5","top_candidates","persistent_candidate_pool"):
        for r in b.get(key) or []:
            if isinstance(r,dict) and r.get("base") and r["base"] not in seen:
                seen.add(r["base"]); rows.append(r)
    # Refresh every candidate against Bithumb live ticker before any gate.
    markets=[r.get("market") or ("KRW-"+r["base"]) for r in rows]
    live,live_err=live_ticker(markets)
    picks=[]; late=[]
    if live_err:
        out={"generated_at_utc":datetime.now(timezone.utc).isoformat(),"version":VERSION,
             "status":"NO_PRECURSOR","reason":"live_bithumb_ticker_unavailable_fail_closed","error":live_err,
             "precursors":[],"late_no_chase":[]}
        Path(OUT_FILE).write_text(json.dumps(out,ensure_ascii=False,indent=2),encoding="utf-8")
        print(json.dumps(out,ensure_ascii=False,indent=2)); return
    for r in rows:
        market=r.get("market") or ("KRW-"+r["base"])
        lt=live.get(market)
        if not lt:
            late.append({"base":r["base"],"market":market,"reason":"live_ticker_missing_fail_closed"}); continue
        bridge_price=f(r.get("price_krw"))
        live_price=f(lt.get("price_krw"))
        ch=f(lt.get("change_24h_pct"))
        m=r.get("metrics") or {}
        vol=f(m.get("m15_vol_spike_x")); persist=f(m.get("m15_vol_persistence_x")); flow=f(m.get("buy_sell_ratio"))
        depth=f(m.get("bid_ask_depth_ratio")); money=f(m.get("money_leads_price_score")); pace=f(m.get("fast_turnover_intensity_x"))
        upper=f(m.get("m15_upper_wick_pct")); week=f(m.get("week_change_pct"))
        m15hl=bool(m.get("m15_low_rising")); m15ma=bool(m.get("m15_close_above_ma")); h1hl=bool(m.get("h1_low_rising")); h1ma=bool(m.get("h1_close_above_ma"))
        x=h4_expansion(market); exp=f((x or {}).get("low_to_high_pct"),999 if x and x.get("error") else 0)
        if x and x.get("error"):
            late.append({"base":r["base"],"price_krw":live_price,"reason":"h4_data_unavailable_fail_closed","h4_expansion":x}); continue
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
            picks.append({"base":r["base"],"market":market,"price_krw":live_price,"bridge_price_krw":bridge_price,"live_price_delta_pct":round((live_price/bridge_price-1)*100,2) if bridge_price>0 else None,"change_24h_pct":ch,"week_change_pct":week,"h4_18bar_expansion":x,"precursor_score":round(score,2),"metrics":{"m15_vol_spike_x":vol,"m15_vol_persistence_x":persist,"buy_sell_ratio":flow,"bid_ask_depth_ratio":depth,"money_leads_price_score":money,"fast_turnover_intensity_x":pace},"confirmation":conf,"permission":"EARLY_WATCH_ONLY"})
    picks.sort(key=lambda x:x["precursor_score"],reverse=True)
    out={"generated_at_utc":datetime.now(timezone.utc).isoformat(),"version":VERSION,"status":"PRECURSOR_FOUND" if picks else "NO_PRECURSOR","policy":{"price_source":"Bithumb /v1/ticker refreshed in same run; fail closed","real_h4_18bar_low_to_high_ge_15":"NO_CHASE","week_ge_12":"NO_CHASE","24h_ge_5":"LATE","execution":"never automatic"},"precursors":picks[:5],"late_no_chase":late[:20]}
    Path(OUT_FILE).write_text(json.dumps(out,ensure_ascii=False,indent=2),encoding="utf-8"); print(json.dumps(out,ensure_ascii=False,indent=2))
if __name__=="__main__":main()
