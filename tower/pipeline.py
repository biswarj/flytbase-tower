"""
The cycle. Crawl, reconcile, re-read only what moved, re-reason, diff beliefs.

The important property is that this is idempotent and incremental. Running it
every 60 seconds when nothing changed costs one crawl and zero model calls.
Running it in the minute after a batch of new documents lands re-reads only
the documents that actually changed and re-reasons only the accounts they
touch. That is what makes an always-on loop affordable rather than theatre.
"""

from __future__ import annotations

import json
import re
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timezone

from . import config
from .store import Store, content_hash, utcnow
from .source import BookOfBusiness
from .sentinel import Sentinel
from .brain import Brain
from . import metrics as M
from .portfolio import build_portfolio


# --------------------------------------------------------------------------
# Evidence units
# --------------------------------------------------------------------------

def _chunk_markdown(text: str, max_chars: int = 900) -> list[tuple[str, str]]:
    """Split a document into citable units with stable anchors.

    Anchors are heading path plus ordinal, not character offsets, so a small
    edit upstream does not invalidate every citation in the document.
    """
    lines = (text or "").splitlines()
    out, buf, heading, idx = [], [], "top", 0
    for ln in lines:
        if re.match(r"^\s{0,3}#{1,6}\s+", ln):
            if buf:
                out.append((f"{heading}#{idx}", "\n".join(buf).strip()))
                idx += 1
                buf = []
            heading = re.sub(r"^\s{0,3}#{1,6}\s+", "", ln).strip()[:60] or "section"
            idx = 0
            continue
        buf.append(ln)
        if sum(len(x) for x in buf) > max_chars:
            out.append((f"{heading}#{idx}", "\n".join(buf).strip()))
            idx += 1
            buf = []
    if buf:
        out.append((f"{heading}#{idx}", "\n".join(buf).strip()))
    return [(a, q) for a, q in out if q]


def build_evidence(store: Store, account_id: str, obj_key: str, doc: dict,
                   doc_date: str | None) -> int:
    rows = []
    for anchor, quote in _chunk_markdown(doc.get("content", "")):
        rows.append({
            "obj_key": obj_key, "account_id": account_id,
            "doc_type": doc.get("doc_type"), "doc_title": doc.get("title"),
            "doc_date": doc_date, "anchor": anchor, "quote": quote[:4000],
        })
    return store.put_evidence(rows)


# --------------------------------------------------------------------------
# Account reasoning
# --------------------------------------------------------------------------

def _fallback_doc_stub() -> dict:
    """Placeholder for a document whose read failed outright. Explicit and
    inert, so a failed read never silently looks like an empty document."""
    return {
        "doc_date_text": "", "summary": "[read failed, not analysed]",
        "people": [], "signals": [], "commitments": [], "commercial_facts": [],
        "support_state": {"open_tickets": 0, "critical_open": 0,
                          "sla_breaches": 0, "recurring_issues": 0},
        "overall_sentiment": "neutral", "_degraded": True,
    }


def _ref_date() -> date:
    if config.REFERENCE_DATE:
        try:
            return datetime.strptime(config.REFERENCE_DATE, "%Y-%m-%d").date()
        except ValueError:
            pass
    return datetime.now(timezone.utc).date()


