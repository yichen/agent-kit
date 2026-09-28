#!/usr/bin/env python3
"""Deliver semantic LearnRise changes to the existing Codex monitoring hub."""
import argparse
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
from datetime import datetime, timedelta, timezone

UTC = timezone.utc
REPO = "yichen/LearnRise"
UUID = re.compile(r"[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}\Z")
SHA = re.compile(r"[0-9a-f]{40}\Z")
ACTION = re.compile(r"[0-9a-f]{20,64}\Z")
LABEL = re.compile(r"[A-Za-z0-9# _-]{1,80}\Z")
TOKEN = re.compile(r"[A-Z0-9_]{1,64}\Z")
GATE_ID = re.compile(r"[A-Za-z0-9:_-]{1,100}\Z")
ARTIFACTS = Path(os.environ.get("AGENTS_ARTIFACTS_ROOT", str(Path.home() / "agents-artifacts"))) / "learnrise-orchestrator"
MAX_BYTES = 4_000_000


class MonitorError(Exception):
    pass


def now_utc():
    return datetime.now(UTC)


def instant(value, name, now=None, max_age=None):
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            raise ValueError("no timezone")
        parsed = parsed.astimezone(UTC)
    except (AttributeError, ValueError) as exc:
        raise MonitorError(f"invalid {name} timestamp") from exc
    if now and max_age and (parsed > now + timedelta(minutes=2) or now - parsed > max_age):
        raise MonitorError(f"stale {name}")
    return parsed


def read_json(path, *, optional=False):
    if optional and not path.exists():
        return None
    if path.stat().st_size > MAX_BYTES:
        raise MonitorError(f"oversized input: {path.name}")
    data = json.loads(path.read_text())
    if not isinstance(data, dict):
        raise MonitorError(f"invalid input: {path.name}")
    return data


