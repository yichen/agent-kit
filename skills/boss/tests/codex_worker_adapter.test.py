#!/usr/bin/env python3
import contextlib
from concurrent.futures import ThreadPoolExecutor
import base64
import hashlib
import io
import json
import os
from pathlib import Path
import socket
import struct
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))
import codex_worker_adapter as adapter


TASK = "019a1234-5678-7abc-8def-0123456789ab"
TASK2 = "019a1234-5678-7abc-8def-0123456789ac"
ACTION = "a" * 64
REPO = "yichen/agent-kit"


class FakeServer:
    def __init__(self, threads=()):
        self.rows = list(threads)
        self.calls = []
        self.created = 0
        self.fail_turn = False

    def threads(self, cwd=None):
        self.calls.append(("thread/list", {"cwd": cwd}))
        return [dict(row) for row in self.rows]

    def read(self, task_id):
        self.calls.append(("thread/read", {"threadId": task_id}))
        return dict(next(row for row in self.rows if row["id"] == task_id))

    def call(self, method, params):
        self.calls.append((method, params))
        if method == "thread/start":
            self.created += 1
            row = {"id": TASK, "threadSource": params["threadSource"], "cwd": params["cwd"],
                   "status": {"type": "idle"}, "turns": []}
            self.rows.append(row)
            return {"thread": row}
        if method == "turn/start":
            if self.fail_turn:
                raise adapter.AdapterError("simulated interrupted response")
            row = next(row for row in self.rows if row["id"] == params["threadId"])
            row["turns"] = [{"status": "inProgress"}]
            row["status"] = {"type": "active", "activeFlags": []}
            return {"turn": {"id": "turn-1"}}
        if method == "thread/resume":
            row = next(row for row in self.rows if row["id"] == params["threadId"])
            row["status"] = {"type": "idle"}
            return {"thread": row}
        if method == "thread/name/set":
            next(row for row in self.rows if row["id"] == params["threadId"])["name"] = params["name"]
            return {}
        raise AssertionError(f"unexpected mutating RPC {method}")


def websocket_frame(opcode, payload=b"", *, final=True, masked=False, reserved=0):
    payload = bytes(payload)
    first = (0x80 if final else 0) | reserved | opcode
    size = len(payload)
    mask_bit = 0x80 if masked else 0
    if size < 126:
        header = bytes((first, mask_bit | size))
    elif size <= 0xFFFF:
        header = bytes((first, mask_bit | 126)) + struct.pack("!H", size)
    else:
        header = bytes((first, mask_bit | 127)) + struct.pack("!Q", size)
    if not masked:
        return header + payload
    key = b"test"
    return header + key + bytes(value ^ key[index & 3] for index, value in enumerate(payload))


def read_socket_exact(connection, length):
    parts = bytearray()
    while len(parts) < length:
        chunk = connection.recv(length - len(parts))
        if not chunk:
            raise ConnectionError("peer closed")
        parts.extend(chunk)
    return bytes(parts)


