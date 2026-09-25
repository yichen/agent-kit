#!/usr/bin/env python3
"""Refresh the legacy ledger and run /boss decisions without an LLM in the loop.

This bridge never launches a task, edits a PR, or merges. Unhandled actions are
durably recorded by reconcile_ledger.py and cause a nonzero exit immediately.
Install a real task worker before claiming autonomous execution.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import fcntl
import json
import os
from pathlib import Path
import re
import sqlite3
import subprocess
import sys
import tempfile
from contextlib import nullcontext

import reconcile_ledger as reconcile

UUID = reconcile.TASK_ID
PR_REF = re.compile(r"\b(?:close[sd]?|fix(?:e[sd])?|resolve[sd]?)\s+#([1-9][0-9]*)\b", re.I)
WEAK_REF = re.compile(r"(?:^|\D)#([1-9][0-9]*)(?:\D|$)")
WRITER = re.compile(r"^exec\s+resume\b[^\n]*?--json\s+([0-9a-f-]{36})(?:\s|$)")
UTC = timezone.utc


class BridgeError(ValueError):
    pass


def run(argv: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(argv, capture_output=True, text=True, check=False, timeout=180)


def writer_ids(processes: str) -> set[str]:
    result = set()
    for line in processes.splitlines():
        fields = line.split(None, 2)
        if len(fields) != 3 or not fields[0].isdigit() or Path(fields[1]).name != "codex":
            continue
        match = WRITER.search(fields[2])
        if match and UUID.fullmatch(match.group(1)):
            result.add(match.group(1))
    return result


def rollout_status(path: Path) -> str:
    if not path.is_file():
        raise BridgeError(f"missing Codex rollout: {path}")
    last = None
    with path.open(encoding="utf-8") as stream:
        for line in stream:
            try:
                item = json.loads(line)
            except json.JSONDecodeError as exc:
                # A writer may be appending a partial final line; no safe idle call.
                raise BridgeError(f"malformed Codex rollout: {path}") from exc
            if item.get("type") == "event_msg":
                event = item.get("payload", {}).get("type")
                if event in {"task_started", "task_complete", "turn_aborted"}:
                    last = event
    return {"task_started": "blocked", "task_complete": "completed",
            "turn_aborted": "interrupted"}.get(last, "queued")


def inventory(ledger: dict, db: Path, processes: str, now: datetime) -> dict:
    ids = {row.get("coding_task_id") or row.get("implementation_thread_id") or row.get("active_task_uuid")
           for row in ledger["objectives"]}
    ids.discard(None)
    for tid in ids:
        if not isinstance(tid, str) or not UUID.fullmatch(tid):
            raise BridgeError(f"invalid task ID in ledger: {tid!r}")
    live = writer_ids(processes)
    observations = []
    # Read-only SQLite URI: this must not create a database or WAL file.
    connection = sqlite3.connect(f"file:{db}?mode=ro&immutable=1", uri=True)
    try:
        for tid in sorted(ids):
            result = connection.execute("SELECT rollout_path FROM threads WHERE id=?", (tid,)).fetchone()
            if result is None:
                raise BridgeError(f"task {tid} missing from local Codex catalog; remote/queued state needs host API")
            status = "running" if tid in live else rollout_status(Path(result[0]))
            observations.append({"id": tid, "status": status})
    finally:
        connection.close()
    return {"as_of": now.isoformat().replace("+00:00", "Z"), "tasks": observations}


def discover_open_prs(ledger: dict, prs: list[dict]) -> list[tuple[str, int]]:
    """Only explicit closing syntax can link a PR; weaker hints demand review."""
    for row in ledger["objectives"]:
        linked = row.get("pull_requests", [])
        if not isinstance(linked, list) or any(type(number) is not int or number < 1 for number in linked):
            raise BridgeError(f"{row.get('id')}: malformed linked PR numbers")
    rows = {row.get("issue_number"): row for row in ledger["objectives"]
            if type(row.get("issue_number")) is int and row.get("work_item")}
    additions = []
    alerts = []
    for pr in prs:
        number = pr.get("number")
        if type(number) is not int or number < 1 or pr.get("state") != "OPEN":
            raise BridgeError("malformed open PR inventory")
        body = pr.get("body") or ""
        title = pr.get("title") or ""
        branch = pr.get("headRefName") or ""
        if not all(isinstance(x, str) for x in (body, title, branch)):
            raise BridgeError(f"PR #{number}: malformed text")
        strong = {int(x) for x in PR_REF.findall(body)} & rows.keys()
        weak = {int(x) for x in WEAK_REF.findall(title + " " + branch + " " + body)} & rows.keys()
        already = [row["id"] for row in rows.values() if number in row.get("pull_requests", [])]
        if len(already) > 1:
            raise BridgeError(f"PR #{number}: linked to multiple objectives: {already}")
        if already:
            continue
        if len(strong) == 1:
            issue = next(iter(strong))
            matches = [row for row in ledger["objectives"] if row.get("issue_number") == issue and row.get("work_item")]
            if len(matches) == 1 and matches[0]["id"] == f"#{issue}":
                additions.append((matches[0]["id"], number))
                continue
        # Many existing PRs use "Implements #N" rather than GitHub closing
        # syntax. Require the issue number to agree in both title and the
        # branch's first issue segment; a mere mention in the body is weak.
        title_ids = {int(x) for x in WEAK_REF.findall(title)} & rows.keys()
        branch_ids = {int(x) for x in re.findall(r"(?:^|/)([1-9][0-9]*)(?:-|$)", branch)} & rows.keys()
        if len(title_ids) == len(branch_ids) == 1 and title_ids == branch_ids:
            issue = next(iter(title_ids))
            matches = [row for row in ledger["objectives"] if row.get("issue_number") == issue and row.get("work_item")]
            if len(matches) == 1 and matches[0]["id"] == f"#{issue}":
                additions.append((matches[0]["id"], number))
                continue
        if strong or weak:
            alerts.append(f"PR #{number}: UNLINKED_PR candidate issues {sorted(strong or weak)}; verify exact objective")
    if alerts:
        raise BridgeError("; ".join(alerts))
    return additions


def atomic_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".boss-runtime-", dir=path.parent)
    try:
        with os.fdopen(fd, "w") as stream:
            json.dump(value, stream, indent=2, sort_keys=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def attach_prs(path: Path, original: bytes, ledger: dict, additions: list[tuple[str, int]]) -> None:
    if not additions:
        return
    if path.read_bytes() != original:
        raise BridgeError("ledger changed during PR discovery; retry")
    rows = {row["id"]: row for row in ledger["objectives"]}
    for rid, number in additions:
        prs = rows[rid].setdefault("pull_requests", [])
        if number not in prs:
            prs.append(number)
            prs.sort()
    atomic_json(path, ledger)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--ledger", type=Path, required=True)
    ap.add_argument("--audit", type=Path, required=True)
    ap.add_argument("--state-db", type=Path, default=Path.home() / ".codex/state_5.sqlite")
    ap.add_argument("--outbox", type=Path, required=True)
    ap.add_argument("--tasks", type=Path, required=True)
    ap.add_argument("--hub-task", help="existing Codex monitoring hub UUID; queue unresolved actions")
    ap.add_argument("--prs-file", type=Path, help="offline fixture only")
    ap.add_argument("--processes-file", type=Path, help="offline fixture only")
    ap.add_argument("--skip-audit", action="store_true", help="offline fixture only")
    ap.add_argument("--dry-run", action="store_true", help="no writes, no audit")
    args = ap.parse_args(argv)
    if args.hub_task and not UUID.fullmatch(args.hub_task):
        raise BridgeError("invalid monitoring hub task ID")
    if args.dry_run and not args.skip_audit:
        raise BridgeError("dry-run requires --skip-audit (audit writes ledger)")
    if args.skip_audit and not args.prs_file:
        raise BridgeError("--skip-audit requires offline PR fixture")
    if (args.prs_file or args.processes_file or args.skip_audit) and not args.dry_run:
        raise BridgeError("fixture options require --dry-run")
    lock_path = args.outbox.with_suffix(".bridge.lock")
    if not args.dry_run:
        lock_path.parent.mkdir(parents=True, exist_ok=True)
    with (nullcontext() if args.dry_run else lock_path.open("a+")) as handle:
        if handle is not None:
            fcntl.flock(handle, fcntl.LOCK_EX)
        repo_data = json.loads(args.ledger.read_text())
        repo = repo_data.get("repository")
        if not isinstance(repo, str) or not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repo):
            raise BridgeError("invalid ledger repository")
        if args.prs_file:
            prs = json.loads(args.prs_file.read_text())
        else:
            result = run(["gh", "pr", "list", "--repo", repo, "--state", "open", "--limit", "1000",
                          "--json", "number,state,title,body,headRefName"])
            if result.returncode:
                raise BridgeError(f"GitHub open PR inventory failed: {result.stderr.strip()}")
            prs = json.loads(result.stdout)
        if not isinstance(prs, list) or len(prs) >= 1000:
            raise BridgeError("PR inventory malformed or truncated")
        original = args.ledger.read_bytes()
        additions = discover_open_prs(repo_data, prs)
        if not args.dry_run:
            attach_prs(args.ledger, original, repo_data, additions)
            if not args.skip_audit:
                audited = run([sys.executable, str(args.audit), str(args.ledger)])
                flags = [line[6:] for line in audited.stdout.splitlines() if line.startswith("FLAG: ")]
                known_gaps = bool(flags) and all(re.fullmatch(
                    r"#[1-9][0-9]*(?: PR[1-9][0-9]*)?: OPEN objective has no active (?:owner|heartbeat)",
                    item) for item in flags)
                if audited.returncode not in (0, 1) or (audited.returncode == 1 and not known_gaps):
                    raise BridgeError(f"GitHub audit failed ({audited.returncode}): {audited.stdout[-2000:]} {audited.stderr[-1000:]}")
                if known_gaps:
                    print("boss runtime: audit found owner/heartbeat gaps; continuing with fresh GitHub observations", file=sys.stderr)
        ledger = json.loads(args.ledger.read_text()) if not args.dry_run else repo_data
        if args.processes_file:
            processes = args.processes_file.read_text()
        else:
            process = run(["pgrep", "-fl", "codex exec resume"])
            if process.returncode not in (0, 1):
                raise BridgeError("cannot inspect Codex writer processes")
            processes = process.stdout
        tasks = inventory(ledger, args.state_db, processes, datetime.now(UTC))
        if args.dry_run:
            if additions:
                print(json.dumps({"actions": [], "inventory": tasks, "pr_additions": additions,
                                  "decision_skipped": "new PR links require a fresh audit before deciding"}))
                return 0
            actions, waiting = reconcile.decide(ledger, {item["id"]: item for item in tasks["tasks"]})
            print(json.dumps({"actions": actions, "waiting": waiting, "inventory": tasks, "pr_additions": additions}))
            return 0
        atomic_json(args.tasks, tasks)
        result = run([sys.executable, str(Path(__file__).with_name("reconcile_ledger.py")),
                      "--ledger", str(args.ledger), "--tasks", str(args.tasks), "--outbox", str(args.outbox), "scan"])
        if result.returncode not in (0, 3):
            raise BridgeError(f"reconciler failed: {result.stdout} {result.stderr}")
        report = json.loads(result.stdout)
        print(json.dumps({"inventory": tasks, "pr_additions": additions, **report}, sort_keys=True))
        if report["actions"]:
            if args.hub_task:
                payload = {"kind": "boss_runtime_actions", "repository": repo,
                           "ledger": str(args.ledger), "outbox": str(args.outbox),
                           "actions": report["actions"], "overdue": report["overdue"]}
                queued = run(["codex", "queue", "--thread", args.hub_task,
                              "--message", json.dumps(payload, sort_keys=True)])
                if queued.returncode:
                    raise BridgeError(f"monitoring hub queue failed: {queued.stderr.strip()}")
            print("UNHANDLED boss actions: queued to monitoring hub when configured; "
                  "no verified asynchronous worker can execute these actions without an agent; "
                  "acknowledge exact IDs after tool success", file=sys.stderr)
            return 3
        return result.returncode


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (OSError, ValueError, sqlite3.Error, subprocess.TimeoutExpired) as exc:
        message = f"boss runtime: {exc}"
        print(message, file=sys.stderr)
        # A failed audit or inventory must wake the hub too. This is a fixed
        # diagnostic message; no command text from GitHub is executed.
        if "--dry-run" not in sys.argv and "--hub-task" in sys.argv:
            index = sys.argv.index("--hub-task") + 1
            if index < len(sys.argv) and UUID.fullmatch(sys.argv[index]):
                alert = {"kind": "boss_runtime_failure", "error": message[:1500]}
                queued = run(["codex", "queue", "--thread", sys.argv[index],
                              "--message", json.dumps(alert)])
                if queued.returncode:
                    print(f"boss runtime: hub alert failed: {queued.stderr.strip()}", file=sys.stderr)
        sys.exit(2)
