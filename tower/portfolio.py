"""
Portfolio layer. The questions a GTM leader actually asks on Monday.

Everything here is computed from the per-account states. No model calls, so
this is instant and identical on every run given the same inputs.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone

from .store import Store, utcnow
from . import metrics as M


def build_portfolio(store: Store) -> dict:
    states = [s["state"] for s in store.all_current_states()]
    states = [s for s in states if s]

    # ---------------------------------------------------------- money
    live = [s for s in states if s["lifecycle"] not in ("churned", "pre-sale")]
    churned = [s for s in states if s["lifecycle"] == "churned"]
    presale = [s for s in states if s["lifecycle"] == "pre-sale"]

    buckets: dict[str, dict] = {}
    for b in M.RENEWAL_BUCKETS:
        buckets[b] = {"arr": 0.0, "accounts": []}
    for s in states:
        b = s["renewal"]["bucket"]
        buckets.setdefault(b, {"arr": 0.0, "accounts": []})
        buckets[b]["arr"] += s["arr"]
        buckets[b]["accounts"].append(
            {"id": s["account_id"], "name": s["name"], "arr": s["arr"],
             "probability": s["renewal"]["probability"],
             "days": s["renewal"]["days_to_renewal"],
             "health": s["health"]["band"]})

    live_arr = sum(s["arr"] for s in live)
    lost_arr = sum(s["arr"] for s in churned)
    weighted = sum(s["arr"] * (s["renewal"]["probability"] or 0) for s in live)
    naive = live_arr  # what the CRM would tell you if you trusted every label

    # ---------------------------------------------------------- actions
    queue = []
    for s in states:
        for a in s.get("actions") or []:
            queue.append({
                **a,
                "lifecycle": s["lifecycle"],
                "health_band": s["health"]["band"],
                "health_score": s["health"]["score"],
                "renewal_bucket": s["renewal"]["bucket"],
                "reality_gap": s["reality_gap"]["severity"],
            })
    queue.sort(key=lambda a: -a.get("value_score", 0))
    for i, a in enumerate(queue, 1):
        a["rank"] = i

    # ------------------------------------------------ expansion vs traps
    opportunities, traps = [], []
    for s in states:
        for o in (s.get("synthesis", {}).get("opportunities") or []):
            row = {**o, "account_id": s["account_id"], "account_name": s["name"],
                   "arr": s["arr"], "health_band": s["health"]["band"],
                   "lifecycle": s["lifecycle"],
                   "usage_trajectory": s["usage"]["flightHours"]["trajectory"],
                   "open_critical": s["support"]["critical_open"],
                   "champion_status": s.get("synthesis", {}).get("champion_status")}
            row["qualification"] = _qualify(row, s)
            if o.get("is_trap") or not row["qualification"]["qualified"]:
                traps.append(row)
            else:
                opportunities.append(row)
    conf_rank = {"committed": 0, "probable": 1, "possible": 2, "speculative": 3}
    opportunities.sort(key=lambda o: (conf_rank.get(o.get("confidence"), 4), -o["arr"]))
    traps.sort(key=lambda o: -o["arr"])

    # ---------------------------------------------------- reality gaps
    gaps = sorted(
        [{"account_id": s["account_id"], "name": s["name"], "arr": s["arr"],
          **s["reality_gap"], "health_score": s["health"]["score"],
          "usage_trajectory": s["usage"]["flightHours"]["trajectory"],
          "usage_headline": s["usage"]["flightHours"]["headline"]}
         for s in states],
        key=lambda g: (-abs(g.get("gap", 0)), -g["arr"]))

    # ------------------------------------------------ contradictions
    contradictions = []
    for s in states:
        for c in (s.get("synthesis", {}).get("contradictions") or []):
            contradictions.append({**c, "account_id": s["account_id"],
                                   "account_name": s["name"], "arr": s["arr"]})
        for cf in (s["renewal"].get("conflicts") or []):
            contradictions.append({
                "topic": "Renewal or contract date",
                "claim_a": f"{s['renewal']['source']['value'] if s['renewal'].get('source') else 'resolved date'} "
                           f"-> {s['renewal']['date']}",
                "source_a": (s["renewal"].get("source") or {}).get("doc", "highest-authority source"),
                "claim_b": f"{cf['value']} -> {cf['date'].isoformat() if hasattr(cf['date'],'isoformat') else cf['date']}",
                "source_b": cf["doc"],
                "resolution": "Higher source authority wins. Customer statements and "
                              "dated correspondence outrank the CRM and stale tables.",
                "matters_because": "The renewal countdown drives every priority score on this account.",
                "account_id": s["account_id"], "account_name": s["name"], "arr": s["arr"],
            })
    contradictions.sort(key=lambda c: -c.get("arr", 0))

    # ---------------------------------------------------- win-back
    winbacks = []
    for s in churned:
        ca = (s.get("synthesis", {}).get("churn_analysis") or {})
        winbacks.append({
            "account_id": s["account_id"], "name": s["name"],
            "lost_arr": s["arr"], "vertical": s.get("vertical"),
            "root_cause": ca.get("root_cause", ""),
            "cause_category": ca.get("cause_category", "not-applicable"),
            "reversible": bool(ca.get("reversible")),
            "what_changed_since": ca.get("what_changed_since", ""),
            "residual_relationship": ca.get("residual_relationship", ""),
            "verdict": ca.get("winback_verdict", "not-applicable"),
            "reasoning": ca.get("winback_reasoning", ""),
            "first_move": ca.get("first_move", ""),
            "score": _winback_score(ca, s),
        })
    winbacks.sort(key=lambda w: -w["score"])

    # ---------------------------------------------------- flight activity
    flying = []
    for s in states:
        u = s["usage"]["flightHours"]
        if not s["usage"]["has_usage"]:
            continue
        flying.append({
            "account_id": s["account_id"], "name": s["name"], "arr": s["arr"],
            "trajectory": u["trajectory"], "pct_3v3": u["pct_3v3"],
            "latest": u["latest"], "peak": u["peak"], "peak_month": u["peak_month"],
            "months": u["months"], "values": u["values"],
            "missions_latest": s["usage"]["missions"]["latest"],
            "headline": u["headline"], "crm_health": s["crm_health"],
            "derived_health": s["health"]["band"],
            "agrees_with_label": s["reality_gap"]["direction"] == "aligned",
        })
    order = {"dormant": 0, "collapsing": 1, "declining": 2, "flat": 3,
             "insufficient-history": 4, "recovered": 5, "growing": 6, "scaling": 7}
    flying.sort(key=lambda f: (order.get(f["trajectory"], 9), -f["arr"]))

    # ---------------------------------------------------- open loops
    loops = []
    for s in states:
        for c in s.get("open_commitments") or []:
            if c.get("owner_side") == "flytbase":
                loops.append({**c, "account_id": s["account_id"],
                              "account_name": s["name"], "arr": s["arr"]})
    loops.sort(key=lambda c: -c["arr"])

    # ---------------------------------------------------- change feed
    changes = store.recent_changes(120)
    deltas = store.recent_deltas(80)
    syncs = store.last_syncs(30)

    stale = [s for s in states
             if s.get("days_silent") is not None and s["days_silent"] > 45
             and s["lifecycle"] != "churned"]
    stale.sort(key=lambda s: -(s["days_silent"] or 0))

    return {
        "generated_at": utcnow(),
        "counts": {
            "accounts": len(states), "live": len(live),
            "churned": len(churned), "presale": len(presale),
        },
        "money": {
            "live_arr": round(live_arr, 2),
            "lost_arr": round(lost_arr, 2),
            "weighted_forecast": round(weighted, 2),
            "naive_forecast": round(naive, 2),
            "forecast_gap": round(naive - weighted, 2),
            "buckets": {k: {"arr": round(v["arr"], 2), "count": len(v["accounts"]),
                            "accounts": v["accounts"]} for k, v in buckets.items()},
        },
        "action_queue": queue[:40],
        "opportunities": opportunities,
        "traps": traps,
        "reality_gaps": gaps,
        "contradictions": contradictions,
        "winbacks": winbacks,
        "flight_activity": flying,
        "open_loops": loops[:40],
        "stale_accounts": [{"account_id": s["account_id"], "name": s["name"],
                            "arr": s["arr"], "days_silent": s["days_silent"],
                            "last_touch": s["last_touch"],
                            "health": s["health"]["band"]} for s in stale],
        "accounts": sorted(
            [{"account_id": s["account_id"], "name": s["name"],
              "lifecycle": s["lifecycle"], "arr": s["arr"],
              "vertical": s.get("vertical"), "region": s.get("region"),
              "health_score": s["health"]["score"], "health_band": s["health"]["band"],
              "crm_health": s["crm_health"], "reality_gap": s["reality_gap"],
              "renewal": s["renewal"], "usage_trajectory": s["usage"]["flightHours"]["trajectory"],
              "usage_pct": s["usage"]["flightHours"]["pct_3v3"],
              "one_line": s.get("synthesis", {}).get("one_line", ""),
              "top_action": (s.get("actions") or [{}])[0].get("action", ""),
              "risks": len(s.get("synthesis", {}).get("top_risks") or []),
              "cs_owner": s.get("cs_owner"), "se_owner": s.get("se_owner"),
              "biggest_drag": s["health"].get("biggest_drag"),
              "days_silent": s.get("days_silent"),
              "degraded": s.get("degraded", False)}
             for s in states],
            key=lambda a: a["health_score"]),
        "change_feed": changes,
        "belief_deltas": deltas,
        "syncs": syncs,
        "system": {
            "last_sync": syncs[0] if syncs else None,
            "total_syncs": len(syncs),
            "withdrawn_objects": [
                dict(r) for r in store.conn.execute(
                    "SELECT obj_key, resource_kind, account_id, withdrawn_at "
                    "FROM raw_object WHERE withdrawn_at IS NOT NULL "
                    "ORDER BY withdrawn_at DESC").fetchall()],
        },
    }


def _qualify(opp: dict, state: dict) -> dict:
    """A stated intention is not a pipeline. Run the disqualifiers explicitly.

    The brief asks for 'traps that look like opportunities but aren't'. This
    is that test, written down so anyone can argue with it.
    """
    fails = []
    syn = state.get("synthesis", {})
    if syn.get("champion_status") in ("departed", "none-identified"):
        fails.append("no active champion in the account")
    if not syn.get("economic_buyer_identified"):
        fails.append("economic buyer never identified")
    if state["support"]["critical_open"] > 0:
        fails.append(f"{state['support']['critical_open']} critical support ticket(s) still open")
    if state["usage"]["flightHours"]["trajectory"] in ("collapsing", "dormant"):
        fails.append("current deployment usage is collapsing or dormant")
    if syn.get("commercial_friction"):
        fails.append("unresolved commercial friction on the existing contract")
    if state["health"]["band"] in ("Critical", "At Risk"):
        fails.append(f"base account health is {state['health']['band']}")
    if opp.get("confidence") == "speculative":
        fails.append("only a speculative signal, nothing funded or scoped")
    if syn.get("competitor_present"):
        fails.append("competitor active in the account")
    return {"qualified": len(fails) == 0, "disqualifiers": fails,
            "verdict": ("Pursue" if not fails else
                        "Do not pursue yet: " + "; ".join(fails))}


def _winback_score(ca: dict, state: dict) -> float:
    """Not every lost customer is worth chasing. Score it rather than guess.

    Weighted on: is the cause something we can now change, is there a warm
    body who would take the call, and how much money is on the table.
    """
    if not ca:
        return 0.0
    score = 0.0
    if ca.get("reversible"):
        score += 40
    cause_weight = {
        "product-gap": 25,           # we may have shipped it since
        "reliability": 18,           # provable with data
        "price": 15,
        "service-failure": 12,
        "champion-loss": 10,
        "competitor-displacement": 8,
        "sponsor-or-budget-change": 5,
        "programme-cancelled": 0,    # nothing to sell back into
        "not-applicable": 0,
    }
    score += cause_weight.get(ca.get("cause_category", "not-applicable"), 5)
    if (ca.get("what_changed_since") or "").strip():
        score += 15
    if (ca.get("residual_relationship") or "").strip():
        score += 12
    arr = state.get("arr") or 0
    score += min(arr / 2000.0, 15)
    return round(score, 1)
