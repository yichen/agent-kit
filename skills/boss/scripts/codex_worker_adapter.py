#!/usr/bin/env python3
"""Launch, inspect, and safely resume visible Codex worker threads for /boss."""
from __future__ import annotations

import datetime as dt
import base64
import contextlib
import fcntl
import hashlib
import hmac
import json
import os
from pathlib import Path
import re
import secrets
import socket
import struct
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


APP_SERVER_SOCKET_RELATIVE_PATH = Path("app-server-control") / "app-server-control.sock"
WEBSOCKET_GUID = b"258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
MAX_HTTP_HEADER_BYTES = 16 * 1024
MAX_WEBSOCKET_MESSAGE_BYTES = 8 * 1024 * 1024
# Keep each inventory response comfortably below the transport frame limit. A
# hard page cap also bounds work if app-server returns a broken cursor chain.
THREAD_LIST_PAGE_SIZE = 10
MAX_THREAD_LIST_PAGES = 100


def app_server_socket_path(*, environ=None, home=None) -> Path:
    """Resolve the local Codex control socket without creating or starting anything."""
    environ = os.environ if environ is None else environ
    home = Path.home() if home is None else Path(home)
    configured = environ.get("CODEX_HOME")
    root = Path(configured).expanduser() if configured else home.expanduser() / ".codex"
    if not root.is_absolute():
        raise AdapterError("CODEX_HOME must be an absolute path")
    return root / APP_SERVER_SOCKET_RELATIVE_PATH


def connect_unix_socket(path: Path, timeout: float):
    """Connect only to the existing local Unix socket; this never launches a daemon."""
    connection = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    connection.settimeout(timeout)
    try:
        connection.connect(str(path))
    except OSError as exc:
        connection.close()
        raise AdapterError(f"cannot connect to Codex app-server socket {path}: {exc}") from exc
    return connection


