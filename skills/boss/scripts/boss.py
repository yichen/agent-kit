#!/usr/bin/env python3
"""Host-scoped, one-master-per-repository coordinator state and reports."""
import argparse
import contextlib
import datetime as dt
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile

UTC = dt.timezone.utc
ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")


def fail(message):
    raise ValueError(message)


def now():
    return dt.datetime.now(UTC)


def iso(value):
    return value.isoformat().replace("+00:00", "Z")


def parse_time(value):
    if not isinstance(value, str):
        fail("invalid timestamp")
    try:
        return dt.datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(UTC)
    except ValueError:
        fail("invalid timestamp")


def identifier(value, label="identifier"):
    if not isinstance(value, str) or not ID.fullmatch(value):
        fail(f"invalid {label}")
    return value


def bounded_text(value, label):
    if not isinstance(value, str) or not value.strip() or len(value) > 500 or any(ord(char) < 32 and char not in "\t\n" for char in value):
        fail(f"invalid {label}")
    return value.strip()


def issue_number(value):
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        fail("issue numbers must be positive integers")
    return value


def repo_path(value):
    path = Path(value)
    if not path.is_absolute() or not path.is_dir():
        fail("repo must be an existing absolute directory")
    result = subprocess.run(["git", "-C", str(path), "rev-parse", "--show-toplevel"], capture_output=True, text=True)
    if result.returncode:
        fail("repo is not a Git repository")
    root = Path(result.stdout.strip()).resolve()
    if root != path.resolve():
        fail("repo must name the Git root")
    return str(root)


def state_path(repo):
    base = Path(os.environ.get("AGENTS_ARTIFACTS_ROOT", str(Path.home() / "agents-artifacts"))) / "boss"
    digest = hashlib.sha256(github_repo(repo).lower().encode()).hexdigest()[:20]
    return base / f"{digest}.json"


def github_repo(repo):
    result = subprocess.run(["git", "-C", repo, "remote", "get-url", "origin"], capture_output=True, text=True)
    if result.returncode:
        fail("origin remote is required for GitHub PR inventory")
    remote = result.stdout.strip()
    match = re.fullmatch(r"(?:git@github\.com:|https://github\.com/)([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+?)(?:\.git)?/?", remote)
    if not match:
        fail("origin must be a github.com owner/repository remote")
    return match.group(1).lower()


@contextlib.contextmanager
def locked(path, write=False):
    if write:
        path.parent.mkdir(parents=True, exist_ok=True)
    elif not path.exists():
        fail("boss is not initialized for this repository")
    # Read-only calls never create a lock file or persistent directory.
    handle = open(path.with_suffix(".lock"), "a+") if write else None
    try:
        if handle:
            fcntl.flock(handle, fcntl.LOCK_EX)
        if path.exists():
            state = json.loads(path.read_text())
        else:
            state = None
        yield state
    finally:
        if handle:
            fcntl.flock(handle, fcntl.LOCK_UN)
            handle.close()


def save(path, state):
    fd, temp = tempfile.mkstemp(prefix=".boss-", dir=path.parent)
    try:
        with os.fdopen(fd, "w") as stream:
            json.dump(state, stream, indent=2, sort_keys=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp, path)
    finally:
        if os.path.exists(temp):
            os.unlink(temp)


def require(state, repo):
    if not isinstance(state, dict) or state.get("version") != 1 or state.get("repo") != github_repo(repo):
        fail("missing or invalid boss state")
    return state


