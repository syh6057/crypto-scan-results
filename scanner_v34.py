import json
import math
from datetime import datetime, timezone
from pathlib import Path

VERSION = "v34.3-persistent-bank-winner-selector"
BRIDGE_FILE = "beam_breakout_bridge.json"
HISTORY_FILE = "breakout_bridge_history.json"
AUDIT_FILE = "beam_miss_audit.json"
BANK_FILE = "urgent_winner_bank.json"
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
    try: return json.loads(Path(path).read_text(encoding="utf-8"))
    except Exception: return default

def save_json(path, value): Path(path).write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
def f(v, default=0.0):
    try: return float(v)
    except Exception: return default

def clip(x, lo, hi): return max(lo, min(hi, x))

def feature_vector(row):
    m=row.get("metrics") or {}; ch=f(row.get("change_24h_pct")); trade=max(1.0,f(row.get("trade_24h_krw"),1.0)); vsp=max(0.0,f(m.get("m15_vol_spike_x"))); vper=max(0.0,f(m.get("m15_vol_persistence_x"))); bsr=max(0.0,f(m.get("buy_sell_ratio"))); depth=max(0.0,f(m.get("bid_ask_depth_ratio"))); pace=f(m.get("price_change_per_5m_pct")); wick=max(0.0,f(m.get("m15_upper_wick_pct"))); money=max(0.0,f(m.get("money_leads_price_score"))); turnover=max(0.0,f(m.get("fast_turnover_intensity_x"))); beam=max(0.0,f(m.get("beam_score")))
    return [clip((ch+2)/12,0,1),clip((math.log10(trade)-8)/3,0,1),clip(math.log1p(vsp)/math.log(61),0,1),clip(math.log1p(vper)/math.log(31),0,1),1.0 if m.get("m15_close_above_ma") else 0.0,1.0 if m.get("h1_close_above_ma") else 0.0,1.0 if m.get("m15_low_rising") else 0.0,1.0 if m.get("h1_low_rising") else 0.0,clip(bsr/3,0,1),clip(depth/3,0,1),clip((pace+1)/3,0,1),clip(1-wick/3,0,1),clip(money/40,0,1),clip(math.log1p(turnover)/math.log(21),0,1),clip(beam/180,0,1),clip(f(row.get("signal_count"))/10,0,1),clip(f(row.get("flow_confirmation_count"))/5,0,1)]

def distance(a,b):
    if not a or not b or len(a)!=len(b): return 9.0
    return math.sqrt(sum((x-y)**2 for x,y in zip(a,b))/len(a))
def similarity(vec,bank):
    if not bank:return 0.0
    ds=sorted(distance(vec,x) for x in bank); k=min(3,len(ds)); d=sum(ds[:k])/k; return math.exp(-4*d*d)

def iter_history_rows(history):
    for snap in history if isinstance(history,list) else []:
        for key in ("promoted","watch"):
            for row in snap.get(key) or []:
                if isinstance(row,dict) and row.get("base"): yield row

def earliest_rows_by_base(history):
    out={}
    for row in iter_history_rows(history): out.setdefault(row.get("base"),row)
    return out

def raw_urgent_candidates(bridge):
    rows=[]; seen=set()
    # Persistent pool is part of the decision universe, not merely UI state.
    sources=(bridge.get("promoted_top3") or [])+(bridge.get("watch_top5") or [])+(bridge.get("persistent_candidate_pool") or [])
    for row in sources:
        if not isinstance(row,dict): continue
        base=row.get("base")
        if not base or base in seen: continue
        pt=row.get("persistent_tracking") or {}
        active_persistent=pt.get("status") in {"TRACKING","SUCCESS_TRACKED"} and pt.get("observable_this_run") is True
        was_urgent=bool(row.get("execution_gate")) or str(row.get("promotion_grade") or "").startswith("A_") or "URGENT" in str(row.get("promotion_grade") or "") or active_persistent
        # Never promote a persistent row with stale metrics.
        if was_urgent and (not pt or active_persistent): seen.add(base); rows.append(row)
    return rows

def hard_live_checks(row):
    m=row.get("metrics") or {}; pt=row.get("persistent_tracking") or {}
    checks={"fresh_metrics": not pt or pt.get("observable_this_run") is True,"still_early_not_already_pumped":-1<=f(row.get("change_24h_pct"),999)<=MAX_ENTRY_CHANGE_PCT,"persistent_15m_volume":f(m.get("m15_vol_persistence_x"))>=MIN_VOL_PERSISTENCE,"price_above_15m_ma":bool(m.get("m15_close_above_ma")),"buy_flow_not_sell_dominant":f(m.get("buy_sell_ratio"))>=MIN_BUY_SELL_RATIO,"short_pace_not_rolling_over":f(m.get("price_change_per_5m_pct"),-999)>=MIN_SHORT_PACE,"upper_wick_not_exhausted":f(m.get("m15_upper_wick_pct"),999)<=MAX_UPPER_WICK_PCT}
    return checks,[k for k,ok in checks.items() if not ok]
def rows_from_bank(bank,side):
    rows=[]
    for base,rec in (bank.get(side) or {}).items():
        snap=(rec or {}).get("snapshot")
        if isinstance(snap,dict) and snap.get("base"): rows.append(snap)
    return rows
def fallback_rows(audit,earliest):
    pos=[x.get("base") for x in (audit.get("current_top_movers") or []) if x.get("capture")=="EARLY_CAPTURE" and f(x.get("change_24h_pct"))>=10 and x.get("base")]; ps=set(pos); neg=[x.get("base") for x in (audit.get("failed_acceleration_examples") or []) if x.get("base") and x.get("base") not in ps]
    return [earliest[b] for b in pos if b in earliest],[earliest[b] for b in neg if b in earliest]