class FakeUnixWebSocketPeer:
    """Local AF_UNIX WebSocket peer that verifies masked client frames."""

    def __init__(self, socket_path, response_factory, *, handshake_mode="valid"):
        self.socket_path = Path(socket_path)
        self.response_factory = response_factory
        self.handshake_mode = handshake_mode
        self.listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.listener.bind(str(self.socket_path))
        self.listener.listen(1)
        self.requests = []
        self.client_frames_masked = True
        self.handshake_request = b""
        self.failure = None
        self.connection = None
        self.thread = threading.Thread(target=self.serve, daemon=True)
        self.thread.start()

    def _read_headers(self):
        data = bytearray()
        while b"\r\n\r\n" not in data:
            chunk = self.connection.recv(4096)
            if not chunk:
                raise ConnectionError("closed during HTTP handshake")
            data.extend(chunk)
            if len(data) > 16 * 1024:
                raise AssertionError("oversized client handshake")
        return bytes(data)

    def _read_client_frame(self):
        first, second = read_socket_exact(self.connection, 2)
        final, opcode = bool(first & 0x80), first & 0x0F
        masked = bool(second & 0x80)
        self.client_frames_masked &= masked
        length = second & 0x7F
        if length == 126:
            length = struct.unpack("!H", read_socket_exact(self.connection, 2))[0]
        elif length == 127:
            length = struct.unpack("!Q", read_socket_exact(self.connection, 8))[0]
        mask = read_socket_exact(self.connection, 4) if masked else b""
        payload = read_socket_exact(self.connection, length)
        if masked:
            payload = bytes(value ^ mask[index & 3] for index, value in enumerate(payload))
        return final, opcode, payload

    def _send_response(self, headers, key):
        status = "HTTP/1.1 101 Switching Protocols"
        upgrade = "Upgrade: websocket\r\n"
        connection = "Connection: Upgrade"
        accept = base64.b64encode(hashlib.sha1(key.encode("ascii") + adapter.WEBSOCKET_GUID).digest()).decode("ascii")
        if self.handshake_mode == "bad_status":
            status = "HTTP/1.1 403 Forbidden"
        elif self.handshake_mode == "bad_accept":
            accept = "incorrect-accept"
        elif self.handshake_mode == "bad_upgrade":
            upgrade = "Upgrade: h2c\r\n"
        elif self.handshake_mode == "duplicate_upgrade":
            upgrade = "Upgrade: websocket\r\nUpgrade: h2c\r\n"
        elif self.handshake_mode == "bad_connection":
            connection = "Connection: keep-alive"
        self.connection.sendall((f"{status}\r\n{upgrade}{connection}\r\nSec-WebSocket-Accept: {accept}\r\n\r\n").encode("ascii"))

    def serve(self):
        try:
            self.connection, _ = self.listener.accept()
            self.connection.settimeout(2)
            self.handshake_request = self._read_headers()
            head = self.handshake_request.split(b"\r\n\r\n", 1)[0].decode("ascii")
            request_lines = head.split("\r\n")
            headers = {}
            for line in request_lines[1:]:
                name, value = line.split(":", 1)
                headers[name.strip().lower()] = value.strip()
            self._send_response(headers, headers["sec-websocket-key"])
            if self.handshake_mode != "valid":
                return
            while True:
                final, opcode, payload = self._read_client_frame()
                if opcode == 0x8:
                    self.connection.sendall(websocket_frame(0x8, payload))
                    return
                if opcode == 0xA:
                    continue
                if opcode == 0x9:
                    self.connection.sendall(websocket_frame(0xA, payload))
                    continue
                if opcode != 0x1 or not final:
                    raise AssertionError("expected a complete masked text request")
                message = json.loads(payload)
                self.requests.append(message)
                frames = self.response_factory(message)
                if frames == "disconnect":
                    return
                for frame in frames or ():
                    self.connection.sendall(frame)
        except (OSError, ConnectionError) as exc:
            self.failure = exc
        except Exception as exc:
            self.failure = exc
        finally:
            if self.connection:
                self.connection.close()
            self.listener.close()

    def join(self):
        self.thread.join(timeout=3)
        if self.thread.is_alive():
            self.listener.close()
            raise AssertionError("fake WebSocket peer did not terminate")


def row(task_id=TASK, action_id=ACTION, turn_status="interrupted", **overrides):
    result = {"id": task_id, "threadSource": f"agent-kit:launch:{action_id}",
              "cwd": "/work/repo-issue-16", "name": "agent-kit #16", "status": {"type": "idle"},
              "turns": [{"status": turn_status}]}
    result.update(overrides)
    return result


def request():
    return {"repo": REPO, "repo_path": "/work/repo", "issue": 16, "action_id": ACTION, "verb": "launch"}


class CodexWorkerAdapterTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.env = mock.patch.dict(os.environ, {"AGENTS_ARTIFACTS_ROOT": str(self.root / "artifacts")})
        self.env.start()

    def tearDown(self):
        self.env.stop()
        self.tmp.cleanup()

    def test_idempotent_retry_returns_existing_task_and_does_not_launch_twice(self):
        server = FakeServer([row(turn_status="inProgress")])
        with mock.patch.object(adapter, "issue_data", side_effect=AssertionError("retry must not reread launch inputs")), \
             mock.patch.object(adapter, "prepare_worktree", side_effect=AssertionError("retry must not create another worktree")):
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                adapter.launch(server, request())
        self.assertEqual(json.loads(out.getvalue())["task_id"], TASK)
        self.assertEqual(server.created, 0)

    def test_launch_timeout_keeps_action_marker_and_retry_recovers_without_duplicate(self):
        server = FakeServer()
        server.fail_turn = True
        with mock.patch.object(adapter, "issue_data", return_value={"title": "Add adapter", "url": "https://github.com/yichen/agent-kit/issues/16", "body": "acceptance"}), \
             mock.patch.object(adapter, "open_pr_for_issue", return_value=None), \
             mock.patch.object(adapter, "prepare_worktree", return_value=(REPO, Path(self.root / "worker"), "codex/issue-16", "origin/main")), \
             mock.patch.object(adapter.subprocess, "run", return_value=mock.Mock(returncode=0)):
            with self.assertRaisesRegex(adapter.AdapterError, "simulated interrupted"):
                adapter.launch(server, request())
        server.fail_turn = False
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            adapter.launch(server, request())
        self.assertEqual(server.created, 1)
        self.assertEqual(json.loads(out.getvalue())["task_id"], TASK)

    def test_approval_prompt_is_reported_as_blocked_in_read_only_inventory(self):
        server = FakeServer([row(turn_status="inProgress", status={"type": "active", "activeFlags": ["waitingOnApproval"]})])
        tasks = adapter.inventory(server)
        self.assertEqual(tasks[0]["status"], "blocked")
        self.assertEqual([method for method, _ in server.calls], ["thread/list", "thread/read"])
        self.assertFalse((self.root / "artifacts").exists(), "read-only inventory wrote persistent adapter state")
        self.assertEqual(tasks[0]["id"], TASK)

    def test_pending_client_id_resolves_to_real_task_uuid(self):
        class PendingServer:
            def threads(self):
                return [{"id": TASK, "clientThreadId": "client-pending-7"}]
        self.assertEqual(adapter.resolve_task_id(PendingServer(), "client-pending-7"), TASK)

    def test_interrupted_ui_with_live_writer_refuses_takeover(self):
        server = FakeServer([row(turn_status="interrupted")])
        with mock.patch.object(adapter, "check_live_writer", return_value="4321 codex exec resume --json " + TASK):
            with self.assertRaisesRegex(adapter.AdapterError, "live Codex writer"):
                adapter.resume(server, {"action_id": ACTION, "task_id": TASK, "prompt": "continue"})
        self.assertFalse(any(method in ("thread/resume", "turn/start") for method, _ in server.calls))

    def test_live_writer_lock_is_detected_even_when_process_argv_has_no_task_uuid(self):
        codex_home = self.root / "codex-home"
        lock_dir = codex_home / "thread-writer-locks"
        lock_dir.mkdir(parents=True)
        lock_path = lock_dir / f"{TASK}.lock"
        lock_path.touch()
        code = "import fcntl,sys,time; f=open(sys.argv[1],'rb'); fcntl.flock(f,fcntl.LOCK_EX); print('ready',flush=True); time.sleep(10)"
        writer = subprocess.Popen([sys.executable, "-c", code, str(lock_path)], stdout=subprocess.PIPE, text=True)
        try:
            self.assertEqual(writer.stdout.readline().strip(), "ready")
            with mock.patch.dict(os.environ, {"CODEX_HOME": str(codex_home)}), \
                 mock.patch.object(adapter, "run", return_value=""):
                self.assertIn("writer lock held", adapter.check_live_writer(TASK))
        finally:
            writer.terminate()
            writer.wait(timeout=5)
            writer.stdout.close()

    def test_app_server_socket_path_resolution_table(self):
        cases = [
            ("default home", {}, Path("/home/test"), Path("/home/test/.codex/app-server-control/app-server-control.sock")),
            ("CODEX_HOME override", {"CODEX_HOME": "/var/tmp/codex"}, Path("/home/test"), Path("/var/tmp/codex/app-server-control/app-server-control.sock")),
            ("tilde override", {"CODEX_HOME": "~/codex-state"}, Path.home(), Path.home() / "codex-state/app-server-control/app-server-control.sock"),
        ]
        for name, environ, home, expected in cases:
            with self.subTest(name=name):
                self.assertEqual(adapter.app_server_socket_path(environ=environ, home=home), expected)
        with self.assertRaisesRegex(adapter.AdapterError, "CODEX_HOME must be an absolute path"):
            adapter.app_server_socket_path(environ={"CODEX_HOME": "relative/codex"}, home=Path("/home/test"))

    def test_app_server_handshake_verifies_upgrade_accept_and_headers_table(self):
        cases = [
            ("bad status", "bad_status", "upgrade rejected"),
            ("wrong accept", "bad_accept", "handshake validation failed"),
            ("wrong upgrade", "bad_upgrade", "handshake validation failed"),
            ("duplicate upgrade", "duplicate_upgrade", "handshake validation failed"),
            ("wrong connection", "bad_connection", "handshake validation failed"),
        ]
        for name, mode, message in cases:
            with self.subTest(name=name):
                peer = FakeUnixWebSocketPeer(self.root / f"{mode}.sock", lambda _request: (), handshake_mode=mode)
                with self.assertRaisesRegex(adapter.AdapterError, message):
                    adapter.AppServer(timeout=0.2, socket_path=peer.socket_path)
                peer.join()
                self.assertFalse((self.root / "artifacts").exists())

    def test_app_server_websocket_masks_client_frames_and_handles_fragmented_notifications(self):
        def responses(message):
            if message["method"] == "initialize":
                return [websocket_frame(0x1, json.dumps({"id": message["id"], "result": {}}).encode())]
            if message["method"] == "thread/list":
                notification = websocket_frame(0x1, b'{"method":"thread/started","params":{}}')
                response = json.dumps({"id": message["id"], "result": {"data": []}}).encode()
                split = len(response) // 2
                return [notification, websocket_frame(0x1, response[:split], final=False),
                        websocket_frame(0x9, b"ping"), websocket_frame(0x0, response[split:])]
            return []

        peer = FakeUnixWebSocketPeer(self.root / "app-server.sock", responses)
        with adapter.AppServer(timeout=0.5, socket_path=peer.socket_path) as server:
            response = server.call("thread/list", {"useStateDbOnly": True})
            self.assertEqual(response, {"data": []})
        peer.join()
        self.assertIsNone(peer.failure)
        self.assertTrue(peer.client_frames_masked)
        request_text = peer.handshake_request.decode("ascii")
        self.assertTrue(request_text.startswith("GET / HTTP/1.1\r\n"))
        headers = {line.split(":", 1)[0].lower(): line.split(":", 1)[1].strip()
                   for line in request_text.split("\r\n")[1:] if ":" in line}
        self.assertEqual(headers["upgrade"].lower(), "websocket")
        self.assertIn("upgrade", headers["connection"].lower())
        self.assertEqual(headers["sec-websocket-version"], "13")
        self.assertEqual(len(base64.b64decode(headers["sec-websocket-key"])), 16)

    def test_app_server_handles_extended_client_and_server_frame_lengths(self):
        sizes = (200, 66_000)
        for size in sizes:
            with self.subTest(size=size):
                def responses(message):
                    if message["method"] == "initialize":
                        return [websocket_frame(0x1, json.dumps({"id": message["id"], "result": {}}).encode())]
                    if "id" not in message:
                        return []
                    result = {"pad": "x" * size}
                    return [websocket_frame(0x1, json.dumps({"id": message["id"], "result": result}).encode())]

                peer = FakeUnixWebSocketPeer(self.root / f"large-{size}.sock", responses)
                with adapter.AppServer(timeout=1, socket_path=peer.socket_path) as server:
                    result = server.call("thread/list", {"payload": "x" * size})
                peer.join()
                self.assertIsNone(peer.failure)
                self.assertTrue(peer.client_frames_masked)
                self.assertEqual(len(result["pad"]), size)
                self.assertEqual(len(peer.requests[-1]["params"]["payload"]), size)

    def test_app_server_rejects_malformed_server_frames_table(self):
        bad_frames = [
            ("masked server frame", lambda: websocket_frame(0x1, b"{}", masked=True), "masked server frame"),
            ("reserved bits", lambda: websocket_frame(0x1, b"{}", reserved=0x40), "reserved bits"),
            ("oversized frame", lambda: b"\x81\x7f" + struct.pack("!Q", adapter.MAX_WEBSOCKET_MESSAGE_BYTES + 1), "exceeds the size limit"),
            ("binary message", lambda: websocket_frame(0x2, b"{}"), "binary JSON-RPC"),
            ("invalid utf8", lambda: websocket_frame(0x1, b"\xff"), "invalid UTF-8"),
            ("unexpected continuation", lambda: websocket_frame(0x0, b"{}"), "continuation frame"),
            ("fragmented control", lambda: websocket_frame(0x9, b"x", final=False), "invalid WebSocket control frame"),
            ("invalid close payload", lambda: websocket_frame(0x8, b"x"), "invalid WebSocket close payload"),
            ("invalid JSON", lambda: websocket_frame(0x1, b"not json"), "malformed JSON-RPC"),
        ]
        for name, make_bad_frame, message in bad_frames:
            with self.subTest(name=name):
                def responses(request):
                    if request["method"] == "initialize":
                        return [websocket_frame(0x1, json.dumps({"id": request["id"], "result": {}}).encode())]
                    return [make_bad_frame()]

                peer = FakeUnixWebSocketPeer(self.root / f"bad-{name.replace(' ', '-')}.sock", responses)
                with adapter.AppServer(timeout=0.5, socket_path=peer.socket_path) as server:
                    with self.assertRaisesRegex(adapter.AdapterError, message):
                        server.call("thread/list", {})
                peer.join()
                self.assertFalse((self.root / "artifacts").exists())

    def test_app_server_timeout_and_connection_loss_fail_closed_table(self):
        cases = [
            ("timeout", lambda request: [websocket_frame(0x1, json.dumps({"id": request["id"], "result": {}}).encode())]
             if request["method"] == "initialize" else (), "timed out during thread/list"),
            ("close during initialize", lambda _request: "disconnect", "connection closed"),
        ]
        for name, responses, message in cases:
            with self.subTest(name=name):
                peer = FakeUnixWebSocketPeer(self.root / f"failure-{name.replace(' ', '-')}.sock", responses)
                if name == "timeout":
                    with adapter.AppServer(timeout=0.05, socket_path=peer.socket_path) as server:
                        with self.assertRaisesRegex(adapter.AdapterError, message):
                            server.call("thread/list", {})
                else:
                    with self.assertRaisesRegex(adapter.AdapterError, message):
                        adapter.AppServer(timeout=0.2, socket_path=peer.socket_path)
                peer.join()

    def test_transport_inventory_is_read_only_and_does_not_initialize_missing_socket(self):
        def responses(message):
            if message["method"] == "initialize":
                return [websocket_frame(0x1, json.dumps({"id": message["id"], "result": {}}).encode())]
            if message["method"] == "thread/list":
                return [websocket_frame(0x1, json.dumps({"id": message["id"], "result": {"data": []}}).encode())]
            return []

        peer = FakeUnixWebSocketPeer(self.root / "inventory.sock", responses)
        out = io.StringIO()
        with contextlib.redirect_stdout(out), adapter.AppServer(timeout=0.5, socket_path=peer.socket_path) as server:
            adapter.inspect(server)
        peer.join()
        methods = [request["method"] for request in peer.requests]
        self.assertEqual(methods, ["initialize", "initialized", "thread/list"])
        self.assertEqual(json.loads(out.getvalue())["tasks"], [])
        self.assertFalse((self.root / "artifacts").exists(), "transport inventory wrote persistent adapter state")

        absent_home = self.root / "absent-codex-home"
        with mock.patch.dict(os.environ, {"CODEX_HOME": str(absent_home)}):
            with self.assertRaisesRegex(adapter.AdapterError, "cannot connect to Codex app-server socket"):
                adapter.AppServer(timeout=0.05)
        self.assertFalse(absent_home.exists(), "connecting to a missing socket created CODEX_HOME")

    def test_resume_uses_exact_matching_task_after_live_state_check(self):
        other = row(TASK2, "b" * 64, turn_status="interrupted")
        resume_root = self.root / "resume-repo"
        resume_root.mkdir()
        adapter.run(["git", "init", "--initial-branch=main", str(resume_root)])
        server = FakeServer([row(turn_status="interrupted", cwd=str(resume_root)), other])
        with mock.patch.object(adapter, "check_live_writer", return_value=None):
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                adapter.resume(server, {"action_id": ACTION, "task_id": TASK, "prompt": "continue"})
        resume_call = next(params for method, params in server.calls if method == "thread/resume")
        self.assertEqual(resume_call["threadId"], TASK)
        self.assertEqual(json.loads(out.getvalue())["task_id"], TASK)
        self.assertEqual(json.loads(out.getvalue())["url"], f"codex://threads/{TASK}")

    def test_concurrent_resume_is_serialized_to_one_writer(self):
        resume_root = self.root / "concurrent-resume-repo"
        resume_root.mkdir()
        adapter.run(["git", "init", "--initial-branch=main", str(resume_root)])
        server = FakeServer([row(turn_status="interrupted", cwd=str(resume_root))])
        request_data = {"action_id": ACTION, "task_id": TASK, "prompt": "continue"}
        with mock.patch.object(adapter, "check_live_writer", return_value=None), \
             mock.patch.object(adapter, "output_task"):
            def attempt():
                try:
                    adapter.resume(server, request_data)
                    return "resumed"
                except adapter.AdapterError as exc:
                    return str(exc)
            with ThreadPoolExecutor(max_workers=2) as pool:
                outcomes = list(pool.map(lambda _: attempt(), range(2)))
        self.assertEqual(outcomes.count("resumed"), 1)
        self.assertEqual(sum("task is running" in item for item in outcomes), 1)
        self.assertEqual(sum(method == "thread/resume" for method, _ in server.calls), 1)
        self.assertEqual(sum(method == "turn/start" for method, _ in server.calls), 1)

    def test_mismatched_task_id_fails_before_resume(self):
        server = FakeServer([row(), row(TASK2, "b" * 64)])
        with self.assertRaisesRegex(adapter.AdapterError, "exact real Codex task UUID"):
            adapter.resume(server, {"action_id": ACTION, "task_id": "client-pending-7"})
        self.assertFalse(server.calls)

    def test_multiple_tasks_with_same_action_id_fail_closed(self):
        server = FakeServer([row(), row(TASK2)])
        with self.assertRaisesRegex(adapter.AdapterError, "multiple Codex tasks"):
            adapter.launch(server, request())
        self.assertEqual(server.created, 0)

    def test_different_action_cannot_create_a_second_worker_for_same_issue(self):
        server = FakeServer([row(action_id="b" * 64, name="agent-kit #16: Existing worker")])
        with self.assertRaisesRegex(adapter.AdapterError, "already has a Codex worker"):
            adapter.launch(server, request())
        self.assertEqual(server.created, 0)

    def test_invalid_action_ids_and_issue_numbers_are_rejected_without_filesystem_effects(self):
        cases = [("bad", 16), ("a" * 63, 16), ("a" * 63 + ";touch-owned", 16), (ACTION, 0), (ACTION, -1), (ACTION, True)]
        server = FakeServer()
        for action_id, issue in cases:
            with self.subTest(action_id=action_id[:12], issue=issue):
                value = {**request(), "action_id": action_id, "issue": issue}
                with self.assertRaises(adapter.AdapterError):
                    adapter.launch(server, value)
        self.assertEqual(server.created, 0)
        self.assertEqual(list((self.root / "artifacts").rglob("*")), [])

    def test_launch_requires_issue_open_and_no_existing_issue_pr(self):
        server = FakeServer()
        with mock.patch.object(adapter, "issue_data", side_effect=adapter.AdapterError("issue is not open; refusing to launch a worker")):
            with self.assertRaisesRegex(adapter.AdapterError, "issue is not open"):
                adapter.launch(server, request())
        with mock.patch.object(adapter, "issue_data", return_value={"state": "OPEN", "title": "x"}), \
             mock.patch.object(adapter, "open_pr_for_issue", return_value={"number": 27, "url": "https://github.com/yichen/agent-kit/pull/27"}):
            with self.assertRaisesRegex(adapter.AdapterError, "open PR already references"):
                adapter.launch(server, request())
        self.assertEqual(server.created, 0)

    def test_open_pr_issue_reference_parser(self):
        with mock.patch.object(adapter, "run", return_value=json.dumps([
            {"number": 1, "title": "Feature #160", "body": ""},
            {"number": 2, "title": "Worker", "body": "Fixes #16"},
        ])):
            self.assertEqual(adapter.open_pr_for_issue(REPO, 16)["number"], 2)

    def test_preflight_creates_verified_sibling_worktree_from_fresh_default_branch(self):
        source = self.root / "repo"
        remote = self.root / "origin.git"
        source.mkdir()
        adapter.run(["git", "init", "--bare", "--initial-branch=main", str(remote)])
        adapter.run(["git", "init", "--initial-branch=main", str(source)])
        adapter.run(["git", "-C", str(source), "config", "user.email", "test@example.invalid"])
        adapter.run(["git", "-C", str(source), "config", "user.name", "Test"])
        (source / "README.md").write_text("baseline\n")
        adapter.run(["git", "-C", str(source), "add", "README.md"])
        adapter.run(["git", "-C", str(source), "commit", "-m", "baseline"])
        adapter.run(["git", "-C", str(source), "remote", "add", "origin", str(remote)])
        adapter.run(["git", "-C", str(source), "push", "-u", "origin", "main"])
        real_run, real_git = adapter.run, adapter.git
        def fake_run(argv, **kwargs):
            if argv[0] == "gh":
                return json.dumps({"defaultBranchRef": {"name": "main"}, "viewerPermission": "WRITE"})
            return real_run(argv, **kwargs)
        def fake_git(path, *args, **kwargs):
            if args == ("remote", "get-url", "origin"):
                return "git@github.com:yichen/agent-kit.git"
            return real_git(path, *args, **kwargs)
        with mock.patch.object(adapter, "run", side_effect=fake_run), \
             mock.patch.object(adapter, "git", side_effect=fake_git):
            repo, target, branch_name, ref = adapter.prepare_worktree(source, 16, ACTION)
            self.assertEqual((repo, ref), (REPO, "origin/main"))
            self.assertTrue(target.is_dir())
            self.assertEqual(real_git(target, "branch", "--show-current"), branch_name)
            self.assertEqual(real_git(target, "rev-parse", "HEAD"), real_git(source, "rev-parse", "origin/main"))
            self.assertTrue(adapter.discard_worktree(source, target, branch_name))
            self.assertFalse(target.exists())

    def test_repository_write_permission_is_required(self):
        source = self.root / "repo"
        adapter.run(["git", "init", "--initial-branch=main", str(source)])
        adapter.run(["git", "-C", str(source), "remote", "add", "origin", "git@github.com:yichen/agent-kit.git"])
        real_run = adapter.run
        def fake_run(argv, **kwargs):
            if argv[0] == "gh":
                return json.dumps({"defaultBranchRef": {"name": "main"}, "viewerPermission": "READ"})
            return real_run(argv, **kwargs)
        with mock.patch.object(adapter, "run", side_effect=fake_run):
            with self.assertRaisesRegex(adapter.AdapterError, "lacks repository write permission"):
                adapter.prepare_worktree(source, 16, ACTION)


if __name__ == "__main__":
    unittest.main(verbosity=2)