def _merge_people(readings: list[dict]) -> list[dict]:
    """One row per human, latest role wins, all quotes retained."""
    people: dict[str, dict] = {}
    for r in readings:
        for p in r.get("people") or []:
            name = (p.get("name") or "").strip()
            if not name:
                continue
            key = name.lower()
            cur = people.setdefault(key, {
                "name": name, "titles": [], "org_side": p.get("org_side", "unknown"),
                "roles": [], "sentiments": [], "status_signals": [],
                "quotes": [], "seen_in": [],
            })
            if p.get("title"):
                cur["titles"].append(p["title"])
            cur["roles"].append(p.get("role", "unknown"))
            cur["sentiments"].append(p.get("sentiment", "unknown"))
            if p.get("status_signal") and p["status_signal"] != "none":
                cur["status_signals"].append(p["status_signal"])
            if p.get("quote"):
                cur["quotes"].append({"quote": p["quote"], "doc": r.get("_doc_title"),
                                      "date": r.get("_doc_date"),
                                      "reason": p.get("role_reason", "")})
            cur["seen_in"].append(r.get("_doc_title"))

    out = []
    for p in people.values():
        roles = [x for x in p["roles"] if x != "unknown"]
        status = p["status_signals"]
        out.append({
            "name": p["name"],
            "title": _most_common(p["titles"]) or "",
            "org_side": p["org_side"],
            "role": _most_common(roles) or "unknown",
            "role_all": sorted(set(roles)),
            "sentiment": _latest_sentiment(p["sentiments"]),
            "status": ("departed" if "departed" in status else
                       "gone-quiet" if "gone-quiet" in status else
                       "changed-role" if "changed-role" in status else "active"),
            "mentions": len(p["seen_in"]),
            "documents": sorted(set(x for x in p["seen_in"] if x)),
            "quotes": p["quotes"][:6],
        })
    rank = {"economic-buyer": 0, "executive-sponsor": 1, "champion": 2,
            "technical-evaluator": 3, "procurement": 4, "blocker": 5,
            "day-to-day-user": 6, "influencer": 7, "unknown": 8}
    out.sort(key=lambda p: (p["org_side"] != "customer", rank.get(p["role"], 9), -p["mentions"]))
    return out


def _most_common(xs):
    xs = [x for x in xs if x]
    return max(set(xs), key=xs.count) if xs else None


def _latest_sentiment(xs):
    xs = [x for x in xs if x and x != "unknown"]
    return xs[-1] if xs else "neutral"


