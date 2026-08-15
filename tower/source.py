"""
Adapter over the Book of Business MCP server.

Nine read-only tools, three of which most people will never touch:
se_list_dataset_files / se_get_dataset_file / se_search_dataset expose a
SECOND system of record (Solutions Engineering: accounts, issues, feature
requests, tasks, meeting notes). That second view is where most of the
contradictions live, and feature_requests.md is what makes the win-back
question answerable rather than a guess.
"""

from __future__ import annotations

import re
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Any

from .mcp_client import MCPClient
from . import config


def _as_list(x: Any) -> list:
    if x is None:
        return []
    if isinstance(x, list):
        return x
    if isinstance(x, dict):
        for k in ("accounts", "documents", "files", "usage", "results", "items", "data"):
            if isinstance(x.get(k), list):
                return x[k]
    return [x]


DOC_TYPE_PATTERNS = [
    (re.compile(r"transcript", re.I), "transcript"),
    (re.compile(r"email|thread|corresp", re.I), "email"),
    (re.compile(r"ticket|support|escalat", re.I), "ticket"),
    (re.compile(r"internal[_ ]?note|note", re.I), "note"),
    (re.compile(r"renewal|subscription|contract|order[_ ]?form|quote|pricing", re.I), "renewal"),
    (re.compile(r"account[_ ]?profile|profile|crm", re.I), "profile"),
    (re.compile(r"usage|flight|telemetry", re.I), "usage"),
]


def classify_doc(file_name: str, title: str = "", declared: str = "") -> str:
    """The API gives a `type`, but it is inconsistent. Trust it, then verify
    against the filename, because doc type drives source authority when two
    sources disagree."""
    blob = f"{declared} {file_name} {title}"
    for rx, label in DOC_TYPE_PATTERNS:
        if rx.search(blob):
            return label
    return (declared or "other").lower()


@dataclass
class AccountRecord:
    id: str
    raw: dict
    documents: list[dict] = field(default_factory=list)
    usage: list[dict] = field(default_factory=list)

    @property
    def name(self) -> str:
        return self.raw.get("name") or self.id

    @property
    def lifecycle(self) -> str:
        return (self.raw.get("category") or "unknown").lower()

    @property
    def arr(self) -> float:
        try:
            return float(self.raw.get("arr") or 0)
        except (TypeError, ValueError):
            return 0.0


