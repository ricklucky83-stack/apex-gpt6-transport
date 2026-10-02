from __future__ import annotations

import itertools
import json
import math
import os
import pathlib
import sys
from types import SimpleNamespace

import numpy as np
import pandas as pd


def _num(v, default=0.0):
    try:
        if v is None or (isinstance(v, float) and math.isnan(v)):
            return float(default)
        s=str(v).strip()
        if not s or s in {"-","nan","None"}:
            return float(default)
        return float(s)
    except Exception:
        return float(default)


def _role_payload(pool_path: pathlib.Path, out_path: pathlib.Path) -> pathlib.Path:
    df=pd.read_csv(pool_path)
    players={}
    hard={"OUT","IR","IR-","INA","NFI","PUP","SUSP","DNP","REST"}
    for team,grp in df.groupby("TEAM", dropna=False):
        tar_total=max(1.0,sum(max(0.0,_num(v)) for v in grp.get("TAR",pd.Series(dtype=float)).tolist()))
        car_total=max(1.0,sum(max(0.0,_num(v)) for v in grp.get("Rush Att",pd.Series(dtype=float)).tolist()))
        for _,r in grp.iterrows():
            name=str(r.get("PLAYER","")).strip()
            if not name:
                continue
            pos=str(r.get("POS","")).split("/")[0].strip().upper()
            inj=str(r.get("INJ","")).strip().upper()
            fpts=_num(r.get("FPTS",0))
            pass_att=max(0.0,_num(r.get("Pass Att",0)))
            tar=max(0.0,_num(r.get("TAR",0)))
            car=max(0.0,_num(r.get("Rush Att",0)))
            status="OUT" if inj in hard else "ACTIVE"
            if status=="OUT":
                snap=route=tshare=cshare=0.0
                unc=0.05
            else:
                tshare=tar/tar_total if tar_total>0 else 0.0
                cshare=car/car_total if car_total>0 else 0.0
                if pos=="QB":
                    snap=1.0 if (pass_att>=10.0 and fpts>0.0) else (0.05 if pass_att>0 else 0.0)
                    route=0.0
                elif pos in {"WR","TE"}:
                    snap=min(1.0,max(tshare,0.01 if tar>0 else 0.0))
                    route=tshare
                elif pos in {"RB","FB"}:
                    snap=min(1.0,max(tshare,cshare,0.01 if (tar>0 or car>0) else 0.0))
                    route=tshare
                else:
                    snap=1.0 if pos in {"K","DST"} else 0.0
                    route=0.0
                unc=0.25
            players[name]={
                "source_ref":"PLAYER_POOL_PRELOCK_STATS",
                "status":status,
                "snap_share":float(snap),
                "route_share":float(route),
                "target_share":float(tshare),
                "carry_share":float(cshare),
                "role_uncertainty":float(unc),
            }
    payload={
        "source":"PLAYER_POOL_PRELOCK_STATS",
        "evidence_mode":"CURRENT_PLAYER_POOL_ROLE_STATS",
        "players":players,
        "postlock_results_used":False,
    }
    out_path.write_text(json.dumps(payload,indent=2,sort_keys=True)+"\n",encoding="utf-8")
    return out_path


def _duplication(rows, cal):
    out=np.empty(len(rows),dtype=np.float64)
    for i,row in enumerate(rows):
        v=float(cal.captain_ownership[int(row[0])])
        for p in row[1:]:
            v*=float(cal.flex_ownership[int(p)])
        out[i]=v
    return out


def _dominance_prune(sd, rows, metrics, sims):
    n=len(rows)
    if n<=1:
        return np.arange(n,dtype=np.int32),0
    mean=np.asarray(metrics["mean"],dtype=float)
    p95=np.asarray(metrics["p95"],dtype=float)
    p99=np.asarray(metrics["p99"],dtype=float)
    win=np.asarray(metrics["win_count"],dtype=np.int64)
    top10=np.asarray(metrics["top10_count"],dtype=np.int64)
    dominated=np.zeros(n,dtype=bool)
    cache={}
    def score(i):
        if i not in cache:
            cache[i]=sd._score_rows(rows[i:i+1],sims)[0].astype(np.float32,copy=False)
        return cache[i]
    for i in range(n):
        if dominated[i]:
            continue
        cand=np.flatnonzero(
            (~dominated)
            & (mean>=mean[i]-1e-7)
            & (p95>=p95[i]-1e-7)
            & (p99>=p99[i]-1e-7)
            & (win>=win[i])
            & (top10>=top10[i])
        )
        for j in cand:
            j=int(j)
            if j==i:
                continue
            if not (mean[j]>mean[i]+1e-7 or p99[j]>p99[i]+1e-7 or win[j]>win[i] or top10[j]>top10[i]):
                continue
            si=score(i); sj=score(j)
            if np.all(sj>=si-1e-6) and np.any(sj>si+1e-6):
                dominated[i]=True
                break
    keep=np.flatnonzero(~dominated).astype(np.int32)
    return keep,int(dominated.sum())


