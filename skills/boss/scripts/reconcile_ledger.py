#!/usr/bin/env python3
"""Deterministic, fail-closed action outbox for an audited ownership ledger.

The caller must first refresh the ledger from GitHub and supply a fresh Codex
task inventory. This program decides *what* must happen. A host worker executes
the returned action with its existing tools, then acknowledges the same ID.
It never starts a task, merges a PR, or changes a GitHub issue itself.
"""
import argparse
import contextlib
from datetime import datetime, timedelta, timezone
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import sys
import tempfile

UTC = timezone.utc
SHA = re.compile(r"^[0-9a-f]{40}$")
TASK_ID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
STATES = {"queued", "running", "completed", "interrupted", "blocked"}


class ReconcileError(ValueError):
    pass


def timestamp(value):
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            raise ValueError("timezone missing")
        return parsed.astimezone(UTC)
    except (AttributeError, ValueError) as exc:
        raise ReconcileError("invalid or missing observation timestamp") from exc


def fresh(value, now, max_age=timedelta(minutes=20)):
    age = now - timestamp(value)
    if age < -timedelta(minutes=2) or age > max_age:
        raise ReconcileError("stale observation; refresh GitHub and task inventory first")


def load_inputs(ledger_path, tasks_path, now):
    ledger = json.loads(ledger_path.read_text())
    tasks = json.loads(tasks_path.read_text())
    if ledger.get("schema_version") != 1 or not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", ledger.get("repository", "")):
        raise ReconcileError("invalid ownership ledger")
    fresh(ledger.get("last_checked_utc"), now)
    if not isinstance(tasks.get("tasks"), list):
        raise ReconcileError("task inventory must contain tasks")
    fresh(tasks.get("as_of"), now)
    task_map = {}
    for task in tasks["tasks"]:
        if not isinstance(task, dict) or not TASK_ID.fullmatch(str(task.get("id", ""))) or task.get("status") not in STATES or task["id"] in task_map:
            raise ReconcileError("invalid or duplicate task observation")
        task_map[task["id"]] = task
    rows = ledger.get("objectives")
    if not isinstance(rows, list) or len({row.get("id") for row in rows if isinstance(row, dict)}) != len(rows):
        raise ReconcileError("invalid or duplicate objective")
    for row in rows:
        if not isinstance(row, dict):
            raise ReconcileError("invalid objective")
        if row.get("work_item"):
            fresh(row.get("last_checked_utc"), now)
    return ledger, task_map


def complete(row):
    # A phase on an OPEN parent epic is complete only when its declared PRs are
    # all observed MERGED. Free-form status/next_gate text is never evidence.
    phase = bool(re.fullmatch(r"#[1-9][0-9]* PR[1-9][0-9]*", str(row.get("id", ""))))
    if row.get("completion_source") == "pull_request" or (row.get("completion_source") is None and phase):
        prs = row.get("pull_requests") or []
        states = row.get("pull_request_states") or {}
        return bool(prs) and all(states.get(str(number)) == "MERGED" for number in prs)
    return row.get("github_state") == "CLOSED"


def action(repo, row, verb, *, task_id=None, pr=None, head=None, reason=None):
    identity = {"repo": repo, "objective": row["id"], "issue": row.get("issue_number"),
                "verb": verb, "task_id": task_id, "pr": pr, "head": head,
                "linked_prs": list(row.get("pull_requests") or [])}
    key = json.dumps(identity, sort_keys=True, separators=(",", ":"))
    return {"id": hashlib.sha256(key.encode()).hexdigest()[:24], **identity,
            "reason": reason or verb}


