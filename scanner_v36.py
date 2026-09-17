import json
from datetime import datetime, timezone
from pathlib import Path
VERSION="v36.1-persistent-primary-continuity"; BRIDGE_FILE="beam_breakout_bridge.json"; SELECTOR_FILE="urgent_winner_selector.json"; STATE_FILE="primary_tracking_state.json"; OUT_FILE="primary_tracking_summary.json"
MIN_LOCK_RUNS=3; REPLACE_CONFIRM_RUNS=2; REPLACE_EDGE_MARGIN=.08; MAX_MISSING_GRACE_RUNS=2; WARNING_MAX_RUNS=2

def load_json(path,default):
    try:return json.loads(Path(path).read_text(encoding="utf-8"))
    except Exception:return default
def save_json(path,value):Path(path).write_text(json.dumps(value,ensure_ascii=False,indent=2),encoding="utf-8")
def f(v,default=0.0):
    try:return float(v)
    except Exception:return default

def by_base(bridge):
    out={}
    # Persistent candidates are part of continuity even after falling out of UI top5.
    for key in ("promoted_top3","watch_top5","top_candidates","persistent_candidate_pool"):
        for row in bridge.get(key) or []:
            if not isinstance(row,dict) or not row.get("base"):continue
            base=row["base"]; pt=row.get("persistent_tracking") or {}
            # Prefer a fresh observable row over an older display copy.
            if base not in out or pt.get("observable_this_run") is True: out[base]=row
    return out

def winner_edge(row):return f((row.get("winner_pattern") or {}).get("winner_edge"),-9.0)
def winner_rows(bridge):return [r for r in (bridge.get("promoted_top3") or []) if isinstance(r,dict) and r.get("base")]
def observable(row):
    if not row:return False
    pt=row.get("persistent_tracking") or {}
    return not pt or pt.get("observable_this_run") is True

def invalidation(row,state):
    if not row:return False,["not_observable_this_run"]
    if not observable(row):return False,["persistent_metrics_stale"]
    m=row.get("metrics") or {}; ep=row.get("entry_plan") or state.get("entry_plan") or {}; price=f(row.get("price_krw")); stop=f(ep.get("hard_stop_krw")); hard=bool(stop and price and price<=stop); structure=(not bool(m.get("m15_close_above_ma"))) and (not bool(m.get("h1_close_above_ma"))); flow=f(m.get("buy_sell_ratio"),1)<.75 and f(m.get("price_change_per_5m_pct"),0)<-.15; volume=f(m.get("m15_vol_persistence_x"),1)<.65; reasons=[]
    if hard:reasons.append("hard_stop_broken")
    if structure:reasons.append("m15_h1_structure_broken")
    if flow:reasons.append("sell_flow_and_negative_pace")
    if volume:reasons.append("volume_persistence_collapsed")
    return hard or (structure and flow) or (structure and volume),reasons
def warning_reasons(row):
    if not row:return ["not_observable_this_run"]
    if not observable(row):return ["persistent_metrics_stale"]
    m=row.get("metrics") or {}; r=[]
    if not bool(m.get("m15_close_above_ma")):r.append("below_m15_ma")
    if not bool(m.get("h1_close_above_ma")):r.append("below_h1_ma")
    if f(m.get("m15_vol_persistence_x"),0)<1:r.append("volume_persistence_below_1x")
    if f(m.get("buy_sell_ratio"),1)<.9:r.append("buy_flow_weak")
    if f(m.get("price_change_per_5m_pct"),0)<-.05:r.append("short_pace_negative")
    return r

