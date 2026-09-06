import json
import math
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from statistics import mean, median
import requests

BITHUMB_BASE = "https://api.bithumb.com"
BINANCE_BASE = "https://data-api.binance.vision"
VERSION = "v16-bithumb-beam-two-track"
MAX_STAGE1 = 160
MAX_STAGE2 = 30
MIN_STAGE1_TRADE_KRW = 100_000_000
MIN_ACTIONABLE_TRADE_KRW = 1_000_000_000
MAX_WORKERS = 8

STABLE_BASES = {"USDT","USDC","FDUSD","USDS","TUSD","DAI","PYUSD","USDP","XUSD","USD1","USDG","RLUSD","USDE","EURI","AEUR","BUSD","UST","USTC"}
SECTORS = {
    "L2": {"ARB","OP","STRK","ZK","MANTA","METIS","BLAST","ZORA"},
    "AI": {"FLOCK","0G","TAO","FET","WLD","ARKM","AGIX","NMR","RENDER","VIRTUAL","AIOZ"},
    "SOLANA_DEFI": {"RAY","JUP","JTO","PYTH","DRIFT","ORCA","KMNO","JITOSOL"},
    "DEFI": {"UNI","SUSHI","AAVE","CRV","LDO","PENDLE","ENA","ETHFI","MORPHO","COMP","SNX"},
    "RWA": {"ONDO","POLYX","LINK","CFG","OM","PLUME"},
    "MEME": {"DOGE","SHIB","BONK","PEPE","WIF","FLOKI","BOME","MEME","TRUMP"},
    "GAMING": {"AXS","SAND","MANA","IMX","GALA","RON","PIXEL","YGG","BIGTIME"},
    "KOREA_LOCAL": {"TEMCO","OBSR","AHT","MBL","META","HUNT","MVC","BOA","EL","BORA"},
}
PATTERNS_LEARNED = [
    "CATALYST_LEAD_PRICE_LAG: 재료가 먼저 알려지고 가격은 0~8%에서 지연 반응한 뒤 가속",
    "SECTOR_ROTATION: 선두 종목이 15~30% 이상 점화되면 같은 섹터 후속주로 수급 순환",
    "PRIOR_VOLUME_EXPANSION: 전일/직전 구간 거래대금이 평소 대비 2배 이상 늘어난 뒤 본격 급등",
    "FIRST_LEG_CONSOLIDATION_SECOND_LEG: 1차 +8~20% 후 거래량 유지·저점 상승·고점 재돌파 시 2차 가속",
    "LOCAL_ONLY_FLOW: Binance 교집합 밖 빗썸 로컬 종목도 탐색해야 함",
]

def now_iso(): return datetime.now(timezone.utc).isoformat()
def safe(v, default=0.0):
    try: return float(v)
    except Exception: return default

def pct(a, b):
    if a is None or b in (None, 0): return None
    return (float(a) / float(b) - 1.0) * 100.0

def rnd(v, n=2): return None if v is None else round(float(v), n)
def avg(vals):
    vals=[float(x) for x in vals if x is not None]
    return mean(vals) if vals else None

def med(vals):
    vals=[float(x) for x in vals if x is not None]
    return median(vals) if vals else None

def get_json(url, params=None, retries=3, timeout=15):
    last=None
    for i in range(retries):
        try:
            r=requests.get(url, params=params, timeout=timeout, headers={"User-Agent":"crypto-beam-scanner/16","Accept":"application/json"})
            if r.status_code==429:
                time.sleep(0.8+i*0.8); continue
            r.raise_for_status(); return r.json()
        except Exception as e:
            last=e; time.sleep(0.5+i*0.5)
    raise last

def get_markets():
    rows=get_json(BITHUMB_BASE+"/v1/market/all", {"isDetails":"true"})
    out={}
    for r in rows:
        m=r.get("market","")
        if not m.startswith("KRW-"): continue
        base=m.split("-",1)[1]
        if base in STABLE_BASES: continue
        out[m]={"market":m,"base":base,"korean_name":r.get("korean_name"),"english_name":r.get("english_name"),"market_warning":r.get("market_warning") or "NONE"}
    return out

def get_tickers(markets):
    result={}; codes=list(markets)
    for i in range(0,len(codes),40):
        try:
            rows=get_json(BITHUMB_BASE+"/v1/ticker", {"markets":",".join(codes[i:i+40])})
            for r in rows:
                if r.get("market"): result[r["market"]]=r
        except Exception: pass
        time.sleep(0.03)
    return result

