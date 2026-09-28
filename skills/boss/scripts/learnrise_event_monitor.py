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
    linked_open_prs = set()
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
            if pr_state == "OPEN":
                linked_open_prs.add(number)
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
    if linked_open_prs - numbers:
        raise MonitorError("open linked PR missing from full inventory")


def semantic(ledger, outbox, supervisor, inventory, now):
    result = {}
    for gate in ledger["dependency_gates"]:
        result[f"dependency:{gate['id']}"] = {"satisfied": gate["satisfied"]}
    for row in ledger["objectives"]:
        prs = row.get("pull_requests") or []
        states = row.get("pull_request_states") or {}
        gate = row.get("github_state") == "OPEN" and bool(row.get("human_gate"))
        state = ("OPEN" if gate else "COMPLETED" if row.get("status") == "COMPLETED"
                 else row.get("github_state") or row.get("status"))
        result[f"objective:{row['id']}"] = {"state": state, "prs": [[n, states.get(str(n)), (row.get("pr_observations") or {}).get(str(n), {}).get("head")] for n in prs], "gate": gate, "activity": row.get("last_activity_utc") if gate else None}
    for pr in ledger["open_pull_requests"]:
        obs = pr.get("observation") or {}
        checks = obs.get("checks") or []
        failed_checks = sorted(c["name"] for c in checks if c.get("state") == "FAILURE")
        failed_digest = hashlib.sha256(json.dumps(failed_checks, separators=(",", ":")).encode()).hexdigest()
        result[f"pr:{pr['number']}"] = {"head": pr["head"], "objective": pr.get("objective"), "merge": pr.get("merge"), "review": (obs.get("review") or {}).get("state"), "failed_check_count": len(failed_checks), "failed_checks_sha256": failed_digest}
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


def entity_kind(entity):
    if not isinstance(entity, str):
        raise MonitorError("invalid cursor entity")
    prefix, separator, identifier = entity.partition(":")
    if not separator:
        raise MonitorError("invalid cursor entity")
    valid = {
        "objective": lambda: LABEL.fullmatch(identifier),
        "pr": lambda: re.fullmatch(r"[1-9][0-9]{0,9}", identifier),
        "task": lambda: UUID.fullmatch(identifier),
        "boss": lambda: re.fullmatch(r"[0-9a-f]{24}", identifier),
        "supervisor": lambda: re.fullmatch(r"[0-9a-f]{20}", identifier),
        "dependency": lambda: GATE_ID.fullmatch(identifier),
    }
    if prefix not in valid or not valid[prefix]():
        raise MonitorError("invalid cursor entity")
    return prefix


def enum(value, choices):
    return type(value) is str and value in choices


