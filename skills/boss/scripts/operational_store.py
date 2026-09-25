#!/usr/bin/env python3
"""Atomic per-repository claims and durable, fenced /boss actions."""
import argparse
import contextlib
import datetime as dt
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import subprocess
import sys
from urllib.parse import quote

UTC = dt.timezone.utc
IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
PHASE = re.compile(r"^[a-z][a-z0-9_-]{0,63}$")


def timestamp():
    return dt.datetime.now(UTC).isoformat().replace("+00:00", "Z")


def valid_id(value, label):
    if not isinstance(value, str) or not IDENTIFIER.fullmatch(value):
        raise ValueError(f"invalid {label}")
    return value


def canonical_repo(path):
    path = Path(path)
    if not path.is_absolute() or not path.is_dir():
        raise ValueError("repo must be an existing absolute directory")
    result = subprocess.run(["git", "-C", str(path), "rev-parse", "--show-toplevel"], capture_output=True, text=True)
    if result.returncode or Path(result.stdout.strip()).resolve() != path.resolve():
        raise ValueError("repo must be a Git root")
    result = subprocess.run(["git", "-C", str(path), "remote", "get-url", "origin"], capture_output=True, text=True)
    if result.returncode:
        raise ValueError("origin remote is required")
    match = re.fullmatch(r"(?:git@github\.com:|https://github\.com/)([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+?)(?:\.git)?/?", result.stdout.strip())
    if not match:
        raise ValueError("origin must be a github.com owner/repository remote")
    return match.group(1).lower()


def default_db():
    root = Path(os.environ.get("AGENTS_ARTIFACTS_ROOT", str(Path.home() / "agents-artifacts")))
    return root / "boss" / "operations.sqlite3"


def connect(path, write=False):
    path = Path(path)
    if write:
        path.parent.mkdir(parents=True, exist_ok=True)
    elif not path.is_file():
        raise ValueError("operational store does not exist")
    if write:
        con = sqlite3.connect(str(path), timeout=10, isolation_level=None)
    else:
        uri = "file:" + quote(str(path.resolve()), safe="/") + "?mode=ro"
        con = sqlite3.connect(uri, uri=True, timeout=10, isolation_level=None)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA foreign_keys=ON")
    con.execute("PRAGMA busy_timeout=10000")
    if write:
        con.executescript("""
        CREATE TABLE IF NOT EXISTS generations (
          repo TEXT NOT NULL, issue INTEGER NOT NULL, phase TEXT NOT NULL,
          generation INTEGER NOT NULL, PRIMARY KEY(repo, issue, phase)
        );
        CREATE TABLE IF NOT EXISTS claims (
          repo TEXT NOT NULL, issue INTEGER NOT NULL, phase TEXT NOT NULL,
          generation INTEGER NOT NULL, owner TEXT NOT NULL, status TEXT NOT NULL,
          acquired_at TEXT NOT NULL, released_at TEXT,
          PRIMARY KEY(repo, issue, phase, generation)
        );
        CREATE UNIQUE INDEX IF NOT EXISTS one_active_claim
          ON claims(repo, issue, phase) WHERE status='active';
        CREATE TABLE IF NOT EXISTS actions (
          action_id TEXT PRIMARY KEY, repo TEXT NOT NULL, issue INTEGER NOT NULL,
          phase TEXT NOT NULL, verb TEXT NOT NULL, attempt INTEGER NOT NULL,
          generation INTEGER NOT NULL,
          owner TEXT NOT NULL, status TEXT NOT NULL, reserved_at TEXT NOT NULL,
          task_id TEXT, acknowledged_at TEXT, verified_at TEXT, last_scan_at TEXT,
          last_effect TEXT
        );
        CREATE TABLE IF NOT EXISTS events (
          sequence INTEGER PRIMARY KEY AUTOINCREMENT, event_id TEXT NOT NULL UNIQUE,
          repo TEXT NOT NULL, issue INTEGER, phase TEXT, generation INTEGER,
          action_id TEXT, kind TEXT NOT NULL, at TEXT NOT NULL, details TEXT NOT NULL
        );
        CREATE TRIGGER IF NOT EXISTS events_no_update BEFORE UPDATE ON events
          BEGIN SELECT RAISE(ABORT, 'event history is append-only'); END;
        CREATE TRIGGER IF NOT EXISTS events_no_delete BEFORE DELETE ON events
          BEGIN SELECT RAISE(ABORT, 'event history is append-only'); END;
        """)
    return con