def get_binance_24h():
    try:
        rows=get_json(BINANCE_BASE+"/api/v3/ticker/24hr", retries=2, timeout=15)
        return {r.get("symbol"):r for r in rows if r.get("symbol")}
    except Exception: return {}

def candles(market, kind, count):
    if kind in (5,15,60,240):
        url=BITHUMB_BASE+f"/v1/candles/minutes/{kind}"; params={"market":market,"count":count}
    elif kind=="days":
        url=BITHUMB_BASE+"/v1/candles/days"; params={"market":market,"count":count}
    else: raise ValueError(kind)
    rows=list(reversed(get_json(url,params,retries=2,timeout=12)))
    if kind in (5,15,60,240) and rows:
        interval_ms=int(kind)*60_000; now_ms=int(time.time()*1000)
        closed=[r for r in rows if int(r.get("timestamp") or 0) and int(r.get("timestamp") or 0)+interval_ms<=now_ms]
        if len(closed)>=max(3,len(rows)-2): rows=closed
    return rows

def candle_metrics(rows, lookback=12):
    if not rows or len(rows)<6: return {}
    closes=[safe(r.get("trade_price")) for r in rows]; opens=[safe(r.get("opening_price")) for r in rows]
    highs=[safe(r.get("high_price")) for r in rows]; lows=[safe(r.get("low_price")) for r in rows]
    vols=[safe(r.get("candle_acc_trade_price")) for r in rows]; latest=rows[-1]
    prior=vols[-(lookback+1):-1] if len(vols)>=lookback+1 else vols[:-1]
    prior4=vols[-6:-2] if len(vols)>=6 else vols[:-2]
    body_top=max(safe(latest.get("opening_price")),safe(latest.get("trade_price")))
    recent_lows=lows[-4:]
    return {
        "last_close":closes[-1],
        "last_price_pct":rnd(pct(closes[-1],opens[-1])),
        "vol_spike_x":rnd(vols[-1]/med(prior),2) if prior and med(prior) else None,
        "vol_persistence_x":rnd(avg(vols[-2:])/avg(prior4),2) if prior4 and avg(prior4) else None,
        "recent_positive_count":sum(1 for r in rows[-4:] if safe(r.get("trade_price"))>=safe(r.get("opening_price"))),
        "upper_wick_pct":rnd(pct(safe(latest.get("high_price")),body_top) or 0.0),
        "low_rising":len(recent_lows)>=3 and recent_lows[-1]>=recent_lows[-2]>=recent_lows[-3],
        "close_above_ma":closes[-1]>=avg(closes[-8:]) if len(closes)>=8 else False,
        "range_pct":rnd(pct(max(highs[-4:]),min(lows[-4:]))) if min(lows[-4:]) else None,
    }

def daily_metrics(rows):
    if not rows or len(rows)<5: return {}
    today=datetime.now(timezone.utc).date().isoformat(); completed=[]
    for r in rows:
        dt=str(r.get("candle_date_time_utc") or "")
        if dt[:10] and dt[:10]!=today: completed.append(r)
    if len(completed)<4: completed=rows[:-1] if len(rows)>4 else rows
    if len(completed)<3: return {}
    last=completed[-1]; prior=completed[:-1]
    last_trade=safe(last.get("candle_acc_trade_price")); prior_trades=[safe(r.get("candle_acc_trade_price")) for r in prior[-5:]]
    first_close=safe(completed[-7].get("trade_price")) if len(completed)>=7 else safe(completed[0].get("trade_price"))
    return {
        "previous_day_change_pct":rnd(pct(safe(last.get("trade_price")),safe(last.get("opening_price")))),
        "previous_day_volume_x":rnd(last_trade/avg(prior_trades),2) if avg(prior_trades) else None,
        "week_change_pct":rnd(pct(safe(completed[-1].get("trade_price")),first_close)),
    }

def prelim_score(t):
    ch=safe(t.get("signed_change_rate"))*100; trade=safe(t.get("acc_trade_price_24h")); high=safe(t.get("high_price")); price=safe(t.get("trade_price")); dist=abs(pct(price,high) or 0)
    s=min(20,max(0,math.log10(max(trade,1)/100_000_000+1)*8))
    if -2<=ch<=8:s+=16
    elif 8<ch<=18:s+=12
    elif -5<=ch<-2:s+=5
    elif 18<ch<=30:s+=6
    if dist<=4:s+=8
    elif dist<=8:s+=4
    return s