def prs(args, repo):
    if args.prs_file:
        data = json.loads(Path(args.prs_file).read_text())
    else:
        command = ["gh", "pr", "list", "--repo", github_repo(repo), "--state", "all", "--limit", "500", "--json", "number,state,createdAt,updatedAt,mergedAt,url,title,headRefOid,mergeable,reviewDecision,statusCheckRollup,isDraft"]
        result = subprocess.run(command, capture_output=True, text=True, timeout=30)
        if result.returncode:
            fail(f"GitHub PR inventory failed: {result.stderr.strip()}")
        data = json.loads(result.stdout)
    if not isinstance(data, list):
        fail("invalid PR inventory")
    if len(data) >= 500:
        fail("PR inventory reached limit; refusing an incomplete report")
    seen = set()
    for pr in data:
        if not isinstance(pr, dict):
            fail("invalid PR inventory")
        number = issue_number(pr.get("number"))
        if number in seen or pr.get("state") not in ("OPEN", "CLOSED", "MERGED"):
            fail("invalid PR inventory")
        seen.add(number)
        if pr.get("mergedAt") is not None:
            parse_time(pr["mergedAt"])
        if pr.get("createdAt") is not None:
            parse_time(pr["createdAt"])
    return data


def pr_actions(repo, pr, linked, instant):
    number = pr["number"]
    head = pr.get("headRefOid")
    valid_head = isinstance(head, str) and bool(re.fullmatch(r"[0-9a-fA-F]{40,64}", head))
    actions = []

    def add(kind, blocker):
        raw = f"{repo}:{number}:{head if valid_head else 'unknown'}:{kind}:{blocker}"
        actions.append({"id": hashlib.sha256(raw.encode()).hexdigest()[:20], "pr": number, "head": head if valid_head else None, "kind": kind, "blocker": blocker})

    if not linked:
        add("unlinked", "no ticket owns this PR")
    if not valid_head:
        add("head_unknown", "current head SHA unavailable")
    mergeable = pr.get("mergeable")
    if mergeable == "CONFLICTING":
        add("conflict", "merge conflict")
    elif mergeable != "MERGEABLE":
        add("mergeability_unknown", "mergeability not verified")
    checks = pr.get("statusCheckRollup")
    if checks is None:
        add("checks_unknown", "current-head checks unavailable")
    elif not isinstance(checks, list):
        fail("invalid PR check inventory")
    elif not checks:
        add("no_checks", "no current-head checks")
    else:
        failed = False
        pending = False
        pending_ages = []
        for check in checks:
            if not isinstance(check, dict):
                fail("invalid PR check inventory")
            conclusion = check.get("conclusion")
            status = check.get("status")
            state = check.get("state")
            if conclusion in ("FAILURE", "CANCELLED", "TIMED_OUT", "ACTION_REQUIRED", "STARTUP_FAILURE") or state in ("FAILURE", "ERROR"):
                failed = True
            elif conclusion in ("SUCCESS", "NEUTRAL", "SKIPPED") or state == "SUCCESS":
                continue
            elif status in ("QUEUED", "IN_PROGRESS", "PENDING", "WAITING", "REQUESTED") or state in ("PENDING", "EXPECTED"):
                pending = True
                stamp = check.get("startedAt") or check.get("createdAt")
                pending_ages.append(parse_time(stamp) if stamp else None)
            else:
                fail("invalid PR check state")
        if failed:
            add("ci_failed", "one or more current-head checks failed")
        if pending:
            stale = any(stamp and instant - stamp > dt.timedelta(minutes=45) for stamp in pending_ages)
            unknown = any(stamp is None for stamp in pending_ages)
            add("ci_stale" if stale else "ci_pending_age_unknown" if unknown else "ci_pending", "current-head checks pending")
    decision = pr.get("reviewDecision")
    if decision == "CHANGES_REQUESTED":
        add("changes_requested", "review changes requested")
    elif decision != "APPROVED":
        add("review_needed", "independent approval not verified")
    return actions


