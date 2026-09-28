import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
from datetime import datetime, timedelta, timezone

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))
import learnrise_event_monitor as monitor
import learnrise_event_watchdog as watchdog

UTC = timezone.utc
HUB = "01a0d565-c171-7120-b828-b04db384021f"


class Result:
    def __init__(self, code=0, stdout=""):
        self.returncode = code
        self.stdout = stdout


class MonitorTests(unittest.TestCase):
    def fixture(self, path, now):
        observed = {"schema_version": 1, "repository": monitor.REPO, "last_checked_utc": now.isoformat(),
                    "dependency_gates": [],
                    "objectives": [{"id": "#1", "github_state": "OPEN", "human_gate": True,
                                    "last_activity_utc": now.isoformat(), "pull_requests": [], "pull_request_states": {}}],
                    "open_pull_requests": [{"number": 2, "head": "a" * 40, "merge": "CLEAN",
                                            "observation": {"checks": [], "review": {"state": "APPROVED"}}}]}
        outbox = {"version": 1, "actions": {"a" * 24: {"state": "pending", "first_seen": now.isoformat(),
                  "action": {"id": "a" * 24, "repo": monitor.REPO, "verb": "REPAIR_PR", "objective": "#1", "pr": 2, "head": "a" * 40}}}}
        supervisor = {"schema_version": 1, "repository": monitor.REPO, "last_scan_utc": now.isoformat(),
                      "actions": {"b" * 20: {"id": "b" * 20, "status": "OPEN", "number": 2,
                                             "head": "a" * 40, "first_seen_utc": now.isoformat(),
                                             "sla_minutes": 30, "kind": "REVIEW"}}}
        monitor.atomic(path / "boss-observed-ledger.json", observed)
        monitor.atomic(path / "boss-action-outbox.json", outbox)
        monitor.atomic(path / "pr-supervisor-state.json", supervisor)
        return {"actions": [outbox["actions"]["a" * 24]["action"]], "overdue": [],
                "inventory": {"as_of": now.isoformat(), "tasks": []}}

    def test_default_dry_run_never_writes_or_runs_processes(self):
        with tempfile.TemporaryDirectory() as tmp:
            state = Path(tmp) / "never-created"
            bad = Path(tmp) / "bad"
            bad.mkdir()
            (bad / "learnrise-monitor-cursor.json").write_text("{")
            with patch.object(monitor, "bounded_run", side_effect=AssertionError("process")), patch.object(monitor, "atomic", side_effect=AssertionError("write")), patch.object(Path, "mkdir", side_effect=AssertionError("mkdir")):
                self.assertEqual(monitor.main(["--state-dir", str(state)]), 2)
                self.assertEqual(monitor.main(["--state-dir", str(bad)]), 2)
            self.assertFalse(state.exists())

    def test_meaningful_changes_table(self):
        cases = [
            ("pr:1", {"head": "a"}, {"head": "a"}, False),
            ("pr:1", {"head": "a"}, {"head": "b"}, True),
            ("pr:1", None, {"head": "a"}, True),
            ("task:x", None, {"status": "running"}, False),
            ("task:x", {"status": "running"}, {"status": "completed"}, True),
            ("objective:#1", {"state": "OPEN"}, {"state": "CLOSED"}, True),
            ("objective:#1", {"activity": "a"}, {"activity": "b"}, True),
            ("boss:a", {"overdue_stage": 0}, {"overdue_stage": 1}, True),
        ]
        for entity, before, after, expected in cases:
            with self.subTest(entity=entity, before=before, after=after):
                self.assertEqual(monitor.meaningful(entity, before, after), expected)

    def test_escalation_and_acknowledged_unchanged(self):
        start = datetime(2026, 9, 28, tzinfo=UTC)
        outbox = {"actions": {"a" * 24: {"action": {"verb": "REPAIR_PR", "objective": "#1", "pr": 1, "head": "b" * 40}, "state": "acknowledged", "first_seen": start.isoformat(), "acknowledged_at": start.isoformat()}}}
        ledger = {"objectives": [], "open_pull_requests": [], "dependency_gates": []}
        supervisor = {"actions": {}}
        inventory = {"tasks": []}
        expected = [(0, 0), (16, 1), (60, 2), (60 + 24 * 60, 3)]
        for minutes, stage in expected:
            with self.subTest(minutes=minutes):
                value = monitor.semantic(ledger, outbox, supervisor, inventory, start + timedelta(minutes=minutes))
                self.assertEqual(value["boss:" + "a" * 24]["overdue_stage"], stage)

    def test_validation_rejects_injection_and_stale_sources(self):
        now = datetime(2026, 9, 28, tzinfo=UTC)
        base = {"schema_version": 1, "repository": monitor.REPO, "last_checked_utc": now.isoformat(), "objectives": [], "open_pull_requests": [], "dependency_gates": []}
        outbox = {"version": 1, "actions": {}}
        supervisor = {"schema_version": 1, "repository": monitor.REPO, "last_scan_utc": now.isoformat(), "actions": {}}
        inventory = {"as_of": now.isoformat(), "tasks": []}
        monitor.validate_sources(base, outbox, supervisor, inventory, HUB, now)
        variants = [
            (dict(base, repository="other/repo"), outbox, supervisor, inventory, HUB),
            (dict(base, last_checked_utc=(now - timedelta(hours=2)).isoformat()), outbox, supervisor, inventory, HUB),
            (base, outbox, supervisor, inventory, HUB + "$(touch /tmp/pwn)"),
            (base, outbox, supervisor, {"as_of": now.isoformat(), "tasks": [{"id": "$(touch /tmp/pwn)", "status": "running"}]}, HUB),
            (base, {"version": 1, "actions": {"a;rm -rf /": {"state": "pending"}}}, supervisor, inventory, HUB),
            (dict(base, open_pull_requests=[{"number": 1, "head": "bad;touch /tmp/pwn"}]), outbox, supervisor, inventory, HUB),
        ]
        for args in variants:
            with self.subTest(args=args):
                with self.assertRaises(monitor.MonitorError):
                    monitor.validate_sources(*args, now)

    def test_open_linked_prs_require_full_open_inventory(self):
        now = monitor.now_utc()
        def pr(number):
            return {"number": number, "head": "a" * 40, "merge": "CLEAN",
                    "observation": {"checks": [], "review": {"state": "APPROVED"}}}
        row = {"id": "#1", "github_state": "OPEN", "pull_requests": [7],
               "pull_request_states": {"7": "OPEN"}}
        base = {"schema_version": 1, "repository": monitor.REPO,
                "last_checked_utc": now.isoformat(), "objectives": [row],
                "open_pull_requests": [pr(7)], "dependency_gates": []}
        outbox = {"version": 1, "actions": {}}
        supervisor = {"schema_version": 1, "repository": monitor.REPO,
                      "last_scan_utc": now.isoformat(), "actions": {}}
        inventory = {"as_of": now.isoformat(), "tasks": []}
        cases = [
            ("complete", [7], {"7": "OPEN"}, [7], True),
            ("empty_inventory", [7], {"7": "OPEN"}, [], False),
            ("wrong_pr_only", [7], {"7": "OPEN"}, [8], False),
            ("truncated_inventory", [7, 8], {"7": "OPEN", "8": "OPEN"}, [7], False),
            ("merged_pr_absent", [7], {"7": "MERGED"}, [], True),
            ("closed_pr_absent", [7], {"7": "CLOSED"}, [], True),
        ]
        for name, linked, states, open_numbers, valid in cases:
            with self.subTest(name=name):
                ledger = {**base, "objectives": [{**row, "pull_requests": linked,
                                                   "pull_request_states": states}],
                          "open_pull_requests": [pr(number) for number in open_numbers]}
                if valid:
                    monitor.validate_sources(ledger, outbox, supervisor, inventory, HUB, now)
                else:
                    with self.assertRaisesRegex(monitor.MonitorError, "open linked PR missing"):
                        monitor.validate_sources(ledger, outbox, supervisor, inventory, HUB, now)

    def test_generation_distinguishes_red_green_red_and_retry(self):
        red = {"head": "a" * 40, "failed_checks": ["build"]}
        green = {"head": "a" * 40, "failed_checks": []}
        first = monitor.event_id("pr:1", green, red, 1)
        self.assertEqual(first, monitor.event_id("pr:1", green, red, 1))
        self.assertNotEqual(first, monitor.event_id("pr:1", green, red, 3))

    def test_acked_supervisor_remains_visible_and_escalates(self):
        now = monitor.now_utc()
        key = "b" * 20
        ledger = {"objectives": [], "open_pull_requests": [], "dependency_gates": []}
        supervisor = {"schema_version": 1, "repository": monitor.REPO, "last_scan_utc": now.isoformat(),
                      "actions": {key: {"id": key, "status": "ACKED", "number": 2, "head": "a" * 40,
                                        "first_seen_utc": (now - timedelta(hours=2)).isoformat(),
                                        "acknowledged_at_utc": (now - timedelta(minutes=40)).isoformat(),
                                        "sla_minutes": 30, "kind": "REVIEW"}}}
        inventory = {"as_of": now.isoformat(), "tasks": []}
        monitor.validate_sources({**ledger, "schema_version": 1, "repository": monitor.REPO,
                                  "last_checked_utc": now.isoformat()},
                                 {"version": 1, "actions": {}}, supervisor, inventory, HUB, now)
        snap = monitor.semantic(ledger, {"actions": {}}, supervisor, inventory, now)
        self.assertEqual(snap["supervisor:" + key]["status"], "ACKED")
        self.assertEqual(snap["supervisor:" + key]["overdue_stage"], 1)

    def test_dependency_gate_satisfaction_transition(self):
        now = monitor.now_utc()
        ledger = {"objectives": [], "open_pull_requests": [],
                  "dependency_gates": [{"id": "gate:device-acceptance", "satisfied": False}]}
        before = monitor.semantic(ledger, {"actions": {}}, {"actions": {}}, {"tasks": []}, now)
        ledger["dependency_gates"][0]["satisfied"] = True
        after = monitor.semantic(ledger, {"actions": {}}, {"actions": {}}, {"tasks": []}, now)
        self.assertTrue(monitor.meaningful("dependency:gate:device-acceptance",
                                           before["dependency:gate:device-acceptance"],
                                           after["dependency:gate:device-acceptance"]))
        self.assertNotEqual(monitor.event_id("dependency:gate:device-acceptance", before, after, 1),
                            monitor.event_id("dependency:gate:device-acceptance", after, before, 2))

    def test_live_scan_transitions_deliver_owner_task_issue_and_gate_events(self):
        task_id = "01a0d565-c171-7120-b828-b04db384021f"
        cases = [
            ("owner_loss", "boss:" + "c" * 24),
            ("task_completion", "task:" + task_id),
            ("issue_closure", "objective:#1"),
            ("human_gate_activity", "objective:#1"),
        ]
        for change, expected_entity in cases:
            with self.subTest(change=change), tempfile.TemporaryDirectory() as tmp:
                path = Path(tmp)
                now = monitor.now_utc()
                report = self.fixture(path, now)
                if change == "task_completion":
                    report["inventory"]["tasks"] = [{"id": task_id, "status": "running"}]
                args = ["--apply", "--state-dir", str(path),
                        "--observed", str(path / "boss-observed-ledger.json"),
                        "--outbox", str(path / "boss-action-outbox.json"),
                        "--supervisor", str(path / "pr-supervisor-state.json")]
                queued = []
                bridge_calls = []
                def runner(argv, timeout=120):
                    if "queue" in argv:
                        queued.append(json.loads(argv[-1]))
                        return Result()
                    bridge_calls.append(argv)
                    return Result(3, json.dumps(report))
                with patch.object(monitor, "bounded_run", side_effect=runner):
                    self.assertEqual(monitor.main(args), 0)
                    self.assertEqual([event["entity"] for event in queued], ["baseline"])
                    self.assertEqual(monitor.main(args), 0)
                    self.assertEqual(len(queued), 1)
                    if change == "owner_loss":
                        outbox_path = path / "boss-action-outbox.json"
                        outbox = monitor.read_json(outbox_path)
                        new_id = "c" * 24
                        action = {"id": new_id, "repo": monitor.REPO, "verb": "RECOVER_OWNER",
                                  "objective": "#1", "pr": 2, "head": "a" * 40}
                        outbox["actions"][new_id] = {"state": "pending", "first_seen": now.isoformat(),
                                                      "action": action}
                        monitor.atomic(outbox_path, outbox)
                        report["actions"].append(action)
                    elif change == "task_completion":
                        report["inventory"]["tasks"][0]["status"] = "completed"
                    else:
                        observed_path = path / "boss-observed-ledger.json"
                        observed = monitor.read_json(observed_path)
                        row = observed["objectives"][0]
                        if change == "issue_closure":
                            row["github_state"] = "CLOSED"
                        else:
                            row["last_activity_utc"] = (now + timedelta(minutes=1)).isoformat()
                        monitor.atomic(observed_path, observed)
                    self.assertEqual(monitor.main(args), 0)
                    self.assertEqual([event["entity"] for event in queued[1:]], [expected_entity])
                    self.assertEqual(queued[1]["repository"], monitor.REPO)
                    self.assertEqual(monitor.main(args), 0)
                    self.assertEqual(len(queued), 2)
                    self.assertEqual(len(bridge_calls), 4)

    def test_overlapping_monitor_run_does_not_scan_or_queue_twice(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp)
            report = self.fixture(path, monitor.now_utc())
            args = ["--apply", "--state-dir", str(path),
                    "--observed", str(path / "boss-observed-ledger.json"),
                    "--outbox", str(path / "boss-action-outbox.json"),
                    "--supervisor", str(path / "pr-supervisor-state.json")]
            nested_results, bridge_calls, queued = [], [], []
            def runner(argv, timeout=120):
                if "queue" in argv:
                    queued.append(json.loads(argv[-1]))
                    return Result()
                bridge_calls.append(argv)
                nested_results.append(monitor.main(args))
                return Result(3, json.dumps(report))
            with patch.object(monitor, "bounded_run", side_effect=runner):
                self.assertEqual(monitor.main(args), 0)
            self.assertEqual(nested_results, [2])
            self.assertEqual(len(bridge_calls), 1)
            self.assertEqual([event["entity"] for event in queued], ["baseline"])
            self.assertTrue(monitor.read_json(path / "learnrise-monitor-cursor.json")["initialized"])

    def test_shadow_baseline_queue_failure_restart_and_unchanged(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp)
            now = monitor.now_utc()
            report = self.fixture(path, now)
            args = ["--state-dir", str(path), "--observed", str(path / "boss-observed-ledger.json"),
                    "--outbox", str(path / "boss-action-outbox.json"), "--supervisor", str(path / "pr-supervisor-state.json")]
            queues = []
            def runner(argv, timeout=120):
                if "queue" in argv:
                    queues.append(json.loads(argv[-1]))
                    return Result(1 if len(queues) == 1 else 0)
                return Result(3, json.dumps(report))
            with patch.object(monitor, "bounded_run", side_effect=runner):
                self.assertEqual(monitor.main(["--shadow", *args]), 0)
                self.assertEqual(queues, [])
                self.assertFalse((path / "learnrise-monitor-cursor.json").exists())
                self.assertEqual(monitor.main(["--apply", *args]), 2)
                cursor = monitor.read_json(path / "learnrise-monitor-cursor.json")
                self.assertFalse(cursor["initialized"])
                self.assertEqual(len(cursor["pending"]), 1)
                baseline = queues[0]
                self.assertEqual(baseline["current"]["counts"], {"boss": 1, "supervisor": 1, "human_gates": 1, "dependency_gates": 0})
                self.assertTrue(baseline["current"]["reconcile_only"])
                self.assertEqual(monitor.main(["--apply", *args]), 0)
                self.assertEqual(queues[0]["event_id"], queues[1]["event_id"])
                self.assertTrue(monitor.read_json(path / "learnrise-monitor-cursor.json")["initialized"])
                self.assertEqual(monitor.main(["--apply", *args]), 0)
                self.assertEqual(len(queues), 2)
                self.assertFalse((path / "learnrise-monitor-fault.json").exists())

    def test_new_action_during_failed_baseline_is_delivered_after_retry(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp)
            now = monitor.now_utc()
            report = self.fixture(path, now)
            args = ["--apply", "--state-dir", str(path),
                    "--observed", str(path / "boss-observed-ledger.json"),
                    "--outbox", str(path / "boss-action-outbox.json"),
                    "--supervisor", str(path / "pr-supervisor-state.json")]
            queued = []
            def runner(argv, timeout=120):
                if "queue" in argv:
                    queued.append(json.loads(argv[-1]))
                    return Result(1 if len(queued) == 1 else 0)
                return Result(3, json.dumps(report))
            with patch.object(monitor, "bounded_run", side_effect=runner):
                self.assertEqual(monitor.main(args), 2)
                baseline_id = queued[0]["event_id"]
                outbox_path = path / "boss-action-outbox.json"
                outbox = monitor.read_json(outbox_path)
                new_id = "c" * 24
                new_action = {"id": new_id, "repo": monitor.REPO, "verb": "RECOVER_OWNER",
                              "objective": "#1", "pr": 2, "head": "a" * 40}
                outbox["actions"][new_id] = {"state": "pending", "first_seen": now.isoformat(),
                                              "action": new_action}
                monitor.atomic(outbox_path, outbox)
                report["actions"].append(new_action)
                self.assertEqual(monitor.main(args), 0)
                self.assertEqual([event["entity"] for event in queued],
                                 ["baseline", "baseline", "boss:" + new_id])
                self.assertEqual(queued[0]["current"]["counts"]["boss"], 1)
                self.assertEqual(queued[1]["event_id"], baseline_id)
                self.assertNotEqual(queued[2]["event_id"], baseline_id)
                cursor = monitor.read_json(path / "learnrise-monitor-cursor.json")
                self.assertTrue(cursor["initialized"])
                self.assertEqual(cursor["pending"], [])
                self.assertEqual(monitor.main(args), 0)
                self.assertEqual(len(queued), 3)

    def test_new_action_stays_pending_when_baseline_retry_fails_again(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp)
            now = monitor.now_utc()
            report = self.fixture(path, now)
            args = ["--apply", "--state-dir", str(path),
                    "--observed", str(path / "boss-observed-ledger.json"),
                    "--outbox", str(path / "boss-action-outbox.json"),
                    "--supervisor", str(path / "pr-supervisor-state.json")]
            queued = []
            def runner(argv, timeout=120):
                if "queue" in argv:
                    queued.append(json.loads(argv[-1]))
                    return Result(1 if len(queued) <= 2 else 0)
                return Result(3, json.dumps(report))
            with patch.object(monitor, "bounded_run", side_effect=runner):
                self.assertEqual(monitor.main(args), 2)
                outbox_path = path / "boss-action-outbox.json"
                outbox = monitor.read_json(outbox_path)
                new_id = "c" * 24
                outbox["actions"][new_id] = {"state": "pending", "first_seen": now.isoformat(),
                    "action": {"id": new_id, "repo": monitor.REPO, "verb": "RECOVER_OWNER",
                               "objective": "#1", "pr": 2, "head": "a" * 40}}
                monitor.atomic(outbox_path, outbox)
                self.assertEqual(monitor.main(args), 2)
                pending = monitor.read_json(path / "learnrise-monitor-cursor.json")["pending"]
                self.assertEqual([event["entity"] for event in pending], ["baseline", "boss:" + new_id])
                self.assertEqual(monitor.main(args), 0)
                self.assertEqual([event["entity"] for event in queued],
                                 ["baseline", "baseline", "baseline", "boss:" + new_id])
                self.assertEqual(len({event["event_id"] for event in queued[:3]}), 1)

    def test_queue_timeout_does_not_clear_watchdog_outage_during_retry(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp)
            (path / "learnrise-monitor-mode").write_text("apply\n")
            now = monitor.now_utc()
            report = self.fixture(path, now)
            args = ["--apply", "--state-dir", str(path),
                    "--observed", str(path / "boss-observed-ledger.json"),
                    "--outbox", str(path / "boss-action-outbox.json"),
                    "--supervisor", str(path / "pr-supervisor-state.json")]
            hub_faults = []
            queue_attempts = []
            alert_ids_during_queue = []
            def fault_queue(argv):
                hub_faults.append(json.loads(argv[-1]))
                return Result()
            def runner(argv, timeout=120):
                if "queue" not in argv:
                    return Result(3, json.dumps(report))
                queue_attempts.append(json.loads(argv[-1]))
                if len(queue_attempts) == 1:
                    return Result(1)
                if len(queue_attempts) == 2:
                    self.assertEqual(watchdog.check(path, HUB, now=now + timedelta(seconds=30),
                                                    queue=fault_queue), 2)
                    alert_ids_during_queue.append(monitor.read_json(path / "learnrise-watchdog-alert.json")["event_id"])
                    raise monitor.MonitorError("queue timeout")
                return Result()
            with patch.object(monitor, "bounded_run", side_effect=runner):
                self.assertEqual(monitor.main(args), 2)
                self.assertIsNone(monitor.read_json(path / "learnrise-monitor-receipt.json", optional=True))
                self.assertEqual(watchdog.check(path, HUB, now=now, queue=fault_queue), 2)
                first_alert = monitor.read_json(path / "learnrise-watchdog-alert.json")["event_id"]
                self.assertEqual(len(hub_faults), 1)
                self.assertEqual(monitor.main(args), 2)
                self.assertEqual(alert_ids_during_queue, [first_alert])
                self.assertTrue((path / "learnrise-monitor-fault.json").exists())
                self.assertIsNone(monitor.read_json(path / "learnrise-monitor-receipt.json", optional=True))
                self.assertEqual(watchdog.check(path, HUB, now=now + timedelta(minutes=1),
                                                queue=fault_queue), 2)
                self.assertEqual(len(hub_faults), 1)
                self.assertEqual(monitor.main(args), 0)
                self.assertFalse((path / "learnrise-monitor-fault.json").exists())
                self.assertIsNotNone(monitor.read_json(path / "learnrise-monitor-receipt.json"))
                self.assertEqual(watchdog.check(path, HUB, now=monitor.now_utc(), queue=fault_queue), 0)
                self.assertFalse((path / "learnrise-watchdog-alert.json").exists())
                self.assertEqual(len({event["event_id"] for event in queue_attempts}), 1)

    def test_bridge_failure_persists_fault_without_queue(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp)
            calls = []
            def runner(argv, timeout=120):
                calls.append(argv)
                return Result(2, "bad")
            with patch.object(monitor, "bounded_run", side_effect=runner):
                self.assertEqual(monitor.main(["--apply", "--state-dir", str(path)]), 2)
            self.assertEqual(len(calls), 1)
            self.assertTrue((path / "learnrise-monitor-fault.json").exists())

    def test_pending_ci_does_not_change_semantics_but_failed_ci_does(self):
        now = monitor.now_utc()
        ledger = {"objectives": [], "dependency_gates": [], "open_pull_requests": [{"number": 2, "head": "a" * 40,
                  "merge": "CLEAN", "observation": {"review": {"state": "MISSING"},
                  "checks": [{"name": "build", "state": "PENDING"}]}}]}
        outbox, supervisor, inventory = {"actions": {}}, {"actions": {}}, {"tasks": []}
        pending = monitor.semantic(ledger, outbox, supervisor, inventory, now)
        ledger["open_pull_requests"][0]["observation"]["checks"][0]["state"] = "SUCCESS"
        success = monitor.semantic(ledger, outbox, supervisor, inventory, now)
        self.assertEqual(pending, success)
        ledger["open_pull_requests"][0]["observation"]["checks"][0]["state"] = "FAILURE"
        failed = monitor.semantic(ledger, outbox, supervisor, inventory, now)
        self.assertNotEqual(success, failed)

    def test_watchdog_stopped_monitor_queue_retry_recovery(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp)
            (path / "learnrise-monitor-mode").write_text("shadow\n")
            now = datetime(2026, 9, 28, tzinfo=UTC)
            monitor.atomic(path / "learnrise-monitor-receipt.json", {"as_of": (now - timedelta(minutes=36)).isoformat(), "mode": "shadow"})
            calls = []
            def failed(argv):
                calls.append(argv)
                return Result(1)
            notify = []
            self.assertEqual(watchdog.check(path, HUB, now=now, queue=failed, notify=lambda argv: notify.append(argv)), 2)
            alert = monitor.read_json(path / "learnrise-watchdog-alert.json")
            self.assertFalse(alert["delivered"])
            self.assertEqual(len(notify), 1)
            self.assertEqual(watchdog.check(path, HUB, now=now + timedelta(minutes=1), queue=lambda argv: calls.append(argv) or Result()), 2)
            self.assertEqual(json.loads(calls[0][-1])["event_id"], json.loads(calls[1][-1])["event_id"])
            self.assertEqual(len(calls), 2)
            monitor.atomic(path / "learnrise-monitor-receipt.json", {"as_of": (now + timedelta(minutes=2)).isoformat(), "mode": "shadow"})
            self.assertEqual(watchdog.check(path, HUB, now=now + timedelta(minutes=2), queue=failed), 0)
            self.assertFalse((path / "learnrise-watchdog-alert.json").exists())

    def test_watchdog_mode_matches_installed_activation_table(self):
        now = datetime(2026, 9, 28, tzinfo=UTC)
        cases = [
            ("shadow_before_cutover", "shadow", "shadow", True, None),
            ("apply_after_cutover", "apply", "apply", True, None),
            ("shadow_receipt_after_activation", "apply", "shadow", False, "monitor receipt mode mismatch"),
            ("apply_receipt_in_shadow", "shadow", "apply", False, "monitor receipt mode mismatch"),
            ("invalid_mode_marker", "invalid", "shadow", False, "monitor activation mode malformed"),
            ("missing_mode_marker", None, "shadow", False, "monitor activation mode missing"),
        ]
        for name, expected_mode, receipt_mode, healthy, reason in cases:
            with self.subTest(name=name), tempfile.TemporaryDirectory() as tmp:
                path = Path(tmp)
                if expected_mode is not None:
                    (path / "learnrise-monitor-mode").write_text(expected_mode + "\n")
                monitor.atomic(path / "learnrise-monitor-receipt.json",
                               {"as_of": now.isoformat(), "mode": receipt_mode})
                queued = []
                result = watchdog.check(path, HUB, now=now,
                                        queue=lambda argv: queued.append(json.loads(argv[-1])) or Result())
                if healthy:
                    self.assertEqual(result, 0)
                    self.assertEqual(queued, [])
                    self.assertFalse((path / "learnrise-watchdog-alert.json").exists())
                else:
                    self.assertEqual(result, 2)
                    self.assertEqual(len(queued), 1)
                    self.assertEqual(queued[0]["reason"], reason)
                    self.assertEqual(monitor.read_json(path / "learnrise-watchdog-alert.json")["reason"], reason)

    def test_watchdog_missing_corrupt_truncated_receipts_are_durable_outages(self):
        now = datetime(2026, 9, 28, tzinfo=UTC)
        cases = [
            ("missing", None, "monitor receipt absent"),
            ("truncated", '{"as_of":', "monitor receipt malformed"),
            ("invalid_json_shape", "[]", "monitor receipt malformed"),
            ("missing_timestamp", "{}", "monitor receipt malformed"),
            ("oversized", "x" * (monitor.MAX_BYTES + 1), "monitor receipt malformed"),
        ]
        for name, contents, expected_reason in cases:
            with self.subTest(name=name), tempfile.TemporaryDirectory() as tmp:
                path = Path(tmp)
                (path / "learnrise-monitor-mode").write_text("shadow\n")
                receipt = path / "learnrise-monitor-receipt.json"
                if contents is not None:
                    receipt.write_text(contents)
                queued = []
                notices = []
                def failed(argv):
                    queued.append(json.loads(argv[-1]))
                    return Result(1)
                self.assertEqual(watchdog.check(path, HUB, now=now, queue=failed,
                                                notify=lambda argv: notices.append(argv)), 2)
                alert = monitor.read_json(path / "learnrise-watchdog-alert.json")
                self.assertEqual(alert["reason"], expected_reason)
                self.assertFalse(alert["delivered"])
                self.assertEqual(len(queued), 1)
                self.assertEqual(len(notices), 1)
                self.assertEqual(watchdog.check(path, HUB, now=now + timedelta(minutes=1),
                                                queue=lambda argv: queued.append(json.loads(argv[-1])) or Result()), 2)
                self.assertEqual(queued[0]["event_id"], queued[1]["event_id"])
                monitor.atomic(receipt, {"as_of": (now + timedelta(minutes=2)).isoformat(), "mode": "shadow"})
                self.assertEqual(watchdog.check(path, HUB, now=now + timedelta(minutes=2),
                                                queue=failed), 0)
                self.assertFalse((path / "learnrise-watchdog-alert.json").exists())


if __name__ == "__main__":
    unittest.main()