def validate_semantic_state(entity, state):
    kind = entity_kind(entity)
    if state is None:
        return
    if not isinstance(state, dict):
        raise MonitorError("invalid cursor semantic state")
    keys = set(state)
    if kind == "objective":
        if keys != {"state", "prs", "gate", "activity"} or not enum(state["state"], {"OPEN", "CLOSED", "COMPLETED"}) or type(state["gate"]) is not bool or not isinstance(state["prs"], list) or len(state["prs"]) > 1000:
            raise MonitorError("invalid objective cursor state")
        if (state["gate"] and state["state"] != "OPEN") or (not state["gate"] and state["activity"] is not None):
            raise MonitorError("invalid objective gate state")
        if state["activity"] is not None:
            instant(state["activity"], "cursor issue activity")
        numbers = set()
        for pr in state["prs"]:
            if (not isinstance(pr, list) or len(pr) != 3 or type(pr[0]) is not int or pr[0] < 1 or
                    pr[0] in numbers or (pr[1] is not None and not enum(pr[1], {"OPEN", "MERGED", "CLOSED"})) or
                    (pr[2] is not None and (not isinstance(pr[2], str) or not SHA.fullmatch(pr[2])))):
                raise MonitorError("invalid objective PR cursor state")
            numbers.add(pr[0])
    elif kind == "pr":
        if (keys not in ({"head", "objective", "merge", "review", "failed_check_count"},
                         {"head", "objective", "merge", "review", "failed_check_count", "failed_checks_sha256"}) or
                not isinstance(state["head"], str) or not SHA.fullmatch(state["head"]) or
                (state["objective"] is not None and (not isinstance(state["objective"], str) or not LABEL.fullmatch(state["objective"]))) or
                not enum(state["merge"], {"CLEAN", "DIRTY", "UNKNOWN"}) or
                not enum(state["review"], {"APPROVED", "CHANGES_REQUESTED", "STALE", "MISSING"}) or
                type(state["failed_check_count"]) is not int or state["failed_check_count"] < 0):
            raise MonitorError("invalid PR cursor state")
        if "failed_checks_sha256" in state and (type(state["failed_checks_sha256"]) is not str or
                not re.fullmatch(r"[0-9a-f]{64}", state["failed_checks_sha256"])):
            raise MonitorError("invalid failed check digest")
    elif kind == "task":
        if keys != {"status"} or not enum(state["status"], {"queued", "running", "completed", "interrupted", "blocked"}):
            raise MonitorError("invalid task cursor state")
    elif kind == "boss":
        if (keys != {"status", "overdue_stage", "verb", "objective", "pr", "head"} or
                not enum(state["status"], {"pending", "acknowledged"}) or
                type(state["overdue_stage"]) is not int or state["overdue_stage"] < 0 or
                not isinstance(state["verb"], str) or not TOKEN.fullmatch(state["verb"]) or
                not isinstance(state["objective"], str) or not LABEL.fullmatch(state["objective"]) or
                (state["pr"] is not None and (type(state["pr"]) is not int or state["pr"] < 1)) or
                (state["head"] is not None and (not isinstance(state["head"], str) or not SHA.fullmatch(state["head"])))):
            raise MonitorError("invalid boss cursor state")
    elif kind == "supervisor":
        if (keys != {"status", "overdue_stage", "kind", "pr", "head"} or
                not enum(state["status"], {"OPEN", "ACKED"}) or
                type(state["overdue_stage"]) is not int or state["overdue_stage"] < 0 or
                not isinstance(state["kind"], str) or not TOKEN.fullmatch(state["kind"]) or
                type(state["pr"]) is not int or state["pr"] < 1 or
                not isinstance(state["head"], str) or not SHA.fullmatch(state["head"])):
            raise MonitorError("invalid supervisor cursor state")
    elif keys != {"satisfied"} or type(state["satisfied"]) is not bool:
        raise MonitorError("invalid dependency cursor state")


def validate_baseline(current, state_dir):
    if not isinstance(current, dict) or set(current) != {"counts", "ids", "snapshot", "snapshot_digest", "reconcile_only"}:
        raise MonitorError("invalid baseline cursor event")
    counts = current["counts"]
    kinds = {"boss", "supervisor", "human_gates", "dependency_gates"}
    if (not isinstance(counts, dict) or set(counts) != kinds or
            any(type(value) is not int or value < 0 for value in counts.values()) or
            not isinstance(current["ids"], list) or len(current["ids"]) > 100 or
            any(type(entity) is not str for entity in current["ids"]) or
            type(current["snapshot_digest"]) is not str or
            not re.fullmatch(r"[0-9a-f]{64}", current["snapshot_digest"]) or
            current["snapshot"] != str(state_dir / f"learnrise-monitor-baseline-{current['snapshot_digest']}.json") or
            current["reconcile_only"] is not True):
        raise MonitorError("invalid baseline cursor event")
    ids = current["ids"]
    if ids != sorted(set(ids)):
        raise MonitorError("invalid baseline IDs")
    for entity in ids:
        if entity_kind(entity) not in {"boss", "supervisor", "objective", "dependency"}:
            raise MonitorError("invalid baseline entity")
    observed = {"boss": sum(item.startswith("boss:") for item in ids),
                "supervisor": sum(item.startswith("supervisor:") for item in ids),
                "human_gates": sum(item.startswith("objective:") for item in ids),
                "dependency_gates": sum(item.startswith("dependency:") for item in ids)}
    if counts != observed:
        raise MonitorError("invalid baseline counts")