def base_row(info,t,binance24):
    base=info["base"]; price=safe(t.get("trade_price")); high=safe(t.get("high_price")); ch=safe(t.get("signed_change_rate"))*100
    b=binance24.get(base+"USDT",{}); bch=safe(b.get("priceChangePercent"),None) if b else None
    return {**info,"price_krw":price,"change_24h_pct":rnd(ch),"trade_24h_krw":safe(t.get("acc_trade_price_24h")),"day_high_krw":high,"day_low_krw":safe(t.get("low_price")),"distance_from_day_high_pct":rnd(abs(pct(price,high) or 0)),"binance_24h_change_pct":rnd(bch) if bch is not None else None,"bithumb_minus_binance_pct":rnd(ch-bch) if bch is not None else None,"warning_flag":info.get("market_warning") not in (None,"","NONE")}

def score_stage1(row):
    ch=safe(row.get("change_24h_pct")); trade=safe(row.get("trade_24h_krw")); m15=row.get("m15",{}); h1=row.get("h1",{}); d1=row.get("d1",{}); dist=safe(row.get("distance_from_day_high_pct"),99)
    liq=min(18,max(0,math.log10(max(trade,1)/100_000_000+1)*7))
    pre=liq
    if -1<=ch<=8:pre+=16
    elif 8<ch<=12:pre+=10
    elif -4<=ch<-1:pre+=5
    elif 12<ch<=18:pre+=3
    vsp=safe(m15.get("vol_spike_x")); vper=safe(m15.get("vol_persistence_x"))
    if vsp>=3:pre+=16
    elif vsp>=2:pre+=12
    elif vsp>=1.5:pre+=8
    if vper>=2:pre+=12
    elif vper>=1.4:pre+=8
    elif vper>=1.1:pre+=4
    p15=safe(m15.get("last_price_pct")); p1=safe(h1.get("last_price_pct"))
    if 0.15<=p15<=2.8:pre+=8
    elif -0.8<=p15<0.15:pre+=3
    if 0<=p1<=5:pre+=7
    elif -1.5<=p1<0:pre+=2
    if safe(m15.get("recent_positive_count"))>=3:pre+=5
    if m15.get("close_above_ma"):pre+=4
    if dist<=3.5:pre+=5
    pdvx=safe(d1.get("previous_day_volume_x")); pdc=safe(d1.get("previous_day_change_pct")); week=safe(d1.get("week_change_pct"))
    if pdvx>=3:pre+=12
    elif pdvx>=2:pre+=8
    elif pdvx>=1.4:pre+=4
    if 0<=pdc<=8:pre+=5
    if 3<=week<=35:pre+=5
    if safe(m15.get("upper_wick_pct"))>2.5:pre-=5
    accel=liq
    if 6<=ch<=22:accel+=18
    elif 2<=ch<6:accel+=8
    elif 22<ch<=35:accel+=7
    if 1.5<=dist<=10:accel+=12
    elif dist<1.5:accel+=5
    if vper>=1.6:accel+=14
    elif vper>=1.15:accel+=8
    if -1.2<=p15<=1.8:accel+=8
    if m15.get("low_rising"):accel+=10
    if m15.get("close_above_ma"):accel+=6
    if pdvx>=2:accel+=7
    if 5<=week<=60:accel+=6
    if safe(m15.get("upper_wick_pct"))>3:accel-=7
    row["pre_ignition_score_raw"]=rnd(pre); row["accelerator_score_raw"]=rnd(accel); return row

def orderbook_metrics(market):
    try:
        rows=get_json(BITHUMB_BASE+"/v1/orderbook",{"markets":market},retries=2,timeout=10)
        if not rows:return {}
        units=rows[0].get("orderbook_units",[])[:5]
        bid=sum(safe(u.get("bid_price"))*safe(u.get("bid_size")) for u in units); ask=sum(safe(u.get("ask_price"))*safe(u.get("ask_size")) for u in units)
        best_bid=safe(units[0].get("bid_price")) if units else 0; best_ask=safe(units[0].get("ask_price")) if units else 0; mid=(best_bid+best_ask)/2 if best_bid and best_ask else 0
        return {"bid_depth_top5_krw":rnd(bid,0),"ask_depth_top5_krw":rnd(ask,0),"bid_ask_depth_ratio":rnd(bid/ask,2) if ask else None,"spread_pct":rnd((best_ask-best_bid)/mid*100,3) if mid else None}
    except Exception:return {}