def atomic(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=".monitor-", dir=path.parent)
    try:
        with os.fdopen(fd, "w") as stream:
            json.dump(data, stream, sort_keys=True, separators=(",", ":"))
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def bounded_run(argv, timeout=120):
    try:
        # File-backed output keeps a noisy bridge/CLI from filling monitor RAM.
        with tempfile.TemporaryFile() as stdout, tempfile.TemporaryFile() as stderr:
            result = subprocess.run(argv, stdout=stdout, stderr=stderr, timeout=timeout,
                                    check=False,
                                    env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"})
            if stdout.tell() > MAX_BYTES or stderr.tell() > 64_000:
                raise MonitorError(f"oversized subprocess output: {Path(argv[0]).name}")
            stdout.seek(0)
            stderr.seek(0)
            return subprocess.CompletedProcess(argv, result.returncode, stdout.read().decode("utf-8"), stderr.read().decode("utf-8"))
    except subprocess.TimeoutExpired as exc:
        raise MonitorError(f"timeout: {Path(argv[0]).name}") from exc


def validate_hub(hub):
    if not isinstance(hub, str) or not UUID.fullmatch(hub):
        raise MonitorError("invalid hub UUID")


def validate_sources(ledger, outbox, supervisor, inventory, hub, now):
    validate_hub(hub)
    if ledger.get("schema_version") != 1 or ledger.get("repository") != REPO:
        raise MonitorError("invalid observed ledger repository/schema")
    instant(ledger.get("last_checked_utc"), "ledger", now, timedelta(minutes=35))
    rows = ledger.get("objectives")
    prs = ledger.get("open_pull_requests")
    if not isinstance(rows, list) or len(rows) > 2000 or not isinstance(prs, list) or len(prs) >= 1000:
        raise MonitorError("missing or truncated objective/PR inventory")
    if supervisor.get("schema_version") != 1 or supervisor.get("repository") != REPO:
        raise MonitorError("invalid supervisor repository/schema")
    instant(supervisor.get("last_scan_utc"), "supervisor", now, timedelta(minutes=35))
    if outbox.get("version") != 1 or not isinstance(outbox.get("actions"), dict) or len(outbox["actions"]) > 10000:
        raise MonitorError("invalid outbox")
    if not isinstance(supervisor.get("actions"), dict) or len(supervisor["actions"]) > 10000:
        raise MonitorError("invalid supervisor actions")
    if not isinstance(inventory, dict) or not isinstance(inventory.get("tasks"), list) or len(inventory["tasks"]) > 5000:
        raise MonitorError("invalid task inventory")
    instant(inventory.get("as_of"), "task inventory", now, timedelta(minutes=35))
    seen = set()
    for task in inventory["tasks"]:
        if not isinstance(task, dict) or not UUID.fullmatch(str(task.get("id", ""))) or task.get("status") not in {"queued", "running", "completed", "interrupted", "blocked"} or task["id"] in seen:
            raise MonitorError("invalid or duplicate task")
        seen.add(task["id"])
    for aid, rec in outbox["actions"].items():
        if not ACTION.fullmatch(aid) or not isinstance(rec, dict) or rec.get("state") not in {"pending", "acknowledged", "resolved", "superseded"} or not isinstance(rec.get("action"), dict) or rec["action"].get("id") != aid or rec["action"].get("repo") != REPO:
            raise MonitorError("invalid boss action")
        instant(rec.get("first_seen"), "boss action")
        if rec["action"].get("task_id") is not None and not UUID.fullmatch(str(rec["action"]["task_id"])):
            raise MonitorError("invalid boss task UUID")
        if not TOKEN.fullmatch(str(rec["action"].get("verb", ""))) or not LABEL.fullmatch(str(rec["action"].get("objective", ""))):
            raise MonitorError("invalid boss action identity")
    for aid, rec in supervisor["actions"].items():
        if not ACTION.fullmatch(aid) or not isinstance(rec, dict) or rec.get("id") != aid or rec.get("status") not in {"OPEN", "ACKED", "RESOLVED", "SUPERSEDED"}:
            raise MonitorError("invalid supervisor action")
        if rec.get("status") in {"OPEN", "ACKED"}:
            instant(rec.get("first_seen_utc"), "supervisor action")
            if rec["status"] == "ACKED":
                instant(rec.get("acknowledged_at_utc"), "supervisor acknowledgment")
            if type(rec.get("number")) is not int or rec["number"] < 1 or not SHA.fullmatch(str(rec.get("head", ""))):
                raise MonitorError("invalid OPEN supervisor action")
            if rec.get("task_id") is not None and not UUID.fullmatch(str(rec["task_id"])):
                raise MonitorError("invalid supervisor task UUID")
            if not TOKEN.fullmatch(str(rec.get("kind", ""))):
                raise MonitorError("invalid supervisor action kind")
    gates = ledger.get("dependency_gates")
    if not isinstance(gates, list) or len(gates) > 2000:
        raise MonitorError("missing or truncated dependency gates")
    gate_ids = set()
    for gate in gates:
        if not isinstance(gate, dict) or not GATE_ID.fullmatch(str(gate.get("id", ""))) or gate["id"] in gate_ids or type(gate.get("satisfied")) is not bool:
            raise MonitorError("invalid or duplicate dependency gate")
        gate_ids.add(gate["id"])
    ids = set()
    for row in rows:
        if not isinstance(row, dict) or not LABEL.fullmatch(str(row.get("id", ""))) or row["id"] in ids or (row.get("github_state") not in {"OPEN", "CLOSED"} and not (row.get("issue_number") is None and row.get("status") == "COMPLETED")):
            raise MonitorError("invalid objective")
        ids.add(row["id"])
        if row.get("work_item"):
            instant(row.get("last_checked_utc"), "objective", now, timedelta(minutes=35))
        if row.get("human_gate") and row["github_state"] == "OPEN" and row.get("last_activity_utc"):
            instant(row["last_activity_utc"], "issue activity")
        for number in row.get("pull_requests") or []:
            if type(number) is not int or number < 1 or number > 10_000_000:
                raise MonitorError("invalid linked PR number")
            pr_state = (row.get("pull_request_states") or {}).get(str(number))
            if pr_state not in {None, "OPEN", "MERGED", "CLOSED"}:
                raise MonitorError("invalid linked PR state")
            head = ((row.get("pr_observations") or {}).get(str(number)) or {}).get("head")
            if head is not None and not SHA.fullmatch(str(head)):
                raise MonitorError("invalid linked PR head")
    numbers = set()
    for pr in prs:
        if not isinstance(pr, dict) or type(pr.get("number")) is not int or pr["number"] < 1 or pr["number"] in numbers or not SHA.fullmatch(str(pr.get("head", ""))):
            raise MonitorError("invalid or duplicate open PR")
        if pr.get("objective") is not None and not LABEL.fullmatch(str(pr["objective"])):
            raise MonitorError("invalid PR objective")
        if pr.get("merge") not in {"CLEAN", "DIRTY", "UNKNOWN"}:
            raise MonitorError("invalid PR merge state")
        observation = pr.get("observation") or {}
        if not isinstance(observation, dict) or not isinstance(observation.get("checks"), list):
            raise MonitorError("invalid PR checks")
        review = observation.get("review") or {}
        if not isinstance(review, dict) or review.get("state") not in {"APPROVED", "CHANGES_REQUESTED", "STALE", "MISSING"}:
            raise MonitorError("invalid PR review state")
        for check in observation["checks"]:
            if not isinstance(check, dict) or not isinstance(check.get("name"), str) or not 1 <= len(check["name"]) <= 120 or any(ord(char) < 32 for char in check["name"]) or check.get("state") not in {"SUCCESS", "PENDING", "FAILURE"}:
                raise MonitorError("invalid PR check")
        numbers.add(pr["number"])


def semantic(ledger, outbox, supervisor, inventory, now):
    result = {}
    for gate in ledger["dependency_gates"]:
        result[f"dependency:{gate['id']}"] = {"satisfied": gate["satisfied"]}
    for row in ledger["objectives"]:
        prs = row.get("pull_requests") or []
        states = row.get("pull_request_states") or {}
        state = row.get("github_state") or row.get("status")
        result[f"objective:{row['id']}"] = {"state": state, "prs": [[n, states.get(str(n)), (row.get("pr_observations") or {}).get(str(n), {}).get("head")] for n in prs], "gate": bool(row.get("human_gate")) if state == "OPEN" else False, "activity": row.get("last_activity_utc") if row.get("human_gate") and state == "OPEN" else None}
    for pr in ledger["open_pull_requests"]:
        obs = pr.get("observation") or {}
        checks = obs.get("checks") or []
        result[f"pr:{pr['number']}"] = {"head": pr["head"], "objective": pr.get("objective"), "merge": pr.get("merge"), "review": (obs.get("review") or {}).get("state"), "failed_check_count": sum(c.get("state") == "FAILURE" for c in checks)}
    for task in inventory["tasks"]:
        result[f"task:{task['id']}"] = {"status": task["status"]}
    for aid, rec in outbox["actions"].items():
        if rec["state"] in {"pending", "acknowledged"}:
            start = instant(rec.get("acknowledged_at") if rec["state"] == "acknowledged" else rec["first_seen"], "action clock")
            age = now - start
            stage = 0 if age <= timedelta(minutes=15) else 1 if age < timedelta(minutes=60) else 2 + int((age - timedelta(minutes=60)) // timedelta(days=1))
            action = rec["action"]
            result[f"boss:{aid}"] = {"status": rec["state"], "overdue_stage": stage, "verb": action.get("verb"), "objective": action.get("objective"), "pr": action.get("pr"), "head": action.get("head")}
    for aid, rec in supervisor["actions"].items():
        if rec["status"] in {"OPEN", "ACKED"}:
            since = rec["acknowledged_at_utc"] if rec["status"] == "ACKED" else rec["first_seen_utc"]
            age = now - instant(since, "supervisor action clock")
            sla = rec.get("sla_minutes")
            if type(sla) is not int or sla < 1 or sla > 1440:
                raise MonitorError("invalid supervisor SLA")
            stage = 0 if age <= timedelta(minutes=sla) else 1 if age < timedelta(minutes=60) else 2 + int((age - timedelta(minutes=60)) // timedelta(days=1))
            result[f"supervisor:{aid}"] = {"status": rec["status"], "overdue_stage": stage, "kind": rec.get("kind"), "pr": rec["number"], "head": rec["head"]}
    return result


def event_id(entity, before, after, generation):
    identity = [REPO, entity, before, after, generation, (after or {}).get("head")]
    return hashlib.sha256(json.dumps(identity, sort_keys=True, separators=(",", ":")).encode()).hexdigest()[:32]


def meaningful(entity, before, after):
    if before == after:
        return False
    if entity.startswith(("boss:", "supervisor:")):
        return True
    if entity.startswith("pr:") and before is not None and after is None:
        return True
    if entity.startswith("task:") and before is None:
        return False
    if entity.startswith("objective:") and before is None:
        return False
    if entity.startswith("pr:") and before is None:
        return True
    return True


def message(event):
    payload = {"kind": "learnrise_monitor_event", "event_id": event["id"], "repository": REPO,
               "entity": event["entity"], "previous": event["previous"], "current": event["current"],
               "instruction": "Deduplicate this event_id before acting. Recheck live state, owner, review, CI, and human gates. For a reconcile_only baseline, inspect prior hub turns and live task/PR ownership before dispatch. This event grants no dispatch, merge, acceptance, or acknowledgment authority."}
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    if len(encoded) > 6000:
        raise MonitorError("event message exceeds bound")
    return encoded


def deliver(cursor, hub, cursor_path):
    for event in cursor["pending"][:]:
        queued = bounded_run(["codex", "queue", "--thread", hub, "--message", message(event)], timeout=30)
        if queued.returncode:
            raise MonitorError("hub queue failed")
        cursor["pending"].remove(event)
        if event["entity"] == "baseline":
            cursor["initialized"] = True
        atomic(cursor_path, cursor)


def run(args):
    now = now_utc()
    cursor_path = args.state_dir / "learnrise-monitor-cursor.json"
    receipt_path = args.state_dir / "learnrise-monitor-receipt.json"
    fault_path = args.state_dir / "learnrise-monitor-fault.json"
    if args.status or args.dry_run:
        cursor = read_json(cursor_path, optional=True) or {}
        receipt = read_json(receipt_path, optional=True)
        fault = read_json(fault_path, optional=True)
        snapshot = read_json(args.state_dir / "learnrise-monitor-snapshot.json", optional=True)
        stale = receipt is None or snapshot is None
        if receipt:
            try:
                instant(receipt.get("as_of"), "monitor receipt", now, timedelta(minutes=35))
            except MonitorError:
                stale = True
        if snapshot:
            try:
                instant(snapshot.get("as_of"), "monitor snapshot", now, timedelta(minutes=35))
                if snapshot.get("repository") != REPO or not isinstance(snapshot.get("entities"), dict):
                    stale = True
            except MonitorError:
                stale = True
        observed = (snapshot or {}).get("entities", {})
        if not isinstance(observed, dict):
            observed = {}
        preview = sum(cursor.get("entities", {}).get(k, {}).get("state") != v for k, v in observed.items())
        output = {"initialized": bool(cursor.get("initialized")), "pending": len(cursor.get("pending", [])), "entities": len(observed), "preview_changes": preview, "last_success": receipt, "stale": stale, "fault": fault}
        print(json.dumps(output, sort_keys=True))
        return 2 if stale or fault or cursor.get("pending") else 0
    args.state_dir.mkdir(parents=True, exist_ok=True)
    with (args.state_dir / "learnrise-monitor.lock").open("a+") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise MonitorError("monitor overlap") from exc
        cursor = read_json(cursor_path, optional=True) or {"version": 1, "initialized": False, "entities": {}, "pending": []}
        if cursor.get("version") != 1 or not isinstance(cursor.get("entities"), dict) or not isinstance(cursor.get("pending"), list):
            raise MonitorError("invalid cursor")
        try:
            bridge = bounded_run([sys.executable, str(args.bridge), "--ledger", str(args.ledger), "--audit", str(args.audit), "--outbox", str(args.outbox), "--tasks", str(args.tasks)], timeout=240)
            if bridge.returncode not in (0, 3) or len(bridge.stdout) > MAX_BYTES:
                raise MonitorError(f"bridge failed ({bridge.returncode})")
            report = json.loads(bridge.stdout)
            if not isinstance(report, dict) or not isinstance(report.get("actions"), list) or not isinstance(report.get("overdue"), list):
                raise MonitorError("malformed bridge report")
            ledger = read_json(args.observed)
            outbox = read_json(args.outbox)
            supervisor = read_json(args.supervisor)
            inventory = report.get("inventory")
            validate_sources(ledger, outbox, supervisor, inventory, args.hub, now)
            snapshot = semantic(ledger, outbox, supervisor, inventory, now)
            atomic(args.state_dir / "learnrise-monitor-snapshot.json", {"as_of": now.isoformat(), "repository": REPO, "entities": snapshot})
            receipt = {"as_of": now.isoformat(), "mode": "shadow" if args.shadow else "apply", "objectives": len(ledger["objectives"]), "open_prs": len(ledger["open_pull_requests"]), "supervisor_open": sum(k.startswith("supervisor:") for k in snapshot)}
            if args.shadow:
                atomic(receipt_path, receipt)
                if not cursor["pending"] and fault_path.exists():
                    fault_path.unlink()
                print(json.dumps({"mode": "shadow", "entities": len(snapshot), "open_prs": len(ledger["open_pull_requests"]), "preview_changes": sum(cursor["entities"].get(k, {}).get("state") != v for k, v in snapshot.items())}))
                return 2 if cursor["pending"] else 0
            first_baseline = not cursor["initialized"] and not any(e["entity"] == "baseline" for e in cursor["pending"])
            if first_baseline:
                unresolved = sorted(k for k in snapshot if k.startswith(("boss:", "supervisor:")) or (k.startswith("objective:") and snapshot[k].get("gate")) or (k.startswith("dependency:") and not snapshot[k]["satisfied"]))
                if len(unresolved) > 100:
                    raise MonitorError("baseline contains too many unresolved IDs for one bounded message")
                baseline = {"id": event_id("baseline", None, {"ids": unresolved}, 1), "entity": "baseline", "previous": None, "current": {"counts": {"boss": sum(k.startswith("boss:") for k in unresolved), "supervisor": sum(k.startswith("supervisor:") for k in unresolved), "human_gates": sum(k.startswith("objective:") for k in unresolved), "dependency_gates": sum(k.startswith("dependency:") for k in unresolved)}, "ids": unresolved, "snapshot": str(args.state_dir / "learnrise-monitor-snapshot.json"), "reconcile_only": True}}
                cursor["pending"].append(baseline)
            if first_baseline:
                # Freeze the source state covered by the baseline. A later scan
                # may discover new entities while its queue call is pending.
                cursor["entities"] = {k: {"state": v, "generation": 0} for k, v in snapshot.items()}
                atomic(cursor_path, cursor)
            # Record transitions from every fresh scan before retrying the
            # baseline. If the baseline queue fails again, later observations
            # are already durable behind it in pending delivery order.
            for entity in sorted(set(cursor["entities"]) | set(snapshot)):
                prior = cursor["entities"].get(entity, {"state": None, "generation": 0})
                current = snapshot.get(entity)
                if current == prior["state"]:
                    continue
                generation = prior["generation"] + 1
                if meaningful(entity, prior["state"], current):
                    cursor["pending"].append({"id": event_id(entity, prior["state"], current, generation), "entity": entity, "previous": prior["state"], "current": current})
                # Retain tombstones so a later red→green→red cycle cannot reuse
                # a prior generation or event ID on the same PR head.
                cursor["entities"][entity] = {"state": current, "generation": generation}
            atomic(cursor_path, cursor)
            deliver(cursor, args.hub, cursor_path)
            atomic(receipt_path, receipt)
            if fault_path.exists():
                fault_path.unlink()
            print(json.dumps({"mode": "apply", "entities": len(snapshot), "pending": len(cursor["pending"])}))
            return 0
        except (MonitorError, OSError, ValueError, json.JSONDecodeError) as exc:
            atomic(fault_path, {"as_of": now.isoformat(), "error": str(exc)[:300]})
            raise


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__)
    mode = ap.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true")
    mode.add_argument("--shadow", action="store_true")
    mode.add_argument("--apply", action="store_true")
    mode.add_argument("--status", action="store_true")
    ap.add_argument("--state-dir", type=Path, default=ARTIFACTS)
    ap.add_argument("--ledger", type=Path, default=ARTIFACTS / "ownership.json")
    ap.add_argument("--audit", type=Path, default=ARTIFACTS / "audit.py")
    ap.add_argument("--outbox", type=Path, default=ARTIFACTS / "boss-action-outbox.json")
    ap.add_argument("--tasks", type=Path, default=ARTIFACTS / "boss-task-inventory.json")
    ap.add_argument("--observed", type=Path, default=ARTIFACTS / "boss-observed-ledger.json")
    ap.add_argument("--supervisor", type=Path, default=ARTIFACTS / "pr-supervisor-state.json")
    ap.add_argument("--bridge", type=Path, default=Path(__file__).with_name("runtime_bridge.py"))
    ap.add_argument("--hub", default="01a0d565-c171-7120-b828-b04db384021f")
    args = ap.parse_args(argv)
    if not (args.shadow or args.apply or args.status):
        args.dry_run = True
    try:
        return run(args)
    except (MonitorError, OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"learnrise monitor: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
