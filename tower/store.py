"""
TOWER - versioned truth store.

Design rule that everything else depends on:
  Nothing is ever deleted. Documents that disappear from the source are
  TOMBSTONED (withdrawn_at is set) and excluded from active reasoning,
  but they stay queryable so we can answer "what did we believe before,
  and which piece of evidence was withdrawn to change it".

The brief says: "At least one document that's currently available stops
being available." A naive upsert loop can never see that. Full-inventory
reconciliation on every cycle can.
"""

import json
import sqlite3
import hashlib
from pathlib import Path
from datetime import datetime, timezone
from typing import Any, Iterable

DB_PATH = Path(__file__).resolve().parent.parent / "data" / "tower.db"


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def content_hash(payload: Any) -> str:
    """Stable hash of a JSON payload. Key order must not matter."""
    blob = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


SCHEMA = """
PRAGMA journal_mode=WAL;

-- ---------------------------------------------------------------- raw layer
-- Every object ever seen from the source API, content addressed.
-- resource_kind: account | document | usage | other
CREATE TABLE IF NOT EXISTS raw_object (
    obj_key        TEXT PRIMARY KEY,        -- "<kind>:<source_id>"
    resource_kind  TEXT NOT NULL,
    source_id      TEXT NOT NULL,
    account_id     TEXT,
    hash           TEXT NOT NULL,
    payload        TEXT NOT NULL,           -- raw JSON exactly as returned
    first_seen_at  TEXT NOT NULL,
    last_seen_at   TEXT NOT NULL,
    revision       INTEGER NOT NULL DEFAULT 1,
    withdrawn_at   TEXT,                    -- tombstone: vanished from source
    UNIQUE(resource_kind, source_id)
);
CREATE INDEX IF NOT EXISTS ix_raw_account ON raw_object(account_id);
CREATE INDEX IF NOT EXISTS ix_raw_live    ON raw_object(withdrawn_at);

-- Full history of every version of every object, so we can diff over time.
CREATE TABLE IF NOT EXISTS raw_revision (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    obj_key       TEXT NOT NULL,
    revision      INTEGER NOT NULL,
    hash          TEXT NOT NULL,
    payload       TEXT NOT NULL,
    observed_at   TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_rev_obj ON raw_revision(obj_key);

-- ------------------------------------------------------------ change ledger
-- The audit trail. This is the proof the system watched, not the operator.
CREATE TABLE IF NOT EXISTS change_event (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    sync_id        INTEGER NOT NULL,
    detected_at    TEXT NOT NULL,
    change_type    TEXT NOT NULL,           -- ADDED | MODIFIED | WITHDRAWN | RESTORED
    resource_kind  TEXT NOT NULL,
    source_id      TEXT NOT NULL,
    account_id     TEXT,
    title          TEXT,
    old_hash       TEXT,
    new_hash       TEXT,
    diff_summary   TEXT
);
CREATE INDEX IF NOT EXISTS ix_chg_sync ON change_event(sync_id);
CREATE INDEX IF NOT EXISTS ix_chg_acct ON change_event(account_id);

CREATE TABLE IF NOT EXISTS sync_run (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at        TEXT NOT NULL,
    finished_at       TEXT,
    trigger           TEXT NOT NULL,        -- scheduler | manual | ci | boot
    objects_seen      INTEGER DEFAULT 0,
    added             INTEGER DEFAULT 0,
    modified          INTEGER DEFAULT 0,
    withdrawn         INTEGER DEFAULT 0,
    accounts_resynth  INTEGER DEFAULT 0,
    status            TEXT DEFAULT 'running',
    error             TEXT,
    notes             TEXT
);

-- --------------------------------------------------------------- evidence
-- Atomic, citable units. Every downstream claim points at one of these.
CREATE TABLE IF NOT EXISTS evidence (
    ev_id         TEXT PRIMARY KEY,         -- deterministic: hash(obj_key, anchor)
    obj_key       TEXT NOT NULL,
    account_id    TEXT NOT NULL,
    doc_type      TEXT,                     -- transcript|email|ticket|note|crm|renewal|usage
    doc_title     TEXT,
    doc_date      TEXT,
    anchor        TEXT,                     -- locator inside the doc
    quote         TEXT NOT NULL,
    speaker       TEXT,
    withdrawn_at  TEXT,
    created_at    TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_ev_acct ON evidence(account_id);
CREATE INDEX IF NOT EXISTS ix_ev_obj  ON evidence(obj_key);

-- ------------------------------------------------------- derived beliefs
-- Versioned. We never overwrite a belief, we supersede it. That is how we
-- answer "did your system update its own understanding" with a diff.
CREATE TABLE IF NOT EXISTS account_state (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    account_id     TEXT NOT NULL,
    version        INTEGER NOT NULL,
    computed_at    TEXT NOT NULL,
    sync_id        INTEGER,
    input_hash     TEXT NOT NULL,           -- hash of all live evidence used
    state_json     TEXT NOT NULL,
    superseded_at  TEXT,
    UNIQUE(account_id, version)
);
CREATE INDEX IF NOT EXISTS ix_state_live ON account_state(account_id, superseded_at);

-- Human readable narration of how a belief moved and why.
CREATE TABLE IF NOT EXISTS belief_delta (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    account_id    TEXT NOT NULL,
    sync_id       INTEGER,
    occurred_at   TEXT NOT NULL,
    field         TEXT NOT NULL,
    old_value     TEXT,
    new_value     TEXT,
    caused_by     TEXT,                     -- JSON list of change_event ids
    narrative     TEXT
);
CREATE INDEX IF NOT EXISTS ix_delta_acct ON belief_delta(account_id);

-- Portfolio level rollup, also versioned.
CREATE TABLE IF NOT EXISTS portfolio_state (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    version      INTEGER NOT NULL,
    computed_at  TEXT NOT NULL,
    sync_id      INTEGER,
    state_json   TEXT NOT NULL
);

-- Cache so we never pay twice to extract from an unchanged document.
CREATE TABLE IF NOT EXISTS extraction (
    obj_key      TEXT NOT NULL,
    hash         TEXT NOT NULL,
    extractor    TEXT NOT NULL,
    result_json  TEXT NOT NULL,
    model        TEXT,
    created_at   TEXT NOT NULL,
    PRIMARY KEY (obj_key, hash, extractor)
);
"""