def validate_cursor(cursor, state_dir):
    if (type(cursor.get("version")) is not int or cursor["version"] != 1 or
            type(cursor.get("initialized")) is not bool or
            not isinstance(cursor.get("entities"), dict) or len(cursor["entities"]) > 20_000 or
            not isinstance(cursor.get("pending"), list) or len(cursor["pending"]) > 10_000):
        raise MonitorError("invalid cursor")
    for entity, record in cursor["entities"].items():
        entity_kind(entity)
        if (not isinstance(record, dict) or set(record) != {"state", "generation"} or
                type(record.get("generation")) is not int or record["generation"] < 0):
            raise MonitorError("invalid cursor entity state")
        validate_semantic_state(entity, record["state"])
    pending_ids = set()
    last_transition = {}
    baseline_count = 0
    for position, event in enumerate(cursor["pending"]):
        if (not isinstance(event, dict) or
                set(event) not in ({"id", "entity", "previous", "current", "generation"},
                                   {"id", "entity", "previous", "current", "generation", "silent"}) or
                type(event["id"]) is not str or not re.fullmatch(r"[0-9a-f]{32}", event["id"]) or
                type(event["generation"]) is not int or event["generation"] < 1 or
                not isinstance(event["entity"], str)):
            raise MonitorError("invalid pending cursor event")
        silent = "silent" in event
        if silent and event["silent"] is not True:
            raise MonitorError("invalid silent transition")
        if event["id"] in pending_ids:
            raise MonitorError("duplicate pending event ID")
        pending_ids.add(event["id"])
        if event["entity"] == "baseline":
            if silent:
                raise MonitorError("invalid silent baseline")
            baseline_count += 1
            if cursor["initialized"] or position != 0 or baseline_count != 1 or event["generation"] != 1 or event["previous"] is not None:
                raise MonitorError("invalid pending baseline")
            validate_baseline(event["current"], state_dir)
        else:
            validate_semantic_state(event["entity"], event["previous"])
            validate_semantic_state(event["entity"], event["current"])
            if (event["previous"] == event["current"] or
                    silent == meaningful(event["entity"], event["previous"], event["current"]) or
                    event["entity"] not in cursor["entities"] or
                    event["generation"] > cursor["entities"][event["entity"]]["generation"]):
                raise MonitorError("invalid pending transition")
            preceding = last_transition.get(event["entity"])
            if silent and preceding is None and event["generation"] == 1:
                raise MonitorError("orphan silent transition")
            if preceding and (event["generation"] != preceding["generation"] + 1 or
                              event["previous"] != preceding["current"]):
                raise MonitorError("broken pending transition chain")
            last_transition[event["entity"]] = event
        if event["id"] != event_id(event["entity"], event["previous"], event["current"], event["generation"]):
            raise MonitorError("invalid pending event identity")
    if not cursor["initialized"] and cursor["pending"] and baseline_count != 1:
        raise MonitorError("missing pending baseline")
    for entity, last in last_transition.items():
        stored = cursor["entities"][entity]
        if last["generation"] != stored["generation"] or last["current"] != stored["state"]:
            raise MonitorError("pending transition does not match cursor")


def event_id(entity, before, after, generation):
    if entity == "baseline" and isinstance(after, dict):
        after = {"counts": after.get("counts"), "ids": after.get("ids"),
                 "reconcile_only": after.get("reconcile_only")}
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
        return isinstance(after, dict) and after.get("state") == "OPEN" and after.get("gate") is True
    if entity.startswith("pr:") and before is None:
        return True
    return True


def preview_changes(entities, observed):
    return sum(entities.get(entity, {}).get("state") != observed.get(entity)
               for entity in entities.keys() | observed.keys())


