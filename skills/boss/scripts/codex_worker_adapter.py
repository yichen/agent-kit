#!/usr/bin/env python3
"""Launch, inspect, and safely resume visible Codex worker threads for /boss."""
from __future__ import annotations

import datetime as dt
import contextlib
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import selectors
import shutil
import subprocess
import sys
import time
import uuid


class AdapterError(Exception):
    pass


class RpcError(AdapterError):
    def __init__(self, message, *, definitive=False):
        super().__init__(message)
        self.definitive = definitive


UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[1-8][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$", re.I)
ACTION_RE = re.compile(r"^[a-f0-9]{64}$")


def run(argv, *, timeout=30, input_text=None):
    try:
        result = subprocess.run(argv, input=input_text, text=True, capture_output=True, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise AdapterError(f"command failed: {Path(argv[0]).name}: {exc}") from exc
    if result.returncode:
        detail = result.stderr.strip().splitlines()[-1:] or result.stdout.strip().splitlines()[-1:]
        raise AdapterError(f"{Path(argv[0]).name} failed" + (f": {detail[0][:240]}" if detail else ""))
    return result.stdout


class AppServer:
    """JSON-RPC client through an already running app-server proxy; never starts a daemon."""

    def __init__(self, timeout=8, process_factory=subprocess.Popen):
        codex = shutil.which("codex")
        if not codex:
            raise AdapterError("Codex CLI is not installed")
        try:
            self.proc = process_factory([codex, "app-server", "proxy"], stdin=subprocess.PIPE,
                                        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                        text=True, bufsize=1)
        except OSError as exc:
            raise AdapterError(f"cannot connect to the existing Codex app-server: {exc}") from exc
        self.timeout = timeout
        self.next_id = 1
        self.buffer = b""
        os.set_blocking(self.proc.stdout.fileno(), False)
        try:
            self.call("initialize", {"clientInfo": {"name": "agent-kit", "title": "Agent Kit", "version": "1"},
                                      "capabilities": {"experimentalApi": False}})
            self.notify("initialized", {})
        except Exception:
            self.close()
            raise

    def notify(self, method, params):
        self.proc.stdin.write(json.dumps({"method": method, "params": params}, separators=(",", ":")) + "\n")
        self.proc.stdin.flush()

    def call(self, method, params):
        request_id = self.next_id
        self.next_id += 1
        self.proc.stdin.write(json.dumps({"id": request_id, "method": method, "params": params}, separators=(",", ":")) + "\n")
        self.proc.stdin.flush()
        selector = selectors.DefaultSelector()
        selector.register(self.proc.stdout.fileno(), selectors.EVENT_READ)
        deadline = time.monotonic() + self.timeout
        try:
            while True:
                left = deadline - time.monotonic()
                if left <= 0 or not selector.select(left):
                    raise AdapterError(f"Codex app-server timed out during {method}; inspect live task state before retrying")
                chunk = os.read(self.proc.stdout.fileno(), 65536)
                if not chunk:
                    err = self.proc.stderr.read(500).strip() if self.proc.poll() is not None else ""
                    raise AdapterError("Codex app-server connection closed" + (f": {err[-240:]}" if err else ""))
                self.buffer += chunk
                while b"\n" in self.buffer:
                    line, self.buffer = self.buffer.split(b"\n", 1)
                    try:
                        message = json.loads(line)
                    except (json.JSONDecodeError, UnicodeDecodeError):
                        continue
                    if message.get("id") != request_id:
                        continue
                    if "error" in message:
                        error = message["error"]
                        raise RpcError(f"Codex app-server {method} failed: {error.get('message', 'unknown error')[:300]}", definitive=True)
                    return message.get("result", {})
        finally:
            selector.close()

    def close(self):
        if not getattr(self, "proc", None):
            return
        try:
            if self.proc.stdin:
                self.proc.stdin.close()
            self.proc.wait(timeout=1)
        except (OSError, subprocess.TimeoutExpired):
            try:
                self.proc.terminate()
                self.proc.wait(timeout=2)
            except (OSError, subprocess.TimeoutExpired):
                self.proc.kill()
        finally:
            for stream in (self.proc.stdout, self.proc.stderr):
                if stream:
                    stream.close()

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        self.close()

    def threads(self, *, cwd=None):
        rows, cursor = [], None
        while True:
            params = {"limit": 100, "sortKey": "created_at", "sortDirection": "desc", "useStateDbOnly": True}
            if cursor:
                params["cursor"] = cursor
            if cwd:
                params["cwd"] = cwd
            result = self.call("thread/list", params)
            rows.extend(result.get("data", []))
            cursor = result.get("nextCursor")
            if not cursor:
                return rows

    def read(self, task_id):
        return self.call("thread/read", {"threadId": task_id, "includeTurns": True}).get("thread", {})


def task_link(task_id):
    return f"codex://threads/{task_id}"


def task_status(thread):
    status = thread.get("status") or {}
    kind = status.get("type") if isinstance(status, dict) else None
    flags = status.get("activeFlags", []) if isinstance(status, dict) else []
    if "waitingOnApproval" in flags or "waitingOnUserInput" in flags:
        return "blocked"
    if kind == "active":
        return "running"
    turns = thread.get("turns") or []
    if turns:
        last = turns[-1].get("status")
        return {"completed": "completed", "interrupted": "interrupted", "failed": "blocked", "inProgress": "running"}.get(last, "queued")
    return "queued" if kind == "idle" else "interrupted"


def inventory(server):
    result = []
    for summary in server.threads():
        marker = summary.get("threadSource")
        if not isinstance(marker, str) or not marker.startswith("agent-kit:launch:"):
            continue
        action_id = marker.removeprefix("agent-kit:launch:")
        if not ACTION_RE.fullmatch(action_id):
            continue
        task_id = summary.get("id")
        if not isinstance(task_id, str) or not UUID_RE.fullmatch(task_id):
            continue
        thread = server.read(task_id)
        result.append({"id": task_id, "action_id": action_id, "status": task_status(thread),
                       "cwd": thread.get("cwd") or summary.get("cwd"), "link": task_link(task_id),
                       "name": thread.get("name") or summary.get("name")})
    return result


def resolve_task_id(server, candidate, client_id=None, timeout=15):
    if isinstance(candidate, str) and UUID_RE.fullmatch(candidate):
        return candidate
    pending = client_id if isinstance(client_id, str) else candidate
    if not isinstance(pending, str) or not pending:
        raise AdapterError("Codex returned neither a task ID nor a pending client ID")
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        for item in server.threads():
            if item.get("clientThreadId") == pending or item.get("id") == pending:
                task_id = item.get("id")
                if isinstance(task_id, str) and UUID_RE.fullmatch(task_id):
                    return task_id
        time.sleep(0.2)
    raise AdapterError("Codex task setup is still pending; inspect the task list and resolve its client ID before retrying")


def git(source, *args, timeout=60):
    return run(["git", "-C", str(source), *args], timeout=timeout).strip()


def discard_worktree(source, target, branch):
    """Remove an action worktree and its branch only when this exact path is registered."""
    try:
        records = git(source, "worktree", "list", "--porcelain")
        registered = any(line == f"worktree {Path(target).resolve()}" for line in records.splitlines())
        if not registered:
            return False
        run(["git", "-C", str(source), "worktree", "remove", "--force", str(target)], timeout=30)
        run(["git", "-C", str(source), "branch", "-D", branch], timeout=30)
        return True
    except AdapterError:
        return False


def issue_data(repo, issue):
    raw = run(["gh", "issue", "view", str(issue), "--repo", repo, "--json", "title,body,state,url"], timeout=25)
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise AdapterError("GitHub returned an invalid issue record") from exc
    if data.get("state") != "OPEN":
        raise AdapterError("issue is not open; refusing to launch a worker")
    return data


def open_pr_for_issue(repo, issue):
    raw = run(["gh", "pr", "list", "--repo", repo, "--state", "open", "--limit", "1000", "--json", "number,title,body,url"], timeout=25)
    try:
        prs = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise AdapterError("GitHub returned an invalid pull request inventory") from exc
    pattern = re.compile(rf"(?<![A-Za-z0-9])(?:#|(?:fix(?:es|ed)?|close[sd]?|resolve[sd]?)\s+#){issue}(?!\d)", re.I)
    return next((pr for pr in prs if pattern.search(f"{pr.get('title', '')}\n{pr.get('body', '')}")), None)


def prepare_worktree(source, issue, action_id):
    source = Path(source).resolve()
    if not source.is_dir() or git(source, "rev-parse", "--show-toplevel") != str(source):
        raise AdapterError("repo_path must be the absolute root of a Git worktree")
    if git(source, "status", "--porcelain"):
        raise AdapterError("source worktree has local changes; refusing to base a worker on an unreviewed state")
    root_parent = source.parent
    if not os.access(root_parent, os.W_OK | os.X_OK):
        raise AdapterError("sibling worktree parent is not writable")
    remote = git(source, "remote", "get-url", "origin")
    match = re.search(r"(?:github\.com[:/])([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+?)(?:\.git)?/?$", remote)
    if not match:
        raise AdapterError("origin must identify a GitHub owner/repository")
    repo = match.group(1)
    raw = run(["gh", "repo", "view", repo, "--json", "defaultBranchRef,viewerPermission"], timeout=25)
    try:
        repository = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise AdapterError("GitHub returned an invalid repository permission record") from exc
    permission = repository.get("viewerPermission")
    if permission not in ("WRITE", "MAINTAIN", "ADMIN"):
        raise AdapterError("GitHub account lacks repository write permission; refusing worker launch")
    branch = (repository.get("defaultBranchRef") or {}).get("name", "")
    if not re.fullmatch(r"[A-Za-z0-9._/-]+", branch) or branch.startswith("-") or ".." in branch:
        raise AdapterError("GitHub returned an invalid default branch")
    git(source, "fetch", "origin", branch, timeout=90)
    ref = f"origin/{branch}"
    if not git(source, "rev-parse", "--verify", ref):
        raise AdapterError("freshly fetched default branch is unavailable")
    target = root_parent / f"{source.name}-issue-{issue}-{action_id[:8]}"
    branch_name = f"codex/issue-{issue}-{action_id[:8]}"
    if target.exists() or target.is_symlink():
        raise AdapterError("stable action worktree path already exists but is not reconciled")
    git(source, "worktree", "add", "-b", branch_name, str(target), ref, timeout=120)
    try:
        if not target.is_dir() or git(target, "rev-parse", "--show-toplevel") != str(target.resolve()):
            raise AdapterError("created sibling path is not the expected Git worktree")
        if git(target, "branch", "--show-current") != branch_name or git(target, "rev-parse", "HEAD") != git(source, "rev-parse", ref):
            raise AdapterError("sibling worktree does not match the freshly fetched default branch")
        if git(target, "status", "--porcelain"):
            raise AdapterError("new sibling worktree is unexpectedly dirty")
    except Exception:
        discard_worktree(source, target, branch_name)
        raise
    return repo, target, branch_name, ref


def check_live_writer(task_id):
    lock_path = Path(os.environ.get("CODEX_HOME", str(Path.home() / ".codex"))) / "thread-writer-locks" / f"{task_id}.lock"
    if lock_path.exists():
        try:
            with lock_path.open("rb") as lock_file:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
        except BlockingIOError:
            return f"Codex thread writer lock held for {task_id}"
        except OSError as exc:
            raise AdapterError("could not verify Codex thread writer lock; refusing resume or takeover") from exc
    try:
        output = run(["ps", "-axo", "pid=,command="], timeout=5)
    except AdapterError:
        raise AdapterError("could not verify live Codex writer identity; refusing resume or takeover")
    needle = task_id.lower()
    for line in output.splitlines():
        lower = line.lower()
        if needle in lower and re.search(r"\bcodex\b", lower) and re.search(r"\b(exec|resume|app-server)\b", lower):
            return line.strip()
    return None


def find_action(server, action_id):
    return [row for row in inventory(server) if row["action_id"] == action_id]


@contextlib.contextmanager
def keyed_lock(key):
    root = Path(os.environ.get("AGENTS_ARTIFACTS_ROOT", str(Path.home() / "agents-artifacts")))
    directory = root / "boss" / "codex-action-locks"
    directory.mkdir(parents=True, exist_ok=True)
    lock_name = hashlib.sha256(key.encode()).hexdigest()
    lock_path = directory / f"{lock_name}.lock"
    with lock_path.open("a+") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def action_lock(action_id):
    return keyed_lock(f"action:{action_id}")


def issue_lock(repo, issue):
    return keyed_lock(f"issue:{repo}:{issue}")


def output_task(task_id):
    print(json.dumps({"task_id": task_id, "url": task_link(task_id)}, separators=(",", ":")))


def _launch_locked(server, request):
    action_id = request.get("action_id")
    issue = request.get("issue")
    if not isinstance(action_id, str) or not ACTION_RE.fullmatch(action_id):
        raise AdapterError("a 64-character stable action_id is required")
    if type(issue) is not int or issue <= 0:
        raise AdapterError("a positive issue number is required")
    existing = find_action(server, action_id)
    if len(existing) > 1:
        raise AdapterError("multiple Codex tasks already carry this action ID; refusing to choose or launch")
    if existing:
        output_task(existing[0]["id"])
        return
    same_issue = [task for task in inventory(server)
                  if (isinstance(task.get("name"), str) and task["name"].startswith(f"agent-kit #{issue}:"))
                  or (isinstance(task.get("cwd"), str)
                      and re.search(rf"-issue-{issue}-[a-f0-9]{{8}}$", Path(task["cwd"]).name))]
    if same_issue:
        ids = ", ".join(task["id"] for task in same_issue)
        raise AdapterError(f"issue #{issue} already has a Codex worker ({ids}); inspect or resume it before relaunch")
    source = request.get("repo_path")
    if not isinstance(source, str) or not Path(source).is_absolute():
        raise AdapterError("repo_path is required for sibling-worktree preflight")
    issue_info = issue_data(request.get("repo"), issue)
    pr = open_pr_for_issue(request["repo"], issue)
    if pr:
        raise AdapterError(f"an open PR already references issue #{issue} ({pr.get('url', pr.get('number'))}); refusing duplicate worker")
    repo, target, branch_name, ref = prepare_worktree(source, issue, action_id)
    prompt = (f"Work only on GitHub issue #{issue} in {repo}. Read the issue and repository instructions, "
              f"implement its acceptance criteria, and report the exact files and verification performed. "
              f"Stable agent-kit action ID: {action_id}. The coordinator owns assignment and the user owns "
              f"product acceptance; do not merge or change live assignment ownership.\n\n"
              f"Issue: {issue_info.get('title', '')}\n{issue_info.get('url', '')}\n\n{issue_info.get('body', '')}")
    marker = f"agent-kit:launch:{action_id}"
    thread_created = False
    try:
        result = server.call("thread/start", {"cwd": str(target), "threadSource": marker,
                                              "approvalPolicy": "on-request", "sandbox": "workspace-write"})
        thread = result.get("thread") or {}
        task_id = resolve_task_id(server, thread.get("id"), thread.get("clientThreadId"))
        thread_created = True
        name = f"agent-kit #{issue}: {issue_info.get('title', '')[:100]}"
        server.call("thread/name/set", {"threadId": task_id, "name": name})
        message_id = str(uuid.uuid5(uuid.NAMESPACE_URL, f"agent-kit:{action_id}:launch"))
        server.call("turn/start", {"threadId": task_id, "clientUserMessageId": message_id,
                                    "input": [{"type": "text", "text": prompt}]})
    except Exception:
        # A JSON-RPC error is a definitive rejection. Transport failures after request
        # transmission are ambiguous, so retain the worktree for reconciliation.
        exc = sys.exc_info()[1]
        if not thread_created and isinstance(exc, RpcError) and exc.definitive:
            discard_worktree(source, target, branch_name)
        raise
    output_task(task_id)


def launch(server, request):
    action_id = request.get("action_id") if isinstance(request, dict) else None
    if not isinstance(action_id, str) or not ACTION_RE.fullmatch(action_id):
        raise AdapterError("a 64-character stable action_id is required")
    issue = request.get("issue")
    if type(issue) is not int or issue <= 0:
        raise AdapterError("a positive issue number is required")
    repo = request.get("repo")
    if not isinstance(repo, str) or not repo:
        raise AdapterError("canonical GitHub repo is required")
    with action_lock(action_id), issue_lock(repo, issue):
        _launch_locked(server, request)


def inspect(server):
    now = dt.datetime.now(dt.timezone.utc).isoformat().replace("+00:00", "Z")
    print(json.dumps({"as_of": now, "tasks": inventory(server)}, separators=(",", ":")))


def _resume_locked(server, request):
    action_id, task_id = request.get("action_id"), request.get("task_id")
    if not isinstance(action_id, str) or not ACTION_RE.fullmatch(action_id):
        raise AdapterError("a 64-character stable action_id is required")
    if not isinstance(task_id, str) or not UUID_RE.fullmatch(task_id):
        raise AdapterError("resume requires the exact real Codex task UUID")
    matches = find_action(server, action_id)
    if len(matches) != 1 or matches[0]["id"] != task_id:
        raise AdapterError("action ID and exact task ID do not identify one live Codex task")
    thread = server.read(task_id)
    current = task_status(thread)
    live = check_live_writer(task_id)
    if current in ("running", "blocked"):
        raise AdapterError(f"task is {current} in app-server state; refusing a second writer")
    if live:
        raise AdapterError("a live Codex writer process still owns this task; refusing takeover")
    cwd = thread.get("cwd")
    if not isinstance(cwd, str) or not Path(cwd).is_dir():
        raise AdapterError("task worktree is unavailable; refusing resume")
    if git(cwd, "rev-parse", "--show-toplevel") != str(Path(cwd).resolve()):
        raise AdapterError("task cwd is no longer its Git worktree root")
    server.call("thread/resume", {"threadId": task_id, "path": cwd})
    prompt = request.get("prompt")
    if not isinstance(prompt, str) or not prompt.strip():
        prompt = "Continue the assigned GitHub issue from the existing task history. Preserve the existing worktree and report progress."
    message_id = request.get("client_user_message_id")
    if not isinstance(message_id, str) or not message_id:
        message_id = str(uuid.uuid5(uuid.NAMESPACE_URL, f"agent-kit:{action_id}:resume:{hashlib.sha256(prompt.encode()).hexdigest()}"))
    server.call("turn/start", {"threadId": task_id, "clientUserMessageId": message_id,
                                "input": [{"type": "text", "text": prompt}]})
    output_task(task_id)


def resume(server, request):
    action_id = request.get("action_id") if isinstance(request, dict) else None
    if not isinstance(action_id, str) or not ACTION_RE.fullmatch(action_id):
        raise AdapterError("a 64-character stable action_id is required")
    with action_lock(action_id):
        _resume_locked(server, request)


def main():
    parser_action = sys.argv[1] if len(sys.argv) == 2 else "launch"
    if parser_action not in ("launch", "inventory", "resume"):
        raise AdapterError("usage: codex_worker_adapter.py [launch|inventory|resume]")
    request = {}
    if parser_action != "inventory":
        try:
            request = json.load(sys.stdin)
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise AdapterError("stdin must contain one JSON request") from exc
        if not isinstance(request, dict):
            raise AdapterError("request must be a JSON object")
    with AppServer() as server:
        if parser_action == "launch":
            launch(server, request)
        elif parser_action == "inventory":
            inspect(server)
        else:
            resume(server, request)


if __name__ == "__main__":
    try:
        main()
    except (AdapterError, OSError, ValueError, KeyError, subprocess.SubprocessError) as exc:
        print(f"codex worker adapter: {exc}", file=sys.stderr)
        sys.exit(2)
