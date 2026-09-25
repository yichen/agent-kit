import importlib.util
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest
from unittest import mock

sys.dont_write_bytecode = True
SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))
spec = importlib.util.spec_from_file_location("boss_scheduler", SCRIPTS / "scheduler.py")
scheduler = importlib.util.module_from_spec(spec)
spec.loader.exec_module(scheduler)
watch_spec = importlib.util.spec_from_file_location("boss_watchdog", SCRIPTS / "watchdog.py")
watchdog = importlib.util.module_from_spec(watch_spec)
watch_spec.loader.exec_module(watchdog)

NOW = datetime(2026, 9, 25, 21, 0, tzinfo=timezone.utc)
TASK = "01a0da55-764b-7382-a952-31dd327b1450"


def sample_state(*, scan_at=None, tickets=None):
    return {"version": 1, "repo": "yichen/agent-kit",
            "monitor": {"name": "agent-kit-12-coordinator", "last_scan_at": scan_at or NOW.isoformat()},
            "tickets": tickets or {"18": {"issue": 18, "status": "launched", "depends": [15, 17],
                                             "task_id": TASK, "prs": []}}}


def issue_rows(*rows):
    return [{"number": number, "state": state} for number, state in rows]


def check_report(state=None, issues=None, prs=None, tasks=None, claims=None):
    return scheduler.compare(state or sample_state(), issues or issue_rows((18, "OPEN")), prs or [],
                             tasks or {TASK: "running"}, claims or [{"issue": 18, "phase": "implementation",
                             "generation": 1, "owner": "codex-parent", "status": "active"}], NOW)