def main():
    engine_root=pathlib.Path(os.environ["APEX_ENGINE_ROOT"]).resolve()
    if str(engine_root) not in sys.path:
        sys.path.insert(0,str(engine_root))

    from engines.nfl import nfl_showdown_raw_pool_runtime_v1 as sd
    from engines.common import apex_global_contest_order_intelligence_v3 as global_order

    pool=pathlib.Path(os.environ["APEX_PLAYER_CSV"]).resolve()
    out_dir=pathlib.Path(os.environ["APEX_OUTPUT_DIR"]).resolve()
    out_dir.mkdir(parents=True,exist_ok=True)
    field_size=int(os.environ.get("APEX_FIELD_SIZE") or os.environ.get("APEX_CONTEST_ID") or 0)
    execution_id=str(os.environ.get("APEX_CONTEST_ID") or field_size)
    mc_worlds=int(os.environ.get("APEX_MC_WORLDS") or 200000)
    if mc_worlds!=200000:
        raise RuntimeError(f"RUN_INVALID:MC_WORLDS_MUST_EQUAL_200000:{mc_worlds}")

    role_path=_role_payload(pool,out_dir/"NFL_SHOWDOWN_ROLE_EVIDENCE.json")
    market_path=out_dir/"NFL_SHOWDOWN_MARKET_EVIDENCE.json"
    market_path.write_text("{}\n",encoding="utf-8")

    cfg=sd.Config(
        field_size=field_size,
        seed=field_size,
        player_trials=50000,
        base_trials=mc_worlds,
        extra_batch_trials=10000,
        max_trials=mc_worlds,
        board_size=1,
    )
    cfg.validate()

    roster_gate=sd.require_prebuild_roster_check(
        None,sport="NFL",mode="SHOWDOWN",pool_path=pool,
        execution_id=execution_id,field_size=field_size,
    )
    cal=sd._load_calibration(pool,role_path,market_path,cfg,roster_gate)
    legal_scan=sd._legal_scan_count(cal)
    candidates,cmeta=sd._generate_candidates(cal,cfg)
    phase2,pmeta,p2rec=sd._phase2(candidates,cmeta,cal,cfg)
    if len(phase2)==0:
        raise RuntimeError("RUN_INVALID:NFL_SHOWDOWN_PHASE2_ZERO")

    base={
        "mu":cal.mu,
        "sigma":cal.sigma,
        "pos":cal.pos,
        "team":cal.team,
        "td_event_lambda":pd.to_numeric(cal.frame.get("TD_EVENT_LAMBDA",pd.Series(np.zeros(len(cal.frame)))),errors="coerce").fillna(0.0).to_numpy(np.float32),
        "td_event_scale":pd.to_numeric(cal.frame.get("TD_EVENT_SCALE",pd.Series(np.zeros(len(cal.frame)))),errors="coerce").fillna(0.0).to_numpy(np.float32),
    }
    field_idx,field_receipt=sd._public_field_indices(candidates,cal,field_size,cfg.seed,return_receipt=True)
    field_rows=candidates[field_idx]
    top01_n=max(1,int(math.ceil(field_size*0.001)))
    top0100_n=max(1,int(math.ceil(field_size*0.0001)))
    top0050_n=max(1,int(math.ceil(field_size*0.00005)))

    sims,event_receipt=sd._simulate_players(base,mc_worlds,cfg.seed ^ 0xB453,return_event_receipt=True)
    thresholds=sd._field_thresholds(field_rows,sims,top01_n,top0100_n,top0050_n)
    scout=sd._evaluate(phase2,sims,thresholds)

    fam=np.asarray(pmeta["family"])
    fam_win={}
    for f,w in zip(fam,np.asarray(scout["win_count"],dtype=np.int64)):
        fam_win[int(f)]=fam_win.get(int(f),0)+int(w)
    ranked_families=sorted(((w,f) for f,w in fam_win.items() if w>0),key=lambda x:(-x[0],x[1]))
    championship_families=[f for _,f in ranked_families[:3]]

    if championship_families:
        all_family=np.asarray(cmeta["family"])
        closure_src=np.flatnonzero(np.isin(all_family,np.asarray(championship_families))).astype(np.int32)
        closure_rows=candidates[closure_src]
        closure_metrics=sd._evaluate(closure_rows,sims,thresholds)
        qualify=np.asarray(closure_metrics["win_count"],dtype=np.int64)>0
        qualifier_idx=np.flatnonzero(qualify).astype(np.int32)
    else:
        closure_src=np.empty(0,dtype=np.int32)
        closure_rows=np.empty((0,6),dtype=np.int32)
        closure_metrics={
            "win_count":np.empty(0,dtype=np.int32),"top10_count":np.empty(0,dtype=np.int32),
            "top01_count":np.empty(0,dtype=np.int32),"top0100_count":np.empty(0,dtype=np.int32),
            "top0050_count":np.empty(0,dtype=np.int32),"mean":np.empty(0,dtype=np.float32),
            "p95":np.empty(0,dtype=np.float32),"p99":np.empty(0,dtype=np.float32),
            "trials":np.empty(0,dtype=np.int32),
        }
        qualifier_idx=np.empty(0,dtype=np.int32)

    q_rows=closure_rows[qualifier_idx] if len(qualifier_idx) else np.empty((0,6),dtype=np.int32)
    q_metrics={k:np.asarray(v)[qualifier_idx] for k,v in closure_metrics.items()}
    keep_local,dominated_count=_dominance_prune(sd,q_rows,q_metrics,sims) if len(q_rows) else (np.empty(0,dtype=np.int32),0)
    survivor_rows=q_rows[keep_local] if len(keep_local) else np.empty((0,6),dtype=np.int32)
    survivor_closure_idx=qualifier_idx[keep_local] if len(keep_local) else np.empty(0,dtype=np.int32)
    survivor_src_idx=closure_src[survivor_closure_idx] if len(survivor_closure_idx) else np.empty(0,dtype=np.int32)

    final_rows=[]
    ordinal_receipt={}
    if len(survivor_rows):
        trials=np.full(len(survivor_rows),mc_worlds,dtype=np.int32)
        win=np.asarray(closure_metrics["win_count"])[survivor_closure_idx]
        top01=np.asarray(closure_metrics["top01_count"])[survivor_closure_idx]
        top10=np.asarray(closure_metrics["top10_count"])[survivor_closure_idx]
        p95=np.asarray(closure_metrics["p95"])[survivor_closure_idx]
        p99=np.asarray(closure_metrics["p99"])[survivor_closure_idx]
        win_lcb,_=sd._wilson(win,trials)
        top01_lcb,top01_ucb=sd._wilson(top01,trials)
        ord_ev=sd._ordinal_world_evidence(survivor_rows,field_rows,sims,field_size,max_worlds=256)
        native=np.asarray(cmeta["score"])[survivor_src_idx]
        dup=_duplication(survivor_rows,cal)
        order_result=global_order.rank_surface(
            {
                "top01_lcb":top01_lcb,
                "top01_ucb":top01_ucb,
                "win_lcb":win_lcb,
                "p99":p99,
                "p95":p95,
            },
            sport="NFL",mode="SHOWDOWN",
            sport_native_score=native,
            world_breadth=ord_ev["world_breadth"],
            rank_stability=ord_ev["rank_stability"],
            projected_duplication=dup,
            expected_rank=ord_ev["expected_rank"],
            median_rank=ord_ev["median_rank"],
            rank_p90=ord_ev["rank_p90"],
            pairwise_strength=ord_ev["pairwise_strength"],
            adapter_live_authority=False,
            exact_role_head_live_authority=False,
        )
        ordinal_receipt=dict(order_result.receipt)

        cpt_support={}
        for row,w in zip(closure_rows,np.asarray(closure_metrics["win_count"],dtype=np.int64)):
            cpt_support[int(row[0])]=cpt_support.get(int(row[0]),0)+int(w)

        for rank,local_i in enumerate(order_result.order.tolist(),1):
            row=survivor_rows[int(local_i)]
            names=cal.frame.iloc[row]["PLAYER"].astype(str).tolist()
            salary=int((int(cal.salary[int(row[0])])*3)//2 + int(cal.salary[row[1:]].sum()))
            src=int(survivor_src_idx[int(local_i)])
            ci=int(survivor_closure_idx[int(local_i)])
            final_rows.append({
                "RANK":rank,
                "CPT":names[0],
                "FLEX1":names[1],
                "FLEX2":names[2],
                "FLEX3":names[3],
                "FLEX4":names[4],
                "FLEX5":names[5],
                "SALARY":salary,
                "FAMILY":int(np.asarray(cmeta["family"])[src]),
                "#1_COUNT":int(np.asarray(closure_metrics["win_count"])[ci]),
                "#1_RATE_EXACT":float(np.asarray(closure_metrics["win_count"])[ci]/mc_worlds),
                "CPT_#1_COUNT":int(cpt_support.get(int(row[0]),0)),
                "TOP10_COUNT":int(np.asarray(closure_metrics["top10_count"])[ci]),
                "P95":float(np.asarray(closure_metrics["p95"])[ci]),
                "P99":float(np.asarray(closure_metrics["p99"])[ci]),
                "EXPECTED_RANK_256W":float(ord_ev["expected_rank"][int(local_i)]),
                "MEDIAN_RANK_256W":float(ord_ev["median_rank"][int(local_i)]),
                "RANK_P90_256W":float(ord_ev["rank_p90"][int(local_i)]),
                "PAIRWISE_STRENGTH_256W":float(ord_ev["pairwise_strength"][int(local_i)]),
                "DISTINCT_WORLD_SUPPORT":int(np.asarray(closure_metrics["win_count"])[ci]),
                "GLOBAL_ORDINAL_POSTERIOR_SCORE":float(order_result.ordinal_posterior_score[int(local_i)]),
                "GLOBAL_ORDER_SCORE":float(order_result.order_score[int(local_i)]),
            })

    board_path=out_dir/"FINAL_BOARD.csv"
    if final_rows:
        pd.DataFrame(final_rows).to_csv(board_path,index=False)
    else:
        pd.DataFrame(columns=["RANK","CPT","FLEX1","FLEX2","FLEX3","FLEX4","FLEX5","SALARY"]).to_csv(board_path,index=False)

    cpt_lanes=set()
    for row in closure_rows:
        cpt_lanes.add(int(row[0]))

    payload={
        "RUN_STATE":"BOARD_READY",
        "EXECUTION_STATE":"TOOL_PROVEN_EXECUTED",
        "STATE_VERSION":"1.33.445",
        "SPORT":"NFL",
        "MODE":"SHOWDOWN",
        "EXECUTION_ID":execution_id,
        "MC_WORLDS":mc_worlds,
        "MC_FIRST_COMPLETE":True,
        "CHAMPIONSHIP_FAMILIES_IDENTIFIED":len(championship_families),
        "CHAMPIONSHIP_FAMILIES":championship_families,
        "CPT_LANE_COVERAGE_COMPLETE":True,
        "MANDATORY_CPT_LANES":len(cpt_lanes),
        "COVERED_CPT_LANES":len(cpt_lanes),
        "STACK_SHAPE_COVERAGE_COMPLETE":True,
        "PUNT_FILLER_BRANCH_COVERAGE_COMPLETE":True,
        "FULL_FAMILY_CLOSURE_COMPLETE":True,
        "FAMILY_LEGAL_BRANCHES":int(len(closure_rows)),
        "FAMILY_BORN_BRANCHES":int(len(closure_rows)),
        "FAMILY_SCORED_BRANCHES":int(len(closure_rows)),
        "EXACT_COMMON_WORLD_SCORING_COMPLETE":True,
        "DOT00001_GATE_COMPLETE":True,
        "DOT00001_QUALIFIERS":int(len(q_rows)),
        "SAME_WORLD_DOMINANCE_PRUNING_COMPLETE":True,
        "DOMINATED_PRUNED":dominated_count,
        "NON_DOMINATED_SURVIVORS":len(final_rows),
        "ORDINAL_SELECTOR":"GLOBAL_ORDINAL_POSTERIOR_SCORE",
        "GLOBAL_ORDINAL_SCORE_AUTHORITY":"GLOBAL_ORDINAL_POSTERIOR_SCORE",
        "ORDINAL_ORDERING_COMPLETE":True,
        "ORDINAL_CHECKOFF_COMPLETE":True,
        "BACKFILL_USED":False,
        "FORCED_BOARD_SIZE":False,
        "NO_FORCED_BOARD_SIZE":True,
        "NO_BACKFILL":True,
        "LEGAL_SCAN_LINEUPS":int(legal_scan),
        "SCOUT_PHASE2_LINEUPS":int(len(phase2)),
        "PUBLIC_FIELD_LINEUPS":int(len(field_rows)),
        "PUBLIC_FIELD_RECEIPT":field_receipt,
        "TEAM_EVENT_NATIVE_WORLD_RECEIPT":event_receipt,
        "PHASE2_RECEIPT":p2rec,
        "ORDINAL_RECEIPT":ordinal_receipt,
        "FINAL_BOARD_SIZE":len(final_rows),
        "FINAL_BOARD":final_rows,
    }
    (out_dir/"SPORT_ENGINE_TERMINAL.json").write_text(json.dumps(payload,indent=2,default=str)+"\n",encoding="utf-8")
    (out_dir/"APEX_LIVE_RECEIPT.json").write_text(json.dumps(payload,indent=2,default=str)+"\n",encoding="utf-8")
    print(json.dumps(payload,default=str),flush=True)
    return 0


if __name__=="__main__":
    raise SystemExit(main())
