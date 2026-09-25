#!/usr/bin/env python3
"""Read-only shadow comparison for the agent-kit /boss scheduler.

The scheduler inventories only the configured agent-kit repository, its local
/boss ledger and claims, GitHub, and live Codex task state. It never creates or
resumes tasks, acquires claims, changes GitHub, or edits source ledgers. It is
no-write by default; an explicit flag opts into writing only the last-success
receipt used by the independent watchdog.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import subprocess
import sys
import tempfile

sys.dont_write_bytecode = True
from codex_worker_adapter import AppServer, task_status

UTC = timezone.utc
TASK_ID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
MAX_AGE = timedelta(minutes=20)


class SchedulerError(ValueError):
    pass


def timestamp(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            raise ValueError("timezone missing")
        return parsed.astimezone(UTC)
    except (AttributeError, ValueError) as exc:
        raise SchedulerError("invalid observation timestamp") from exc


def run_json(command: list[str], *, runner=subprocess.run):
    try:
        result = runner(command, capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise SchedulerError(f"{Path(command[0]).name} inventory failed: {exc}") from exc
    if result.returncode:
        raise SchedulerError(f"{Path(command[0]).name} inventory failed: {result.stderr.strip()[:500]}")
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise SchedulerError(f"{Path(command[0]).name} returned invalid JSON") from exc


def repo_identity(repo_path: Path) -> str:
    if not repo_path.is_absolute() or not repo_path.is_dir():
        raise SchedulerError("repo must be an existing absolute Git root")
    result = subprocess.run(["git", "-C", str(repo_path), "rev-parse", "--show-toplevel"],
                            capture_output=True, text=True, timeout=10)
    if result.returncode or Path(result.stdout.strip()).resolve() != repo_path.resolve():
        raise SchedulerError("repo must name the Git root")
    remote = subprocess.run(["git", "-C", str(repo_path), "remote", "get-url", "origin"],
                            capture_output=True, text=True, timeout=10)
    if remote.returncode:
        raise SchedulerError("origin remote unavailable")
    match = re.fullmatch(r"(?:git@github\.com:|https://github\.com/)([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+?)(?:\.git)?/?", remote.stdout.strip())
    if not match or match.group(1).lower() != "yichen/agent-kit":
        raise SchedulerError("this scheduler is scoped to yichen/agent-kit")
    return match.group(1).lower()


def boss_state_path(repo: str) -> Path:
    digest = hashlib.sha256(repo.lower().encode()).hexdigest()[:20]
    return Path(os.environ.get("AGENTS_ARTIFACTS_ROOT", str(Path.home() / "agents-artifacts"))) / "boss" / f"{digest}.json"


def read_boss_state(path: Path, repo: str, now: datetime) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise SchedulerError(f"agent-kit /boss ledger unavailable or malformed: {exc}") from exc
    if not isinstance(data, dict) or data.get("version") != 1 or data.get("repo") != repo:
        raise SchedulerError("agent-kit /boss ledger identity is invalid")
    monitor = data.get("monitor")
    if not isinstance(monitor, dict) or not monitor.get("name") or not monitor.get("last_scan_at"):
        raise SchedulerError("agent-kit /boss ledger has no prior scan")
    age = now - timestamp(monitor["last_scan_at"])
    if age < -timedelta(minutes=2):
        raise SchedulerError("agent-kit /boss scan timestamp is in the future")
    tickets = data.get("tickets")
    if not isinstance(tickets, dict):
        raise SchedulerError("agent-kit /boss ticket ledger is malformed")
    return data


def gh_inventory(repo: str, *, runner=subprocess.run) -> tuple[list[dict], list[dict]]:
    if repo != "yichen/agent-kit":
        raise SchedulerError("this scheduler is scoped to yichen/agent-kit")
    issues = run_json(["gh", "issue", "list", "--repo", repo, "--state", "all", "--limit", "1000",
                       "--json", "number,state,updatedAt"], runner=runner)
    prs = run_json(["gh", "pr", "list", "--repo", repo, "--state", "all", "--limit", "1000",
                    "--json", "number,state,headRefOid,updatedAt"], runner=runner)
    if not isinstance(issues, list) or len(issues) >= 1000 or not isinstance(prs, list) or len(prs) >= 1000:
        raise SchedulerError("GitHub inventory is malformed or truncated")
    for rows, states, label in ((issues, {"OPEN", "CLOSED"}, "issue"), (prs, {"OPEN", "CLOSED", "MERGED"}, "PR")):
        for row in rows:
            if (not isinstance(row, dict) or type(row.get("number")) is not int or row["number"] < 1
                    or row.get("state") not in states):
                raise SchedulerError(f"GitHub {label} inventory contains an invalid row")
    return issues, prs


def live_tasks(task_ids: set[str], repo_path: Path, *, server_factory=AppServer) -> dict:
    if any(not isinstance(task_id, str) or not TASK_ID.fullmatch(task_id) for task_id in task_ids):
        raise SchedulerError("agent-kit ledger contains an invalid task ID")
    try:
        with server_factory() as server:
            summaries = {}
            for item in server.threads():
                if not isinstance(item, dict) or not isinstance(item.get("id"), str):
                    raise SchedulerError("live Codex inventory contains a malformed thread")
                if item["id"] in summaries:
                    raise SchedulerError("live Codex inventory contains a duplicate thread ID")
                summaries[item["id"]] = item
            result, relevant = {}, []
            issue_name = re.compile(r"^agent-kit\s+#([1-9][0-9]*)(?:\s|:)", re.I)
            issue_cwd = re.compile(r"^agent-kit-issue-([1-9][0-9]*)-[a-f0-9]{8}$")
            for task_id in sorted(task_ids):
                if task_id not in summaries:
                    result[task_id] = "unknown"
            for task_id, summary in summaries.items():
                marker = summary.get("threadSource")
                marker_id = marker.removeprefix("agent-kit:launch:") if isinstance(marker, str) and marker.startswith("agent-kit:launch:") else None
                name = summary.get("name")
                name_match = issue_name.search(name) if isinstance(name, str) else None
                cwd = summary.get("cwd")
                cwd_path = Path(cwd) if isinstance(cwd, str) and cwd else None
                cwd_match = issue_cwd.fullmatch(cwd_path.name) if cwd_path else None
                checkout_match = bool(cwd_path and (cwd_path == repo_path.resolve()
                                                     or cwd_path.name.startswith("agent-kit-issue-")))
                if task_id not in task_ids and not marker_id and not name_match and not cwd_match and not checkout_match:
                    continue
                thread = server.read(task_id)
                status = task_status(thread)
                result[task_id] = status
                relevant.append({"id": task_id, "status": status, "action_id": marker_id,
                                 "issue": int(name_match.group(1)) if name_match else int(cwd_match.group(1)) if cwd_match else None,
                                 "cwd": cwd or thread.get("cwd")})
            return {"by_id": result, "relevant": relevant, "all_live_threads": len(summaries)}
    except Exception as exc:
        raise SchedulerError(f"live Codex inventory unavailable: {str(exc)[:500]}") from exc


def read_claims(db_path: Path, repo: str) -> list[dict]:
    if not db_path.is_file():
        raise SchedulerError("operational claim store is unavailable")
    uri = "file:" + str(db_path.resolve()) + "?mode=ro"
    try:
        connection = sqlite3.connect(uri, uri=True, timeout=5)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA query_only=ON")
        rows = connection.execute("""
            SELECT c.repo,c.issue,c.phase,c.generation,c.owner,c.status,c.acquired_at,
                   COALESCE(GROUP_CONCAT(DISTINCT a.action_id),'') AS action_ids,
                   COALESCE(GROUP_CONCAT(DISTINCT a.task_id),'') AS task_ids
              FROM claims c LEFT JOIN actions a
                ON a.repo=c.repo AND a.issue=c.issue AND a.phase=c.phase
               AND a.generation=c.generation AND a.owner=c.owner
             WHERE c.repo=? AND c.status='active'
             GROUP BY c.repo,c.issue,c.phase,c.generation,c.owner,c.status,c.acquired_at
             ORDER BY c.issue,c.phase
        """, (repo.lower(),)).fetchall()
        return [dict(row) for row in rows]
    except sqlite3.Error as exc:
        raise SchedulerError(f"operational claim inventory unavailable: {exc}") from exc
    finally:
        if "connection" in locals():
            connection.close()


def compare(state: dict, issues: list[dict], prs: list[dict], tasks: dict, claims: list[dict], now: datetime) -> dict:
    task_map = tasks.get("by_id", tasks)
    issue_map = {row.get("number"): row for row in issues if isinstance(row, dict)}
    pr_map = {row.get("number"): row for row in prs if isinstance(row, dict)}
    if len(issue_map) != len(issues) or len(pr_map) != len(prs):
        raise SchedulerError("duplicate or malformed GitHub inventory rows")
    disputes = []
    known_tasks = set()
    active_claims = {}
    for claim in claims:
        active_claims.setdefault(claim["issue"], []).append(claim)
    for issue, ticket in state["tickets"].items():
        number = ticket.get("issue")
        live_issue = issue_map.get(number)
        if live_issue is None:
            disputes.append({"kind": "issue_missing", "issue": number})
        elif ticket.get("status") == "resolved" and live_issue.get("state") != "CLOSED":
            disputes.append({"kind": "ticket_state", "issue": number, "ledger": ticket.get("status"), "github": live_issue.get("state")})
        elif ticket.get("status") in ("ready", "launching", "launched") and live_issue.get("state") != "OPEN":
            disputes.append({"kind": "ticket_state", "issue": number, "ledger": ticket.get("status"), "github": live_issue.get("state")})
        for pr_number in ticket.get("prs", []):
            live_pr = pr_map.get(pr_number)
            if live_pr is None:
                disputes.append({"kind": "pr_missing", "issue": number, "pr": pr_number})
            elif ticket.get("status") == "resolved" and live_pr.get("state") != "MERGED":
                disputes.append({"kind": "resolution_pr_state", "issue": number, "pr": pr_number, "github": live_pr.get("state")})
        task_id = ticket.get("task_id")
        if task_id:
            known_tasks.add(task_id)
            task_state = task_map.get(task_id, "unknown")
            if task_state == "unknown":
                disputes.append({"kind": "task_missing", "issue": number, "task_id": task_id})
            if ticket.get("status") == "launched" and task_state in ("completed", "interrupted"):
                disputes.append({"kind": "task_terminal", "issue": number, "task_id": task_id, "live": task_state})
        active = active_claims.get(number, [])
        if ticket.get("status") in ("launching", "launched") and len(active) != 1:
            disputes.append({"kind": "claim_count", "issue": number, "expected": 1, "actual": len(active)})
        if ticket.get("status") in ("ready", "resolved") and active:
            disputes.append({"kind": "unexpected_active_claim", "issue": number, "ticket_status": ticket.get("status")})
        if len(active) > 1:
            disputes.append({"kind": "multiple_active_claims", "issue": number,
                             "phases": [item["phase"] for item in active]})
        for claim in active:
            if ticket.get("task_id") and ticket["task_id"] not in (claim.get("task_ids") or "").split(","):
                disputes.append({"kind": "claim_task_mismatch", "issue": number,
                                 "claim_generation": claim["generation"], "task_id": ticket["task_id"]})
            if ticket.get("action_id") and ticket["action_id"] not in (claim.get("action_ids") or "").split(","):
                disputes.append({"kind": "claim_action_mismatch", "issue": number,
                                 "claim_generation": claim["generation"], "action_id": ticket["action_id"]})
    for issue, active in active_claims.items():
        if str(issue) not in state["tickets"]:
            disputes.append({"kind": "untracked_active_claim", "issue": issue,
                             "phases": [row["phase"] for row in active]})
    ticket_by_issue = {ticket.get("issue"): ticket for ticket in state["tickets"].values()}
    task_owners = {ticket.get("task_id"): ticket.get("issue") for ticket in state["tickets"].values()
                   if ticket.get("task_id")}
    action_owners = {ticket.get("action_id"): ticket.get("issue") for ticket in state["tickets"].values()
                     if ticket.get("action_id")}
    for row in tasks.get("relevant", []):
        if row.get("status") not in {"queued", "running", "blocked", "interrupted"}:
            continue
        issue = row.get("issue") or action_owners.get(row.get("action_id")) or task_owners.get(row["id"])
        ticket = ticket_by_issue.get(issue)
        if ticket is None:
            disputes.append({"kind": "unmatched_live_task", "task_id": row["id"], "issue": issue,
                             "status": row["status"]})
        elif ticket.get("task_id") != row["id"]:
            disputes.append({"kind": "duplicate_live_task", "issue": issue,
                             "expected_task_id": ticket.get("task_id"), "task_id": row["id"],
                             "status": row["status"]})
        elif row.get("action_id") and ticket.get("action_id") and row["action_id"] != ticket["action_id"]:
            disputes.append({"kind": "live_task_action_mismatch", "issue": issue,
                             "task_id": row["id"], "action_id": row["action_id"]})
    all_prs = [row for row in prs if row.get("state") == "OPEN"]
    pr_owners = {}
    for ticket in state["tickets"].values():
        for pr_number in ticket.get("prs", []):
            pr_owners.setdefault(pr_number, []).append(ticket.get("issue"))
    for pr_number, owners in pr_owners.items():
        if len(owners) > 1:
            disputes.append({"kind": "multiple_pr_owners", "pr": pr_number, "issues": owners})
    tracked_prs = set(pr_owners)
    for pr in all_prs:
        if pr.get("number") not in tracked_prs:
            disputes.append({"kind": "untracked_open_pr", "pr": pr.get("number"), "head": pr.get("headRefOid")})
    pilot_candidates = []
    for issue, ticket in state["tickets"].items():
        if ticket.get("status") != "ready":
            continue
        if all(state["tickets"].get(str(dep), {}).get("status") == "resolved" for dep in ticket.get("depends", [])):
            if issue_map.get(ticket.get("issue"), {}).get("state") == "OPEN":
                pilot_candidates.append(ticket.get("issue"))
    return {"as_of": now.isoformat().replace("+00:00", "Z"), "repository": "yichen/agent-kit",
            "mode": "shadow", "assignment_enabled": False, "pi_dispatch_enabled": False,
            "pi_dispatch": "disabled; issue #20 remains gated and unlaunched",
            "live_task_scope": "all app-server summaries counted; task details read for ledger IDs and agent-kit action/name/worktree matches; active unmatched matches are disputes",
            "ledger_scan_age_minutes": max(0, round((now - timestamp(state["monitor"]["last_scan_at"])).total_seconds() / 60, 1)),
            "ledger_scan_stale": now - timestamp(state["monitor"]["last_scan_at"]) > MAX_AGE,
            "inventory": {"issues": len(issues), "open_issues": sum(row.get("state") == "OPEN" for row in issues),
                          "pull_requests": len(prs), "open_pull_requests": len(all_prs),
                          "tickets": len(state["tickets"]), "ledger_task_ids": len(known_tasks),
                          "live_task_observations": len(task_map), "agent_kit_live_tasks": len(tasks.get("relevant", [])),
                          "all_live_threads": tasks.get("all_live_threads", len(task_map)), "active_claims": len(claims),
                          "remaining_claim_capacity": max(0, 1 - len(claims))},
            "pilot_candidates": pilot_candidates,
            "pilot_blocker": None if pilot_candidates else "no dependency-ready tracked issue is available for low-risk pilot selection",
            "cutover_requirements": ["resolved ownership disputes", "all canaries pass", "one safe pilot completes", "human/product acceptance recorded"],
            "disputes": disputes, "disputed": bool(disputes), "eligible_for_cutover": False}


def atomic_receipt(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp = tempfile.mkstemp(prefix=".boss-last-success-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(value, stream, sort_keys=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp, path)
    finally:
        if os.path.exists(temp):
            os.unlink(temp)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", type=Path, required=True)
    parser.add_argument("--operations-db", type=Path, default=Path.home() / "agents-artifacts/boss/operations.sqlite3")
    parser.add_argument("--last-success", type=Path, required=True)
    parser.add_argument("--write-last-success", action="store_true",
                        help="explicitly persist the successful comparison receipt for the independent watchdog")
    args = parser.parse_args(argv)
    now = datetime.now(UTC)
    try:
        repo = repo_identity(args.repo)
        state = read_boss_state(boss_state_path(repo), repo, now)
        issues, prs = gh_inventory(repo)
        task_ids = {ticket.get("task_id") for ticket in state["tickets"].values() if ticket.get("task_id")}
        tasks = live_tasks(task_ids, args.repo.resolve())
        claims = read_claims(args.operations_db, repo)
        result = compare(state, issues, prs, tasks, claims, now)
        if args.write_last_success:
            atomic_receipt(args.last_success, result)
        print(json.dumps(result, sort_keys=True))
        return 0 if not result["disputed"] else 3
    except (SchedulerError, OSError, sqlite3.Error, subprocess.TimeoutExpired) as exc:
        print(f"boss scheduler: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
