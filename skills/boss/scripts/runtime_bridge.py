#!/usr/bin/env python3
"""Audit an isolated ledger snapshot and run /boss decisions without an LLM in the loop.

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
import shutil
import sqlite3
import subprocess
import sys
import tempfile
from contextlib import nullcontext
from urllib.parse import quote

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


def parse_time(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            raise ValueError("timezone missing")
        return parsed.astimezone(UTC)
    except (AttributeError, ValueError) as exc:
        raise BridgeError("malformed review timestamp") from exc


def open_pr_inventory_command(repo: str) -> list[str]:
    if (not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repo)
            or any(part in {".", ".."} for part in repo.split("/"))):
        raise BridgeError("invalid ledger repository")
    return ["gh", "pr", "list", "--repo", repo, "--state", "open", "--limit", "1000",
            "--json", "number,state,title,body,headRefName,headRefOid,baseRefName,mergeable,author,reviewDecision"]


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
            if not isinstance(item, dict):
                raise BridgeError(f"malformed Codex rollout: {path}")
            if item.get("type") == "event_msg":
                payload = item.get("payload")
                if not isinstance(payload, dict):
                    raise BridgeError(f"malformed Codex rollout: {path}")
                event = payload.get("type")
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
    # `immutable=1` silently ignores committed rows still in the live WAL.
    # Query a private, short-lived copy so SQLite can read the WAL without
    # creating or changing sidecar files in the host Codex catalog directory.
    def signature(path: Path) -> tuple[int, int, int, int] | None:
        try:
            stat = path.stat()
        except FileNotFoundError:
            return None
        return stat.st_ino, stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns

    wal = Path(f"{db}-wal")
    before = (signature(db), signature(wal))
    if before[0] is None:
        raise BridgeError(f"missing local Codex catalog: {db}")
    with tempfile.TemporaryDirectory(prefix="boss-catalog-") as temporary:
        snapshot = Path(temporary) / "catalog.sqlite"
        shutil.copyfile(db, snapshot)
        if before[1] is not None:
            shutil.copyfile(wal, Path(f"{snapshot}-wal"))
        if (signature(db), signature(wal)) != before:
            raise BridgeError("local Codex catalog changed during snapshot; retry next scan")
        connection = sqlite3.connect(f"file:{snapshot}?mode=ro", uri=True)
        try:
            connection.execute("PRAGMA query_only=ON")
            for tid in sorted(ids):
                result = connection.execute("SELECT rollout_path FROM threads WHERE id=?", (tid,)).fetchone()
                if result is None:
                    raise BridgeError(f"task {tid} missing from local Codex catalog; remote/queued state needs host API")
                rollout_path = result[0]
                if not isinstance(rollout_path, str) or not rollout_path:
                    raise BridgeError(f"task {tid} has invalid rollout path")
                status = "running" if tid in live else rollout_status(Path(rollout_path))
                observations.append({"id": tid, "status": status})
        finally:
            connection.close()
    return {"as_of": now.isoformat().replace("+00:00", "Z"), "tasks": observations}


def discover_open_prs(ledger: dict, prs: list[dict]) -> list[tuple[str, int]]:
    """Only explicit closing syntax can link a PR; weaker hints demand review."""
    if not isinstance(ledger, dict) or not isinstance(ledger.get("objectives"), list) or any(
        not isinstance(row, dict) for row in ledger["objectives"]
    ):
        raise BridgeError("malformed ownership objectives")
    ids = [row.get("id") for row in ledger["objectives"]]
    if any(not isinstance(rid, str) or not rid for rid in ids) or len(set(ids)) != len(ids):
        raise BridgeError("malformed or duplicate objective IDs")
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
        title_ids = {int(x) for x in WEAK_REF.findall(title)} & rows.keys()
        branch_ids = {int(x) for x in re.findall(r"(?:^|/)([1-9][0-9]*)(?:-|$)", branch)} & rows.keys()
        already = [row for row in ledger["objectives"] if number in row.get("pull_requests", [])]
        if len(already) > 1:
            raise BridgeError(f"PR #{number}: linked to multiple objectives: {[row['id'] for row in already]}")
        if already:
            linked_issue = already[0].get("issue_number")
            if (strong and strong != {linked_issue}) or ((title_ids | branch_ids) - {linked_issue}):
                alerts.append(f"PR #{number}: live issue references conflict with linked objective {already[0]['id']}")
            continue
        if len(strong) > 1:
            alerts.append(f"PR #{number}: multiple tracked closing references {sorted(strong)}")
            continue
        if len(strong) == 1:
            if (title_ids | branch_ids) - strong:
                alerts.append(f"PR #{number}: conflicting tracked issue references in title, branch, and closing text")
                continue
            issue = next(iter(strong))
            matches = [row for row in ledger["objectives"] if row.get("issue_number") == issue and row.get("work_item")]
            if len(matches) == 1 and matches[0]["id"] == f"#{issue}":
                additions.append((matches[0]["id"], number))
                continue
        # Many existing PRs use "Implements #N" rather than GitHub closing
        # syntax. Require the issue number to agree in both title and the
        # branch's first issue segment; a mere mention in the body is weak.
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


def classify_open_prs(ledger: dict, prs: list[dict]) -> tuple[list[tuple[str, int]], list[dict]]:
    """Link only unambiguous PRs; preserve every other open PR as quarantine."""
    discover_open_prs(ledger, [])  # validate ownership data before classifying per-PR ambiguity
    additions, quarantined = [], []
    seen = set()
    if not isinstance(prs, list):
        raise BridgeError("malformed open PR inventory")
    for pr in prs:
        if not isinstance(pr, dict) or type(pr.get("number")) is not int or pr["number"] < 1 or pr.get("state") != "OPEN":
            raise BridgeError("malformed open PR inventory")
        number = pr["number"]
        if number in seen:
            raise BridgeError("duplicate open PR inventory item")
        seen.add(number)
        try:
            linked = discover_open_prs(ledger, [pr])
        except BridgeError as exc:
            tracked = {row.get("issue_number") for row in ledger["objectives"]
                       if row.get("work_item") and type(row.get("issue_number")) is int}
            text = " ".join(pr.get(key) or "" for key in ("body", "title", "headRefName"))
            candidates = sorted({int(value) for value in WEAK_REF.findall(text)} & tracked)
            quarantined.append({"number": number, "reason": str(exc),
                                "candidate_objectives": sorted(row["id"] for row in ledger["objectives"]
                                                               if row.get("work_item") and row.get("issue_number") in candidates)})
            continue
        if linked:
            additions.extend(linked)
            continue
        matches = [row for row in ledger["objectives"] if number in row.get("pull_requests", [])]
        if len(matches) == 1:
            # discover_open_prs succeeded and confirmed any live references agree.
            continue
        elif len(matches) > 1:
            quarantined.append({"number": number, "reason": "PR is linked to multiple objectives",
                                "candidate_objectives": [row["id"] for row in matches]})
        else:
            quarantined.append({"number": number, "reason": "open PR has no unambiguous tracked issue owner"})
    return additions, quarantined


def current_pr_observations(repo: str, ledger: dict, prs: list[dict], additions: list[tuple[str, int]], quarantined: list[dict], *, fixtures: bool) -> list[dict]:
    owners = {number: objective for objective, number in additions}
    for row in ledger["objectives"]:
        for number in row.get("pull_requests", []):
            if number in {pr["number"] for pr in prs}:
                owners.setdefault(number, row["id"])
    quarantined_by_number = {item["number"]: item for item in quarantined}
    observations = []
    for pr in prs:
        number = pr["number"]
        head = pr.get("headRefOid")
        if fixtures:
            required = pr.get("requiredChecks")
            raw_checks = pr.get("requiredCheckObservations")
            raw_reviews = pr.get("reviews", [])
            mergeable = pr.get("mergeable")
            review_decision = pr.get("reviewDecision")
        else:
            if not isinstance(head, str) or not re.fullmatch(r"[0-9a-f]{40}", head):
                raise BridgeError(f"PR #{number}: current head missing")
            result = run(["gh", "pr", "checks", "--repo", repo, str(number), "--required",
                          "--json", "name,state,startedAt"])
            no_required_reported = (result.returncode == 1 and
                                    re.fullmatch(r"no required checks reported on the '.+' branch\s*", result.stderr.strip()) is not None)
            if no_required_reported:
                # Preserve the PR and its ordinary check state even when no
                # required-check policy is configured. The independent policy
                # lookup below will leave required empty and block verification.
                result = run(["gh", "pr", "checks", "--repo", repo, str(number),
                              "--json", "name,state,startedAt"])
            if result.returncode not in (0, 8):
                raise BridgeError(f"PR #{number}: required check inventory failed: {result.stderr.strip()}")
            try:
                raw_checks = json.loads(result.stdout)
            except json.JSONDecodeError as exc:
                raise BridgeError(f"PR #{number}: malformed required check inventory") from exc
            verified = run(["gh", "pr", "view", "--repo", repo, str(number), "--json", "headRefOid"])
            if verified.returncode:
                raise BridgeError(f"PR #{number}: cannot verify current head after check inventory")
            try:
                verified_head = json.loads(verified.stdout).get("headRefOid")
            except (json.JSONDecodeError, AttributeError) as exc:
                raise BridgeError(f"PR #{number}: malformed post-check head observation") from exc
            if verified_head != head:
                raise BridgeError(f"PR #{number}: head changed during check inventory; retry scan")
            required = required_contexts(repo, pr.get("baseRefName"))
            mergeable = pr.get("mergeable")
            review_decision = pr.get("reviewDecision")
            result = run(["gh", "api", "--paginate", "--slurp",
                          f"repos/{repo}/pulls/{number}/reviews"])
            if result.returncode:
                raise BridgeError(f"PR #{number}: review inventory failed: {result.stderr.strip()}")
            try:
                raw_reviews = json.loads(result.stdout)
            except json.JSONDecodeError as exc:
                raise BridgeError(f"PR #{number}: malformed review inventory") from exc
            if (not isinstance(raw_reviews, list) or
                    any(not isinstance(page, list) for page in raw_reviews)):
                raise BridgeError(f"PR #{number}: malformed review inventory")
            raw_reviews = [review for page in raw_reviews for review in page]
            # Refuse to combine checks and reviews from different heads.
            verified = run(["gh", "pr", "view", "--repo", repo, str(number), "--json", "headRefOid"])
            if verified.returncode:
                raise BridgeError(f"PR #{number}: cannot verify current head after review inventory")
            try:
                verified_head = json.loads(verified.stdout).get("headRefOid")
            except (json.JSONDecodeError, AttributeError) as exc:
                raise BridgeError(f"PR #{number}: malformed post-review head observation") from exc
            if verified_head != head:
                raise BridgeError(f"PR #{number}: head changed during review inventory; retry scan")
        if not isinstance(head, str) or not re.fullmatch(r"[0-9a-f]{40}", head):
            raise BridgeError(f"PR #{number}: current head missing")
        checks = []
        if raw_checks is not None:
            if not isinstance(raw_checks, list):
                raise BridgeError(f"PR #{number}: malformed required check inventory")
            for check in raw_checks:
                if not isinstance(check, dict) or not isinstance(check.get("name"), str):
                    raise BridgeError(f"PR #{number}: malformed required check")
                raw_state = check.get("state")
                if raw_state == "SUCCESS":
                    state = "SUCCESS"
                elif raw_state in ("PENDING", "EXPECTED", "QUEUED", "IN_PROGRESS", "WAITING", "REQUESTED"):
                    state = "PENDING"
                elif raw_state in ("FAILURE", "ERROR", "CANCELLED", "TIMED_OUT", "ACTION_REQUIRED", "STARTUP_FAILURE", "SKIPPED", "NEUTRAL"):
                    state = "FAILURE"
                else:
                    state = None
                if state is None:
                    raise BridgeError(f"PR #{number}: unknown required check state")
                checks.append({"name": check["name"], "head": head, "state": state,
                               "started_at": check.get("startedAt")})
        author = pr.get("author")
        if (not isinstance(author, dict) or not isinstance(author.get("login"), str) or
                not author["login"] or not isinstance(raw_reviews, list)):
            raise BridgeError(f"PR #{number}: author or review inventory unavailable")
        latest_reviews = {}
        for review in raw_reviews:
            if (not isinstance(review, dict) or not isinstance(review.get("user"), dict) or
                    not isinstance(review["user"].get("login"), str) or
                    review.get("state") not in ("PENDING", "APPROVED", "CHANGES_REQUESTED", "COMMENTED", "DISMISSED") or
                    (review.get("state") != "PENDING" and
                     (not isinstance(review.get("commit_id"), str) or
                      not isinstance(review.get("submitted_at"), str)))):
                raise BridgeError(f"PR #{number}: malformed review evidence")
            if review["state"] == "PENDING":
                # Draft reviews are not submitted approval evidence and must
                # not override the latest submitted review from that reviewer.
                continue
            reviewer = review["user"]["login"]
            submitted_at = parse_time(review["submitted_at"])
            prior = latest_reviews.get(reviewer)
            if prior is None or submitted_at > prior["submitted_at"]:
                latest_reviews[reviewer] = {"state": review["state"], "head": review["commit_id"],
                                            "submitted_at": submitted_at}
        independent = [review for reviewer, review in latest_reviews.items() if reviewer != author["login"]]
        current_approval = next((review for review in independent
                                 if review["state"] == "APPROVED" and review["head"] == head), None)
        if any(review["state"] == "CHANGES_REQUESTED" for review in independent) or review_decision == "CHANGES_REQUESTED":
            review_observation = {"state": "CHANGES_REQUESTED", "head": None}
        elif current_approval:
            review_observation = {"state": "APPROVED", "head": head}
        elif independent:
            review_observation = {"state": "STALE", "head": max(independent, key=lambda item: item["submitted_at"])["head"]}
        else:
            review_observation = {"state": "MISSING", "head": None}
        quarantine = quarantined_by_number.get(number, {})
        observations.append({"number": number, "objective": owners.get(number),
                             "quarantine_reason": quarantine.get("reason"),
                             "candidate_objectives": quarantine.get("candidate_objectives", []), "head": head,
                             "merge": {"MERGEABLE": "CLEAN", "CONFLICTING": "DIRTY"}.get(mergeable, "UNKNOWN"),
                             "observation": {"head": head,
                                             "merge": {"MERGEABLE": "CLEAN", "CONFLICTING": "DIRTY"}.get(mergeable, "UNKNOWN"),
                                             "required_checks": required or [], "checks": checks,
                                             "review": review_observation}})
    return observations


def project_pr_observations(ledger: dict, observations: list[dict]) -> dict:
    projected = json.loads(json.dumps(ledger))
    projected["open_pull_requests"] = observations
    projected["quarantined_objective_ids"] = sorted({objective for item in observations
                                                       for objective in item.get("candidate_objectives", [])})
    for item in observations:
        if item.get("objective"):
            row = next(row for row in projected["objectives"] if row["id"] == item["objective"])
            row.setdefault("pull_request_states", {})[str(item["number"])] = "OPEN"
    return projected


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


def project_prs(ledger: dict, additions: list[tuple[str, int]]) -> dict:
    """Add live PR links to an isolated observation, never to the source ledger."""
    projected = json.loads(json.dumps(ledger))
    rows = {row["id"]: row for row in projected["objectives"]}
    for rid, number in additions:
        prs = rows[rid].setdefault("pull_requests", [])
        if number not in prs:
            prs.append(number)
            prs.sort()
    return projected


def required_contexts(repo: str, branch: str) -> list[str]:
    """Read configured required context names independently from check results."""
    if not isinstance(branch, str) or not branch:
        raise BridgeError("PR base branch is missing")
    encoded_branch = quote(branch, safe="")
    result = run(["gh", "api", f"repos/{repo}/rules/branches/{encoded_branch}?per_page=100"])
    if result.returncode:
        raise BridgeError(f"cannot read active branch rules for {branch}: {result.stderr.strip()}")
    try:
        rules = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise BridgeError(f"malformed active branch rules for {branch}") from exc
    if not isinstance(rules, list) or len(rules) >= 100:
        raise BridgeError(f"active branch rules for {branch} are malformed or truncated")
    contexts = set()
    for rule in rules:
        if not isinstance(rule, dict):
            raise BridgeError(f"malformed active branch rule for {branch}")
        if rule.get("type") != "required_status_checks":
            continue
        params = rule.get("parameters")
        required = params.get("required_status_checks") if isinstance(params, dict) else None
        if not isinstance(required, list) or any(not isinstance(item, dict) or
                                                  not isinstance(item.get("context"), str) or
                                                  not item["context"] for item in required):
            raise BridgeError(f"malformed required status check rule for {branch}")
        contexts.update(item["context"] for item in required)

    protected = run(["gh", "api", f"repos/{repo}/branches/{encoded_branch}/protection/required_status_checks"])
    if protected.returncode:
        if "404" not in protected.stderr:
            raise BridgeError(f"cannot read branch protection checks for {branch}: {protected.stderr.strip()}")
    else:
        try:
            protection = json.loads(protected.stdout)
        except json.JSONDecodeError as exc:
            raise BridgeError(f"malformed branch protection checks for {branch}") from exc
        if not isinstance(protection, dict):
            raise BridgeError(f"malformed branch protection checks for {branch}")
        legacy = protection.get("contexts", [])
        checks = protection.get("checks", [])
        if not isinstance(legacy, list) or any(not isinstance(name, str) or not name for name in legacy):
            raise BridgeError(f"malformed branch protection contexts for {branch}")
        if not isinstance(checks, list) or any(not isinstance(item, dict) or
                                                not isinstance(item.get("context"), str) or
                                                not item["context"] for item in checks):
            raise BridgeError(f"malformed branch protection check rules for {branch}")
        contexts.update(legacy)
        contexts.update(item["context"] for item in checks)
    return sorted(contexts)


def carry_observed_prs(canonical: dict, previous: dict) -> dict:
    """Retain discovered links after a PR leaves the open-PR inventory."""
    discover_open_prs(canonical, [])
    discover_open_prs(previous, [])
    if previous.get("repository") != canonical.get("repository"):
        raise BridgeError("observed ledger repository changed")
    current = {row["id"]: row for row in canonical["objectives"]}
    additions = []
    for row in previous["objectives"]:
        now = current.get(row["id"])
        if now is None:
            continue
        if now.get("issue_number") != row.get("issue_number"):
            raise BridgeError(f"{row['id']}: issue identity changed since prior observation")
        additions.extend((row["id"], number) for number in row.get("pull_requests", []))
    return project_prs(canonical, additions)


def require_unchanged(path: Path, original: bytes) -> None:
    if path.read_bytes() != original:
        raise BridgeError("canonical ledger changed during audit; retry before deciding actions")


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
    ap.add_argument("--dry-run", action="store_true", help="no persistent writes or audit; inventory uses a temporary catalog snapshot")
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
        source_bytes = args.ledger.read_bytes()
        repo_data = json.loads(source_bytes)
        if not isinstance(repo_data, dict):
            raise BridgeError("invalid ledger object")
        repo = repo_data.get("repository")
        if not isinstance(repo, str) or not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repo):
            raise BridgeError("invalid ledger repository")
        if args.prs_file:
            prs = json.loads(args.prs_file.read_text())
        else:
            result = run(open_pr_inventory_command(repo))
            if result.returncode:
                raise BridgeError(f"GitHub open PR inventory failed: {result.stderr.strip()}")
            prs = json.loads(result.stdout)
        if not isinstance(prs, list) or len(prs) >= 1000:
            raise BridgeError("PR inventory malformed or truncated")
        observed_ledger = args.tasks.with_name("boss-observed-ledger.json")
        basis = carry_observed_prs(repo_data, json.loads(observed_ledger.read_text())) if observed_ledger.exists() else repo_data
        additions, quarantined = classify_open_prs(basis, prs)
        pr_observations = current_pr_observations(repo, basis, prs, additions, quarantined,
                                                   fixtures=bool(args.prs_file))
        projection = project_pr_observations(project_prs(basis, additions), pr_observations)
        if not args.dry_run:
            atomic_json(observed_ledger, projection)
            if not args.skip_audit:
                audited = run([sys.executable, str(args.audit), str(observed_ledger)])
                flags = [line[6:] for line in audited.stdout.splitlines() if line.startswith("FLAG: ")]
                known_gaps = bool(flags) and all(re.fullmatch(
                    r"#[1-9][0-9]*(?: PR[1-9][0-9]*)?: OPEN objective has no active (?:owner|heartbeat)",
                    item) for item in flags)
                if audited.returncode not in (0, 1) or (audited.returncode == 1 and not known_gaps):
                    raise BridgeError(f"GitHub audit failed ({audited.returncode}): {audited.stdout[-2000:]} {audited.stderr[-1000:]}")
                if known_gaps:
                    print("boss runtime: audit found owner/heartbeat gaps; continuing with fresh GitHub observations", file=sys.stderr)
            require_unchanged(args.ledger, source_bytes)
        ledger = json.loads(observed_ledger.read_text()) if not args.dry_run else projection
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
                                  "quarantined_prs": quarantined,
                                  "decision_skipped": "new PR links require a fresh audit before deciding"}))
                return 0
            actions, waiting = reconcile.decide(ledger, {item["id"]: item for item in tasks["tasks"]})
            print(json.dumps({"actions": actions, "waiting": waiting, "inventory": tasks, "pr_additions": additions,
                              "quarantined_prs": quarantined}))
            return 0
        atomic_json(args.tasks, tasks)
        require_unchanged(args.ledger, source_bytes)
        result = run([sys.executable, str(Path(__file__).with_name("reconcile_ledger.py")),
                      "--ledger", str(observed_ledger), "--tasks", str(args.tasks), "--outbox", str(args.outbox), "scan"])
        if result.returncode not in (0, 3):
            raise BridgeError(f"reconciler failed: {result.stdout} {result.stderr}")
        report = json.loads(result.stdout)
        print(json.dumps({"inventory": tasks, "pr_additions": additions,
                          "quarantined_prs": quarantined, **report}, sort_keys=True))
        if report["actions"]:
            if args.hub_task:
                require_unchanged(args.ledger, source_bytes)
                payload = {"kind": "boss_runtime_actions", "repository": repo,
                           "ledger": str(observed_ledger), "canonical_ledger": str(args.ledger),
                           "outbox": str(args.outbox),
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
