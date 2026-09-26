import importlib.util
import io
import json
from contextlib import redirect_stdout
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
ACTION = "1891040975ac93ed1da2b6cdf3bd2f5d6064b642ce9624c6d1695a43c51c0263"
EXTRA_TASK = "01a0da55-764b-7382-a952-31dd327b1451"


def sample_state(*, scan_at=None, tickets=None):
    return {"version": 1, "repo": "yichen/agent-kit",
            "monitor": {"name": "agent-kit-12-coordinator", "last_scan_at": scan_at or NOW.isoformat()},
            "tickets": tickets or {"18": {"issue": 18, "status": "launched", "depends": [15, 17],
                                             "task_id": TASK, "action_id": ACTION, "prs": []}}}


def issue_rows(*rows):
    return [{"number": number, "state": state} for number, state in rows]


def claim(issue=18, phase="implementation"):
    return {"issue": issue, "phase": phase, "generation": 1, "owner": "codex-parent", "status": "active",
            "action_ids": ACTION, "task_ids": TASK}


def check_report(state=None, issues=None, prs=None, tasks=None, claims=None, now=NOW):
    tasks = tasks or {TASK: "running"}
    if "by_id" not in tasks:
        tasks = {"by_id": tasks, "relevant": [{"id": task_id, "status": status} for task_id, status in tasks.items()],
                 "all_live_threads": len(tasks)}
    return scheduler.compare(state or sample_state(), issues or issue_rows((18, "OPEN")), prs or [],
                             tasks, claims or [claim()], now)


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
             [claim()], {"ticket_state"}),
            ("missing live task", sample_state(), issue_rows((18, "OPEN")), [], {TASK: "unknown"},
             [claim()], {"task_missing"}),
            ("terminal task while ticket launched", sample_state(), issue_rows((18, "OPEN")), [], {TASK: "completed"},
             [claim()], {"task_terminal"}),
            ("duplicate active claims", sample_state(), issue_rows((18, "OPEN")), [], {TASK: "running"},
             [claim(), claim(phase="pr_ci_repair")], {"claim_count", "multiple_active_claims"}),
            ("untracked open PR", sample_state(), issue_rows((18, "OPEN")), [{"number": 99, "state": "OPEN", "headRefOid": "a" * 40}],
             {TASK: "running"}, [claim()], {"untracked_open_pr"}),
        ]
        for name, state, issues, prs, tasks, claims, expected in cases:
            with self.subTest(name=name):
                actual = check_report(state, issues, prs, tasks, claims)
                self.assertTrue(actual["disputed"])
                self.assertEqual({row["kind"] for row in actual["disputes"]}, expected)

    def test_full_capacity_and_pi_gate_never_claim_or_launch(self):
        report = check_report(claims=[claim(phase="implementation")])
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
        self.assertEqual(report["dependency_ready_issues"], [])
        self.assertIsNone(report["low_risk_pilot_issue"])
        self.assertIn("no dependency-ready", report["pilot_blocker"])
        self.assertFalse(report["eligible_for_cutover"])

    def test_dependency_readiness_alone_never_approves_a_low_risk_pilot(self):
        tickets = {"21": {"issue": 21, "status": "ready", "depends": [], "prs": []}}
        report = check_report(sample_state(tickets=tickets), issue_rows((21, "OPEN")), claims=[])
        self.assertEqual(report["dependency_ready_issues"], [21])
        self.assertIsNone(report["low_risk_pilot_issue"])
        self.assertIn("explicit human pilot selection is missing", report["pilot_blocker"])
        self.assertFalse(report["eligible_for_cutover"])

    def test_stale_manual_scan_does_not_break_recurring_shadow_scheduler(self):
        stale = NOW - timedelta(minutes=46)
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / "boss.json"
            path.write_text(json.dumps(sample_state(scan_at=stale.isoformat())))
            before = path.read_bytes()
            state = scheduler.read_boss_state(path, "yichen/agent-kit", NOW)
            ages = []
            for minutes in (15, 30, 45):
                instant = NOW + timedelta(minutes=minutes)
                state = scheduler.read_boss_state(path, "yichen/agent-kit", instant)
                report = check_report(state=state, issues=issue_rows((18, "OPEN")),
                                      tasks={"by_id": {TASK: "running"}, "relevant": [{"id": TASK, "status": "running"}], "all_live_threads": 1}, now=instant)
                ages.append(report["ledger_scan_age_minutes"])
                self.assertFalse(report["assignment_enabled"])
            self.assertEqual(ages, [61.0, 76.0, 91.0])
            self.assertTrue(report["ledger_scan_stale"])
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
                 mock.patch.object(scheduler, "gh_inventory", side_effect=scheduler.SchedulerError("GitHub outage")):
                self.assertEqual(scheduler.main(["--repo", str(root), "--last-success", str(receipt)]), 2)
            self.assertEqual(receipt.read_bytes(), original)

    def test_main_is_no_write_by_default_and_receipt_is_explicit_opt_in(self):
        for opt_in in (False, True):
            with self.subTest(opt_in=opt_in), tempfile.TemporaryDirectory() as root:
                root = Path(root)
                state_path = root / "boss.json"
                db = root / "operations.sqlite3"
                receipt = root / "last-success.json"
                state_path.write_text(json.dumps(sample_state()))
                con = sqlite3.connect(db)
                con.execute("CREATE TABLE canary(value TEXT)")
                con.execute("INSERT INTO canary VALUES('unchanged')")
                con.commit()
                con.close()
                receipt_bytes = b'{"as_of":"old-receipt"}\n'
                receipt.write_bytes(receipt_bytes)
                state_before, db_before = state_path.read_bytes(), db.read_bytes()
                argv = ["scheduler.py", "--repo", str(root), "--operations-db", str(db), "--last-success", str(receipt)]
                if opt_in:
                    argv.append("--write-last-success")
                with mock.patch.object(scheduler, "repo_identity", return_value="yichen/agent-kit"), \
                     mock.patch.object(scheduler, "boss_state_path", return_value=state_path), \
                     mock.patch.object(scheduler, "gh_inventory", return_value=(issue_rows((18, "OPEN")), [])), \
                     mock.patch.object(scheduler, "live_tasks", return_value={TASK: "running"}), \
                     mock.patch.object(scheduler, "read_claims", return_value=[claim()]), \
                     redirect_stdout(io.StringIO()):
                    self.assertEqual(scheduler.main(argv[1:]), 0)
                self.assertEqual(state_path.read_bytes(), state_before)
                self.assertEqual(db.read_bytes(), db_before)
                if opt_in:
                    self.assertNotEqual(receipt.read_bytes(), receipt_bytes)
                    self.assertEqual(json.loads(receipt.read_text())["assignment_enabled"], False)
                else:
                    self.assertEqual(receipt.read_bytes(), receipt_bytes)

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
            con.execute("CREATE TABLE actions(action_id TEXT,repo TEXT,issue INTEGER,phase TEXT,generation INTEGER,owner TEXT,task_id TEXT)")
            con.execute("INSERT INTO claims VALUES('yichen/agent-kit',18,'implementation',1,'worker','active','now')")
            con.execute("INSERT INTO actions VALUES('a','yichen/agent-kit',18,'implementation',1,'worker','task')")
            con.commit()
            con.close()
            before = {p.name: (p.read_bytes(), p.stat().st_mtime_ns) for p in Path(root).iterdir()}
            result = scheduler.read_claims(db, "yichen/agent-kit")
            after = {p.name: (p.read_bytes(), p.stat().st_mtime_ns) for p in Path(root).iterdir()}
            self.assertEqual(len(result), 1)
            self.assertEqual(before, after)

    def test_live_inventory_scans_agent_kit_tasks_and_skips_unrelated_threads(self):
        calls = []
        class Server:
            def __enter__(self): return self
            def __exit__(self, *_args): pass
            def threads(self):
                calls.append("thread/list")
                return [{"id": TASK, "status": {"type": "active"}}, {"id": EXTRA_TASK, "status": {"type": "active"}, "name": "agent-kit #18: duplicate",
                         "cwd": "/tmp/agent-kit-issue-18-deadbeef"},
                        {"id": "01a0da55-764b-7382-a952-31dd327b1452", "name": "unrelated task", "cwd": "/tmp/other"}]
            def read(self, task_id):
                raise AssertionError("scheduler must not hydrate unbounded thread history")
        result = scheduler.live_tasks({TASK}, Path("/tmp/agent-kit"), server_factory=Server)
        self.assertEqual(result["by_id"], {TASK: "running", EXTRA_TASK: "running"})
        self.assertEqual(result["all_live_threads"], 3)
        self.assertEqual(len(result["relevant"]), 2)
        self.assertEqual(calls, ["thread/list"])

    def test_not_loaded_live_thread_is_unknown_without_reading_history(self):
        calls = []
        class Server:
            def __enter__(self): return self
            def __exit__(self, *_args): pass
            def threads(self):
                calls.append("thread/list")
                return [{"id": EXTRA_TASK, "name": "agent-kit #18: archived worker",
                         "status": {"type": "notLoaded"}, "turns": []}]
            def read(self, _task_id):
                raise AssertionError("not-loaded history must not be hydrated")
        result = scheduler.live_tasks(set(), Path("/tmp/agent-kit"), server_factory=Server)
        self.assertEqual(result["by_id"], {EXTRA_TASK: "unknown"})
        self.assertEqual(result["relevant"][0]["status"], "unknown")
        self.assertEqual(calls, ["thread/list"])

    def test_active_duplicate_or_unmatched_agent_kit_task_is_a_dispute(self):
        cases = [
            ({"by_id": {TASK: "running", EXTRA_TASK: "running"},
              "relevant": [{"id": TASK, "status": "running"}, {"id": EXTRA_TASK, "status": "running", "issue": 18}],
              "all_live_threads": 2}, "duplicate_live_task"),
            ({"by_id": {TASK: "running", EXTRA_TASK: "running"},
              "relevant": [{"id": TASK, "status": "running"}, {"id": EXTRA_TASK, "status": "running"}],
              "all_live_threads": 2}, "unmatched_live_task"),
        ]
        for inventory, expected in cases:
            with self.subTest(expected=expected):
                report = check_report(tasks=inventory)
                self.assertIn(expected, {item["kind"] for item in report["disputes"]})

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