def report(state, inventory, instant):
    tickets = state["tickets"]
    linked = {number: ticket for ticket in tickets.values() for number in ticket["prs"]}
    open_prs = [pr for pr in inventory if pr["state"] == "OPEN"]
    recent_merged = [pr for pr in inventory if pr["state"] == "MERGED" and pr.get("mergedAt") and instant - dt.timedelta(hours=5) <= parse_time(pr["mergedAt"]) <= instant]
    recent_created = [pr for pr in inventory if pr.get("createdAt") and instant - dt.timedelta(hours=5) <= parse_time(pr["createdAt"]) <= instant]
    resolved = [ticket for ticket in tickets.values() if ticket["status"] == "resolved" and parse_time(ticket["resolved_at"]) >= instant - dt.timedelta(hours=24)]
    counts = {kind: sum(1 for pr in recent_merged if linked.get(pr["number"], {}).get("kind") == kind) for kind in ("feature", "testability")}
    counts["unclassified"] = len(recent_merged) - sum(counts.values())
    merged_features = []
    for pr in recent_merged:
        ticket = linked.get(pr["number"])
        if ticket and ticket["kind"] == "feature":
            merged_features.append({"issue": ticket["issue"], "pr": pr["number"], "summary": ticket.get("summary", "unknown"), "availability": ticket.get("availability", "unknown"), "test_environment": ticket.get("test_environment", "unknown"), "human_gate": ticket.get("human_gate", "unknown")})
    actions = [action for pr in open_prs for action in pr_actions(state["repo"], pr, pr["number"] in linked, instant)]
    monitor = state["monitor"]
    last = monitor.get("last_scan_at")
    age = (instant - parse_time(last)).total_seconds() / 60 if last else None
    health = "unconfigured" if not monitor.get("name") else "unverified"
    hourly = []
    for offset in range(5, 0, -1):
        start = instant - dt.timedelta(hours=offset)
        end = start + dt.timedelta(hours=1)
        hourly.append({"start": iso(start), "created": [pr["number"] for pr in recent_created if start <= parse_time(pr["createdAt"]) < end], "merged": [pr["number"] for pr in recent_merged if start <= parse_time(pr["mergedAt"]) < end]})
    waiting = {key: [dep for dep in ticket["depends"] if tickets[str(dep)]["status"] != "resolved"] for key, ticket in tickets.items() if ticket["status"] == "ready"}
    return {
        "repo": state["repo"], "master": state["master"], "hub": state.get("hub"),
        "active_tickets": [ticket for ticket in tickets.values() if ticket["status"] != "resolved"],
        "open_prs": open_prs, "unlinked_open_prs": [pr["number"] for pr in open_prs if pr["number"] not in linked],
        "pr_actions": actions,
        "merged_5h": {"total": len(recent_merged), "numbers": [pr["number"] for pr in recent_merged], **counts},
        "merged_features_5h": merged_features,
        "human_decisions": [{"issue": ticket["issue"], "gate": ticket.get("human_gate", "unknown")} for ticket in tickets.values() if ticket["status"] != "resolved" and ticket.get("human_gate", "unknown") != "unknown"],
        "created_5h": [pr["number"] for pr in recent_created], "hourly_5h": hourly,
        "dependency_waits": {key: deps for key, deps in waiting.items() if deps},
        "resolved_24h": resolved,
        "resolved_actions_24h": [item for item in state.get("action_history", {}).values() if item.get("status") == "resolved" and parse_time(item["resolved_at"]) >= instant - dt.timedelta(hours=24)],
        "monitor": {"name": monitor.get("name"), "last_scan_at": last, "last_scan_age_minutes": age, "health": health},
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--prs-file", help="local test fixture; never use for live reporting")
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("init", "adopt", "status", "scan", "doctor", "handoff", "reconcile"):
        sub.add_parser(name).add_argument("--repo", required=True)
    sub.choices["init"].add_argument("--master", required=True)
    sub.choices["init"].add_argument("--hub")
    sub.choices["adopt"].add_argument("--master", required=True)
    sub.choices["adopt"].add_argument("--from", dest="from_master", required=True)
    ticket = sub.add_parser("ticket")
    tsub = ticket.add_subparsers(dest="action", required=True)
    for name in ("add", "resolve", "pr", "launch", "confirm", "abandon"):
        p = tsub.add_parser(name)
        p.add_argument("--repo", required=True)
        p.add_argument("--issue", type=int, required=True)
    tsub.choices["add"].add_argument("--kind", choices=("feature", "testability"), required=True)
    tsub.choices["add"].add_argument("--depends", default="")
    tsub.choices["add"].add_argument("--summary", default="unknown")
    tsub.choices["add"].add_argument("--availability", choices=("unknown", "testable", "not_testable"), default="unknown")
    tsub.choices["add"].add_argument("--test-environment", default="unknown")
    tsub.choices["add"].add_argument("--human-gate", default="unknown")
    tsub.choices["resolve"].add_argument("--note", required=True)
    tsub.choices["resolve"].add_argument("--pr", type=int, required=True)
    tsub.choices["pr"].add_argument("--pr", type=int, required=True)
    tsub.choices["launch"].add_argument("--adapter", required=True)
    tsub.choices["launch"].add_argument("--apply", action="store_true")
    tsub.choices["launch"].add_argument("--phase")
    tsub.choices["launch"].add_argument("--owner")
    tsub.choices["launch"].add_argument("--generation", type=int)
    tsub.choices["confirm"].add_argument("--action-id", required=True)
    tsub.choices["confirm"].add_argument("--task-id", required=True)
    tsub.choices["confirm"].add_argument("--phase", required=True)
    tsub.choices["confirm"].add_argument("--owner", required=True)
    tsub.choices["confirm"].add_argument("--generation", type=int, required=True)
    tsub.choices["abandon"].add_argument("--action-id", required=True)
    tsub.choices["abandon"].add_argument("--evidence", required=True)
    tsub.choices["abandon"].add_argument("--phase", required=True)
    tsub.choices["abandon"].add_argument("--owner", required=True)
    tsub.choices["abandon"].add_argument("--generation", type=int, required=True)
    tsub.choices["abandon"].add_argument("--inventory", required=True)
    monitor = sub.add_parser("monitor")
    msub = monitor.add_subparsers(dest="action", required=True)
    msub.add_parser("set").add_argument("--repo", required=True)
    msub.choices["set"].add_argument("--name", required=True)
    args = parser.parse_args()
    if args.prs_file and os.environ.get("BOSS_TEST_MODE") != "1":
        fail("--prs-file is available only in explicit local test mode")
    repo = repo_path(args.repo)
    path = state_path(repo)
    if args.command == "init":
        master = identifier(args.master, "master")
        hub = identifier(args.hub, "hub") if args.hub else None
        with locked(path, write=True) as state:
            if state:
                require(state, repo)
                if state["master"] != master or (hub and state.get("hub") not in (None, hub)):
                    fail("repository already has a different boss master or hub")
                if hub and not state.get("hub"):
                    state["hub"] = hub
                    save(path, state)
            else:
                state = {"version": 1, "repo": github_repo(repo), "repo_path": repo, "master": master, "hub": hub, "tickets": {}, "action_history": {}, "monitor": {"name": None, "last_scan_at": None}, "created_at": iso(now())}
                save(path, state)
        print(json.dumps({"repo": github_repo(repo), "master": master, "state": str(path)}))
        return
    if args.command == "adopt":
        master = identifier(args.master, "master")
        old = identifier(args.from_master, "old master")
        with locked(path, write=True) as state:
            require(state, repo)
            if state["master"] != old or master == old:
                fail("master transfer precondition failed")
            state["master"] = master
            state.setdefault("adoptions", []).append({"from": old, "to": master, "at": iso(now())})
            save(path, state)
        print(json.dumps({"master": master}))
        return
    if args.command == "ticket":
        number = issue_number(args.issue)
        if args.action == "launch" and not args.apply:
            with locked(path) as state:
                require(state, repo)
                result = launch_eligibility(state, number)
            print(json.dumps(result))
            return
        with locked(path, write=True) as state:
            require(state, repo)
            tickets = state["tickets"]
            key = str(number)
            if args.action == "add":
                summary = bounded_text(args.summary, "summary")
                environment = bounded_text(args.test_environment, "test environment")
                gate = bounded_text(args.human_gate, "human gate")
                deps = [] if not args.depends else [issue_number(int(item)) for item in args.depends.split(",") if item.isdecimal()]
                if args.depends and len(deps) != len(args.depends.split(",")):
                    fail("invalid dependency list")
                if len(deps) != len(set(deps)) or number in deps:
                    fail("duplicate or self dependency")
                if key in tickets:
                    if any((tickets[key]["kind"] != args.kind, tickets[key]["depends"] != deps, tickets[key].get("summary", "unknown") != summary, tickets[key].get("availability", "unknown") != args.availability, tickets[key].get("test_environment", "unknown") != environment, tickets[key].get("human_gate", "unknown") != gate)):
                        fail("ticket already exists with different fields")
                else:
                    if any(str(dep) not in tickets for dep in deps):
                        fail("register dependencies before dependent tickets")
                    tickets[key] = {"issue": number, "kind": args.kind, "depends": deps, "summary": summary, "availability": args.availability, "test_environment": environment, "human_gate": gate, "status": "ready", "prs": [], "created_at": iso(now())}
                    save(path, state)
            elif args.action == "resolve":
                if key not in tickets:
                    fail("unknown ticket")
                if tickets[key]["status"] != "resolved":
                    pr = issue_number(args.pr)
                    if pr not in tickets[key]["prs"] or not any(item["number"] == pr and item["state"] == "MERGED" for item in prs(args, repo)):
                        fail("resolution requires a linked merged PR")
                    note = args.note.strip()
                    if not note or len(note) > 500:
                        fail("note must be 1-500 characters")
                    tickets[key].update(status="resolved", resolved_at=iso(now()), resolution=note, resolution_pr=pr)
                    save(path, state)
            elif args.action == "pr":
                pr = issue_number(args.pr)
                if key not in tickets:
                    fail("unknown ticket")
                if any(pr in item["prs"] for other, item in tickets.items() if other != key):
                    fail("PR already linked to another ticket")
                if pr not in tickets[key]["prs"]:
                    tickets[key]["prs"].append(pr)
                    save(path, state)
            elif args.action == "confirm":
                if key not in tickets:
                    fail("launch confirmation precondition failed")
                task_id = identifier(args.task_id, "task ID")
                ticket = tickets[key]
                if ticket["status"] == "launched":
                    if ticket.get("action_id") != args.action_id or ticket.get("task_id") != task_id:
                        fail("launch confirmation precondition failed")
                    operational_store(repo, "ack", "--issue", str(number), "--phase", args.phase, "--owner", args.owner, "--generation", str(args.generation), "--action-id", args.action_id, "--task-id", task_id)
                elif ticket["status"] in ("ready", "launching") and ticket.get("action_id") in (None, args.action_id):
                    operational_store(repo, "ack", "--issue", str(number), "--phase", args.phase, "--owner", args.owner, "--generation", str(args.generation), "--action-id", args.action_id, "--task-id", task_id)
                    ticket.update(status="launched", action_id=args.action_id, task_id=task_id, launched_at=iso(now()))
                    save(path, state)
                else:
                    fail("launch confirmation precondition failed")
            elif args.action == "abandon":
                if key not in tickets or (tickets[key]["status"] == "launching" and tickets[key].get("action_id") != args.action_id) or tickets[key]["status"] not in ("ready", "launching"):
                    fail("launch abandonment precondition failed")
                evidence = args.evidence.strip()
                if len(evidence) < 10 or len(evidence) > 500:
                    fail("abandonment evidence must be 10-500 characters")
                operational_store(repo, "abandon", "--issue", str(number), "--phase", args.phase, "--owner", args.owner, "--generation", str(args.generation), "--action-id", args.action_id, "--inventory", args.inventory, "--evidence", evidence)
                tickets[key].update(status="ready", launch_abandoned_at=iso(now()), launch_abandonment=evidence)
                tickets[key].pop("action_id", None)
                save(path, state)
            else:
                launch_eligibility(state, number)
                adapter = Path(args.adapter)
                if not adapter.is_absolute() or not adapter.is_file() or not os.access(adapter, os.X_OK):
                    fail("adapter must be an absolute executable file")
                if not args.phase or not args.owner or not args.generation or args.generation <= 0:
                    fail("ticket launch --apply requires --phase, --owner, and the active claim --generation")
                launched = operational_store(repo, "dispatch", "--issue", str(number), "--phase", args.phase, "--owner", args.owner, "--generation", str(args.generation), "--verb", "launch", "--adapter", str(adapter), "--kind", tickets[key]["kind"], "--master", state["master"])
                tickets[key].update(status="launched", action_id=launched["action_id"], task_id=launched["task_id"], launched_at=iso(now()))
                save(path, state)
        print(json.dumps({"issue": number, "status": tickets[key]["status"]}))
        return
    if args.command == "monitor":
        name = identifier(args.name, "monitor name")
        with locked(path, write=True) as state:
            require(state, repo)
            state["monitor"]["name"] = name
            save(path, state)
        print(json.dumps({"monitor": name}))
        return
    with locked(path, write=args.command == "scan") as state:
        require(state, repo)
        inventory = prs(args, repo)
        if args.command == "scan":
            instant = now()
            current = report(state, inventory, instant)["pr_actions"]
            current_ids = {item["id"] for item in current}
            history = state.setdefault("action_history", {})
            for item in current:
                record = history.setdefault(item["id"], {**item, "status": "open", "first_seen_at": iso(instant)})
                record.update(status="open", last_seen_at=iso(instant))
                record.pop("resolved_at", None)
            for action_id, record in history.items():
                if record["status"] == "open" and action_id not in current_ids:
                    record.update(status="resolved", resolved_at=iso(instant))
            state["monitor"]["last_scan_at"] = iso(instant)
            save(path, state)
        result = report(state, inventory, now())
    if args.command == "doctor":
        result = {"repo": github_repo(repo), "monitor": result["monitor"], "pr_actions": result["pr_actions"], "healthy": result["monitor"]["health"] == "healthy" and not result["pr_actions"]}
    if args.command == "reconcile":
        result = {"repo": github_repo(repo), "master": state["master"], "ready": [item["issue"] for item in result["active_tickets"] if item["status"] == "ready" and str(item["issue"]) not in result["dependency_waits"]], "waiting": result["dependency_waits"], "launching": [item["issue"] for item in result["active_tickets"] if item["status"] == "launching"], "launched": [item["issue"] for item in result["active_tickets"] if item["status"] == "launched"], "pr_actions": result["pr_actions"], "monitor": result["monitor"]}
    if args.command == "handoff":
        result = {"hub": result["hub"], "repo": github_repo(repo), "master": result["master"], "active_issues": [item["issue"] for item in result["active_tickets"]], "open_prs": [item["number"] for item in result["open_prs"]], "pr_actions": result["pr_actions"], "monitor": result["monitor"]}
    print(json.dumps(result, indent=2, sort_keys=True))


def launch_eligibility(state, number):
    ticket = state["tickets"].get(str(number))
    if not ticket:
        fail("unknown ticket")
    if ticket["status"] == "launching":
        fail("ticket already has a launch reservation; reconcile by action ID before any retry")
    if ticket["status"] != "ready":
        fail("ticket is already launched or resolved")
    blocked = [dep for dep in ticket["depends"] if state["tickets"][str(dep)]["status"] != "resolved"]
    if blocked:
        fail(f"unresolved dependencies: {blocked}")
    return {"issue": number, "eligible": True, "will_launch": False}


def operational_store(repo, *arguments):
    script = Path(__file__).with_name("operational_store.py")
    result = subprocess.run([sys.executable, str(script), *arguments, "--repo", repo], capture_output=True, text=True, timeout=120)
    if result.returncode:
        fail(result.stderr.strip()[:500] or "operational store command failed")
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError:
        fail("operational store returned invalid output")


if __name__ == "__main__":
    try:
        main()
    except (ValueError, OSError, subprocess.TimeoutExpired, json.JSONDecodeError) as exc:
        print(f"boss: {exc}", file=sys.stderr)
        sys.exit(2)
