#!/usr/bin/env python3
import contextlib
import io
import json
import os
from pathlib import Path
import sys
import tempfile
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