def reason_account(store: Store, brain: Brain, account_id: str, sync_id: int | None,
                   log=lambda *a: None) -> dict:
    """Rebuild one account's working truth from LIVE evidence only."""
    ref = _ref_date()

    acc_obj = store.get_object(f"account:{account_id}")
    if not acc_obj or acc_obj["withdrawn_at"]:
        return {}
    acc = json.loads(acc_obj["payload"])
    meta = acc.get("meta") or {}
    lifecycle = (meta.get("category") or "unknown").lower()
    name = meta.get("name") or account_id
    arr = float(meta.get("arr") or 0)

    # ---- usage (deterministic) -----------------------------------------
    usage_obj = store.get_object(f"usage:{account_id}")
    series = []
    if usage_obj and not usage_obj["withdrawn_at"]:
        series = (json.loads(usage_obj["payload"]) or {}).get("series", [])
    u_hours = M.usage_profile(series, "flightHours")
    u_miss = M.usage_profile(series, "missions")

    # ---- documents (model reads, cached by content hash) ----------------
    #
    # Two phases on purpose. Work out which documents actually need reading,
    # then read only those, with bounded concurrency. Anything already cached
    # costs nothing, which is what keeps a 60 second poll affordable once the
    # first pass is done.
    rows = [r for r in store.live_objects("document")
            if r["account_id"] == account_id]
    prepared: list[list] = []
    to_read: list[tuple] = []
    for row in rows:
        doc = json.loads(row["payload"])
        cached = store.get_extraction(row["obj_key"], row["hash"], "doc_v1")
        # A degraded result is a placeholder, not a reading. If the reading
        # layer is available now, treat it as a cache miss and read properly.
        # Without this, one outage would permanently poison the cache: the
        # content hash never changes, so the document would never be re-read.
        if cached is not None and cached.get("_degraded") and brain.enabled:
            cached = None
        prepared.append([row, doc, cached])
        if cached is None:
            to_read.append((row, doc))

    if to_read:
        log(f"    reading {len(to_read)} document(s), "
            f"{len(prepared) - len(to_read)} cached")

        def _read(pair):
            row_, doc_ = pair
            out = brain.read_document(
                account_name=name, lifecycle=lifecycle,
                doc_type=doc_.get("doc_type", "other"),
                title=doc_.get("title", ""), content=doc_.get("content", ""),
            )
            if brain.enabled and config.READ_DELAY_SECONDS:
                time.sleep(config.READ_DELAY_SECONDS)
            return row_["obj_key"], out

        workers = max(1, min(config.READ_CONCURRENCY, len(to_read)))
        fresh: dict = {}
        with ThreadPoolExecutor(max_workers=workers) as ex:
            for key, out in ex.map(_read, to_read):
                fresh[key] = out
        # Every SQLite write happens back on this thread. Single writer.
        for row_, _doc in to_read:
            out = fresh.get(row_["obj_key"])
            if out is not None:
                store.put_extraction(row_["obj_key"], row_["hash"], "doc_v1",
                                     out, config.MODEL_EXTRACT)
        for item in prepared:
            if item[2] is None:
                item[2] = fresh.get(item[0]["obj_key"])

    for row, doc, cached in prepared:
        if cached is None:
            cached = _fallback_doc_stub()
        # Date resolution, two independent routes. The reading layer quotes a
        # date if the document states one in prose. If it did not, or if the
        # reading layer is degraded, scan the raw markdown for a structured
        # date. Whichever wins, the basis is recorded and shown in the UI so a
        # reader can tell a stated date from an inferred one.
        d, how = M.resolve_any_date(cached.get("doc_date_text", ""), ref)
        if d is None:
            d, how = M.sniff_document_date(doc.get("content", ""), ref)
        cached = dict(cached)
        cached["_doc_title"] = doc.get("title")
        cached["_doc_type"] = doc.get("doc_type")
        cached["_doc_date"] = d.isoformat() if d else None
        cached["_date_basis"] = how
        readings.append(cached)
        doc_index.append({
            "file": row["source_id"].split("/", 1)[-1],
            "title": doc.get("title"), "type": doc.get("doc_type"),
            "date": cached["_doc_date"], "date_basis": how,
            "date_text": cached.get("doc_date_text", ""),
            "summary": cached.get("summary", ""),
        })
        build_evidence(store, account_id, row["obj_key"], doc, cached["_doc_date"])

    store.commit()

    # ---- deterministic aggregation of what was read ---------------------
    support = {"open_tickets": 0, "critical_open": 0, "sla_breaches": 0, "recurring_issues": 0}
    for r in readings:
        s = r.get("support_state") or {}
        for k in support:
            try:
                support[k] += int(s.get(k) or 0)
            except (TypeError, ValueError):
                pass

    people = _merge_people(readings)
    customer_people = [p for p in people if p["org_side"] == "customer"]
    champion = next((p for p in customer_people if p["role"] == "champion"), None)
    champion_departed = bool(champion and champion["status"] in ("departed", "gone-quiet"))
    eb_known = any(p["role"] in ("economic-buyer", "executive-sponsor") for p in customer_people)

    dated = [r["_doc_date"] for r in readings if r.get("_doc_date")]
    last_touch = max(dated) if dated else None
    days_silent = None
    if last_touch:
        try:
            days_silent = (ref - datetime.strptime(last_touch, "%Y-%m-%d").date()).days
        except ValueError:
            pass

    # renewal date: highest-authority source wins
    renewal_date, renewal_src, renewal_conflicts = _pick_commercial(
        readings, ("renewal_date", "renewal", "contract_end", "poc_end_date"), ref)

    # Deterministic backstop. Renewal proximity drives every priority score on
    # the portfolio, so it must not silently vanish when the reading layer is
    # unavailable. Scan renewal-type documents directly, and keep the matched
    # line as the quote so the date is still traceable.
    if renewal_date is None:
        for row in store.live_objects("document"):
            if row["account_id"] != account_id:
                continue
            doc = json.loads(row["payload"])
            if doc.get("doc_type") not in ("renewal", "profile", "email"):
                continue
            d, how, line = M.sniff_renewal_date(doc.get("content", ""), ref)
            if d and (renewal_date is None or d > renewal_date):
                renewal_date = d
                renewal_src = {"doc": doc.get("title"), "quote": line,
                               "value": line, "basis": f"{how} (deterministic scan)"}

    days_to_renewal = (renewal_date - ref).days if renewal_date else None

    open_commitments = [
        {"owner_side": c.get("owner_side"), "owner": c.get("owner_name", ""),
         "promise": c.get("promise"), "due": c.get("due_text", ""),
         "quote": c.get("quote"), "doc": r.get("_doc_title"), "date": r.get("_doc_date")}
        for r in readings for c in (r.get("commitments") or [])
        if not c.get("appears_closed")
    ]

    all_signals = [
        {**s, "doc": r.get("_doc_title"), "date": r.get("_doc_date"),
         "doc_type": r.get("_doc_type")}
        for r in readings for s in (r.get("signals") or [])
    ]
    sev_rank = {"critical": 0, "high": 1, "medium": 2, "low": 3}
    all_signals.sort(key=lambda s: (sev_rank.get(s.get("severity", "low"), 4),
                                    s.get("date") or ""))

    competitor = any(s["kind"] == "competitor" for s in all_signals)
    friction = any(s["kind"] == "commercial-friction" for s in all_signals)

    # ---- narrative synthesis -------------------------------------------
    dossier = _dossier(name, account_id, meta, lifecycle, u_hours, u_miss,
                       support, people, doc_index, all_signals, open_commitments,
                       renewal_date, renewal_src, renewal_conflicts, days_silent, ref)
    synth = brain.synthesise_account(dossier=dossier)

    # ---- health (deterministic, decomposable) ---------------------------
    factors = {
        "usage_trajectory": M.HealthModel.score_usage(u_hours),
        "support_burden": M.HealthModel.score_support(
            support["open_tickets"], support["critical_open"],
            support["sla_breaches"], support["recurring_issues"]),
        "relationship": M.HealthModel.score_relationship(
            has_champion=bool(champion),
            champion_departed=champion_departed or synth.get("champion_status") == "departed",
            contacts_known=len(customer_people), days_silent=days_silent,
            economic_buyer_known=eb_known or bool(synth.get("economic_buyer_identified"))),
        "sentiment": M.HealthModel.score_sentiment(
            synth.get("sentiment_label", "neutral"), synth.get("sentiment_trend", "stable")),
        "commercial": M.HealthModel.score_commercial(
            days_to_renewal, friction or bool(synth.get("commercial_friction")),
            competitor or bool(synth.get("competitor_present")),
            int(synth.get("unresolved_commercial_items") or 0)),
    }
    if lifecycle == "churned":
        health = {"score": 0.0, "band": "Critical", "factors":
                  [{"factor": "churned", "quality": 0, "weight": 100, "contribution": 0,
                    "points_lost": 100, "reason": "Account has already churned."}],
                  "biggest_drag": "churned"}
    else:
        health = M.HealthModel.compute(factors)

    gap = M.reality_gap(meta.get("health", ""), health["band"])

    prob, prob_notes = M.renewal_probability(
        health["score"], days_to_renewal, lifecycle,
        competitor or bool(synth.get("competitor_present")),
        friction or bool(synth.get("commercial_friction")),
        u_hours["trajectory"])
    bucket = M.renewal_bucket(prob, lifecycle)

    actions = []
    for a in (synth.get("next_actions") or []):
        val = M.action_value(
            arr=arr,
            prob_loss=float(a.get("prob_loss_if_ignored") or 0),
            impact=float(a.get("impact") or 0.3),
            effort=float(a.get("effort") or 3),
            days_to_renewal=days_to_renewal,
            expansion_value=float(a.get("expansion_value_usd") or 0),
        )
        actions.append({**a, "value_score": val, "account_id": account_id,
                        "account_name": name, "arr": arr,
                        "days_to_renewal": days_to_renewal})
    actions.sort(key=lambda a: -a["value_score"])

    state = {
        "account_id": account_id, "name": name, "lifecycle": lifecycle,
        "vertical": meta.get("vertical"), "region": meta.get("region"),
        "arr": arr, "tier": meta.get("tier"), "docks_raw": meta.get("docks"),
        "cs_owner": meta.get("csOwner"), "se_owner": meta.get("seOwner"),
        "crm_health": meta.get("health"), "crm_sentiment": meta.get("sentiment"),
        "crm_champion_tagged": meta.get("championTagged"),
        "computed_at": utcnow(), "reference_date": ref.isoformat(),
        "health": health, "reality_gap": gap,
        "usage": {"flightHours": u_hours, "missions": u_miss,
                  "series": series, "has_usage": bool(series)},
        "support": support,
        "people": people,
        "days_silent": days_silent, "last_touch": last_touch,
        "renewal": {"date": renewal_date.isoformat() if renewal_date else None,
                    "days_to_renewal": days_to_renewal, "source": renewal_src,
                    "conflicts": renewal_conflicts,
                    "probability": round(prob, 3) if prob >= 0 else None,
                    "bucket": bucket, "basis": prob_notes},
        "signals": all_signals,
        "open_commitments": open_commitments,
        "documents": sorted(doc_index, key=lambda d: d.get("date") or "", reverse=True),
        "synthesis": synth,
        "actions": actions,
        "degraded": bool(synth.get("_degraded")),
    }
    return state