class UnixWebSocket:
    """Small bounded RFC 6455 client for Codex's local Unix-socket transport."""

    def __init__(self, connection, *, timeout=8):
        self.connection = connection
        self.timeout = timeout
        self.buffer = bytearray()
        self.handshake_complete = False
        self.close_sent = False
        self.closed = False

    @classmethod
    def connect(cls, path, *, timeout=8, connector=connect_unix_socket):
        connection = connector(Path(path), timeout)
        websocket = cls(connection, timeout=timeout)
        try:
            websocket.handshake()
            return websocket
        except socket.timeout as exc:
            websocket.close()
            raise AdapterError("timed out during Codex app-server WebSocket handshake") from exc
        except Exception:
            websocket.close()
            raise

    def _set_deadline(self, deadline):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("Codex app-server socket timed out")
        self.connection.settimeout(remaining)

    def _sendall(self, data, deadline):
        self._set_deadline(deadline)
        self.connection.sendall(data)

    def _recv_exact(self, length, deadline):
        while len(self.buffer) < length:
            self._set_deadline(deadline)
            chunk = self.connection.recv(max(1, min(65536, length - len(self.buffer))))
            if not chunk:
                raise ConnectionError("Codex app-server closed the WebSocket")
            self.buffer.extend(chunk)
        result = bytes(self.buffer[:length])
        del self.buffer[:length]
        return result

    def handshake(self):
        deadline = time.monotonic() + self.timeout
        client_key = base64.b64encode(secrets.token_bytes(16)).decode("ascii")
        request = (
            "GET / HTTP/1.1\r\n"
            "Host: localhost\r\n"
            "Upgrade: websocket\r\n"
            "Connection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {client_key}\r\n"
            "Sec-WebSocket-Version: 13\r\n\r\n"
        ).encode("ascii")
        self._sendall(request, deadline)
        while b"\r\n\r\n" not in self.buffer:
            if len(self.buffer) >= MAX_HTTP_HEADER_BYTES:
                raise AdapterError("Codex app-server WebSocket response headers exceed the size limit")
            self._set_deadline(deadline)
            chunk = self.connection.recv(min(4096, MAX_HTTP_HEADER_BYTES - len(self.buffer)))
            if not chunk:
                raise ConnectionError("Codex app-server closed during the WebSocket handshake")
            self.buffer.extend(chunk)
        raw_headers, remainder = bytes(self.buffer).split(b"\r\n\r\n", 1)
        self.buffer = bytearray(remainder)
        try:
            lines = raw_headers.decode("iso-8859-1").split("\r\n")
        except UnicodeDecodeError as exc:
            raise AdapterError("Codex app-server returned invalid WebSocket response headers") from exc
        if not lines or not re.fullmatch(r"HTTP/1\.[01] 101(?: .*)?", lines[0]):
            status = lines[0][:100] if lines else "empty response"
            raise AdapterError(f"Codex app-server WebSocket upgrade rejected: {status}")
        headers = {}
        for line in lines[1:]:
            if ":" not in line:
                raise AdapterError("Codex app-server returned a malformed WebSocket response header")
            name, value = line.split(":", 1)
            headers.setdefault(name.strip().lower(), []).append(value.strip())
        connection_tokens = {token.strip().lower()
                             for value in headers.get("connection", []) for token in value.split(",")}
        upgrade_values = headers.get("upgrade", [])
        accept_values = headers.get("sec-websocket-accept", [])
        expected_accept = base64.b64encode(hashlib.sha1(client_key.encode("ascii") + WEBSOCKET_GUID).digest()).decode("ascii")
        if (len(upgrade_values) != 1
                or upgrade_values[0].lower() != "websocket"
                or "upgrade" not in connection_tokens
                or len(accept_values) != 1
                or not hmac.compare_digest(accept_values[0], expected_accept)):
            raise AdapterError("Codex app-server WebSocket handshake validation failed")
        if "sec-websocket-extensions" in headers:
            raise AdapterError("Codex app-server negotiated an unsupported WebSocket extension")
        self.handshake_complete = True

    def _send_frame(self, opcode, payload, deadline, *, final=True):
        if self.closed:
            raise ConnectionError("Codex app-server WebSocket is closed")
        payload = bytes(payload)
        if len(payload) > MAX_WEBSOCKET_MESSAGE_BYTES:
            raise AdapterError("Codex app-server WebSocket message exceeds the size limit")
        first = (0x80 if final else 0) | opcode
        size = len(payload)
        if size < 126:
            header = bytes((first, 0x80 | size))
        elif size <= 0xFFFF:
            header = bytes((first, 0x80 | 126)) + struct.pack("!H", size)
        else:
            header = bytes((first, 0x80 | 127)) + struct.pack("!Q", size)
        mask = secrets.token_bytes(4)
        masked = bytes(value ^ mask[index & 3] for index, value in enumerate(payload))
        self._sendall(header + mask + masked, deadline)

    def send_text(self, text, deadline):
        try:
            payload = text.encode("utf-8")
        except UnicodeEncodeError as exc:
            raise AdapterError("Codex app-server request is not valid UTF-8") from exc
        self._send_frame(0x1, payload, deadline)

    def _recv_frame(self, deadline):
        first, second = self._recv_exact(2, deadline)
        final = bool(first & 0x80)
        if first & 0x70:
            raise AdapterError("Codex app-server sent a WebSocket frame with reserved bits set")
        opcode = first & 0x0F
        if second & 0x80:
            raise AdapterError("Codex app-server sent a masked server frame")
        length = second & 0x7F
        if length == 126:
            length = struct.unpack("!H", self._recv_exact(2, deadline))[0]
            if length < 126:
                raise AdapterError("Codex app-server sent a non-canonical WebSocket frame length")
        elif length == 127:
            extended = struct.unpack("!Q", self._recv_exact(8, deadline))[0]
            if extended & (1 << 63) or extended <= 0xFFFF:
                raise AdapterError("Codex app-server sent an invalid WebSocket frame length")
            length = extended
        if length > MAX_WEBSOCKET_MESSAGE_BYTES:
            raise AdapterError("Codex app-server WebSocket frame exceeds the size limit")
        if opcode >= 0x8 and (not final or length > 125):
            raise AdapterError("Codex app-server sent an invalid WebSocket control frame")
        if opcode not in (0x0, 0x1, 0x2, 0x8, 0x9, 0xA):
            raise AdapterError("Codex app-server sent an unsupported WebSocket opcode")
        return final, opcode, self._recv_exact(length, deadline)

    def recv_text(self, deadline):
        fragments = None
        while True:
            final, opcode, payload = self._recv_frame(deadline)
            if opcode == 0x8:
                if len(payload) == 1:
                    raise AdapterError("Codex app-server sent an invalid WebSocket close payload")
                if len(payload) >= 2:
                    code = struct.unpack("!H", payload[:2])[0]
                    if code < 1000 or code >= 5000 or code in (1004, 1005, 1006, 1015):
                        raise AdapterError("Codex app-server sent an invalid WebSocket close code")
                    try:
                        payload[2:].decode("utf-8")
                    except UnicodeDecodeError as exc:
                        raise AdapterError("Codex app-server sent an invalid WebSocket close reason") from exc
                if not self.close_sent:
                    try:
                        self._send_frame(0x8, payload[:125], min(deadline, time.monotonic() + 0.2))
                        self.close_sent = True
                    except (OSError, TimeoutError, AdapterError):
                        pass
                raise ConnectionError("Codex app-server closed the WebSocket")
            if opcode == 0x9:
                self._send_frame(0xA, payload, deadline)
                continue
            if opcode == 0xA:
                continue
            if opcode == 0x2:
                raise AdapterError("Codex app-server sent a binary JSON-RPC message")
            if opcode == 0x1:
                if fragments is not None:
                    raise AdapterError("Codex app-server started a new message before finishing a fragmented one")
                if final:
                    try:
                        return payload.decode("utf-8")
                    except UnicodeDecodeError as exc:
                        raise AdapterError("Codex app-server sent invalid UTF-8 in a text frame") from exc
                fragments = bytearray(payload)
                continue
            if fragments is None:
                raise AdapterError("Codex app-server sent a continuation frame without a message")
            fragments.extend(payload)
            if len(fragments) > MAX_WEBSOCKET_MESSAGE_BYTES:
                raise AdapterError("Codex app-server fragmented message exceeds the size limit")
            if final:
                try:
                    return fragments.decode("utf-8")
                except UnicodeDecodeError as exc:
                    raise AdapterError("Codex app-server sent invalid UTF-8 in a text frame") from exc

    def close(self):
        if self.closed:
            return
        if self.handshake_complete and not self.close_sent:
            deadline = time.monotonic() + min(self.timeout, 0.2)
            try:
                self._send_frame(0x8, struct.pack("!H", 1000), deadline)
                self.close_sent = True
                while time.monotonic() < deadline:
                    final, opcode, payload = self._recv_frame(deadline)
                    if opcode == 0x8:
                        break
                    if opcode == 0x9:
                        self._send_frame(0xA, payload, deadline)
            except (OSError, TimeoutError, socket.timeout, AdapterError):
                pass
        self.closed = True
        try:
            self.connection.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        self.connection.close()