def decide(ledger, tasks):
    repo = ledger["repository"]
    rows = {row["id"]: row for row in ledger["objectives"]}
    edges = ledger.get("dependency_edges", [])
    gates = {gate["id"]: gate for gate in ledger.get("dependency_gates", [])}
    actions, waiting = [], []
    for row in ledger["objectives"]:
        if not row.get("work_item") or complete(row):
            continue
        rid = row["id"]
        hold = row.get("dispatch_hold")
        if hold is not None:
            if not isinstance(hold, dict) or not isinstance(hold.get("reason"), str) or not hold["reason"]:
                raise ReconcileError(f"{rid}: invalid dispatch hold")
            until = hold.get("until")
            if until not in rows or not complete(rows[until]):
                waiting.append({"objective": rid, "reason": "hold", "until": until})
                continue
        unmet = []
        for edge in edges:
            if edge.get("after") != rid or edge.get("phase") != "start":
                continue
            before = edge.get("before")
            if before in rows:
                satisfied = complete(rows[before])
            elif before in gates:
                satisfied = gates[before].get("satisfied") is True
            else:
                raise ReconcileError(f"{rid}: unknown dependency {before}")
            if not satisfied:
                unmet.append(before)
        if unmet:
            waiting.append({"objective": rid, "reason": "dependency", "until": unmet})
            continue
        if row.get("human_gate") is True:
            waiting.append({"objective": rid, "reason": "human_gate"})
            continue
        task_id = row.get("coding_task_id") or row.get("implementation_thread_id") or row.get("active_task_uuid")
        prs = row.get("pull_requests") or []
        states = row.get("pull_request_states") or {}
        open_prs = [pr for pr in prs if states.get(str(pr)) == "OPEN"]
        if len(open_prs) > 1:
            raise ReconcileError(f"{rid}: multiple open PRs need explicit phase ownership")
        if open_prs:
            pr = open_prs[0]
            obs = (row.get("pr_observations") or {}).get(str(pr))
            if not isinstance(obs, dict) or not SHA.fullmatch(str(obs.get("head", ""))):
                raise ReconcileError(f"{rid}: current PR head missing")
            head = obs["head"]
            if obs.get("merge") == "DIRTY" or obs.get("failed_checks"):
                verb = "REPAIR_PR" if task_id and task_id in tasks else "RECOVER_OWNER"
                actions.append(action(repo, row, verb, task_id=task_id, pr=pr, head=head,
                                      reason="conflict or current-head CI failure"))
            elif obs.get("merge") == "CLEAN" and obs.get("check_count", 0) > 0 and obs.get("passed_count", 0) > 0 and not obs.get("pending_checks"):
                actions.append(action(repo, row, "VERIFY_MERGE", task_id=task_id, pr=pr, head=head,
                                      reason="review only: verify required exact-head contexts and independent current-head review; this action does not authorize merge"))
            else:
                waiting.append({"objective": rid, "reason": "pr_checks_pending_or_unknown", "pr": pr, "head": head})
            continue
        if prs and all(states.get(str(pr)) == "MERGED" for pr in prs):
            # Code is already merged. An open issue can still require rollout,
            # acceptance, or explicit closure; never launch duplicate coding.
            actions.append(action(repo, row, "RECONCILE_ISSUE", task_id=task_id,
                                  pr=prs[-1], reason="all linked PRs merged but issue remains open; verify acceptance and closure gates"))
            continue
        pending_client = row.get("pending_client_thread_id")
        if not task_id and pending_client is not None:
            if not isinstance(pending_client, str) or not pending_client.strip():
                raise ReconcileError(f"{rid}: invalid pending client thread ID")
            actions.append(action(repo, row, "RECOVER_OWNER",
                                  reason="Codex task setup is pending; resolve the client thread ID before any launch"))
            continue
        if task_id:
            task = tasks.get(task_id)
            if task is None:
                actions.append(action(repo, row, "RECOVER_OWNER", task_id=task_id,
                                      reason="task ID absent from fresh inventory; inspect before retry"))
            elif task["status"] in ("completed", "interrupted"):
                actions.append(action(repo, row, "RESUME_TASK", task_id=task_id,
                                      reason="coding turn ended with issue still open and no open PR"))
            elif task["status"] == "blocked":
                actions.append(action(repo, row, "RECOVER_OWNER", task_id=task_id,
                                      reason="task blocked; inspect exact blocker"))
            else:
                waiting.append({"objective": rid, "reason": "task_active", "task_id": task_id})
            continue
        if any(states.get(str(pr)) not in ("MERGED", "CLOSED") for pr in prs):
            raise ReconcileError(f"{rid}: incomplete PR observation")
        # owner_active is intentionally ignored: only a fresh task or PR counts.
        actions.append(action(repo, row, "LAUNCH_TASK", reason="ready with no task or PR"))
    return actions, waiting