def trade_flow_metrics(market):
    try:
        rows=get_json(BITHUMB_BASE+"/v1/trades/ticks",{"market":market,"count":200},retries=2,timeout=10); buy=sell=0.0
        for r in rows:
            v=safe(r.get("trade_volume"))*safe(r.get("trade_price")); side=str(r.get("ask_bid") or "").upper()
            if side=="BID":buy+=v
            elif side=="ASK":sell+=v
        return {"recent_buy_krw":rnd(buy,0),"recent_sell_krw":rnd(sell,0),"buy_sell_ratio":rnd(buy/sell,2) if sell else (99.0 if buy else None)}
    except Exception:return {}

def sector_of(base):
    for name,members in SECTORS.items():
        if base in members:return name
    return "OTHER"

def add_sector_rotation(rows):
    bybase={r["base"]:r for r in rows}; leaders={}
    for sector,members in SECTORS.items():
        rr=[bybase[b] for b in members if b in bybase]
        if rr:
            lead=max(rr,key=lambda x:safe(x.get("change_24h_pct"),-999)); leaders[sector]={"base":lead["base"],"change_24h_pct":lead["change_24h_pct"],"trade_24h_krw":lead["trade_24h_krw"]}
    for row in rows:
        sector=sector_of(row["base"]); row["sector"]=sector; bonus=0; reason=None; leader=leaders.get(sector)
        if leader and leader["base"]!=row["base"]:
            lc=safe(leader.get("change_24h_pct")); ch=safe(row.get("change_24h_pct"))
            if lc>=30 and ch<=12:bonus=12; reason=f"{leader['base']} {lc:.1f}% 선두 후속주"
            elif lc>=15 and ch<=10:bonus=8; reason=f"{leader['base']} {lc:.1f}% 선두 후속주"
        row["sector_rotation_bonus"]=bonus; row["sector_rotation_reason"]=reason
    return leaders

def enrich_stage2(row):
    market=row["market"]
    try:row["m5"]=candle_metrics(candles(market,5,30),16)
    except Exception:row["m5"]={}
    try:row["h4"]=candle_metrics(candles(market,240,8),5)
    except Exception:row["h4"]={}
    row["orderbook"]=orderbook_metrics(market); row["trade_flow"]=trade_flow_metrics(market); return row

def final_score(row):
    pre=safe(row.get("pre_ignition_score_raw")); accel=safe(row.get("accelerator_score_raw")); m5=row.get("m5",{}); h4=row.get("h4",{}); ob=row.get("orderbook",{}); flow=row.get("trade_flow",{})
    ch=safe(row.get("change_24h_pct")); trade=safe(row.get("trade_24h_krw")); bonus=safe(row.get("sector_rotation_bonus"))
    v5=safe(m5.get("vol_spike_x")); vp5=safe(m5.get("vol_persistence_x"))
    if v5>=3:bonus+=12
    elif v5>=2:bonus+=8
    elif v5>=1.5:bonus+=5
    if vp5>=1.5:bonus+=7
    elif vp5>=1.1:bonus+=3
    if h4.get("low_rising"):bonus+=7
    if h4.get("close_above_ma"):bonus+=3
    bar=safe(ob.get("bid_ask_depth_ratio"))
    if bar>=1.5:bonus+=8
    elif bar>=1.15:bonus+=4
    elif 0<bar<0.65:bonus-=6
    bsr=safe(flow.get("buy_sell_ratio"))
    if bsr>=1.5:bonus+=10
    elif bsr>=1.15:bonus+=6
    elif 0<bsr<0.75:bonus-=7
    spread=safe(ob.get("spread_pct"),99)
    if spread<=0.4:bonus+=4
    elif spread>1.2:bonus-=8
    lr=row.get("bithumb_minus_binance_pct")
    if lr is not None:
        lr=safe(lr)
        if 1<=lr<=5 and ch<=15:bonus+=4
        elif lr>10 and ch>15:bonus-=6
    penalty=0
    if row.get("warning_flag"):penalty+=15
    if trade<MIN_ACTIONABLE_TRADE_KRW:penalty+=8
    if ch>35:penalty+=18
    elif ch>25:penalty+=8
    if safe(row.get("distance_from_day_high_pct"),99)>15:penalty+=6
    pre_final=pre+bonus-penalty; accel_final=accel+bonus-penalty; track="PRE_IGNITION" if pre_final>=accel_final else "ACCELERATOR"; score=max(pre_final,accel_final)
    row["pre_ignition_score"]=rnd(pre_final); row["accelerator_score"]=rnd(accel_final); row["track"]=track; row["beam_score"]=rnd(score); row["risk_penalty"]=rnd(penalty); row["needs_web_catalyst_check"]=True
    hard=score>=72 and not row.get("warning_flag") and trade>=MIN_ACTIONABLE_TRADE_KRW and spread<=1.2 and (bsr==0 or bsr>=0.85) and ch<=25
    watch=score>=62 and not row.get("warning_flag") and trade>=500_000_000 and ch<=30
    status="ENTER_CANDIDATE" if hard else ("WAIT_CONFIRMATION" if watch else ("SPECULATIVE_WARNING_ONLY" if row.get("warning_flag") and score>=60 else "WATCH"))
    price=safe(row.get("price_krw")); stop_pct=3.2 if track=="PRE_IGNITION" else 4.2
    row["entry_plan"]={"status":status,"reference_price_krw":rnd(price,8),"do_not_chase_above_krw":rnd(price*1.025,8),"invalidation_krw":rnd(price*(1-stop_pct/100),8),"target_1_krw":rnd(price*1.08,8),"target_2_krw":rnd(price*1.15,8),"target_3_krw":rnd(price*1.25,8)}
    return row