def _pick_commercial(readings, fields, ref):
    """Resolve a commercial fact when several documents state it differently.
    Higher source authority wins; every loser is recorded as a conflict."""
    cands = []
    for r in readings:
        auth = config.DOC_AUTHORITY.get(r.get("_doc_type", ""), 1)
        for f in r.get("commercial_facts") or []:
            if (f.get("field") or "").lower().replace(" ", "_") in fields:
                d, how = M.resolve_any_date(f.get("value", ""), ref)
                if d:
                    cands.append({"date": d, "value": f.get("value"),
                                  "doc": r.get("_doc_title"),
                                  "doc_type": r.get("_doc_type"),
                                  "authority": auth, "quote": f.get("quote"),
                                  "basis": how, "doc_date": r.get("_doc_date")})
    if not cands:
        return None, None, []
    cands.sort(key=lambda c: (-c["authority"], c["doc_date"] or ""), reverse=False)
    cands.sort(key=lambda c: (-c["authority"], -(len(c["doc_date"] or ""))))
    winner = cands[0]
    conflicts = [c for c in cands[1:] if c["date"] != winner["date"]]
    return winner["date"], {"doc": winner["doc"], "quote": winner["quote"],
                            "value": winner["value"]}, conflicts


def _dossier(name, aid, meta, lifecycle, uh, um, support, people, docs,
             signals, commitments, renewal_date, renewal_src, conflicts,
             days_silent, ref) -> str:
    """Everything the synthesis layer is allowed to reason over. Computed
    numbers are handed in already computed."""
    L = []
    A = L.append
    A(f"ACCOUNT: {name}  (id: {aid})")
    A(f"LIFECYCLE STAGE: {lifecycle}")
    A(f"TODAY (reference date): {ref.isoformat()}")
    A(f"RAW CRM RECORD: {json.dumps(meta, default=str)}")
    A("NOTE: in this dataset the CRM 'health' field is unreliable and near-uniform "
      "across all accounts. Do not treat it as evidence.")
    A("")
    A("PRE-COMPUTED USAGE ANALYTICS (do not recalculate):")
    A(f"  flight hours: {uh['headline']}")
    A(f"    trajectory={uh['trajectory']} pct_3v3={uh['pct_3v3']} "
      f"latest={uh['latest']} peak={uh['peak']} ({uh['peak_month']}) "
      f"slope/mo={uh['slope_per_month']} dormant_months={uh['dormant_months']} "
      f"recovering={uh['recovering']}")
    A(f"  missions:     {um['headline']}")
    A(f"  monthly series: {json.dumps(uh['months'])} hours={json.dumps(uh['values'])}")
    A("")
    A(f"AGGREGATED SUPPORT STATE: {json.dumps(support)}")
    if days_silent is not None:
        A(f"DAYS SINCE MOST RECENT DATED DOCUMENT: {days_silent}")
    if renewal_date:
        A(f"RESOLVED RENEWAL/CONTRACT DATE: {renewal_date.isoformat()} "
          f"(from {renewal_src['doc']}, stated as {renewal_src['value']!r})")
        A(f"  days from today: {(renewal_date - ref).days}")
    if conflicts:
        A("  CONFLICTING DATES FOUND IN OTHER SOURCES (report these as contradictions):")
        for c in conflicts:
            A(f"    {c['doc']} says {c['value']!r} -> {c['date'].isoformat()}  quote: {c['quote'][:200]}")
    A("")
    A("PEOPLE MERGED ACROSS ALL DOCUMENTS:")
    for p in people[:20]:
        A(f"  {p['name']} | {p['title'] or 'title unknown'} | {p['org_side']} | "
          f"role={p['role']} | sentiment={p['sentiment']} | status={p['status']} | "
          f"seen in {p['mentions']} doc(s)")
        for q in p["quotes"][:2]:
            A(f"      quote: {q['quote'][:220]}")
    A("")
    A("DOCUMENT INVENTORY (live only):")
    for d in docs:
        A(f"  [{d['type']}] {d['title']} | date={d['date']} ({d['date_basis']}, "
          f"stated {d['date_text']!r}) | {d['summary'][:200]}")
    A("")
    A("SIGNALS EXTRACTED (severity ordered):")
    for s in signals[:45]:
        A(f"  [{s.get('severity')}] {s.get('kind')} :: {s.get('claim')}")
        A(f"      from {s.get('doc')} ({s.get('date')}) quote: {(s.get('quote') or '')[:260]}")
        if s.get("dollar_hint"):
            A(f"      figure mentioned: {s['dollar_hint']}")
    A("")
    A("OPEN COMMITMENTS (nobody has closed these):")
    for c in commitments[:25]:
        A(f"  {c['owner_side']} / {c['owner'] or 'unnamed'}: {c['promise']} "
          f"(due {c['due'] or 'unstated'}) from {c['doc']} quote: {(c['quote'] or '')[:200]}")
    return "\n".join(L)