class Store:
    def __init__(self, path: Path = DB_PATH):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(self.path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)
        self.conn.commit()

    # ---------------------------------------------------------------- sync
    def start_sync(self, trigger: str) -> int:
        cur = self.conn.execute(
            "INSERT INTO sync_run (started_at, trigger) VALUES (?,?)",
            (utcnow(), trigger),
        )
        self.conn.commit()
        return cur.lastrowid

    def finish_sync(self, sync_id: int, **fields) -> None:
        fields.setdefault("finished_at", utcnow())
        fields.setdefault("status", "ok")
        sets = ", ".join(f"{k}=?" for k in fields)
        self.conn.execute(
            f"UPDATE sync_run SET {sets} WHERE id=?", (*fields.values(), sync_id)
        )
        self.conn.commit()

    def last_syncs(self, n: int = 25) -> list[dict]:
        rows = self.conn.execute(
            "SELECT * FROM sync_run ORDER BY id DESC LIMIT ?", (n,)
        ).fetchall()
        return [dict(r) for r in rows]

    # ----------------------------------------------------------- raw layer
    def live_objects(self, kind: str | None = None) -> list[dict]:
        q = "SELECT * FROM raw_object WHERE withdrawn_at IS NULL"
        args: tuple = ()
        if kind:
            q += " AND resource_kind=?"
            args = (kind,)
        return [dict(r) for r in self.conn.execute(q, args).fetchall()]

    def get_object(self, obj_key: str) -> dict | None:
        r = self.conn.execute(
            "SELECT * FROM raw_object WHERE obj_key=?", (obj_key,)
        ).fetchone()
        return dict(r) if r else None

    def known_keys(self, kind: str | None = None) -> dict[str, dict]:
        """All keys we have ever seen, live or tombstoned, for reconciliation."""
        q = "SELECT obj_key, hash, withdrawn_at, revision, account_id, resource_kind FROM raw_object"
        args: tuple = ()
        if kind:
            q += " WHERE resource_kind=?"
            args = (kind,)
        return {r["obj_key"]: dict(r) for r in self.conn.execute(q, args).fetchall()}

    def upsert_object(
        self,
        *,
        resource_kind: str,
        source_id: str,
        account_id: str | None,
        payload: Any,
    ) -> tuple[str, str | None]:
        """Returns (change_type, old_hash). change_type in ADDED|MODIFIED|RESTORED|UNCHANGED."""
        obj_key = f"{resource_kind}:{source_id}"
        h = content_hash(payload)
        now = utcnow()
        row = self.conn.execute(
            "SELECT hash, revision, withdrawn_at FROM raw_object WHERE obj_key=?",
            (obj_key,),
        ).fetchone()

        if row is None:
            self.conn.execute(
                """INSERT INTO raw_object
                   (obj_key,resource_kind,source_id,account_id,hash,payload,
                    first_seen_at,last_seen_at,revision)
                   VALUES (?,?,?,?,?,?,?,?,1)""",
                (obj_key, resource_kind, source_id, account_id, h,
                 json.dumps(payload, default=str), now, now),
            )
            self._add_revision(obj_key, 1, h, payload, now)
            return "ADDED", None

        old_hash, rev, withdrawn = row["hash"], row["revision"], row["withdrawn_at"]

        if old_hash == h and not withdrawn:
            self.conn.execute(
                "UPDATE raw_object SET last_seen_at=? WHERE obj_key=?", (now, obj_key)
            )
            return "UNCHANGED", old_hash

        new_rev = rev + 1 if old_hash != h else rev
        self.conn.execute(
            """UPDATE raw_object
               SET hash=?, payload=?, last_seen_at=?, revision=?,
                   withdrawn_at=NULL, account_id=COALESCE(?, account_id)
               WHERE obj_key=?""",
            (h, json.dumps(payload, default=str), now, new_rev, account_id, obj_key),
        )
        if old_hash != h:
            self._add_revision(obj_key, new_rev, h, payload, now)
        return ("RESTORED" if withdrawn else "MODIFIED"), old_hash

    def _add_revision(self, obj_key, revision, h, payload, now) -> None:
        self.conn.execute(
            """INSERT INTO raw_revision (obj_key,revision,hash,payload,observed_at)
               VALUES (?,?,?,?,?)""",
            (obj_key, revision, h, json.dumps(payload, default=str), now),
        )

    def tombstone(self, obj_key: str) -> None:
        now = utcnow()
        self.conn.execute(
            "UPDATE raw_object SET withdrawn_at=? WHERE obj_key=? AND withdrawn_at IS NULL",
            (now, obj_key),
        )
        self.conn.execute(
            "UPDATE evidence SET withdrawn_at=? WHERE obj_key=? AND withdrawn_at IS NULL",
            (now, obj_key),
        )

    # ---------------------------------------------------------- change log
    def log_change(self, sync_id: int, **kw) -> int:
        cur = self.conn.execute(
            """INSERT INTO change_event
               (sync_id,detected_at,change_type,resource_kind,source_id,
                account_id,title,old_hash,new_hash,diff_summary)
               VALUES (?,?,?,?,?,?,?,?,?,?)""",
            (
                sync_id, utcnow(), kw["change_type"], kw["resource_kind"],
                kw["source_id"], kw.get("account_id"), kw.get("title"),
                kw.get("old_hash"), kw.get("new_hash"), kw.get("diff_summary"),
            ),
        )
        return cur.lastrowid

    def changes_for_sync(self, sync_id: int) -> list[dict]:
        return [dict(r) for r in self.conn.execute(
            "SELECT * FROM change_event WHERE sync_id=? ORDER BY id", (sync_id,)
        ).fetchall()]

    def recent_changes(self, n: int = 200) -> list[dict]:
        return [dict(r) for r in self.conn.execute(
            "SELECT * FROM change_event ORDER BY id DESC LIMIT ?", (n,)
        ).fetchall()]

    # ------------------------------------------------------------ evidence
    def put_evidence(self, rows: Iterable[dict]) -> int:
        n = 0
        now = utcnow()
        for r in rows:
            ev_id = hashlib.sha256(
                f"{r['obj_key']}|{r['anchor']}".encode()
            ).hexdigest()[:20]
            self.conn.execute(
                """INSERT INTO evidence
                   (ev_id,obj_key,account_id,doc_type,doc_title,doc_date,
                    anchor,quote,speaker,created_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(ev_id) DO UPDATE SET
                     quote=excluded.quote, doc_date=excluded.doc_date,
                     doc_title=excluded.doc_title, withdrawn_at=NULL""",
                (ev_id, r["obj_key"], r["account_id"], r.get("doc_type"),
                 r.get("doc_title"), r.get("doc_date"), r["anchor"],
                 r["quote"], r.get("speaker"), now),
            )
            n += 1
        return n

    def evidence_for(self, account_id: str, include_withdrawn: bool = False) -> list[dict]:
        q = "SELECT * FROM evidence WHERE account_id=?"
        if not include_withdrawn:
            q += " AND withdrawn_at IS NULL"
        q += " ORDER BY doc_date DESC, ev_id"
        return [dict(r) for r in self.conn.execute(q, (account_id,)).fetchall()]

    def evidence_by_ids(self, ids: list[str]) -> dict[str, dict]:
        if not ids:
            return {}
        marks = ",".join("?" * len(ids))
        rows = self.conn.execute(
            f"SELECT * FROM evidence WHERE ev_id IN ({marks})", ids
        ).fetchall()
        return {r["ev_id"]: dict(r) for r in rows}

    # -------------------------------------------------------------- state
    def current_state(self, account_id: str) -> dict | None:
        r = self.conn.execute(
            """SELECT * FROM account_state
               WHERE account_id=? AND superseded_at IS NULL
               ORDER BY version DESC LIMIT 1""",
            (account_id,),
        ).fetchone()
        if not r:
            return None
        d = dict(r)
        d["state"] = json.loads(d.pop("state_json"))
        return d

    def all_current_states(self) -> list[dict]:
        rows = self.conn.execute(
            """SELECT * FROM account_state
               WHERE superseded_at IS NULL ORDER BY account_id"""
        ).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d["state"] = json.loads(d.pop("state_json"))
            out.append(d)
        return out

    def save_state(self, account_id: str, state: dict, input_hash: str,
                   sync_id: int | None) -> tuple[int, dict | None]:
        prev = self.current_state(account_id)
        version = (prev["version"] + 1) if prev else 1
        now = utcnow()
        if prev:
            self.conn.execute(
                "UPDATE account_state SET superseded_at=? WHERE id=?", (now, prev["id"])
            )
        self.conn.execute(
            """INSERT INTO account_state
               (account_id,version,computed_at,sync_id,input_hash,state_json)
               VALUES (?,?,?,?,?,?)""",
            (account_id, version, now, sync_id, input_hash,
             json.dumps(state, default=str)),
        )
        self.conn.commit()
        return version, (prev["state"] if prev else None)

    def state_history(self, account_id: str) -> list[dict]:
        rows = self.conn.execute(
            "SELECT version, computed_at, sync_id, input_hash FROM account_state "
            "WHERE account_id=? ORDER BY version DESC", (account_id,)
        ).fetchall()
        return [dict(r) for r in rows]

    def log_delta(self, account_id: str, sync_id: int | None, field: str,
                  old, new, caused_by: list[int], narrative: str) -> None:
        self.conn.execute(
            """INSERT INTO belief_delta
               (account_id,sync_id,occurred_at,field,old_value,new_value,
                caused_by,narrative)
               VALUES (?,?,?,?,?,?,?,?)""",
            (account_id, sync_id, utcnow(), field,
             json.dumps(old, default=str), json.dumps(new, default=str),
             json.dumps(caused_by), narrative),
        )

    def recent_deltas(self, n: int = 100, account_id: str | None = None) -> list[dict]:
        q = "SELECT * FROM belief_delta"
        args: tuple = ()
        if account_id:
            q += " WHERE account_id=?"
            args = (account_id,)
        q += " ORDER BY id DESC LIMIT ?"
        return [dict(r) for r in self.conn.execute(q, (*args, n)).fetchall()]

    def save_portfolio(self, state: dict, sync_id: int | None) -> int:
        r = self.conn.execute("SELECT MAX(version) v FROM portfolio_state").fetchone()
        version = (r["v"] or 0) + 1
        self.conn.execute(
            """INSERT INTO portfolio_state (version,computed_at,sync_id,state_json)
               VALUES (?,?,?,?)""",
            (version, utcnow(), sync_id, json.dumps(state, default=str)),
        )
        self.conn.commit()
        return version

    def current_portfolio(self) -> dict | None:
        r = self.conn.execute(
            "SELECT * FROM portfolio_state ORDER BY version DESC LIMIT 1"
        ).fetchone()
        if not r:
            return None
        d = dict(r)
        d["state"] = json.loads(d.pop("state_json"))
        return d

    # --------------------------------------------------------- extraction
    def get_extraction(self, obj_key: str, h: str, extractor: str) -> dict | None:
        r = self.conn.execute(
            "SELECT result_json FROM extraction WHERE obj_key=? AND hash=? AND extractor=?",
            (obj_key, h, extractor),
        ).fetchone()
        return json.loads(r["result_json"]) if r else None

    def put_extraction(self, obj_key: str, h: str, extractor: str,
                       result: dict, model: str) -> None:
        self.conn.execute(
            """INSERT OR REPLACE INTO extraction
               (obj_key,hash,extractor,result_json,model,created_at)
               VALUES (?,?,?,?,?,?)""",
            (obj_key, h, extractor, json.dumps(result, default=str), model, utcnow()),
        )
        self.conn.commit()

    def commit(self) -> None:
        self.conn.commit()