class BookOfBusiness:
    """Read-only client. Every method is a pure fetch. No reasoning here."""

    def __init__(self, endpoint: str | None = None, token: str | None = None):
        self.mcp = MCPClient(
            endpoint or config.MCP_ENDPOINT, token or config.MCP_TOKEN
        )

    # ------------------------------------------------------------ accounts
    def list_accounts(self) -> list[dict]:
        return _as_list(self.mcp.call("list_accounts"))

    def get_account(self, account_id: str) -> dict:
        r = self.mcp.call("get_account", {"id": account_id})
        return r if isinstance(r, dict) else {"raw": r}

    def list_account_documents(self, account_id: str) -> list[dict]:
        out = []
        for d in _as_list(self.mcp.call("list_account_documents", {"id": account_id})):
            if isinstance(d, str):
                d = {"file": d, "title": d, "type": ""}
            d = dict(d)
            d["doc_type"] = classify_doc(
                d.get("file", ""), d.get("title", ""), d.get("type", "")
            )
            out.append(d)
        return out

    def get_account_document(self, account_id: str, file: str) -> str:
        r = self.mcp.call("get_account_document", {"id": account_id, "file": file})
        if isinstance(r, str):
            return r
        if isinstance(r, dict):
            for k in ("content", "text", "body", "markdown"):
                if isinstance(r.get(k), str):
                    return r[k]
        return str(r)

    def get_account_usage(self, account_id: str) -> list[dict]:
        rows = _as_list(self.mcp.call("get_account_usage", {"id": account_id}))
        clean = []
        for r in rows:
            if not isinstance(r, dict):
                continue
            m = r.get("month") or r.get("period")
            if not m:
                continue
            clean.append({
                "month": str(m)[:7],
                "flightHours": _num(r.get("flightHours", r.get("flight_hours"))),
                "missions": _num(r.get("missions", r.get("mission_count"))),
            })
        clean.sort(key=lambda x: x["month"])
        return clean

    def search_documents(self, query: str) -> Any:
        return self.mcp.call("search_documents", {"query": query})

    # ------------------------------------- solutions engineering dataset
    def se_list_files(self) -> list[dict]:
        out = []
        for f in _as_list(self.mcp.call("se_list_dataset_files")):
            if isinstance(f, str):
                f = {"file": f, "title": f}
            out.append(dict(f))
        return out

    def se_get_file(self, file: str) -> str:
        r = self.mcp.call("se_get_dataset_file", {"file": file})
        if isinstance(r, str):
            return r
        if isinstance(r, dict):
            for k in ("content", "text", "body", "markdown"):
                if isinstance(r.get(k), str):
                    return r[k]
        return str(r)

    def se_search(self, query: str) -> Any:
        return self.mcp.call("se_search_dataset", {"query": query})

    # --------------------------------------------------------- full crawl
    def crawl(self, log=lambda *a: None, workers: int = 6) -> dict:
        """One complete inventory of the source of truth.

        Returns EVERYTHING currently visible. The sentinel diffs this against
        what we hold. Crawling the full inventory each cycle (rather than
        asking for 'what changed') is the only way to notice that a document
        we used to have is gone.

        That completeness costs roughly 170 round trips, which is minutes if
        done serially and seconds if not. Since the whole promise is a 60
        second poll, the fan-out is not an optimisation, it is the feature.
        """
        accounts = self.list_accounts()
        log(f"accounts: {len(accounts)}")
        out: dict = {"accounts": {}, "se": {}, "errors": []}
        lock = threading.Lock()

        def err(msg: str):
            with lock:
                out["errors"].append(msg)

        def fetch_account(a: dict):
            aid = a.get("id")
            if not aid:
                return
            rec: dict = {"meta": a, "detail": {}, "documents": {}, "usage": []}
            try:
                rec["detail"] = self.get_account(aid)
            except Exception as e:
                err(f"get_account {aid}: {e}")
            try:
                docs = self.list_account_documents(aid)
            except Exception as e:
                err(f"list_docs {aid}: {e}")
                docs = []
            try:
                rec["usage"] = self.get_account_usage(aid)
            except Exception as e:
                err(f"usage {aid}: {e}")

            def fetch_doc(d: dict):
                fn = d.get("file")
                if not fn:
                    return
                try:
                    body = self.get_account_document(aid, fn)
                except Exception as e:
                    err(f"doc {aid}/{fn}: {e}")
                    return
                with lock:
                    rec["documents"][fn] = {
                        "file": fn,
                        "title": d.get("title") or fn,
                        "declared_type": d.get("type") or "",
                        "doc_type": d.get("doc_type") or classify_doc(fn),
                        "content": body,
                    }

            if docs:
                with ThreadPoolExecutor(max_workers=min(workers, len(docs))) as ex:
                    list(ex.map(fetch_doc, docs))

            with lock:
                out["accounts"][aid] = rec
            log(f"  {aid}: {len(rec['documents'])} docs, {len(rec['usage'])} usage months")

        def fetch_se(f: dict):
            fn = f.get("file")
            if not fn:
                return
            try:
                body = self.se_get_file(fn)
            except Exception as e:
                err(f"se {fn}: {e}")
                return
            with lock:
                out["se"][fn] = {"file": fn, "title": f.get("title") or fn,
                                 "content": body}

        with ThreadPoolExecutor(max_workers=workers) as ex:
            list(ex.map(fetch_account, accounts))

        try:
            se_files = self.se_list_files()
            with ThreadPoolExecutor(max_workers=workers) as ex:
                list(ex.map(fetch_se, se_files))
            log(f"se dataset: {len(out['se'])} files")
        except Exception as e:
            err(f"se dataset: {e}")

        return out

    def close(self):
        self.mcp.close()


def _num(v) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0