# --------------------------------------------------------------------------
# Belief diffing
# --------------------------------------------------------------------------

WATCHED = [
    ("health.band", lambda s: s.get("health", {}).get("band")),
    ("health.score", lambda s: s.get("health", {}).get("score")),
    ("renewal.bucket", lambda s: s.get("renewal", {}).get("bucket")),
    ("renewal.probability", lambda s: s.get("renewal", {}).get("probability")),
    ("renewal.date", lambda s: s.get("renewal", {}).get("date")),
    ("usage.trajectory", lambda s: s.get("usage", {}).get("flightHours", {}).get("trajectory")),
    ("reality_gap.severity", lambda s: s.get("reality_gap", {}).get("severity")),
    ("sentiment", lambda s: s.get("synthesis", {}).get("sentiment_label")),
    ("champion_status", lambda s: s.get("synthesis", {}).get("champion_status")),
    ("top_action", lambda s: (s.get("actions") or [{}])[0].get("action")),
    ("open_risks", lambda s: len(s.get("synthesis", {}).get("top_risks") or [])),
    ("live_documents", lambda s: len(s.get("documents") or [])),
]


def diff_beliefs(before: dict | None, after: dict) -> list[dict]:
    if not before:
        return []
    out = []
    for field, get in WATCHED:
        a, b = get(before), get(after)
        if isinstance(a, float) and isinstance(b, float):
            if abs(a - b) < 0.5:
                continue
        if a != b:
            out.append({"field": field, "old": a, "new": b})
    return out