class SchedulerTests(unittest.TestCase):
    def test_matching_agent_kit_state_is_shadow_only(self):
        report = check_report()
        self.assertFalse(report["disputed"])
        self.assertFalse(report["assignment_enabled"])
        self.assertFalse(report["pi_dispatch_enabled"])
        self.assertFalse(report["eligible_for_cutover"])

    def test_dispute_classifier_table(self):
        cases = [
            ("issue closed in GitHub", sample_state(), issue_rows((18, "CLOSED")), [], {TASK: "running"},
             [{"issue": 18, "phase": "implementation"}], {"ticket_state"}),
            ("missing live task", sample_state(), issue_rows((18, "OPEN")), [], {TASK: "unknown"},
             [{"issue": 18, "phase": "implementation"}], {"task_missing"}),
            ("terminal task while ticket launched", sample_state(), issue_rows((18, "OPEN")), [], {TASK: "completed"},
             [{"issue": 18, "phase": "implementation"}], {"task_terminal"}),
            ("duplicate active claims", sample_state(), issue_rows((18, "OPEN")), [], {TASK: "running"},
             [{"issue": 18, "phase": "implementation"}, {"issue": 18, "phase": "pr_ci_repair"}], {"claim_count", "multiple_active_claims"}),
            ("untracked open PR", sample_state(), issue_rows((18, "OPEN")), [{"number": 99, "state": "OPEN", "headRefOid": "a" * 40}],
             {TASK: "running"}, [{"issue": 18, "phase": "implementation"}], {"untracked_open_pr"}),
        ]
        for name, state, issues, prs, tasks, claims, expected in cases:
            with self.subTest(name=name):
                actual = check_report(state, issues, prs, tasks, claims)
                self.assertTrue(actual["disputed"])
                self.assertEqual({row["kind"] for row in actual["disputes"]}, expected)

    def test_full_capacity_and_pi_gate_never_claim_or_launch(self):
        report = check_report(claims=[{"issue": 18, "phase": phase} for phase in ("implementation",)])
        self.assertEqual(report["inventory"]["remaining_claim_capacity"], 0)
        self.assertFalse(report["assignment_enabled"])
        self.assertFalse(report["pi_dispatch_enabled"])
        self.assertIn("issue #20 remains gated", report["pi_dispatch"])
        self.assertFalse(any("action" in key or "launch" in key for key in report))

    def test_dependent_adapter_issues_are_not_pilot_candidates(self):
        tickets = {
            "18": {"issue": 18, "status": "launched", "depends": [15, 17], "task_id": TASK, "prs": []},
            "19": {"issue": 19, "status": "ready", "depends": [18], "prs": []},
            "20": {"issue": 20, "status": "ready", "depends": [18], "prs": []},
        }
        report = check_report(sample_state(tickets=tickets), issue_rows((18, "OPEN"), (19, "OPEN"), (20, "OPEN")))
        self.assertEqual(report["pilot_candidates"], [])
        self.assertIn("no dependency-ready", report["pilot_blocker"])
        self.assertFalse(report["eligible_for_cutover"])

    def test_stale_ledger_fails_closed(self):
        stale = NOW - timedelta(minutes=21)
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / "boss.json"
            path.write_text(json.dumps(sample_state(scan_at=stale.isoformat())))
            before = path.read_bytes()
            with self.assertRaisesRegex(scheduler.SchedulerError, "stale"):
                scheduler.read_boss_state(path, "yichen/agent-kit", NOW)
            self.assertEqual(path.read_bytes(), before)

    def test_github_outage_does_not_produce_a_successful_inventory(self):
        class Failed:
            returncode = 1
            stdout = ""
            stderr = "API rate limit"
        calls = []
        def runner(command, **_kwargs):
            calls.append(command)
            return Failed()
        with self.assertRaisesRegex(scheduler.SchedulerError, "gh inventory failed"):
            scheduler.gh_inventory("yichen/agent-kit", runner=runner)
        self.assertEqual(len(calls), 1)
        self.assertIn("issue", calls[0])

    def test_github_outage_does_not_refresh_last_success_receipt(self):
        with tempfile.TemporaryDirectory() as root:
            root = Path(root)
            state_path = root / "boss.json"
            receipt = root / "last-success.json"
            state_path.write_text(json.dumps(sample_state()))
            original = b'{"as_of":"old-receipt"}\n'
            receipt.write_bytes(original)
            with mock.patch.object(scheduler, "repo_identity", return_value="yichen/agent-kit"), \
                 mock.patch.object(scheduler, "boss_state_path", return_value=state_path), \
                 mock.patch.object(scheduler, "gh_inventory", side_effect=scheduler.SchedulerError("GitHub outage")), \
                 mock.patch.object(sys, "argv", ["scheduler.py", "--repo", str(root), "--last-success", str(receipt)]):
                self.assertEqual(scheduler.main(), 2)
            self.assertEqual(receipt.read_bytes(), original)

    def test_scope_rejects_other_repo_and_shell_injection_text(self):
        for repo in ("yichen/LearnRise", "yichen/agent-kit;touch /tmp/nope", "../agent-kit"):
            with self.subTest(repo=repo):
                with self.assertRaisesRegex(scheduler.SchedulerError, "scoped to yichen/agent-kit"):
                    scheduler.gh_inventory(repo, runner=lambda *_args, **_kwargs: self.fail("runner must not execute"))

    def test_github_inventory_rejects_malformed_rows_table(self):
        class Success:
            returncode = 0
            stderr = ""
            def __init__(self, data): self.stdout = json.dumps(data)
        cases = [
            ("valid issue inventory", [{"number": 18, "state": "OPEN"}], [{"number": 22, "state": "MERGED"}], False),
            ("boolean issue number", [{"number": True, "state": "OPEN"}], [], True),
            ("injection text issue number", [{"number": "18;touch /tmp/nope", "state": "OPEN"}], [], True),
            ("unknown PR state", [{"number": 18, "state": "OPEN"}], [{"number": 22, "state": "PENDING"}], True),
        ]
        for name, issues, prs, invalid in cases:
            with self.subTest(name=name):
                calls = iter([Success(issues), Success(prs)])
                if invalid:
                    with self.assertRaisesRegex(scheduler.SchedulerError, "invalid row"):
                        scheduler.gh_inventory("yichen/agent-kit", runner=lambda *_args, **_kwargs: next(calls))
                else:
                    self.assertEqual(scheduler.gh_inventory("yichen/agent-kit", runner=lambda *_args, **_kwargs: next(calls)),
                                     (issues, prs))

    def test_read_only_claim_query_leaves_database_and_sidecars_unchanged(self):
        with tempfile.TemporaryDirectory() as root:
            db = Path(root) / "operations.sqlite3"
            con = sqlite3.connect(db)
            con.execute("CREATE TABLE claims(repo TEXT,issue INTEGER,phase TEXT,generation INTEGER,owner TEXT,status TEXT,acquired_at TEXT)")
            con.execute("INSERT INTO claims VALUES('yichen/agent-kit',18,'implementation',1,'worker','active','now')")
            con.commit()
            con.close()
            before = {p.name: (p.read_bytes(), p.stat().st_mtime_ns) for p in Path(root).iterdir()}
            result = scheduler.read_claims(db, "yichen/agent-kit")
            after = {p.name: (p.read_bytes(), p.stat().st_mtime_ns) for p in Path(root).iterdir()}
            self.assertEqual(len(result), 1)
            self.assertEqual(before, after)

    def test_live_inventory_uses_list_and_read_only(self):
        calls = []
        class Server:
            def __enter__(self): return self
            def __exit__(self, *_args): pass
            def threads(self):
                calls.append("thread/list")
                return [{"id": TASK}]
            def read(self, task_id):
                calls.append("thread/read")
                self.assert_task = task_id
                return {"status": {"type": "active"}, "turns": []}
        result = scheduler.live_tasks({TASK}, server_factory=Server)
        self.assertEqual(result, {TASK: "running"})
        self.assertEqual(calls, ["thread/list", "thread/read"])

    def test_stopped_scheduler_and_disputed_receipt_fail_watchdog(self):
        with tempfile.TemporaryDirectory() as root:
            receipt = Path(root) / "last-success.json"
            self.assertFalse(watchdog.healthy(receipt, NOW)[0])
            receipt.write_text(json.dumps({"as_of": NOW.isoformat(), "mode": "shadow",
                                           "assignment_enabled": False, "disputed": True}))
            ok, message = watchdog.healthy(receipt, NOW)
            self.assertFalse(ok)
            self.assertIn("unresolved ownership disputes", message)

    def test_watchdog_rejects_future_and_old_receipts(self):
        cases = [
            (NOW + timedelta(minutes=3), "future"),
            (NOW - timedelta(minutes=33), "33 minutes old"),
        ]
        for checked, expected in cases:
            with self.subTest(checked=checked):
                with tempfile.TemporaryDirectory() as root:
                    path = Path(root) / "receipt.json"
                    path.write_text(json.dumps({"as_of": checked.isoformat(), "mode": "shadow",
                                               "assignment_enabled": False, "disputed": False}))
                    ok, message = watchdog.healthy(path, NOW)
                    self.assertFalse(ok)
                    self.assertIn(expected, message)


if __name__ == "__main__":
    unittest.main()