@contextlib.contextmanager
def lock(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.with_suffix(path.suffix + ".lock").open("a+") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def atomic_write(path, data):
    fd, temp = tempfile.mkstemp(prefix=".boss-outbox-", dir=path.parent)
    try:
        with os.fdopen(fd, "w") as handle:
            json.dump(data, handle, sort_keys=True, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp, path)
    finally:
        if os.path.exists(temp):
            os.unlink(temp)


def sync_outbox(path, actions, instant, cycle_minutes=15):
    with lock(path):
        data = json.loads(path.read_text()) if path.exists() else {"version": 1, "actions": {}}
        if data.get("version") != 1 or not isinstance(data.get("actions"), dict):
            raise ReconcileError("invalid outbox")
        current = {item["id"] for item in actions}
        overdue = []
        for item in actions:
            record = data["actions"].setdefault(item["id"], {"action": item, "state": "pending", "first_seen": instant.isoformat()})
            if record["state"] in ("superseded", "resolved"):
                record.update(state="pending", first_seen=instant.isoformat())
            if record["state"] == "pending" and instant - timestamp(record["first_seen"]) > timedelta(minutes=cycle_minutes):
                overdue.append(item["id"])
            if record["state"] == "acknowledged" and instant - timestamp(record["acknowledged_at"]) > timedelta(minutes=cycle_minutes):
                # The host reported doing the work, but the next fresh inventory
                # still yields the same action. Do not silently treat that as done.
                overdue.append(item["id"])
        for aid, record in data["actions"].items():
            if aid not in current and record["state"] == "pending":
                record["state"] = "superseded"
                record["superseded_at"] = instant.isoformat()
            elif aid not in current and record["state"] == "acknowledged":
                record["state"] = "resolved"
                record["resolved_at"] = instant.isoformat()
        atomic_write(path, data)
    return overdue


def acknowledge(path, aid, evidence, instant):
    if not re.fullmatch(r"[0-9a-f]{24}", aid) or len(evidence.strip()) < 10 or len(evidence) > 500:
        raise ReconcileError("invalid action ID or evidence")
    with lock(path):
        data = json.loads(path.read_text())
        record = data.get("actions", {}).get(aid)
        if not record or record.get("state") not in ("pending", "acknowledged"):
            raise ReconcileError("action is absent or superseded; refresh first")
        if record["state"] == "acknowledged" and record.get("evidence") != evidence:
            raise ReconcileError("conflicting acknowledgment")
        if record["state"] == "acknowledged":
            # An idempotent retry cannot postpone an unchanged-action alert.
            return record
        record.update(state="acknowledged", evidence=evidence, acknowledged_at=instant.isoformat())
        atomic_write(path, data)
    return record


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ledger", type=Path)
    parser.add_argument("--tasks", type=Path)
    parser.add_argument("--outbox", type=Path)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("plan", help="read-only; decide actions from fresh observations")
    sub.add_parser("scan", help="write durable actions and fail if any remains unacknowledged after one cycle")
    ack = sub.add_parser("ack", help="CAS acknowledgment after performing a displayed action")
    ack.add_argument("--id", required=True)
    ack.add_argument("--evidence", required=True)
    args = parser.parse_args(argv)
    instant = datetime.now(UTC)
    if args.command == "ack":
        if not args.outbox:
            raise ReconcileError("--outbox is required")
        print(json.dumps(acknowledge(args.outbox, args.id, args.evidence, instant)))
        return 0
    if not args.ledger or not args.tasks:
        raise ReconcileError("--ledger and --tasks are required")
    ledger, tasks = load_inputs(args.ledger, args.tasks, instant)
    actions, waiting = decide(ledger, tasks)
    overdue = []
    if args.command == "scan":
        if not args.outbox:
            raise ReconcileError("--outbox is required for scan")
        overdue = sync_outbox(args.outbox, actions, instant)
    print(json.dumps({"actions": actions, "waiting": waiting, "overdue": overdue}, sort_keys=True))
    return 3 if overdue else 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"boss reconcile: {exc}", file=sys.stderr)
        sys.exit(2)