def baseline_current(snapshot, state_dir):
    unresolved = sorted(k for k in snapshot if k.startswith(("boss:", "supervisor:")) or
                        (k.startswith("objective:") and snapshot[k].get("gate")) or
                        (k.startswith("dependency:") and not snapshot[k]["satisfied"]))
    if len(unresolved) > 100:
        raise MonitorError("baseline contains too many unresolved IDs for one bounded message")
    digest = hashlib.sha256(json.dumps(snapshot, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    current = {"counts": {"boss": sum(k.startswith("boss:") for k in unresolved),
                       "supervisor": sum(k.startswith("supervisor:") for k in unresolved),
                       "human_gates": sum(k.startswith("objective:") for k in unresolved),
                       "dependency_gates": sum(k.startswith("dependency:") for k in unresolved)},
            "ids": unresolved,
            "snapshot": str(state_dir / f"learnrise-monitor-baseline-{digest}.json"),
            "snapshot_digest": digest,
            "reconcile_only": True}
    # The queue limit applies to the complete encoded event, including the
    # path and instruction, rather than merely to the unresolved-ID count.
    message({"id": event_id("baseline", None, current, 1), "entity": "baseline",
             "previous": None, "current": current})
    return current


def verify_baseline_snapshot(current):
    content = read_json(Path(current["snapshot"]))
    if (set(content) != {"repository", "entities", "semantic_sha256"} or
            content["repository"] != REPO or content["semantic_sha256"] != current["snapshot_digest"] or
            not isinstance(content["entities"], dict) or
            hashlib.sha256(json.dumps(content["entities"], sort_keys=True, separators=(",", ":")).encode()).hexdigest() != current["snapshot_digest"] or
            baseline_current(content["entities"], Path(current["snapshot"]).parent) != current):
        raise MonitorError("baseline snapshot content mismatch")
    return content["entities"]


def write_baseline_snapshot(snapshot, current):
    path = Path(current["snapshot"])
    content = {"repository": REPO, "entities": snapshot,
               "semantic_sha256": current["snapshot_digest"]}
    if baseline_current(snapshot, path.parent) != current:
        raise MonitorError("baseline snapshot key mismatch")
    fd, temporary = tempfile.mkstemp(prefix=".baseline-", dir=path.parent)
    try:
        with os.fdopen(fd, "w") as stream:
            json.dump(content, stream, sort_keys=True, separators=(",", ":"))
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        try:
            os.link(temporary, path)
        except FileExistsError:
            pass
    finally:
        os.unlink(temporary)
    if verify_baseline_snapshot(current) != snapshot:
        raise MonitorError("baseline snapshot collision")


def validate_cutover_ready(state_dir, hub, now):
    ready = read_json(state_dir / "learnrise-monitor-cutover-ready.json")
    receipt = read_json(state_dir / "learnrise-monitor-receipt.json")
    snapshot = read_json(state_dir / "learnrise-monitor-snapshot.json")
    if (set(ready) not in ({"version", "as_of", "hub", "coverage", "drain", "shadow", "canaries"},
                           {"version", "as_of", "hub", "coverage", "drain", "shadow", "canaries", "rearm"}) or
            type(ready["version"]) is not int or ready["version"] != 1 or ready["hub"] != hub or
            not isinstance(ready["coverage"], dict) or
            set(ready["coverage"]) != {"covered_heartbeat_ids", "paused_heartbeat_ids", "objectives", "complete", "paused_at"} or
            not isinstance(ready["drain"], dict) or
            set(ready["drain"]) != {"confirmed_at", "method", "no_prior_turns"} or
            not isinstance(ready["shadow"], dict) or
            set(ready["shadow"]) != {"receipt_as_of", "baseline_id", "reviewed"} or
            not isinstance(ready["canaries"], dict) or
            set(ready["canaries"]) != {"queue", "watchdog", "preview"}):
        raise MonitorError("invalid cutover readiness artifact")
    coverage, drain, shadow = ready["coverage"], ready["drain"], ready["shadow"]
    covered, paused, objectives = (coverage["covered_heartbeat_ids"],
                                   coverage["paused_heartbeat_ids"], coverage["objectives"])
    if (not isinstance(covered, list) or not covered or any(type(v) is not str or not UUID.fullmatch(v) for v in covered) or
            not isinstance(paused, list) or sorted(paused) != sorted(covered) or len(set(covered)) != len(covered) or
            not isinstance(objectives, list) or not objectives or
            any(type(v) is not str or not LABEL.fullmatch(v) for v in objectives) or len(set(objectives)) != len(objectives) or
            coverage["complete"] is not True or drain["no_prior_turns"] is not True or
            drain["method"] not in {"observed_empty", "interval_elapsed"} or
            shadow["reviewed"] is not True or any(value is not True for value in ready["canaries"].values())):
        raise MonitorError("incomplete cutover readiness artifact")
    ready_at = instant(ready["as_of"], "cutover readiness", now, timedelta(minutes=35))
    paused_at = instant(coverage["paused_at"], "heartbeat pause", now, timedelta(minutes=35))
    drained_at = instant(drain["confirmed_at"], "hub drain", now, timedelta(minutes=35))
    if not paused_at <= drained_at <= ready_at or (drain["method"] == "interval_elapsed" and drained_at - paused_at < timedelta(minutes=15)):
        raise MonitorError("invalid cutover sequence")
    if (receipt.get("mode") != "shadow" or receipt.get("as_of") != shadow["receipt_as_of"] or
            snapshot.get("repository") != REPO or snapshot.get("as_of") != receipt.get("as_of") or
            not isinstance(snapshot.get("entities"), dict)):
        raise MonitorError("cutover shadow evidence mismatch")
    instant(receipt["as_of"], "cutover shadow receipt", now, timedelta(minutes=35))
    for entity, state in snapshot["entities"].items():
        if state is None:
            raise MonitorError("invalid cutover snapshot entity")
        validate_semantic_state(entity, state)
    baseline = baseline_current(snapshot["entities"], state_dir)
    if shadow["baseline_id"] != event_id("baseline", None, baseline, 1):
        raise MonitorError("cutover baseline review mismatch")
    return ready, snapshot["entities"]


def validate_rearm_marker(marker, cursor):
    baseline = next((event for event in cursor["pending"] if event["entity"] == "baseline"), None)
    if (not isinstance(marker, dict) or set(marker) != {"version", "failed_at", "baseline_id"} or
            type(marker["version"]) is not int or marker["version"] != 1 or
            (marker["baseline_id"] is not None and
             (type(marker["baseline_id"]) is not str or not re.fullmatch(r"[0-9a-f]{32}", marker["baseline_id"]))) or
            (baseline is not None and marker["baseline_id"] not in (None, baseline["id"])) or cursor["initialized"]):
        raise MonitorError("invalid cutover rearm marker")
    return instant(marker["failed_at"], "cutover rearm failure")


def attempt_as_rearm_marker(attempt):
    if (not isinstance(attempt, dict) or set(attempt) != {"version", "started_at", "baseline_id", "pid"} or
            type(attempt["version"]) is not int or attempt["version"] != 1 or
            type(attempt["pid"]) is not int or attempt["pid"] < 1 or
            (attempt["baseline_id"] is not None and
             (type(attempt["baseline_id"]) is not str or not re.fullmatch(r"[0-9a-f]{32}", attempt["baseline_id"])))):
        raise MonitorError("invalid cutover attempt")
    instant(attempt["started_at"], "cutover attempt")
    return {"version": 1, "failed_at": attempt["started_at"],
            "baseline_id": attempt["baseline_id"]}


def latest_rearm_marker(marker, attempt):
    if attempt is None:
        return marker
    attempt_marker = attempt_as_rearm_marker(attempt)
    if marker is None:
        return attempt_marker
    if instant(marker["failed_at"], "cutover rearm failure") < instant(attempt["started_at"], "cutover attempt"):
        return attempt_marker
    return marker


def validate_rearm_ready(ready, marker, cursor):
    failed_at = validate_rearm_marker(marker, cursor)
    rearm = ready.get("rearm")
    if (not isinstance(rearm, dict) or set(rearm) != {"pending_event_ids", "reviewed"} or
            rearm["reviewed"] is not True or type(rearm["pending_event_ids"]) is not list or
            rearm["pending_event_ids"] != [event["id"] for event in cursor["pending"]] or
            instant(ready["as_of"], "cutover rearm readiness") <= failed_at):
        raise MonitorError("cutover rearm evidence missing or stale")


def message(event):
    payload = {"kind": "learnrise_monitor_event", "event_id": event["id"], "repository": REPO,
               "entity": event["entity"], "previous": event["previous"], "current": event["current"],
               "instruction": "Deduplicate this event_id before acting. Recheck live state, owner, review, CI, and human gates. For a reconcile_only baseline, inspect prior hub turns and live task/PR ownership before dispatch. This event grants no dispatch, merge, acceptance, or acknowledgment authority."}
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    if len(encoded) > 6000:
        raise MonitorError("event message exceeds bound")
    return encoded


def deliver(cursor, hub, cursor_path, rearm_path, attempt_path):
    for event in cursor["pending"][:]:
        if "silent" not in event:
            queued = bounded_run(["codex", "queue", "--thread", hub, "--message", message(event)], timeout=30)
            if queued.returncode:
                raise MonitorError("hub queue failed")
        cursor["pending"].remove(event)
        if event["entity"] == "baseline":
            cursor["initialized"] = True
        atomic(cursor_path, cursor)
        if event["entity"] == "baseline" and rearm_path.exists():
            rearm_path.unlink()
        if event["entity"] == "baseline" and attempt_path.exists():
            attempt_path.unlink()


def run(args):
    now = now_utc()
    cursor_path = args.state_dir / "learnrise-monitor-cursor.json"
    receipt_path = args.state_dir / "learnrise-monitor-receipt.json"
    fault_path = args.state_dir / "learnrise-monitor-fault.json"
    rearm_path = args.state_dir / "learnrise-monitor-rearm-required.json"
    attempt_path = args.state_dir / "learnrise-monitor-cutover-attempt.json"
    if args.cutover_check:
        validate_hub(args.hub)
        ready, _ = validate_cutover_ready(args.state_dir, args.hub, now)
        marker = read_json(rearm_path, optional=True)
        attempt = read_json(attempt_path, optional=True)
        marker = latest_rearm_marker(marker, attempt)
        cursor = read_json(cursor_path, optional=True) or {"version": 1, "initialized": False,
                                                            "entities": {}, "pending": []}
        validate_cursor(cursor, args.state_dir)
        committed_baseline = cursor["initialized"] and not any(
            event["entity"] == "baseline" for event in cursor["pending"])
        if marker is not None and not committed_baseline:
            validate_rearm_ready(ready, marker, cursor)
        elif marker is None and "rearm" in ready:
            raise MonitorError("unexpected cutover rearm evidence")
        print(json.dumps({"cutover_ready": True, "hub": args.hub}, sort_keys=True))
        return 0
    if args.status or args.dry_run:
        cursor_error = None
        try:
            cursor = read_json(cursor_path, optional=True)
            if cursor is not None:
                validate_cursor(cursor, args.state_dir)
        except (MonitorError, OSError, ValueError, TypeError, KeyError, RecursionError, json.JSONDecodeError) as exc:
            cursor_error = str(exc) or "malformed cursor"
            cursor = None
        cursor = cursor or {}
        receipt = read_json(receipt_path, optional=True)
        fault = read_json(fault_path, optional=True)
        rearm_required = rearm_path.exists() or attempt_path.exists()
        if cursor_error:
            fault = {"error": "invalid cursor: " + cursor_error[:300]}
        if rearm_required:
            fault = {"error": "cutover rearm required"}
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
        preview = preview_changes(cursor.get("entities", {}), observed)
        output = {"initialized": bool(cursor.get("initialized")), "pending": len(cursor.get("pending", [])), "entities": len(observed), "preview_changes": preview, "last_success": receipt, "stale": stale, "fault": fault, "rearm_required": rearm_required}
        print(json.dumps(output, sort_keys=True))
        return 2 if stale or fault or cursor.get("pending") else 0
    args.state_dir.mkdir(parents=True, exist_ok=True)
    with (args.state_dir / "learnrise-monitor.lock").open("a+") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise MonitorError("monitor overlap") from exc
        cursor = None
        cursor_valid = False
        cutover_ready = None
        try:
            cursor = read_json(cursor_path, optional=True)
            if cursor is None:
                cursor = {"version": 1, "initialized": False, "entities": {}, "pending": []}
            validate_cursor(cursor, args.state_dir)
            cursor_valid = True
            pending_baseline = next((event for event in cursor["pending"] if event["entity"] == "baseline"), None)
            if args.apply and cursor["initialized"] and pending_baseline is None:
                # The cursor commit is the baseline delivery commit point.
                # A process killed between that write and marker cleanup must
                # resume without retrying the already delivered baseline.
                for completed_marker in (rearm_path, attempt_path):
                    if completed_marker.exists():
                        completed_marker.unlink()
            if pending_baseline is not None:
                verify_baseline_snapshot(pending_baseline["current"])
            reviewed_snapshot = None
            mode_path = args.state_dir / "learnrise-monitor-mode"
            if args.apply:
                if not mode_path.exists() or mode_path.stat().st_size > 16 or mode_path.read_text().strip() != "apply":
                    raise MonitorError("monitor is not activated for apply")
                marker = read_json(rearm_path, optional=True)
                attempt = read_json(attempt_path, optional=True)
                marker = latest_rearm_marker(marker, attempt)
                if cursor["initialized"]:
                    if marker is not None or args.rearm or attempt is not None:
                        raise MonitorError("unexpected cutover rearm state")
                elif marker is None and not cursor["pending"]:
                    if args.rearm:
                        raise MonitorError("unexpected cutover rearm state")
                    cutover_ready, reviewed_snapshot = validate_cutover_ready(args.state_dir, args.hub, now)
                    if "rearm" in cutover_ready:
                        raise MonitorError("unexpected cutover rearm evidence")
                else:
                    if marker is None:
                        baseline = next(event for event in cursor["pending"] if event["entity"] == "baseline")
                        marker = {"version": 1, "failed_at": now.isoformat(), "baseline_id": baseline["id"]}
                        atomic(rearm_path, marker)
                    if not args.rearm:
                        raise MonitorError("cutover rearm required")
                    cutover_ready, reviewed_snapshot = validate_cutover_ready(args.state_dir, args.hub, now)
                    validate_rearm_ready(cutover_ready, marker, cursor)
            elif args.rearm:
                raise MonitorError("rearm requires apply")
            if args.apply and not cursor["initialized"]:
                # The lock covers this write and the first bridge call. A killed
                # process leaves a durable disarm signal before any fallible scan.
                atomic(attempt_path, {"version": 1, "started_at": now.isoformat(),
                                      "baseline_id": (pending_baseline["id"] if pending_baseline else
                                                      cutover_ready["shadow"]["baseline_id"]),
                                      "pid": os.getpid()})
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
            if reviewed_snapshot is not None and snapshot != reviewed_snapshot:
                raise MonitorError("live state changed since cutover review")
            shadow_baseline = baseline_current(snapshot, args.state_dir) if args.shadow else None
            atomic(args.state_dir / "learnrise-monitor-snapshot.json", {"as_of": now.isoformat(), "repository": REPO, "entities": snapshot})
            receipt = {"as_of": now.isoformat(), "mode": "shadow" if args.shadow else "apply", "objectives": len(ledger["objectives"]), "open_prs": len(ledger["open_pull_requests"]), "supervisor_open": sum(k.startswith("supervisor:") for k in snapshot)}
            if args.shadow:
                atomic(receipt_path, receipt)
                if not cursor["pending"] and fault_path.exists():
                    fault_path.unlink()
                print(json.dumps({"mode": "shadow", "entities": len(snapshot), "open_prs": len(ledger["open_pull_requests"]), "preview_changes": preview_changes(cursor["entities"], snapshot), "baseline": shadow_baseline, "baseline_event_id": event_id("baseline", None, shadow_baseline, 1)}))
                return 2 if cursor["pending"] else 0
            first_baseline = not cursor["initialized"] and not any(e["entity"] == "baseline" for e in cursor["pending"])
            if first_baseline:
                baseline_state = baseline_current(snapshot, args.state_dir)
                write_baseline_snapshot(snapshot, baseline_state)
                baseline = {"id": event_id("baseline", None, baseline_state, 1), "entity": "baseline", "previous": None, "current": baseline_state, "generation": 1}
                cursor["pending"].append(baseline)
            if first_baseline:
                # Freeze the source state covered by the baseline. A later scan
                # may discover new entities while its queue call is pending.
                cursor["entities"] = {k: {"state": v, "generation": 0} for k, v in snapshot.items()}
                atomic(cursor_path, cursor)
            # Record transitions from every fresh scan before retrying the
            # baseline. If the baseline queue fails again, later observations
            # are already durable behind it in pending delivery order.
            pending_entities = {event["entity"] for event in cursor["pending"]}
            for entity in sorted(set(cursor["entities"]) | set(snapshot)):
                prior = cursor["entities"].get(entity, {"state": None, "generation": 0})
                current = snapshot.get(entity)
                if current == prior["state"]:
                    continue
                generation = prior["generation"] + 1
                should_queue = meaningful(entity, prior["state"], current)
                if should_queue or entity in pending_entities:
                    event = {"id": event_id(entity, prior["state"], current, generation), "entity": entity,
                             "previous": prior["state"], "current": current, "generation": generation}
                    if not should_queue:
                        event["silent"] = True
                    cursor["pending"].append(event)
                    pending_entities.add(entity)
                # Retain tombstones so a later red→green→red cycle cannot reuse
                # a prior generation or event ID on the same PR head.
                cursor["entities"][entity] = {"state": current, "generation": generation}
            atomic(cursor_path, cursor)
            deliver(cursor, args.hub, cursor_path, rearm_path, attempt_path)
            atomic(receipt_path, receipt)
            if fault_path.exists():
                fault_path.unlink()
            print(json.dumps({"mode": "apply", "entities": len(snapshot), "pending": len(cursor["pending"])}))
            return 0
        except (MonitorError, OSError, ValueError, TypeError, KeyError, RecursionError, json.JSONDecodeError) as exc:
            if args.apply and (not cursor_valid or not cursor["initialized"]):
                baseline = (next((event for event in cursor["pending"] if event["entity"] == "baseline"), None)
                            if cursor_valid else None)
                if args.rearm or not rearm_path.exists():
                    reviewed_id = cutover_ready["shadow"]["baseline_id"] if cutover_ready else None
                    atomic(rearm_path, {"version": 1, "failed_at": now.isoformat(),
                                        "baseline_id": baseline["id"] if baseline else reviewed_id})
                if attempt_path.exists() and rearm_path.exists():
                    attempt_path.unlink()
            atomic(fault_path, {"as_of": now.isoformat(), "error": str(exc)[:300]})
            raise


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__)
    mode = ap.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true")
    mode.add_argument("--shadow", action="store_true")
    mode.add_argument("--apply", action="store_true")
    mode.add_argument("--status", action="store_true")
    mode.add_argument("--cutover-check", action="store_true")
    ap.add_argument("--rearm", action="store_true", help="manually retry a failed cutover baseline after renewed review")
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
    if args.rearm and not args.apply:
        ap.error("--rearm requires --apply")
    if not (args.shadow or args.apply or args.status or args.cutover_check):
        args.dry_run = True
    try:
        return run(args)
    except (MonitorError, OSError, ValueError, TypeError, KeyError, RecursionError, json.JSONDecodeError) as exc:
        print(f"learnrise monitor: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