def event(con, repo, issue, phase, generation, action_id, kind, details):
    con.execute("INSERT INTO events(event_id,repo,issue,phase,generation,action_id,kind,at,details) VALUES(?,?,?,?,?,?,?,?,?)",
                (hashlib.sha256(f"{timestamp()}:{os.getpid()}:{kind}:{action_id}".encode()).hexdigest(), repo, issue, phase, generation, action_id, kind, timestamp(), json.dumps(details, sort_keys=True)))


def begin(con):
    con.execute("BEGIN IMMEDIATE")


@contextlib.contextmanager
def action_guard(db_path, repo, issue, phase, verb, nonblocking=False):
    key = hashlib.sha256(f"{repo}:{issue}:{phase}:{verb}".encode()).hexdigest()
    lock_path = Path(db_path).with_name(f".action-{key}.lock")
    handle = open(lock_path, "a+")
    try:
        flags = fcntl.LOCK_EX | (fcntl.LOCK_NB if nonblocking else 0)
        try:
            fcntl.flock(handle, flags)
        except BlockingIOError:
            raise ValueError("action dispatch is still in progress; cannot abandon")
        yield handle
    finally:
        try:
            fcntl.flock(handle, fcntl.LOCK_UN)
        finally:
            handle.close()


def claim(con, repo, issue, phase, owner):
    begin(con)
    try:
        row = con.execute("SELECT generation,owner FROM claims WHERE repo=? AND issue=? AND phase=? AND status='active'", (repo, issue, phase)).fetchone()
        if row:
            if row["owner"] != owner:
                raise ValueError(f"claim held by another owner at generation {row['generation']}")
            con.commit()
            return {"repo": repo, "issue": issue, "phase": phase, "generation": row["generation"], "owner": owner, "status": "active", "reused": True}
        old = con.execute("SELECT generation FROM generations WHERE repo=? AND issue=? AND phase=?", (repo, issue, phase)).fetchone()
        generation = (old["generation"] if old else 0) + 1
        con.execute("INSERT INTO generations VALUES(?,?,?,?) ON CONFLICT(repo,issue,phase) DO UPDATE SET generation=excluded.generation", (repo, issue, phase, generation))
        con.execute("INSERT INTO claims VALUES(?,?,?,?,?,'active',?,NULL)", (repo, issue, phase, generation, owner, timestamp()))
        event(con, repo, issue, phase, generation, None, "claim_acquired", {"owner": owner})
        con.commit()
        return {"repo": repo, "issue": issue, "phase": phase, "generation": generation, "owner": owner, "status": "active", "reused": False}
    except Exception:
        con.rollback()
        raise


def release(con, repo, issue, phase, owner, generation):
    begin(con)
    try:
        pending = con.execute("SELECT action_id FROM actions WHERE repo=? AND issue=? AND phase=? AND generation=? AND status IN ('reserved','dispatching','attempt_finished','acknowledged')", (repo, issue, phase, generation)).fetchone()
        if pending:
            raise ValueError(f"claim has an unverified action: {pending['action_id']}")
        changed = con.execute("UPDATE claims SET status='released',released_at=? WHERE repo=? AND issue=? AND phase=? AND owner=? AND generation=? AND status='active'", (timestamp(), repo, issue, phase, owner, generation)).rowcount
        if not changed:
            raise ValueError("stale generation or claim is not active")
        event(con, repo, issue, phase, generation, None, "claim_released", {"owner": owner})
        con.commit()
    except Exception:
        con.rollback()
        raise


def action_id(repo, issue, phase, verb, attempt):
    return hashlib.sha256(f"{repo}:{issue}:{phase}:{verb}:{attempt}".encode()).hexdigest()