def fetch_stage1(market,info,ticker,binance24):
    row=base_row(info,ticker,binance24)
    try:
        row["m15"]=candle_metrics(candles(market,15,30),12); row["h1"]=candle_metrics(candles(market,60,24),12); row["d1"]=daily_metrics(candles(market,"days",9)); row["stage1_ok"]=bool(row["m15"] and row["h1"])
    except Exception as e:row["stage1_ok"]=False; row["stage1_error"]=str(e)
    return score_stage1(row)

def main():
    started=time.time(); hard_blockers=[]; warnings=[]
    try:markets=get_markets()
    except Exception as e:markets={}; hard_blockers.append(f"bithumb_market_list_failed:{e}")
    tickers=get_tickers(markets) if markets else {}
    if not tickers:hard_blockers.append("bithumb_ticker_unavailable")
    binance24=get_binance_24h()
    if not binance24:warnings.append("binance_optional_crosscheck_unavailable")
    base_rows=[]
    for market,info in markets.items():
        t=tickers.get(market)
        if not t or safe(t.get("acc_trade_price_24h"))<MIN_STAGE1_TRADE_KRW:continue
        base_rows.append((prelim_score(t),market,info,t))
    base_rows.sort(key=lambda x:x[0],reverse=True)
    top_liq=sorted(base_rows,key=lambda x:safe(x[3].get("acc_trade_price_24h")),reverse=True)[:70]; top_pre=base_rows[:100]
    top_change=sorted([x for x in base_rows if -2<=safe(x[3].get("signed_change_rate"))*100<=25],key=lambda x:safe(x[3].get("signed_change_rate")),reverse=True)[:60]
    selected=[]; seen=set()
    for group in (top_pre,top_liq,top_change):
        for item in group:
            if item[1] not in seen:selected.append(item);seen.add(item[1])
            if len(selected)>=MAX_STAGE1:break
        if len(selected)>=MAX_STAGE1:break
    stage1=[]
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as ex:
        fs=[ex.submit(fetch_stage1,m,i,t,binance24) for _,m,i,t in selected]
        for f in as_completed(fs):
            try:stage1.append(f.result())
            except Exception:pass
    stage1_ok=[r for r in stage1 if r.get("stage1_ok")]; success_ratio=len(stage1_ok)/max(1,len(selected))
    if success_ratio<0.70:hard_blockers.append(f"stage1_data_coverage_low:{success_ratio:.1%}")
    elif success_ratio<0.85:warnings.append(f"stage1_data_coverage_partial:{success_ratio:.1%}")
    leaders=add_sector_rotation(stage1_ok)
    stage1_ok.sort(key=lambda r:max(safe(r.get("pre_ignition_score_raw")),safe(r.get("accelerator_score_raw")))+safe(r.get("sector_rotation_bonus")),reverse=True)
    finalists=stage1_ok[:MAX_STAGE2]; enriched=[]
    with ThreadPoolExecutor(max_workers=6) as ex:
        fs=[ex.submit(enrich_stage2,dict(r)) for r in finalists]
        for f in as_completed(fs):
            try:enriched.append(final_score(f.result()))
            except Exception:pass
    enriched.sort(key=lambda r:safe(r.get("beam_score")),reverse=True)
    normal=[r for r in enriched if not r.get("warning_flag")]; speculative=[r for r in enriched if r.get("warning_flag")]
    enter=[r for r in normal if r.get("entry_plan",{}).get("status")=="ENTER_CANDIDATE"]; confirm=[r for r in normal if r.get("entry_plan",{}).get("status")=="WAIT_CONFIRMATION"]
    pool=enter+[r for r in confirm if r not in enter]+[r for r in normal if r not in enter and r not in confirm]; trade_candidates=pool[:2]
    if len(trade_candidates)<2:hard_blockers.append("fewer_than_two_rankable_candidates")
    output={
        "generated_at_utc":now_iso(),"source_version":VERSION,
        "data_sources":{"bithumb_market_list":"live","bithumb_ticker":"live" if tickers else "unavailable","bithumb_candles":"live","bithumb_orderbook":"live_for_finalists","bithumb_recent_trades":"live_for_finalists","binance_24h":"optional_crosscheck" if binance24 else "unavailable","news_catalyst":"DEFERRED_TO_CHAT_WEB_CHECK"},
        "health":{"analysis_ready":not hard_blockers,"hard_blockers":hard_blockers,"warnings":warnings,"markets_total":len(markets),"tickers_loaded":len(tickers),"stage1_requested":len(selected),"stage1_success":len(stage1_ok),"stage1_success_ratio":rnd(success_ratio*100),"stage2_success":len(enriched),"runtime_sec":rnd(time.time()-started,1)},
        "logic":{"universe":"ALL_BITHUMB_KRW; Binance intersection is NOT a gate","tracks":["PRE_IGNITION","ACCELERATOR"],"patterns_learned_and_applied":PATTERNS_LEARNED,"warning_assets":"scanned but penalized and separated; never silently discarded","risk_news":"not a hard gate in GitHub runtime; ChatGPT must web-check top2 before trade conclusion"},
        "sector_leaders":leaders,"trade_candidates":trade_candidates,
        "pre_ignition_top5":sorted(normal,key=lambda r:safe(r.get("pre_ignition_score")),reverse=True)[:5],
        "accelerator_top5":sorted(normal,key=lambda r:safe(r.get("accelerator_score")),reverse=True)[:5],
        "speculative_warning_top3":speculative[:3],"stage2_ranked":enriched}
    def slim(r):
        if not r: return None
        keys=["base","market","korean_name","price_krw","change_24h_pct","trade_24h_krw","track","beam_score","pre_ignition_score","accelerator_score","sector","sector_rotation_reason","m5","m15","h1","h4","d1","orderbook","trade_flow","binance_24h_change_pct","bithumb_minus_binance_pct","warning_flag","needs_web_catalyst_check","entry_plan"]
        return {k:r.get(k) for k in keys}
    summary={
        "generated_at_utc":output["generated_at_utc"],
        "source_version":VERSION,
        "health":output["health"],
        "data_sources":output["data_sources"],
        "logic":output["logic"],
        "trade_candidates":[slim(r) for r in trade_candidates[:2]],
        "pre_ignition_top5":[slim(r) for r in output["pre_ignition_top5"][:5]],
        "accelerator_top5":[slim(r) for r in output["accelerator_top5"][:5]],
        "speculative_warning_top3":[slim(r) for r in output["speculative_warning_top3"][:3]],
        "sector_leaders":leaders,
    }
    with open("beam_scan_result.json","w",encoding="utf-8") as f:json.dump(output,f,ensure_ascii=False,indent=2)
    with open("beam_latest_summary.json","w",encoding="utf-8") as f:json.dump(summary,f,ensure_ascii=False,indent=2)
    print(json.dumps({"generated_at_utc":output["generated_at_utc"],"health":output["health"],"trade_candidates":[{"base":r.get("base"),"price_krw":r.get("price_krw"),"beam_score":r.get("beam_score"),"track":r.get("track"),"status":r.get("entry_plan",{}).get("status")} for r in trade_candidates]},ensure_ascii=False,indent=2))

if __name__=="__main__":main()