def main():
    now=datetime.now(timezone.utc).isoformat(); bridge=load_json(BRIDGE_FILE,{}); selector=load_json(SELECTOR_FILE,{}); state=load_json(STATE_FILE,{})
    if not isinstance(state,dict):state={}
    rows=by_base(bridge); fresh=winner_rows(bridge); fresh_primary=fresh[0] if fresh else None; primary=state.get("primary_base"); status=state.get("status") or "NONE"; lock=int(state.get("lock_runs") or 0); wruns=int(state.get("warning_runs") or 0); missing=int(state.get("missing_runs") or 0); challenger=state.get("challenger_base"); cruns=int(state.get("challenger_runs") or 0); transition="UNCHANGED"
    if not primary:
        if fresh_primary:primary=fresh_primary.get("base");status="HOLD";lock=1;wruns=missing=0;transition="NEW_PRIMARY"
    else:
        row=rows.get(primary); invalid,reasons=invalidation(row,state)
        if invalid:status="INVALIDATED";transition="PRIMARY_INVALIDATED"
        elif row and observable(row):
            missing=0;lock+=1;w=warning_reasons(row)
            if len(w)>=2:wruns+=1;status="WARNING"
            else:wruns=0;status="HOLD"
            if status=="WARNING" and wruns>WARNING_MAX_RUNS: transition="WARNING_PERSISTENT"
        else:
            missing+=1;status="WARNING" if missing<=MAX_MISSING_GRACE_RUNS else "INVALIDATED";transition="PRIMARY_PERSISTENT_STALE_GRACE" if status=="WARNING" else "PRIMARY_MISSING_INVALIDATED"
        if fresh_primary and fresh_primary.get("base")!=primary and status!="INVALIDATED":
            cand=fresh_primary.get("base"); edge_delta=winner_edge(fresh_primary)-winner_edge(rows.get(primary) or {})
            if cand==challenger and edge_delta>=REPLACE_EDGE_MARGIN:cruns+=1
            elif edge_delta>=REPLACE_EDGE_MARGIN:challenger,cruns=cand,1
            else:challenger,cruns=None,0
            if lock>=MIN_LOCK_RUNS and cruns>=REPLACE_CONFIRM_RUNS:primary=cand;status="HOLD";lock=1;wruns=missing=0;challenger,cruns=None,0;transition="PRIMARY_REPLACED_CONFIRMED"
        elif not fresh_primary or fresh_primary.get("base")==primary:challenger,cruns=None,0
        if status=="INVALIDATED" and fresh_primary and fresh_primary.get("base")!=primary:primary=fresh_primary.get("base");status="HOLD";lock=1;wruns=missing=0;challenger,cruns=None,0;transition="INVALIDATED_TO_NEW_PRIMARY"
    row=rows.get(primary) if primary else None
    # Only fresh observable metrics can remain executable; stale persistent rows get WARNING, never BUY.
    if row and status in {"HOLD","WARNING"}:
        if not observable(row):status="WARNING"
        tracked=dict(row); tracked["tracking"]={"version":VERSION,"status":status,"lock_runs":lock,"warning_runs":wruns,"missing_runs":missing,"transition":transition,"principle":"keep primary through ranking loss using persistent pool; stale metrics are warning-only; replace only after confirmed superior challenger"}; tracked["execution_allowed"]=status=="HOLD" and observable(row); tracked["promotion_grade"]="A_URGENT_CONTINUATION" if tracked["execution_allowed"] else "WATCH_PRIMARY_WARNING"; others=[x for x in (bridge.get("promoted_top3") or []) if x.get("base")!=primary]; bridge["promoted_top3"]=([tracked]+others)[:3] if tracked["execution_allowed"] else others[:3]; watches=[x for x in (bridge.get("watch_top5") or []) if x.get("base")!=primary]; bridge["watch_top5"]=([tracked]+watches)[:5] if not tracked["execution_allowed"] else watches[:5]; bridge["top_candidates"]=([tracked]+[x for x in (bridge.get("top_candidates") or []) if x.get("base")!=primary])[:5]
    bridge["primary_continuity"]={"generated_at_utc":now,"version":VERSION,"primary_base":primary,"status":status,"lock_runs":lock,"warning_runs":wruns,"missing_runs":missing,"challenger_base":challenger,"challenger_runs":cruns,"transition":transition,"persistent_pool_integrated":True}; bridge["version"]=f"{bridge.get('version','')}+{VERSION}"; save_json(BRIDGE_FILE,bridge)
    state={"updated_at_utc":now,"version":VERSION,"primary_base":primary,"status":status,"lock_runs":lock,"warning_runs":wruns,"missing_runs":missing,"challenger_base":challenger,"challenger_runs":cruns,"transition":transition,"entry_plan":(row or {}).get("entry_plan") or state.get("entry_plan") or {},"last_price_krw":(row or {}).get("price_krw"),"last_winner_edge":winner_edge(row or {}),"persistent_pool_integrated":True}; save_json(STATE_FILE,state); save_json(OUT_FILE,{**state,"fresh_winner_matches":[x.get("base") for x in fresh],"selector_status":selector.get("status")}); print(json.dumps(state,ensure_ascii=False,indent=2))
if __name__=="__main__":main()