def reserve(con, repo, issue, phase, owner, generation, verb):
    begin(con)
    try:
        active = con.execute("SELECT 1 FROM claims WHERE repo=? AND issue=? AND phase=? AND owner=? AND generation=? AND status='active'", (repo, issue, phase, owner, generation)).fetchone()
        if not active:
            raise ValueError("stale generation or claim is not active")
        previous = con.execute("SELECT * FROM actions WHERE repo=? AND issue=? AND phase=? AND verb=? ORDER BY attempt DESC LIMIT 1", (repo, issue, phase, verb)).fetchone()
        if previous and previous["status"] != "abandoned":
            con.commit()
            return dict(previous), False
        attempt = previous["attempt"] + 1 if previous else 1
        aid = action_id(repo, issue, phase, verb, attempt)
        con.execute("INSERT INTO actions(action_id,repo,issue,phase,verb,attempt,generation,owner,status,reserved_at) VALUES(?,?,?,?,?,?,?,?, 'reserved',?)", (aid, repo, issue, phase, verb, attempt, generation, owner, timestamp()))
        event(con, repo, issue, phase, generation, aid, "action_reserved", {"verb": verb, "attempt": attempt, "owner": owner})
        row = con.execute("SELECT * FROM actions WHERE action_id=?", (aid,)).fetchone()
        con.commit()
        return dict(row), True
    except Exception:
        con.rollback()
        raise


def start_action(con, repo, issue, phase, owner, generation, aid):
    begin(con)
    try:
        active = con.execute("SELECT 1 FROM claims WHERE repo=? AND issue=? AND phase=? AND owner=? AND generation=? AND status='active'", (repo, issue, phase, owner, generation)).fetchone()
        if not active:
            raise ValueError("stale generation or owner: action start rejected")
        changed = con.execute("UPDATE actions SET status='dispatching' WHERE action_id=? AND repo=? AND issue=? AND phase=? AND owner=? AND generation=? AND status='reserved'", (aid, repo, issue, phase, owner, generation)).rowcount
        if not changed:
            raise ValueError("action start compare-and-set failed")
        event(con, repo, issue, phase, generation, aid, "action_dispatch_started", {"owner": owner})
        con.commit()
    except Exception:
        con.rollback()
        raise


def finish_action(con, repo, issue, phase, owner, generation, aid, evidence):
    if not isinstance(evidence, str) or len(evidence.strip()) < 10 or len(evidence.strip()) > 500:
        raise ValueError("completion evidence must be 10-500 characters")
    begin(con)
    try:
        active = con.execute("SELECT 1 FROM claims WHERE repo=? AND issue=? AND phase=? AND owner=? AND generation=? AND status='active'", (repo, issue, phase, owner, generation)).fetchone()
        if not active:
            raise ValueError("stale generation or owner: action finish rejected")
        changed = con.execute("UPDATE actions SET status='attempt_finished' WHERE action_id=? AND repo=? AND issue=? AND phase=? AND owner=? AND generation=? AND status='dispatching'", (aid, repo, issue, phase, owner, generation)).rowcount
        if not changed:
            raise ValueError("action finish compare-and-set failed")
        event(con, repo, issue, phase, generation, aid, "action_attempt_finished", {"evidence": evidence.strip(), "owner": owner})
        con.commit()
    except Exception:
        con.rollback()
        raise


def abandon(con, db_path, repo, issue, phase, owner, generation, aid, inventory, evidence):
    if not isinstance(evidence, str) or len(evidence.strip()) < 10 or len(evidence.strip()) > 500:
        raise ValueError("abandonment evidence must be 10-500 characters")
    with action_guard(db_path, repo, issue, phase, "launch", nonblocking=True):
        as_of, tasks = load_inventory(inventory)
        if aid in tasks:
            raise ValueError("cannot abandon: live inventory contains this action ID")
        begin(con)
        try:
            active = con.execute("SELECT 1 FROM claims WHERE repo=? AND issue=? AND phase=? AND owner=? AND generation=? AND status='active'", (repo, issue, phase, owner, generation)).fetchone()
            if not active:
                raise ValueError("stale generation: abandonment rejected")
            row = con.execute("SELECT reserved_at FROM actions WHERE action_id=? AND repo=? AND issue=? AND phase=? AND owner=? AND generation=? AND status IN ('reserved','attempt_finished')", (aid, repo, issue, phase, owner, generation)).fetchone()
            if not row:
                raise ValueError("action is in flight or abandonment compare-and-set failed; finish the launch attempt before abandonment")
            reserved_at = dt.datetime.fromisoformat(row["reserved_at"].replace("Z", "+00:00"))
            observed_at = dt.datetime.fromisoformat(as_of.replace("Z", "+00:00"))
            if observed_at <= reserved_at:
                raise ValueError("abandonment inventory must be newer than the action reservation")
            con.execute("UPDATE actions SET status='abandoned',last_effect='absent_at_abandonment' WHERE action_id=?", (aid,))
            event(con, repo, issue, phase, generation, aid, "action_abandoned", {"evidence": evidence.strip(), "inventory_as_of": as_of})
            con.commit()
        except Exception:
            con.rollback()
            raise