def main():
    now=datetime.now(timezone.utc).isoformat(); bridge=load_json(BRIDGE_FILE,{}); history=load_json(HISTORY_FILE,[]); audit=load_json(AUDIT_FILE,{}); bank=load_json(BANK_FILE,{}); earliest=earliest_rows_by_base(history)
    positive_rows=rows_from_bank(bank,"positive"); negative_rows=rows_from_bank(bank,"negative"); bank_source="durable_v35_bank"
    if len(positive_rows)<MIN_POSITIVE_EXAMPLES:
        fpos,fneg=fallback_rows(audit,earliest); ep={x.get("base") for x in positive_rows}; en={x.get("base") for x in negative_rows}; positive_rows += [x for x in fpos if x.get("base") not in ep]; ps={x.get("base") for x in positive_rows}; negative_rows=[x for x in negative_rows if x.get("base") not in ps]; negative_rows += [x for x in fneg if x.get("base") not in en and x.get("base") not in ps]; bank_source="durable_v35_bank_plus_fallback"
    positive_set={x.get("base") for x in positive_rows}; negative_rows=[x for x in negative_rows if x.get("base") not in positive_set]; pos_bank=[feature_vector(x) for x in positive_rows]; neg_bank=[feature_vector(x) for x in negative_rows]
    scored=[]
    for row in raw_urgent_candidates(bridge):
        vec=feature_vector(row); pos_sim=similarity(vec,pos_bank); neg_sim=similarity(vec,neg_bank); edge=pos_sim-neg_sim if neg_bank else pos_sim-.50; checks,failed=hard_live_checks(row); winner_match=len(pos_bank)>=MIN_POSITIVE_EXAMPLES and pos_sim>=MIN_POS_SIM and edge>=MIN_EDGE and not failed; out=dict(row); out["winner_pattern"]={"version":VERSION,"training_source":bank_source,"positive_examples":[x.get("base") for x in positive_rows],"negative_examples":[x.get("base") for x in negative_rows],"positive_similarity":round(pos_sim,4),"negative_similarity":round(neg_sim,4),"winner_edge":round(edge,4),"live_checks":checks,"failed_live_checks":failed,"winner_match":winner_match,"logic":"score fresh current and persistent early/urgent candidates against durable forward winners; stale persistent metrics can never promote"}; out["execution_allowed"]=bool(winner_match); out["promotion_grade"]="A_URGENT_WINNER_MATCH" if winner_match else "WATCH_URGENT_NONWINNER_PATTERN"; scored.append(out)
    scored.sort(key=lambda x:(1 if (x.get("winner_pattern") or {}).get("winner_match") else 0,f((x.get("winner_pattern") or {}).get("winner_edge")),f((x.get("winner_pattern") or {}).get("positive_similarity")),f(x.get("priority_score"))),reverse=True); winners=[x for x in scored if (x.get("winner_pattern") or {}).get("winner_match")]; nonwinners=[x for x in scored if not (x.get("winner_pattern") or {}).get("winner_match")]
    ordinary=[]; urgent_bases={x.get("base") for x in scored}
    for row in bridge.get("watch_top5") or []:
        if row.get("base") not in urgent_bases: ordinary.append(row)
    bridge["promoted_top3"]=winners[:3]; bridge["watch_top5"]=(nonwinners+ordinary)[:5]; bridge["top_candidates"]=(winners+nonwinners+ordinary)[:5]; bridge["winner_pattern_selector"]={"generated_at_utc":now,"version":VERSION,"training_source":bank_source,"positive_bases_with_history":[x.get("base") for x in positive_rows],"negative_bases_with_history":[x.get("base") for x in negative_rows],"winner_matches":[x.get("base") for x in winners],"rejected_urgent":[x.get("base") for x in nonwinners],"minimum_positive_examples":MIN_POSITIVE_EXAMPLES,"persistent_pool_integrated":True,"principle":"URGENT/persistent is the pool; actionable requires fresh metrics, durable winner resemblance, and clean live checks."}; bridge["version"]=f"{bridge.get('version','')}+{VERSION}"; bridge["status"]="URGENT_WINNER_MATCH" if winners else ("WATCH_ONLY" if bridge.get("watch_top5") else "NO_SIGNAL"); save_json(BRIDGE_FILE,bridge)
    result={"generated_at_utc":now,"version":VERSION,"status":bridge["status"],"training_source":bank_source,"persistent_pool_integrated":True,"winner_matches":[{"base":x.get("base"),"price_krw":x.get("price_krw"),"change_24h_pct":x.get("change_24h_pct"),"positive_similarity":(x.get("winner_pattern") or {}).get("positive_similarity"),"negative_similarity":(x.get("winner_pattern") or {}).get("negative_similarity"),"winner_edge":(x.get("winner_pattern") or {}).get("winner_edge")} for x in winners[:3]],"rejected_urgent":[{"base":x.get("base"),"failed_live_checks":(x.get("winner_pattern") or {}).get("failed_live_checks"),"positive_similarity":(x.get("winner_pattern") or {}).get("positive_similarity"),"negative_similarity":(x.get("winner_pattern") or {}).get("negative_similarity"),"winner_edge":(x.get("winner_pattern") or {}).get("winner_edge")} for x in nonwinners],"positive_examples":[x.get("base") for x in positive_rows],"negative_examples":[x.get("base") for x in negative_rows],"label_conflicts_removed":sorted(positive_set.intersection({x.get("base") for x in rows_from_bank(bank,"negative")}))}; save_json(OUT_FILE,result); save_json(STATE_FILE,result); print(json.dumps(result,ensure_ascii=False,indent=2))
if __name__=="__main__": main()