class AppServer:
    """JSON-RPC client on the existing local socket; it never starts a daemon."""

    def __init__(self, timeout=8, socket_path=None, socket_connector=connect_unix_socket):
        self.timeout = timeout
        path = Path(socket_path) if socket_path is not None else app_server_socket_path()
        self.websocket = UnixWebSocket.connect(path, timeout=timeout, connector=socket_connector)
        self.next_id = 1
        try:
            self.call("initialize", {"clientInfo": {"name": "agent-kit", "title": "Agent Kit", "version": "1"},
                                      "capabilities": {"experimentalApi": False}})
            self.notify("initialized", {})
        except Exception:
            self.close()
            raise

    def notify(self, method, params):
        message = json.dumps({"method": method, "params": params}, separators=(",", ":"))
        try:
            self.websocket.send_text(message, time.monotonic() + self.timeout)
        except (OSError, TimeoutError, socket.timeout, ConnectionError) as exc:
            raise AdapterError(f"Codex app-server notification {method} failed: {exc}") from exc

    def call(self, method, params):
        request_id = self.next_id
        self.next_id += 1
        message = json.dumps({"id": request_id, "method": method, "params": params}, separators=(",", ":"))
        deadline = time.monotonic() + self.timeout
        try:
            self.websocket.send_text(message, deadline)
            while True:
                text = self.websocket.recv_text(deadline)
                try:
                    response = json.loads(text)
                except json.JSONDecodeError as exc:
                    raise AdapterError("Codex app-server sent malformed JSON-RPC") from exc
                if not isinstance(response, dict):
                    raise AdapterError("Codex app-server sent a malformed JSON-RPC message")
                if response.get("id") != request_id:
                    continue
                if "error" in response:
                    error = response["error"]
                    message = error.get("message", "unknown error") if isinstance(error, dict) else "unknown error"
                    raise RpcError(f"Codex app-server {method} failed: {message[:300]}", definitive=True)
                result = response.get("result", {})
                if not isinstance(result, dict):
                    raise AdapterError("Codex app-server returned a malformed JSON-RPC result")
                return result
        except (TimeoutError, socket.timeout) as exc:
            raise AdapterError(f"Codex app-server timed out during {method}; inspect live task state before retrying") from exc
        except ConnectionError as exc:
            raise AdapterError("Codex app-server connection closed") from exc
        except OSError as exc:
            raise AdapterError(f"Codex app-server transport failed during {method}: {exc}") from exc

    def close(self):
        websocket = getattr(self, "websocket", None)
        if websocket:
            websocket.close()

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        self.close()

    def threads(self, *, cwd=None):
        rows, cursor = [], None
        seen_cursors = set()
        for _page_number in range(MAX_THREAD_LIST_PAGES):
            params = {"limit": THREAD_LIST_PAGE_SIZE, "sortKey": "created_at", "sortDirection": "desc", "useStateDbOnly": True}
            if cursor:
                params["cursor"] = cursor
            if cwd:
                params["cwd"] = cwd
            result = self.call("thread/list", params)
            page = result.get("data")
            if not isinstance(page, list) or len(page) > THREAD_LIST_PAGE_SIZE:
                raise AdapterError("Codex app-server returned a malformed thread/list page")
            rows.extend(page)
            next_cursor = result.get("nextCursor")
            if next_cursor is None or next_cursor == "":
                return rows
            if not isinstance(next_cursor, str):
                raise AdapterError("Codex app-server returned a malformed thread/list cursor")
            if next_cursor in seen_cursors or next_cursor == cursor:
                raise AdapterError("Codex app-server returned a repeated thread/list cursor")
            seen_cursors.add(next_cursor)
            cursor = next_cursor
        raise AdapterError(f"Codex app-server thread/list exceeded the {MAX_THREAD_LIST_PAGES}-page limit")

    def read(self, task_id):
        return self.call("thread/read", {"threadId": task_id, "includeTurns": True}).get("thread", {})


def task_link(task_id):
    return f"codex://threads/{task_id}"


def task_status(thread):
    status = thread.get("status") or {}
    kind = status.get("type") if isinstance(status, dict) else None
    if kind == "notLoaded":
        return "unknown"
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