def acknowledge(con, repo, issue, phase, owner, generation, aid, task_id):
    task_id = valid_id(task_id, "task ID")
    begin(con)
    try:
        active = con.execute("SELECT 1 FROM claims WHERE repo=? AND issue=? AND phase=? AND owner=? AND generation=? AND status='active'", (repo, issue, phase, owner, generation)).fetchone()
        if not active:
            raise ValueError("stale generation or owner: acknowledgment rejected")
        changed = con.execute("UPDATE actions SET status='acknowledged',task_id=?,acknowledged_at=? WHERE action_id=? AND repo=? AND issue=? AND phase=? AND owner=? AND generation=? AND status='dispatching'", (task_id, timestamp(), aid, repo, issue, phase, owner, generation)).rowcount
        if not changed:
            existing = con.execute("SELECT status,task_id FROM actions WHERE action_id=? AND repo=? AND issue=? AND phase=? AND owner=? AND generation=?", (aid, repo, issue, phase, owner, generation)).fetchone()
            if not existing or existing["status"] not in ("acknowledged", "effect_verified") or existing["task_id"] != task_id:
                raise ValueError("acknowledgment compare-and-set failed")
        else:
            event(con, repo, issue, phase, generation, aid, "action_acknowledged", {"task_id": task_id})
        con.commit()
    except Exception:
        con.rollback()
        raise


def load_inventory(path):
    data = json.loads(Path(path).read_text())
    if not isinstance(data, dict) or not isinstance(data.get("tasks"), list):
        raise ValueError("invalid task inventory")
    as_of = data.get("as_of")
    if not isinstance(as_of, str):
        raise ValueError("invalid task inventory timestamp")
    try:
        observed_at = dt.datetime.fromisoformat(as_of.replace("Z", "+00:00"))
    except ValueError:
        raise ValueError("invalid task inventory timestamp")
    if observed_at.tzinfo is None:
        raise ValueError("task inventory timestamp must include a timezone")
    age = dt.datetime.now(UTC) - observed_at.astimezone(UTC)
    if age > dt.timedelta(minutes=15) or age < dt.timedelta(minutes=-1):
        raise ValueError("task inventory is stale or from the future")
    tasks = {}
    for task in data["tasks"]:
        if not isinstance(task, dict) or not isinstance(task.get("id"), str) or not IDENTIFIER.fullmatch(task["id"]):
            raise ValueError("invalid task inventory entry")
        if task.get("status") not in ("queued", "running", "completed", "interrupted", "blocked"):
            raise ValueError("invalid task inventory status")
        aid = task.get("action_id")
        if aid is not None and (not isinstance(aid, str) or not re.fullmatch(r"[a-f0-9]{64}", aid)):
            raise ValueError("invalid task action ID")
        if aid in tasks:
            raise ValueError("duplicate task action ID")
        if aid:
            tasks[aid] = task
    return as_of, tasks