# --------------------------------------------------------------------------
# One full cycle
# --------------------------------------------------------------------------

def run_cycle(store: Store, trigger: str = "manual", force_all: bool = False,
              log=print) -> dict:
    sync_id = store.start_sync(trigger)
    t0 = datetime.now(timezone.utc)
    try:
        bob = BookOfBusiness()
        log(f"[sync {sync_id}] crawling source ...")
        crawl = bob.crawl(log=lambda m: log("   " + m))
        bob.close()

        sentinel = Sentinel(store)
        report = sentinel.reconcile(crawl, sync_id, log=log)
        log(f"[sync {sync_id}] {report['objects_seen']} objects, "
            f"+{len(report['added'])} added, ~{len(report['modified'])} modified, "
            f"-{len(report['withdrawn'])} WITHDRAWN, {report['unchanged']} unchanged")

        brain = Brain()
        if not brain.enabled:
            log("[warn] no model key: running in degraded (deterministic) mode")
        else:
            log(f"[brain] reading via {brain.provider}, model {config.MODEL_EXTRACT}"
                if brain.provider == "anthropic" else
                f"[brain] reading via {brain.provider}, model {config.OPENAI_MODEL}")

        held = store.all_current_states()
        targets = ([a["account_id"] for a in store.live_objects("account")]
                   if force_all or not held
                   else list(report["touched_accounts"]))

        # Self-healing. If an account's current belief was built while the
        # reading layer was unavailable, it is a placeholder, not an answer.
        # The source has not changed, so nothing would ever mark it dirty.
        # Rebuild it as soon as reading becomes possible again.
        if brain.enabled:
            repair = [s["account_id"] for s in held if s["state"].get("degraded")]
            if repair:
                log(f"[repair] {len(repair)} account(s) hold degraded beliefs, "
                    f"rebuilding now that reading is available")
                targets = sorted(set(targets) | set(repair))

        events = store.changes_for_sync(sync_id)
        by_account: dict[str, list[dict]] = {}
        for e in events:
            by_account.setdefault(e["account_id"] or "*", []).append(e)

        resynth = 0
        for aid in targets:
            log(f"[sync {sync_id}] reasoning over {aid}")
            before = store.current_state(aid)
            before_state = before["state"] if before else None
            state = reason_account(store, brain, aid, sync_id, log=log)
            if not state:
                continue
            ev_ids = [e["id"] for e in by_account.get(aid, [])]
            ihash = content_hash({
                "docs": sorted(d["file"] for d in state["documents"]),
                "usage": state["usage"]["series"],
                "crm": state["crm_health"],
            })
            store.save_state(aid, state, ihash, sync_id)
            resynth += 1

            deltas = diff_beliefs(before_state, state)
            if deltas and before_state:
                narrative = brain.narrate_change(
                    account=state["name"], before=before_state, after=state,
                    changes=by_account.get(aid, []))
                for d in deltas:
                    store.log_delta(aid, sync_id, d["field"], d["old"], d["new"],
                                    ev_ids, narrative)
                log(f"    belief moved: " + ", ".join(
                    f"{d['field']} {d['old']} -> {d['new']}" for d in deltas))
        store.commit()

        # Close the sync record BEFORE snapshotting the portfolio, otherwise
        # the published change feed reports its own sync as still running with
        # zero objects, which reads as a broken audit trail.
        store.finish_sync(
            sync_id, objects_seen=report["objects_seen"], added=len(report["added"]),
            modified=len(report["modified"]), withdrawn=len(report["withdrawn"]),
            accounts_resynth=resynth, status="ok",
            notes=json.dumps({"provider": brain.provider,
                              "model_calls": brain.calls,
                              "model_failures": brain.failures,
                              "last_model_error": brain.last_error,
                              "in_tok": brain.input_tokens,
                              "out_tok": brain.output_tokens,
                              "crawl_errors": crawl.get("errors", [])[:5]}))

        pf = build_portfolio(store)
        pf["reading"] = {"provider": brain.provider, "enabled": brain.enabled,
                         "calls": brain.calls, "failures": brain.failures,
                         "last_error": brain.last_error}
        store.save_portfolio(pf, sync_id)
        dur = (datetime.now(timezone.utc) - t0).total_seconds()
        log(f"[sync {sync_id}] done in {dur:.1f}s, {resynth} accounts re-reasoned, "
            f"{brain.calls} model calls")
        return {"sync_id": sync_id, "report": report, "resynth": resynth,
                "model_calls": brain.calls}
    except Exception as e:
        import traceback
        store.finish_sync(sync_id, status="error", error=traceback.format_exc()[-4000:])
        log(f"[sync {sync_id}] FAILED: {e}")
        raise
