"""
Entry points.

  python -m tower.run once      one cycle, then exit          (CI / cron)
  python -m tower.run daemon    poll forever                  (always-on)
  python -m tower.run export    write public/ from the db     (Pages)

The daemon is the thing that satisfies "the system itself needs to have
picked it up, with no manual re-import and no re-triggering a script by
hand". It runs as a long-lived GitHub Actions job, polls every 60 seconds,
and commits whenever state actually moves. The commit history is the proof:
public, timestamped, and written by a runner neither of us is touching.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from . import config
from .store import Store
from .pipeline import run_cycle
from .portfolio import build_portfolio


def log(*a):
    ts = datetime.now(timezone.utc).strftime("%H:%M:%S")
    print(f"{ts}Z", *a, flush=True)


# --------------------------------------------------------------------------
# Export: everything the dashboard needs as flat JSON, no backend required.
# A static dashboard cannot go down, cannot cold-start, and cannot lose the
# demo at 4:55 PM.
# --------------------------------------------------------------------------

def export(store: Store) -> dict:
    # Fold the write-ahead log back into the main database file before we
    # commit it. The committed .db is how state survives a runner restart, so
    # it has to be self-contained. If the baseline were ever lost, the next
    # crawl would report every object as newly ADDED and the change detection
    # would be worthless exactly when it matters.
    try:
        store.conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        store.conn.commit()
    except Exception as e:
        log(f"wal checkpoint failed (non-fatal): {e}")

    out = config.PUBLIC
    out.mkdir(parents=True, exist_ok=True)
    (out / "accounts").mkdir(exist_ok=True)

    pf = store.current_portfolio()
    portfolio = pf["state"] if pf else build_portfolio(store)

    (out / "portfolio.json").write_text(
        json.dumps(portfolio, indent=1, default=str), encoding="utf-8")

    for s in store.all_current_states():
        st = s["state"]
        st["_version"] = s["version"]
        st["_history"] = store.state_history(st["account_id"])[:20]
        st["_deltas"] = store.recent_deltas(40, st["account_id"])
        st["_evidence_count"] = len(store.evidence_for(st["account_id"]))
        (out / "accounts" / f"{st['account_id']}.json").write_text(
            json.dumps(st, indent=1, default=str), encoding="utf-8")

    meta = {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "accounts": [a["account_id"] for a in portfolio.get("accounts", [])],
        "last_sync": portfolio.get("system", {}).get("last_sync"),
        "syncs": portfolio.get("syncs", [])[:40],
    }
    (out / "meta.json").write_text(json.dumps(meta, indent=1, default=str),
                                   encoding="utf-8")

    src = config.WEB / "index.html"
    if src.exists():
        shutil.copy(src, out / "index.html")
    (out / ".nojekyll").write_text("", encoding="utf-8")
    log(f"exported {len(portfolio.get('accounts', []))} accounts to {out}")
    return portfolio


# --------------------------------------------------------------------------
# Git commit from inside the runner. This is the audit trail.
# --------------------------------------------------------------------------

def git_publish(message: str) -> bool:
    if os.environ.get("TOWER_NO_GIT"):
        return False
    try:
        subprocess.run(["git", "config", "user.name", "tower-sentinel"], check=False)
        subprocess.run(["git", "config", "user.email",
                        "tower-sentinel@users.noreply.github.com"], check=False)
        subprocess.run(["git", "add", "-A", "public", "data"], check=False)
        r = subprocess.run(["git", "diff", "--cached", "--quiet"])
        if r.returncode == 0:
            return False                      # nothing moved, stay quiet
        subprocess.run(["git", "commit", "-m", message], check=True)
        for attempt in range(4):
            p = subprocess.run(["git", "push"], capture_output=True, text=True)
            if p.returncode == 0:
                log(f"published: {message}")
                return True
            subprocess.run(["git", "pull", "--rebase", "--autostash"],
                           capture_output=True, text=True)
            time.sleep(2 + attempt * 2)
        log("push failed after retries")
        return False
    except Exception as e:
        log(f"git publish error: {e}")
        return False


def _commit_message(res: dict) -> str:
    r = res["report"]
    bits = []
    if r["added"]:
        bits.append(f"+{len(r['added'])} added")
    if r["modified"]:
        bits.append(f"~{len(r['modified'])} modified")
    if r["withdrawn"]:
        bits.append(f"-{len(r['withdrawn'])} WITHDRAWN")
    if r["restored"]:
        bits.append(f"^{len(r['restored'])} restored")
    head = ", ".join(bits) or "no source change"
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%SZ")
    detail = ""
    if r["withdrawn"]:
        detail = "\n\nWithdrawn from source:\n" + "\n".join(
            f"  {w['key']}" for w in r["withdrawn"])
    return (f"sync {res['sync_id']} @ {ts}: {head}; "
            f"{res['resynth']} accounts re-reasoned{detail}")


# --------------------------------------------------------------------------

def cmd_once(publish: bool = True, force: bool = False) -> dict:
    store = Store()
    res = run_cycle(store, trigger=os.environ.get("TOWER_TRIGGER", "manual"),
                    force_all=force, log=log)
    export(store)
    if publish and res["report"]["total_changes"] > 0 or force:
        git_publish(_commit_message(res))
    return res


def cmd_daemon() -> None:
    store = Store()
    started = time.time()
    interval = config.POLL_SECONDS
    n = 0
    log(f"TOWER daemon up. polling every {interval}s, "
        f"max run {config.MAX_RUN_SECONDS}s")
    # Always do a full pass on boot so a fresh runner rebuilds complete state.
    force_first = not store.all_current_states()
    while time.time() - started < config.MAX_RUN_SECONDS:
        n += 1
        try:
            os.environ["TOWER_TRIGGER"] = "scheduler"
            res = run_cycle(store, trigger="scheduler",
                            force_all=force_first, log=log)
            force_first = False
            export(store)
            if res["report"]["total_changes"] > 0 or n == 1:
                git_publish(_commit_message(res))
            else:
                log(f"tick {n}: no change")
        except Exception as e:
            log(f"tick {n} error: {e}")
        time.sleep(interval)
    log("daemon reached max run time, exiting cleanly for restart")


def main() -> None:
    cmd = sys.argv[1] if len(sys.argv) > 1 else "once"
    force = "--force" in sys.argv
    if cmd == "once":
        cmd_once(publish="--no-publish" not in sys.argv, force=force)
    elif cmd == "daemon":
        cmd_daemon()
    elif cmd == "export":
        export(Store())
    elif cmd == "status":
        s = Store()
        print(json.dumps({"syncs": s.last_syncs(10),
                          "accounts": len(s.all_current_states()),
                          "changes": s.recent_changes(20)}, indent=1, default=str))
    else:
        print(__doc__)
        sys.exit(1)


if __name__ == "__main__":
    main()