def scan(con, repo, inventory):
    as_of, tasks = load_inventory(inventory)
    begin(con)
    changes = []
    try:
        rows = con.execute("SELECT * FROM actions WHERE repo=? AND status!='effect_verified' ORDER BY reserved_at", (repo,)).fetchall()
        for row in rows:
            task = tasks.get(row["action_id"])
            current = con.execute("SELECT generation FROM generations WHERE repo=? AND issue=? AND phase=?", (repo, row["issue"], row["phase"])).fetchone()
            if current and current["generation"] != row["generation"]:
                con.execute("UPDATE actions SET last_scan_at=?,last_effect='stale_generation' WHERE action_id=?", (as_of, row["action_id"]))
                event(con, repo, row["issue"], row["phase"], row["generation"], row["action_id"], "action_stale_generation", {"current_generation": current["generation"]})
                changes.append({"action_id": row["action_id"], "status": "action_stale_generation"})
                continue
            effect = "present" if task and (not row["task_id"] or task["id"] == row["task_id"]) else "missing"
            if row["status"] in ("reserved", "dispatching", "attempt_finished") and task:
                # Recover a process crash after task creation and before acknowledgment.
                con.execute("UPDATE actions SET status='acknowledged',task_id=?,acknowledged_at=? WHERE action_id=? AND status IN ('reserved','dispatching','attempt_finished')", (task["id"], as_of, row["action_id"]))
                event(con, repo, row["issue"], row["phase"], row["generation"], row["action_id"], "action_acknowledged_recovered", {"task_id": task["id"]})
                row = dict(row)
                row["task_id"] = task["id"]
                row["status"] = "acknowledged"
            if row["status"] in ("acknowledged", "reserved", "dispatching", "attempt_finished"):
                if effect == "present":
                    con.execute("UPDATE actions SET status='effect_verified',verified_at=?,last_scan_at=?,last_effect='present' WHERE action_id=?", (as_of, as_of, row["action_id"]))
                    event(con, repo, row["issue"], row["phase"], row["generation"], row["action_id"], "action_effect_verified", {"task_id": row["task_id"]})
                    status = "effect_verified"
                else:
                    con.execute("UPDATE actions SET last_scan_at=?,last_effect='missing' WHERE action_id=?", (as_of, row["action_id"]))
                    kind = "action_unacknowledged" if row["status"] in ("reserved", "dispatching") else "action_effect_missing"
                    event(con, repo, row["issue"], row["phase"], row["generation"], row["action_id"], kind, {"status": row["status"]})
                    status = kind
                changes.append({"action_id": row["action_id"], "status": status})
        con.commit()
        return {"repo": repo, "as_of": as_of, "actions": changes}
    except Exception:
        con.rollback()
        raise


def rebuild(con, repo, inventory):
    # Validate the source inventory before clearing any derived projections.
    load_inventory(inventory)
    begin(con)
    try:
        con.execute("DELETE FROM actions WHERE repo=?", (repo,))
        con.execute("DELETE FROM claims WHERE repo=?", (repo,))
        con.execute("DELETE FROM generations WHERE repo=?", (repo,))
        for row in con.execute("SELECT * FROM events WHERE repo=? ORDER BY sequence", (repo,)).fetchall():
            details = json.loads(row["details"])
            if row["kind"] == "claim_acquired":
                current = con.execute("SELECT generation FROM generations WHERE repo=? AND issue=? AND phase=?", (repo, row["issue"], row["phase"])).fetchone()
                generation = max(current["generation"] if current else 0, row["generation"])
                con.execute("INSERT INTO generations VALUES(?,?,?,?) ON CONFLICT(repo,issue,phase) DO UPDATE SET generation=excluded.generation", (repo, row["issue"], row["phase"], generation))
                con.execute("INSERT INTO claims VALUES(?,?,?,?,?,'active',?,NULL)", (repo, row["issue"], row["phase"], row["generation"], details["owner"], row["at"]))
            elif row["kind"] == "claim_released":
                con.execute("UPDATE claims SET status='released',released_at=? WHERE repo=? AND issue=? AND phase=? AND generation=?", (row["at"], repo, row["issue"], row["phase"], row["generation"]))
            elif row["kind"] == "action_reserved":
                con.execute("INSERT INTO actions(action_id,repo,issue,phase,verb,attempt,generation,owner,status,reserved_at) VALUES(?,?,?,?,?,?,?,?,'reserved',?)", (row["action_id"], repo, row["issue"], row["phase"], details["verb"], details["attempt"], row["generation"], details["owner"], row["at"]))
            elif row["kind"] == "action_dispatch_started":
                con.execute("UPDATE actions SET status='dispatching' WHERE action_id=?", (row["action_id"],))
            elif row["kind"] == "action_attempt_finished":
                con.execute("UPDATE actions SET status='attempt_finished' WHERE action_id=?", (row["action_id"],))
            elif row["kind"] in ("action_acknowledged", "action_acknowledged_recovered"):
                con.execute("UPDATE actions SET status='acknowledged',task_id=?,acknowledged_at=? WHERE action_id=?", (details["task_id"], row["at"], row["action_id"]))
            elif row["kind"] == "action_abandoned":
                con.execute("UPDATE actions SET status='abandoned',last_effect='absent_at_abandonment' WHERE action_id=?", (row["action_id"],))
            elif row["kind"] == "action_effect_verified":
                con.execute("UPDATE actions SET status='effect_verified',verified_at=?,last_scan_at=?,last_effect='present' WHERE action_id=?", (row["at"], row["at"], row["action_id"]))
            elif row["kind"] in ("action_unacknowledged", "action_effect_missing", "action_stale_generation"):
                effect = "stale_generation" if row["kind"] == "action_stale_generation" else "missing"
                con.execute("UPDATE actions SET last_scan_at=?,last_effect=? WHERE action_id=?", (row["at"], effect, row["action_id"]))
        con.commit()
    except Exception:
        con.rollback()
        raise
    return scan(con, repo, inventory)


