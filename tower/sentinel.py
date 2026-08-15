"""
THE SENTINEL - full-inventory reconciliation.

This is the part of TOWER that answers the hardest line in the brief:

  "At least one document that's currently available stops being available."

Every incremental-sync design in the world fails that sentence. If you ask
the source "what is new since last time" you will never be told about the
thing that quietly disappeared. So the sentinel does the unglamorous thing:
it takes a COMPLETE inventory every cycle, content-hashes every object, and
reconciles the full key set against what we hold.

Three outcomes per object:
  ADDED      key we have never seen
  MODIFIED   key we hold, different content hash
  WITHDRAWN  key we hold, absent from this crawl  <-- the one that matters
  (RESTORED  key we tombstoned, now back)

Withdrawn objects are tombstoned, never deleted. Their evidence is marked
withdrawn and drops out of active reasoning, which is what forces the
downstream belief to actually move. That produces the sentence we want on
the change feed: "we no longer believe X, because the only evidence for X
was withdrawn."
"""

from __future__ import annotations

import json
from typing import Callable

from .store import Store, content_hash


def _diff_summary(old: dict | None, new: dict) -> str:
    """Short human sentence about what actually changed inside an object."""
    if old is None:
        return ""
    try:
        o = json.loads(old["payload"]) if isinstance(old.get("payload"), str) else old
    except Exception:
        return "content changed"
    if not isinstance(o, dict) or not isinstance(new, dict):
        return "content changed"

    changed = []
    for k in sorted(set(o) | set(new)):
        a, b = o.get(k), new.get(k)
        if a == b:
            continue
        if k == "content" and isinstance(a, str) and isinstance(b, str):
            delta = len(b) - len(a)
            changed.append(f"content {'+' if delta >= 0 else ''}{delta} chars")
        else:
            sa, sb = str(a)[:60], str(b)[:60]
            changed.append(f"{k}: {sa!r} -> {sb!r}")
    return "; ".join(changed[:6]) or "content changed"


class Sentinel:
    def __init__(self, store: Store):
        self.store = store

    # ------------------------------------------------------------------
    def reconcile(
        self,
        crawl: dict,
        sync_id: int,
        log: Callable[..., None] = lambda *a: None,
    ) -> dict:
        """Diff a full crawl against held state. Returns a change report."""

        seen: dict[str, dict] = {}

        # ---- flatten the crawl into addressable objects -----------------
        for aid, rec in crawl.get("accounts", {}).items():
            meta = dict(rec.get("meta") or {})
            detail = rec.get("detail") or {}
            # the CRM record for the account
            seen[f"account:{aid}"] = {
                "resource_kind": "account",
                "source_id": aid,
                "account_id": aid,
                "title": meta.get("name") or aid,
                "payload": {"meta": meta, "detail": _strip_docs(detail)},
            }
            # each source document
            for fn, d in (rec.get("documents") or {}).items():
                seen[f"document:{aid}/{fn}"] = {
                    "resource_kind": "document",
                    "source_id": f"{aid}/{fn}",
                    "account_id": aid,
                    "title": d.get("title") or fn,
                    "payload": d,
                }
            # usage series as a single versioned object
            if rec.get("usage"):
                seen[f"usage:{aid}"] = {
                    "resource_kind": "usage",
                    "source_id": aid,
                    "account_id": aid,
                    "title": f"{aid} usage series",
                    "payload": {"series": rec["usage"]},
                }

        for fn, d in (crawl.get("se") or {}).items():
            seen[f"se:{fn}"] = {
                "resource_kind": "se",
                "source_id": fn,
                "account_id": None,
                "title": d.get("title") or fn,
                "payload": d,
            }

        # ---- upsert everything currently visible ------------------------
        report = {"added": [], "modified": [], "withdrawn": [], "restored": [],
                  "unchanged": 0, "touched_accounts": set(), "event_ids": []}

        for key, o in seen.items():
            change, old_hash = self.store.upsert_object(
                resource_kind=o["resource_kind"],
                source_id=o["source_id"],
                account_id=o["account_id"],
                payload=o["payload"],
            )
            if change == "UNCHANGED":
                report["unchanged"] += 1
                continue

            prev = None
            if change in ("MODIFIED", "RESTORED"):
                prev = self.store.conn.execute(
                    "SELECT payload FROM raw_revision WHERE obj_key=? "
                    "ORDER BY revision DESC LIMIT 1 OFFSET 1", (key,)
                ).fetchone()
                prev = dict(prev) if prev else None

            ev_id = self.store.log_change(
                sync_id,
                change_type=change,
                resource_kind=o["resource_kind"],
                source_id=o["source_id"],
                account_id=o["account_id"],
                title=o["title"],
                old_hash=old_hash,
                new_hash=content_hash(o["payload"]),
                diff_summary=_diff_summary(prev, o["payload"]),
            )
            report["event_ids"].append(ev_id)
            bucket = {"ADDED": "added", "MODIFIED": "modified",
                      "RESTORED": "restored"}[change]
            report[bucket].append({"key": key, "title": o["title"],
                                   "account_id": o["account_id"], "event_id": ev_id})
            if o["account_id"]:
                report["touched_accounts"].add(o["account_id"])
            log(f"  {change:<9} {key}")

        # ---- THE PART EVERYONE FORGETS ----------------------------------
        # Anything we hold as live but did not see in this complete crawl
        # has been withdrawn from the source.
        held = self.store.known_keys()
        for key, row in held.items():
            if row["withdrawn_at"] is not None:
                continue
            if key in seen:
                continue
            self.store.tombstone(key)
            ev_id = self.store.log_change(
                sync_id,
                change_type="WITHDRAWN",
                resource_kind=row["resource_kind"],
                source_id=key.split(":", 1)[1],
                account_id=row["account_id"],
                title=key,
                old_hash=row["hash"],
                new_hash=None,
                diff_summary="object is no longer served by the source; "
                             "tombstoned and removed from active reasoning",
            )
            report["event_ids"].append(ev_id)
            report["withdrawn"].append({"key": key, "account_id": row["account_id"],
                                        "event_id": ev_id})
            if row["account_id"]:
                report["touched_accounts"].add(row["account_id"])
            log(f"  WITHDRAWN {key}")

        # A withdrawal with no account attached (an SE file) still changes
        # portfolio-level reasoning, so mark every account dirty in that case.
        if any(w["account_id"] is None for w in report["withdrawn"]):
            for a in self.store.live_objects("account"):
                report["touched_accounts"].add(a["account_id"])

        self.store.commit()
        report["touched_accounts"] = sorted(report["touched_accounts"])
        report["total_changes"] = (
            len(report["added"]) + len(report["modified"])
            + len(report["withdrawn"]) + len(report["restored"])
        )
        report["objects_seen"] = len(seen)
        return report


def _strip_docs(detail: dict) -> dict:
    """get_account echoes the document list. Keep the names (so a document
    vanishing from the manifest is itself a detectable change) but never the
    bodies, which live in their own objects."""
    if not isinstance(detail, dict):
        return {}
    out = {}
    for k, v in detail.items():
        if isinstance(v, list) and v and isinstance(v[0], dict) and "file" in v[0]:
            out[k] = sorted(x.get("file", "") for x in v)
        else:
            out[k] = v
    return out