def dispatch(con, db_path, repo, issue, phase, owner, generation, verb, adapter, context=None):
    executable = Path(adapter)
    if not executable.is_absolute() or not executable.is_file() or not os.access(executable, os.X_OK):
        raise ValueError("adapter must be an absolute executable file")
    with action_guard(db_path, repo, issue, phase, verb) as guard:
        row, created = reserve(con, repo, issue, phase, owner, generation, verb)
        if not created:
            # The adapter may have created a session before its caller crashed. Never retry it.
            raise ValueError(f"action already reserved ({row['status']}); reconcile action {row['action_id']} before any retry")
        start_action(con, repo, issue, phase, owner, generation, row["action_id"])
        request = {"repo": repo, "issue": issue, "phase": phase, "generation": generation, "owner": owner, "verb": verb, "action_id": row["action_id"]}
        if context:
            request.update(context)
        result = subprocess.run([str(executable)], input=json.dumps(request), capture_output=True, text=True, timeout=120, pass_fds=(guard.fileno(),))
        if result.returncode:
            finish_action(con, repo, issue, phase, owner, generation, row["action_id"], f"Adapter returned exit code {result.returncode}; call ended without a task ID")
            raise ValueError(f"adapter failed; reservation {row['action_id']} remains for reconciliation: {result.stderr.strip()[:300]}")
        try:
            answer = json.loads(result.stdout)
            task_id = valid_id(answer["task_id"], "task ID")
        except (KeyError, TypeError, ValueError, json.JSONDecodeError):
            finish_action(con, repo, issue, phase, owner, generation, row["action_id"], "Adapter call returned without a valid task ID; reconcile the live inventory")
            raise ValueError(f"adapter returned no valid task_id; reservation {row['action_id']} remains for reconciliation")
        acknowledge(con, repo, issue, phase, owner, generation, row["action_id"], task_id)
        return {"action_id": row["action_id"], "task_id": task_id, "status": "acknowledged"}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=default_db())
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("claim", "release", "reserve", "start", "finish", "dispatch", "ack", "abandon", "scan", "rebuild", "history", "status"):
        cmd = sub.add_parser(name)
        cmd.add_argument("--repo", required=True)
    for name in ("claim", "release", "reserve", "start", "finish", "dispatch", "ack", "abandon"):
        p = sub.choices[name]
        p.add_argument("--issue", type=int, required=True)
        p.add_argument("--phase", required=True)
    for name in ("claim", "release", "reserve", "start", "finish", "dispatch", "ack", "abandon"):
        sub.choices[name].add_argument("--owner", required=True)
    sub.choices["release"].add_argument("--generation", type=int, required=True)
    sub.choices["reserve"].add_argument("--generation", type=int, required=True)
    sub.choices["reserve"].add_argument("--verb", required=True)
    for name in ("start", "finish"):
        sub.choices[name].add_argument("--generation", type=int, required=True)
        sub.choices[name].add_argument("--action-id", required=True)
    sub.choices["finish"].add_argument("--evidence", required=True)
    sub.choices["dispatch"].add_argument("--generation", type=int, required=True)
    sub.choices["dispatch"].add_argument("--verb", required=True)
    sub.choices["dispatch"].add_argument("--adapter", required=True)
    sub.choices["dispatch"].add_argument("--kind", choices=("feature", "testability"))
    sub.choices["dispatch"].add_argument("--master")
    sub.choices["ack"].add_argument("--generation", type=int, required=True)
    sub.choices["ack"].add_argument("--action-id", required=True)
    sub.choices["ack"].add_argument("--task-id", required=True)
    sub.choices["abandon"].add_argument("--generation", type=int, required=True)
    sub.choices["abandon"].add_argument("--action-id", required=True)
    sub.choices["abandon"].add_argument("--inventory", type=Path, required=True)
    sub.choices["abandon"].add_argument("--evidence", required=True)
    sub.choices["scan"].add_argument("--inventory", type=Path, required=True)
    sub.choices["rebuild"].add_argument("--inventory", type=Path, required=True)
    sub.choices["history"].add_argument("--issue", type=int)
    args = parser.parse_args()
    repo = canonical_repo(args.repo)
    write = args.command not in ("history", "status")
    con = connect(args.db, write=write)
    try:
        if args.command in ("claim", "release", "reserve", "start", "finish", "dispatch", "ack", "abandon"):
            if isinstance(args.issue, bool) or args.issue <= 0:
                raise ValueError("issue numbers must be positive integers")
            if not isinstance(args.phase, str) or not PHASE.fullmatch(args.phase):
                raise ValueError("invalid phase")
        if args.command in ("claim", "release", "reserve", "start", "finish", "dispatch"):
            valid_id(args.owner, "owner")
        if args.command == "claim":
            result = claim(con, repo, args.issue, args.phase, args.owner)
        elif args.command == "release":
            if args.generation <= 0:
                raise ValueError("generation must be positive")
            release(con, repo, args.issue, args.phase, args.owner, args.generation)
            result = {"released": True, "generation": args.generation}
        elif args.command == "reserve":
            if args.generation <= 0:
                raise ValueError("generation must be positive")
            valid_id(args.verb, "verb")
            row, created = reserve(con, repo, args.issue, args.phase, args.owner, args.generation, args.verb)
            result = {"action": row, "created": created}
        elif args.command == "start":
            if args.generation <= 0:
                raise ValueError("generation must be positive")
            start_action(con, repo, args.issue, args.phase, args.owner, args.generation, args.action_id)
            result = {"started": True, "action_id": args.action_id}
        elif args.command == "finish":
            if args.generation <= 0:
                raise ValueError("generation must be positive")
            finish_action(con, repo, args.issue, args.phase, args.owner, args.generation, args.action_id, args.evidence)
            result = {"finished": True, "action_id": args.action_id}
        elif args.command == "dispatch":
            if args.generation <= 0:
                raise ValueError("generation must be positive")
            valid_id(args.verb, "verb")
            context = {key: value for key, value in (("kind", args.kind), ("master", args.master)) if value is not None}
            if args.master:
                valid_id(args.master, "master")
            result = dispatch(con, args.db, repo, args.issue, args.phase, args.owner, args.generation, args.verb, args.adapter, context)
        elif args.command == "ack":
            if args.generation <= 0:
                raise ValueError("generation must be positive")
            valid_id(args.owner, "owner")
            acknowledge(con, repo, args.issue, args.phase, args.owner, args.generation, args.action_id, args.task_id)
            result = {"acknowledged": True, "action_id": args.action_id}
        elif args.command == "abandon":
            if args.generation <= 0:
                raise ValueError("generation must be positive")
            abandon(con, args.db, repo, args.issue, args.phase, args.owner, args.generation, args.action_id, args.inventory, args.evidence)
            result = {"abandoned": True, "action_id": args.action_id}
        elif args.command == "scan":
            result = scan(con, repo, args.inventory)
        elif args.command == "rebuild":
            result = rebuild(con, repo, args.inventory)
        elif args.command == "history":
            query = "SELECT * FROM events WHERE repo=?"
            params = [repo]
            if args.issue:
                query += " AND issue=?"
                params.append(args.issue)
            query += " ORDER BY sequence"
            result = [dict(row) for row in con.execute(query, params)]
        else:
            result = {"repo": repo, "active_claims": [dict(row) for row in con.execute("SELECT issue,phase,generation,owner,acquired_at FROM claims WHERE repo=? AND status='active' ORDER BY issue,phase", (repo,))], "actions": [dict(row) for row in con.execute("SELECT action_id,issue,phase,verb,attempt,generation,owner,status,task_id,reserved_at,acknowledged_at,verified_at,last_scan_at,last_effect FROM actions WHERE repo=? ORDER BY reserved_at", (repo,))]}
        print(json.dumps(result, sort_keys=True))
    finally:
        con.close()


if __name__ == "__main__":
    try:
        main()
    except (ValueError, OSError, sqlite3.Error, subprocess.TimeoutExpired, json.JSONDecodeError) as exc:
        print(f"boss store: {exc}", file=sys.stderr)
        sys.exit(2)
