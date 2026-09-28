import importlib.util
import copy
from contextlib import redirect_stdout
import io
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
        report = {"actions": [outbox["actions"]["a" * 24]["action"]], "overdue": [],
                  "inventory": {"as_of": now.isoformat(), "tasks": []}}
        snapshot = monitor.semantic(observed, outbox, supervisor, report["inventory"], now)
        monitor.atomic(path / "learnrise-monitor-snapshot.json",
                       {"as_of": now.isoformat(), "repository": monitor.REPO, "entities": snapshot})
        monitor.atomic(path / "learnrise-monitor-receipt.json", {"as_of": now.isoformat(), "mode": "shadow"})
        self.cutover_fixture(path, now.isoformat(), snapshot)
        (path / "learnrise-monitor-mode").write_text("apply\n")
        return report

    def cutover_fixture(self, path, as_of, snapshot):
        baseline = monitor.baseline_current(snapshot, path)
        monitor.atomic(path / "learnrise-monitor-cutover-ready.json",
                       {"version": 1, "as_of": as_of, "hub": HUB,
                        "coverage": {"covered_heartbeat_ids": [HUB], "paused_heartbeat_ids": [HUB],
                                     "objectives": ["#1"], "complete": True, "paused_at": as_of},
                        "drain": {"confirmed_at": as_of, "method": "observed_empty", "no_prior_turns": True},
                        "shadow": {"receipt_as_of": as_of,
                                   "baseline_id": monitor.event_id("baseline", None, baseline, 1),
                                   "reviewed": True},
                        "canaries": {"queue": True, "watchdog": True, "preview": True}})

    def rearm_fixture(self, path):
        receipt = monitor.read_json(path / "learnrise-monitor-receipt.json")
        snapshot = monitor.read_json(path / "learnrise-monitor-snapshot.json")["entities"]
        self.cutover_fixture(path, receipt["as_of"], snapshot)
        ready_path = path / "learnrise-monitor-cutover-ready.json"
        ready = monitor.read_json(ready_path)
        cursor = monitor.read_json(path / "learnrise-monitor-cursor.json", optional=True) or {"pending": []}
        ready["rearm"] = {"pending_event_ids": [event["id"] for event in cursor["pending"]],
                          "reviewed": True}
        monitor.atomic(ready_path, ready)

    def refresh_shadow_fixture(self, path, now, report):
        snapshot = monitor.semantic(monitor.read_json(path / "boss-observed-ledger.json"),
                                    monitor.read_json(path / "boss-action-outbox.json"),
                                    monitor.read_json(path / "pr-supervisor-state.json"),
                                    report["inventory"], now)
        monitor.atomic(path / "learnrise-monitor-snapshot.json",
                       {"as_of": now.isoformat(), "repository": monitor.REPO, "entities": snapshot})
        monitor.atomic(path / "learnrise-monitor-receipt.json",
                       {"as_of": now.isoformat(), "mode": "shadow"})
        self.cutover_fixture(path, now.isoformat(), snapshot)

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
            ("objective:#2", None, {"state": "OPEN", "gate": True}, True),
            ("objective:#2", None, {"state": "OPEN", "gate": False}, False),
            ("objective:#2", None, {"state": "CLOSED", "gate": False}, False),
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

    def test_completed_status_preserves_open_human_gate_table(self):
        now = monitor.now_utc()
        cases = [("OPEN", "COMPLETED", True, "OPEN", True),
                 ("OPEN", "COMPLETED", False, "COMPLETED", False),
                 ("CLOSED", "COMPLETED", True, "COMPLETED", False),
                 ("OPEN", "IN_PROGRESS", True, "OPEN", True),
                 ("OPEN", "IN_PROGRESS", False, "OPEN", False),
                 ("CLOSED", "IN_PROGRESS", True, "CLOSED", False)]
        for github_state, status, human_gate, expected_state, expected_gate in cases:
            with self.subTest(github_state=github_state, status=status, human_gate=human_gate):
                ledger = {"dependency_gates": [], "open_pull_requests": [],
                          "objectives": [{"id": "#1", "github_state": github_state,
                                          "status": status, "human_gate": human_gate,
                                          "last_activity_utc": now.isoformat(),
                                          "pull_requests": [], "pull_request_states": {}}]}
                state = monitor.semantic(ledger, {"actions": {}}, {"actions": {}},
                                         {"tasks": []}, now)["objective:#1"]
                self.assertEqual(state["state"], expected_state)
                self.assertIs(state["gate"], expected_gate)
                self.assertEqual(state["activity"], now.isoformat() if expected_gate else None)
                baseline = monitor.baseline_current({"objective:#1": state}, Path("/tmp"))
                self.assertEqual("objective:#1" in baseline["ids"], expected_gate)

    def test_merged_pr_completion_keeps_open_issue_gate_activity_live(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp)
            now = monitor.now_utc()
            report = self.fixture(path, now)
            observed_path = path / "boss-observed-ledger.json"
            observed = monitor.read_json(observed_path)
            row = observed["objectives"][0]
            row.update(status="COMPLETED", pull_requests=[2], pull_request_states={"2": "MERGED"})
            observed["open_pull_requests"] = []
            monitor.atomic(observed_path, observed)
            self.refresh_shadow_fixture(path, now, report)
            args = ["--apply", "--state-dir", str(path), "--observed", str(observed_path),
                    "--outbox", str(path / "boss-action-outbox.json"),
                    "--supervisor", str(path / "pr-supervisor-state.json")]
            queued = []
            def runner(argv, timeout=120):
                if "queue" in argv:
                    queued.append(json.loads(argv[-1]))
                    return Result()
                return Result(3, json.dumps(report))
            with patch.object(monitor, "bounded_run", side_effect=runner):
                self.assertEqual(monitor.main(args), 0)
                self.assertIn("objective:#1", queued[0]["current"]["ids"])
                row["last_activity_utc"] = (now + timedelta(minutes=1)).isoformat()
                monitor.atomic(observed_path, observed)
                self.assertEqual(monitor.main(args), 0)
                self.assertEqual([event["entity"] for event in queued], ["baseline", "objective:#1"])
                self.assertEqual(queued[1]["current"]["state"], "OPEN")
                self.assertEqual(monitor.main(args), 0)
                self.assertEqual(len(queued), 2)

    def test_failed_check_identity_changes_wake_once_reorder_is_quiet(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp)
            now = monitor.now_utc()
            report = self.fixture(path, now)
            observed_path = path / "boss-observed-ledger.json"
            observed = monitor.read_json(observed_path)
            checks = observed["open_pull_requests"][0]["observation"]["checks"]
            checks[:] = [{"name": "check-A", "state": "FAILURE"},
                         {"name": "check-B", "state": "FAILURE"}]
            monitor.atomic(observed_path, observed)
            self.refresh_shadow_fixture(path, now, report)
            args = ["--apply", "--state-dir", str(path), "--observed", str(observed_path),
                    "--outbox", str(path / "boss-action-outbox.json"),
                    "--supervisor", str(path / "pr-supervisor-state.json")]
            queued = []
            def runner(argv, timeout=120):
                if "queue" in argv:
                    queued.append(json.loads(argv[-1]))
                    return Result()
                return Result(3, json.dumps(report))
            with patch.object(monitor, "bounded_run", side_effect=runner):
                self.assertEqual(monitor.main(args), 0)
                checks.reverse()
                monitor.atomic(observed_path, observed)
                self.assertEqual(monitor.main(args), 0)
                self.assertEqual(len(queued), 1)
                checks[0]["name"] = "check-C"
                monitor.atomic(observed_path, observed)
                self.assertEqual(monitor.main(args), 0)
                self.assertEqual([event["entity"] for event in queued], ["baseline", "pr:2"])
                self.assertNotEqual(queued[1]["previous"]["failed_checks_sha256"],
                                    queued[1]["current"]["failed_checks_sha256"])
                self.assertEqual(queued[1]["current"]["failed_check_count"], 2)
                self.assertEqual(monitor.main(args), 0)
                self.assertEqual(len(queued), 2)

    def test_failed_check_cursor_schema_table(self):
        base = {"head": "a" * 40, "objective": "#1", "merge": "CLEAN",
                "review": "APPROVED", "failed_check_count": 2,
                "failed_checks_sha256": "a" * 64}
        cases = [
            ("current", base, True),
            ("legacy_retry", {key: value for key, value in base.items() if key != "failed_checks_sha256"}, True),
            ("non_string", {**base, "failed_checks_sha256": 7}, False),
            ("wrong_length", {**base, "failed_checks_sha256": "a" * 63}, False),
            ("uppercase", {**base, "failed_checks_sha256": "A" * 64}, False),
            ("injection", {**base, "failed_checks_sha256": "a" * 63 + "\n"}, False),
            ("extra_field", {**base, "failed_checks": ["check-A", "check-B"]}, False),
        ]
        for name, state, valid in cases:
            with self.subTest(name=name):
                if valid:
                    monitor.validate_semantic_state("pr:2", state)
                else:
                    with self.assertRaises(monitor.MonitorError):
                        monitor.validate_semantic_state("pr:2", state)

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
                    self.refresh_shadow_fixture(path, now, report)
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

    def test_new_human_gate_wakes_after_baseline_but_benign_objective_does_not(self):
        cases = [("open_human_gate", "OPEN", True, True),
                 ("open_benign", "OPEN", False, False),
                 ("closed_old_gate", "CLOSED", True, False)]
        for name, state, human_gate, should_wake in cases:
            with self.subTest(name=name), tempfile.TemporaryDirectory() as tmp:
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
                        return Result()
                    return Result(3, json.dumps(report))
                with patch.object(monitor, "bounded_run", side_effect=runner):
                    self.assertEqual(monitor.main(args), 0)
                    self.assertEqual([item["entity"] for item in queued], ["baseline"])
                    self.assertEqual(queued[0]["current"]["counts"]["human_gates"], 1)
                    observed_path = path / "boss-observed-ledger.json"
                    observed = monitor.read_json(observed_path)
                    observed["objectives"].append({"id": "#2", "github_state": state,
                                                   "human_gate": human_gate,
                                                   "last_activity_utc": now.isoformat(),
                                                   "pull_requests": [], "pull_request_states": {}})
                    monitor.atomic(observed_path, observed)
                    if human_gate and state == "OPEN":
                        report["waiting"] = [{"objective": "#2", "reason": "human_gate"}]
                    self.assertEqual(monitor.main(args), 0)
                    self.assertEqual([item["entity"] for item in queued[1:]],
                                     ["objective:#2"] if should_wake else [])
                    self.assertEqual(monitor.main(args), 0)
                    self.assertEqual(len(queued), 2 if should_wake else 1)

    def test_malformed_cursor_fails_before_bridge_with_durable_fault(self):
        valid = {"version": 1, "initialized": True, "entities": {}, "pending": []}
        cases = [
            ("truncated_json", "{"),
            ("deep_json", '{"version":1,"initialized":true,"entities":' + "[" * 1200 + "0" + "]" * 1200 + ',"pending":[]}'),
            ("invalid_utf8", b"\xff"),
            ("top_level_array", "[]"),
            ("missing_top_level", "{}"),
            ("nested_generation_missing", json.dumps({**valid, "entities": {"objective:#1": {"state": {"state": "OPEN"}}}})),
            ("nested_generation_text", json.dumps({**valid, "entities": {"objective:#1": {"state": None, "generation": "1"}}})),
            ("nested_state_list", json.dumps({**valid, "entities": {"objective:#1": {"state": [], "generation": 1}}})),
            ("pending_event_id_missing", json.dumps({**valid, "pending": [{"entity": "objective:#1", "previous": None, "current": {}}]})),
            ("pending_event_id_malformed", json.dumps({**valid, "pending": [{"id": "$(touch /tmp/pwn)", "entity": "objective:#1", "previous": None, "current": {}}]})),
            ("pending_current_list", json.dumps({**valid, "pending": [{"id": "a" * 32, "entity": "objective:#1", "previous": None, "current": []}]})),
        ]
        for name, raw in cases:
            with self.subTest(name=name), tempfile.TemporaryDirectory() as tmp:
                path = Path(tmp)
                cursor_path = path / "learnrise-monitor-cursor.json"
                original = raw if isinstance(raw, bytes) else raw.encode()
                cursor_path.write_bytes(original)
                with patch.object(monitor, "bounded_run", side_effect=AssertionError("bridge or queue ran")):
                    self.assertEqual(monitor.main(["--apply", "--state-dir", str(path)]), 2)
                self.assertEqual(cursor_path.read_bytes(), original)
                self.assertFalse((path / "learnrise-monitor-snapshot.json").exists())
                self.assertFalse((path / "learnrise-monitor-receipt.json").exists())
                fault = monitor.read_json(path / "learnrise-monitor-fault.json")
                self.assertIsInstance(fault["error"], str)
                self.assertTrue(fault["error"])
                if name != "deep_json":
                    expected_error = {"truncated_json": "Expecting", "invalid_utf8": "codec"}.get(name, "cursor")
                    self.assertIn(expected_error, fault["error"])

    def test_shadow_preview_counts_additions_deletions_and_unchanged(self):
        task_id = "01a0d565-c171-7120-b828-b04db384021f"
        cases = [("unchanged", "none", 0), ("addition", "remove_pr", 1),
                 ("deletion", "add_task", 1), ("both", "both", 2)]
        for name, change, expected in cases:
            with self.subTest(name=name), tempfile.TemporaryDirectory() as tmp:
                path = Path(tmp)
                now = monitor.now_utc()
                report = self.fixture(path, now)
                ledger = monitor.read_json(path / "boss-observed-ledger.json")
                outbox = monitor.read_json(path / "boss-action-outbox.json")
                supervisor = monitor.read_json(path / "pr-supervisor-state.json")
                observed = monitor.semantic(ledger, outbox, supervisor, report["inventory"], now)
                entities = {entity: {"state": state, "generation": 0}
                            for entity, state in observed.items()}
                if change in {"remove_pr", "both"}:
                    del entities["pr:2"]
                if change in {"add_task", "both"}:
                    entities["task:" + task_id] = {"state": {"status": "running"}, "generation": 1}
                cursor_path = path / "learnrise-monitor-cursor.json"
                monitor.atomic(cursor_path, {"version": 1, "initialized": True,
                                             "entities": entities, "pending": []})
                original = cursor_path.read_bytes()
                def runner(argv, timeout=120):
                    self.assertNotIn("queue", argv)
                    return Result(3, json.dumps(report))
                output = io.StringIO()
                with patch.object(monitor, "bounded_run", side_effect=runner), redirect_stdout(output):
                    self.assertEqual(monitor.main(["--shadow", "--state-dir", str(path),
                                                   "--observed", str(path / "boss-observed-ledger.json"),
                                                   "--outbox", str(path / "boss-action-outbox.json"),
                                                   "--supervisor", str(path / "pr-supervisor-state.json")]), 0)
                printed = json.loads(output.getvalue())
                self.assertEqual(printed["preview_changes"], expected)
                self.assertEqual(printed["baseline"], monitor.baseline_current(observed, path))
                self.assertEqual(printed["baseline_event_id"],
                                 monitor.event_id("baseline", None, printed["baseline"], 1))
                self.assertEqual(printed["baseline"]["counts"],
                                 {"boss": 1, "supervisor": 1, "human_gates": 1, "dependency_gates": 0})
                self.assertEqual(printed["baseline"]["ids"],
                                 sorted(["boss:" + "a" * 24, "supervisor:" + "b" * 20, "objective:#1"]))
                self.assertEqual(cursor_path.read_bytes(), original)

    def test_shadow_uses_post_bridge_clock_for_fresh_sources(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp)
            finished = monitor.now_utc()
            started = finished - timedelta(minutes=3)
            report = self.fixture(path, finished)
            with patch.object(monitor, "now_utc", side_effect=[started, finished]), \
                 patch.object(monitor, "bounded_run", return_value=Result(3, json.dumps(report))), \
                 redirect_stdout(io.StringIO()):
                self.assertEqual(monitor.main(["--shadow", "--state-dir", str(path),
                                               "--observed", str(path / "boss-observed-ledger.json"),
                                               "--outbox", str(path / "boss-action-outbox.json"),
                                               "--supervisor", str(path / "pr-supervisor-state.json")]), 0)
            receipt = monitor.read_json(path / "learnrise-monitor-receipt.json")
            self.assertEqual(receipt["as_of"], finished.isoformat())

    def test_shadow_baseline_preview_is_bounded_before_receipt_write(self):
        cases = [("at_bound", 97, 0), ("over_bound", 98, 2)]
        for name, gate_count, expected in cases:
            with self.subTest(name=name), tempfile.TemporaryDirectory() as tmp:
                path = Path(tmp)
                now = monitor.now_utc()
                report = self.fixture(path, now)
                observed_path = path / "boss-observed-ledger.json"
                observed = monitor.read_json(observed_path)
                observed["dependency_gates"] = [{"id": f"gate:{number}", "satisfied": False}
                                                for number in range(gate_count)]
                monitor.atomic(observed_path, observed)
                receipt_path = path / "learnrise-monitor-receipt.json"
                snapshot_path = path / "learnrise-monitor-snapshot.json"
                originals = (receipt_path.read_bytes(), snapshot_path.read_bytes())
                def runner(argv, timeout=120):
                    self.assertNotIn("queue", argv)
                    return Result(3, json.dumps(report))
                output = io.StringIO()
                with patch.object(monitor, "bounded_run", side_effect=runner), redirect_stdout(output):
                    self.assertEqual(monitor.main(["--shadow", "--state-dir", str(path),
                                                   "--observed", str(observed_path),
                                                   "--outbox", str(path / "boss-action-outbox.json"),
                                                   "--supervisor", str(path / "pr-supervisor-state.json")]), expected)
                if expected == 0:
                    self.assertEqual(len(json.loads(output.getvalue())["baseline"]["ids"]), 100)
                else:
                    self.assertEqual((receipt_path.read_bytes(), snapshot_path.read_bytes()), originals)
                    self.assertTrue((path / "learnrise-monitor-fault.json").exists())

    def test_status_fails_closed_on_malformed_cursor_without_writes(self):
        cases = [("truncated", "{"),
                 ("nested", json.dumps({"version": 1, "initialized": True,
                                         "entities": {"objective:#1": {"state": {"state": "OPEN"},
                                                                       "generation": 1}}, "pending": []}))]
        for name, raw in cases:
            with self.subTest(name=name), tempfile.TemporaryDirectory() as tmp:
                path = Path(tmp)
                cursor_path = path / "learnrise-monitor-cursor.json"
                cursor_path.write_text(raw)
                output = io.StringIO()
                with patch.object(monitor, "atomic", side_effect=AssertionError("write")), \
                     patch.object(monitor, "bounded_run", side_effect=AssertionError("process")), \
                     redirect_stdout(output):
                    self.assertEqual(monitor.main(["--status", "--state-dir", str(path)]), 2)
                reported = json.loads(output.getvalue())
                self.assertIn("invalid cursor", reported["fault"]["error"])
                self.assertEqual(cursor_path.read_text(), raw)
                self.assertFalse((path / "learnrise-monitor-fault.json").exists())

    def test_cutover_check_is_read_only_and_rejects_stale_or_incomplete_evidence(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp)
            now = monitor.now_utc().isoformat()
            baseline = monitor.baseline_current({}, path)
            receipt = {"as_of": now, "mode": "shadow"}
            snapshot = {"as_of": now, "repository": monitor.REPO, "entities": {}}
            ready = {"version": 1, "as_of": now, "hub": HUB,
                     "coverage": {"covered_heartbeat_ids": ["learnrise-519-monitor"],
                                  "paused_heartbeat_ids": ["learnrise-519-monitor"],
                                  "objectives": ["#1"], "complete": True, "paused_at": now},
                     "drain": {"confirmed_at": now, "method": "observed_empty", "no_prior_turns": True},
                     "shadow": {"receipt_as_of": now,
                                "baseline_id": monitor.event_id("baseline", None, baseline, 1),
                                "reviewed": True},
                     "canaries": {"queue": True, "watchdog": True, "preview": True}}
            files = {"learnrise-monitor-receipt.json": receipt,
                     "learnrise-monitor-snapshot.json": snapshot,
                     "learnrise-monitor-cutover-ready.json": ready}
            for name, contents in files.items():
                monitor.atomic(path / name, contents)
            cases = [("ready", None, 0),
                     ("wrong_hub", lambda doc: doc.update(hub="01a0d565-c171-7120-b828-b04db384021e"), 2),
                     ("unpaused", lambda doc: doc["coverage"].update(paused_heartbeat_ids=[]), 2),
                     ("injected_automation_id", lambda doc: doc["coverage"].update(
                         covered_heartbeat_ids=["learnrise-519-monitor;touch /tmp/pwn"],
                         paused_heartbeat_ids=["learnrise-519-monitor;touch /tmp/pwn"]), 2),
                     ("empty_automation_id", lambda doc: doc["coverage"].update(
                         covered_heartbeat_ids=[""], paused_heartbeat_ids=[""]), 2),
                     ("unreviewed", lambda doc: doc["shadow"].update(reviewed=False), 2),
                     ("wrong_baseline", lambda doc: doc["shadow"].update(baseline_id="a" * 32), 2)]
            for name, mutate, expected in cases:
                with self.subTest(name=name):
                    current = copy.deepcopy(ready)
                    if mutate:
                        mutate(current)
                    monitor.atomic(path / "learnrise-monitor-cutover-ready.json", current)
                    originals = {name: (path / name).read_bytes() for name in files}
                    with patch.object(monitor, "atomic", side_effect=AssertionError("write")), \
                         patch.object(monitor, "bounded_run", side_effect=AssertionError("process")), \
                         patch.object(Path, "mkdir", side_effect=AssertionError("mkdir")), \
                         redirect_stdout(io.StringIO()):
                        self.assertEqual(monitor.main(["--cutover-check", "--state-dir", str(path),
                                                       "--hub", HUB]), expected)
                    self.assertEqual({name: (path / name).read_bytes() for name in files}, originals)

    def test_installed_first_apply_requires_gate_and_reviewed_baseline(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp)
            now = monitor.now_utc()
            report = self.fixture(path, now)
            args = ["--apply", "--state-dir", str(path),
                    "--observed", str(path / "boss-observed-ledger.json"),
                    "--outbox", str(path / "boss-action-outbox.json"),
                    "--supervisor", str(path / "pr-supervisor-state.json")]
            mode = path / "learnrise-monitor-mode"
            mode.unlink()
            with patch.object(monitor, "bounded_run", side_effect=AssertionError("bridge or queue ran")):
                self.assertEqual(monitor.main(args), 2)
            mode.write_text("shadow\n")
            with patch.object(monitor, "bounded_run", side_effect=AssertionError("bridge or queue ran")):
                self.assertEqual(monitor.main(args), 2)
            self.assertFalse((path / "learnrise-monitor-cursor.json").exists())
            mode.write_text("apply\n")
            ledger = monitor.read_json(path / "boss-observed-ledger.json")
            outbox = monitor.read_json(path / "boss-action-outbox.json")
            supervisor = monitor.read_json(path / "pr-supervisor-state.json")
            snapshot = monitor.semantic(ledger, outbox, supervisor, report["inventory"], now)
            as_of = now.isoformat()
            monitor.atomic(path / "learnrise-monitor-snapshot.json",
                           {"as_of": as_of, "repository": monitor.REPO, "entities": snapshot})
            monitor.atomic(path / "learnrise-monitor-receipt.json", {"as_of": as_of, "mode": "shadow"})
            baseline = monitor.baseline_current(snapshot, path)
            monitor.atomic(path / "learnrise-monitor-cutover-ready.json",
                           {"version": 1, "as_of": as_of, "hub": HUB,
                            "coverage": {"covered_heartbeat_ids": [HUB], "paused_heartbeat_ids": [HUB],
                                         "objectives": ["#1"], "complete": True, "paused_at": as_of},
                            "drain": {"confirmed_at": as_of, "method": "observed_empty", "no_prior_turns": True},
                            "shadow": {"receipt_as_of": as_of,
                                       "baseline_id": monitor.event_id("baseline", None, baseline, 1),
                                       "reviewed": True},
                            "canaries": {"queue": True, "watchdog": True, "preview": True}})
            observed_path = path / "boss-observed-ledger.json"
            changed = copy.deepcopy(ledger)
            changed["objectives"][0]["human_gate"] = False
            monitor.atomic(observed_path, changed)
            queued = []
            def runner(argv, timeout=120):
                if "queue" in argv:
                    queued.append(json.loads(argv[-1]))
                    return Result()
                return Result(3, json.dumps(report))
            with patch.object(monitor, "bounded_run", side_effect=runner):
                self.assertEqual(monitor.main(args), 2)
                self.assertEqual(queued, [])
                self.assertFalse((path / "learnrise-monitor-cursor.json").exists())
                monitor.atomic(observed_path, ledger)
                self.assertEqual(monitor.main(args), 2)
                self.assertEqual(queued, [])
                self.assertEqual(monitor.main(["--shadow", *args[1:]]), 0)
                self.rearm_fixture(path)
                self.assertEqual(monitor.main(["--rearm", *args]), 0)
            self.assertEqual([event["entity"] for event in queued], ["baseline"])
            self.assertEqual(queued[0]["event_id"],
                             monitor.event_id("baseline", None, baseline, 1))

    def test_first_apply_rejects_semantic_drift_with_same_baseline_id(self):
        task_id = "01a0d565-c171-7120-b828-b04db384021f"
        cases = [("unchanged", None, 0), ("pr_head", "pr_head", 2),
                 ("action_status", "action_status", 2), ("task_state", "task_state", 2)]
        for name, change, expected in cases:
            with self.subTest(name=name), tempfile.TemporaryDirectory() as tmp:
                path = Path(tmp)
                now = monitor.now_utc()
                report = self.fixture(path, now)
                report["inventory"]["tasks"] = [{"id": task_id, "status": "running"}]
                args = ["--state-dir", str(path),
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
                    self.assertEqual(monitor.main(["--shadow", *args]), 0)
                    receipt = monitor.read_json(path / "learnrise-monitor-receipt.json")
                    shadow = monitor.read_json(path / "learnrise-monitor-snapshot.json")
                    self.cutover_fixture(path, receipt["as_of"], shadow["entities"])
                    original_snapshot = (path / "learnrise-monitor-snapshot.json").read_bytes()
                    original_baseline = monitor.event_id(
                        "baseline", None, monitor.baseline_current(shadow["entities"], path), 1)
                    ledger = monitor.read_json(path / "boss-observed-ledger.json")
                    outbox = monitor.read_json(path / "boss-action-outbox.json")
                    if change == "pr_head":
                        ledger["open_pull_requests"][0]["head"] = "b" * 40
                        monitor.atomic(path / "boss-observed-ledger.json", ledger)
                    elif change == "action_status":
                        outbox["actions"]["a" * 24]["state"] = "acknowledged"
                        outbox["actions"]["a" * 24]["acknowledged_at"] = now.isoformat()
                        monitor.atomic(path / "boss-action-outbox.json", outbox)
                    elif change == "task_state":
                        report["inventory"]["tasks"][0]["status"] = "completed"
                    changed_snapshot = monitor.semantic(ledger, outbox,
                                                        monitor.read_json(path / "pr-supervisor-state.json"),
                                                        report["inventory"], now)
                    self.assertEqual(monitor.event_id("baseline", None,
                                     monitor.baseline_current(changed_snapshot, path), 1), original_baseline)
                    self.assertEqual(monitor.main(["--apply", *args]), expected)
                    if expected:
                        if change == "pr_head":
                            ledger["open_pull_requests"][0]["head"] = "a" * 40
                            monitor.atomic(path / "boss-observed-ledger.json", ledger)
                        elif change == "action_status":
                            outbox["actions"]["a" * 24]["state"] = "pending"
                            outbox["actions"]["a" * 24].pop("acknowledged_at")
                            monitor.atomic(path / "boss-action-outbox.json", outbox)
                        else:
                            report["inventory"]["tasks"][0]["status"] = "running"
                        self.assertEqual(monitor.main(["--apply", *args]), 2)
                        self.assertEqual(len(bridge_calls), 2)
                if expected:
                    self.assertEqual(queued, [])
                    self.assertFalse((path / "learnrise-monitor-cursor.json").exists())
                    self.assertEqual((path / "learnrise-monitor-snapshot.json").read_bytes(), original_snapshot)
                    self.assertTrue((path / "learnrise-monitor-fault.json").exists())
                else:
                    self.assertEqual([event["entity"] for event in queued], ["baseline"])
                    self.assertTrue(monitor.read_json(path / "learnrise-monitor-cursor.json")["initialized"])

    def test_pending_cursor_schema_and_identity_fail_closed(self):
        activity = "2026-09-28T00:00:00+00:00"
        before = {"state": "OPEN", "prs": [], "gate": True, "activity": activity}
        after = {**before, "activity": "2026-09-28T00:01:00+00:00"}
        mutations = [
            ("numeric_id", "transition", lambda c: c["pending"][0].update(id=int("1" * 32))),
            ("wrong_id", "transition", lambda c: c["pending"][0].update(id="a" * 32)),
            ("generation_bool", "transition", lambda c: c["pending"][0].update(generation=True)),
            ("generation_out_of_range", "transition", lambda c: c["pending"][0].update(generation=3)),
            ("forged_entity", "transition", lambda c: c["pending"][0].update(entity="objective:#2")),
            ("pending_previous_extra", "transition", lambda c: c["pending"][0]["previous"].update(command="$(touch /tmp/pwn)")),
            ("pending_current_missing", "transition", lambda c: c["pending"][0]["current"].pop("prs")),
            ("pending_current_bad_gate", "transition", lambda c: c["pending"][0]["current"].update(gate="true")),
            ("entity_state_extra", "transition", lambda c: c["entities"]["objective:#1"]["state"].update(command="$(touch /tmp/pwn)")),
            ("entity_state_bad_enum", "transition", lambda c: c["entities"]["objective:#1"]["state"].update(state=["OPEN"])),
            ("baseline_wrong_id", "baseline", lambda c: c["pending"][0].update(id="a" * 32)),
            ("baseline_bad_counts", "baseline", lambda c: c["pending"][0]["current"]["counts"].update(human_gates=2)),
            ("baseline_bad_entity", "baseline", lambda c: c["pending"][0]["current"]["ids"].append("task:$(touch /tmp/pwn)")),
            ("baseline_bad_snapshot", "baseline", lambda c: c["pending"][0]["current"].update(snapshot="/tmp/other")),
            ("baseline_silent_forgery", "baseline", lambda c: c["pending"][0].update(silent=True)),
        ]
        for name, kind, mutate in mutations:
            with self.subTest(name=name), tempfile.TemporaryDirectory() as tmp:
                path = Path(tmp)
                transition = {"id": monitor.event_id("objective:#1", before, after, 2),
                              "entity": "objective:#1", "previous": before, "current": after,
                              "generation": 2}
                baseline_current = monitor.baseline_current({"objective:#1": before}, path)
                baseline = {"id": monitor.event_id("baseline", None, baseline_current, 1),
                            "entity": "baseline", "previous": None,
                            "current": baseline_current, "generation": 1}
                valid = ({"version": 1, "initialized": True,
                          "entities": {"objective:#1": {"state": after, "generation": 2}},
                          "pending": [transition]} if kind == "transition" else
                         {"version": 1, "initialized": False,
                          "entities": {"objective:#1": {"state": before, "generation": 0}},
                          "pending": [baseline]})
                monitor.validate_cursor(valid, path)
                malformed = copy.deepcopy(valid)
                mutate(malformed)
                original = json.dumps(malformed).encode()
                cursor_path = path / "learnrise-monitor-cursor.json"
                cursor_path.write_bytes(original)
                with patch.object(monitor, "bounded_run", side_effect=AssertionError("bridge or queue ran")):
                    self.assertEqual(monitor.main(["--apply", "--state-dir", str(path)]), 2)
                self.assertEqual(cursor_path.read_bytes(), original)
                self.assertFalse((path / "learnrise-monitor-snapshot.json").exists())
                self.assertFalse((path / "learnrise-monitor-receipt.json").exists())
                self.assertTrue((path / "learnrise-monitor-fault.json").exists())

    def test_pending_transition_chains_are_contiguous_and_end_at_cursor(self):
        states = [{"state": "OPEN", "prs": [], "gate": True,
                   "activity": f"2026-09-28T00:0{minute}:00+00:00"} for minute in range(3)]
        def make_event(before, after, generation):
            return {"id": monitor.event_id("objective:#1", before, after, generation),
                    "entity": "objective:#1", "previous": before, "current": after,
                    "generation": generation}
        cases = [
            ("duplicate_id", lambda c: c["pending"].append(copy.deepcopy(c["pending"][0]))),
            ("generation_gap", lambda c: c["pending"][1].update(generation=4)),
            ("broken_previous", lambda c: c["pending"][1].update(previous=states[0])),
            ("stale_endpoint", lambda c: c["entities"]["objective:#1"].update(state=states[1])),
            ("stale_generation", lambda c: c["entities"]["objective:#1"].update(generation=4)),
            ("reversed_order", lambda c: c["pending"].reverse()),
            ("unrelated_current", lambda c: c["pending"][1].update(current=states[0])),
            ("forged_silent_meaningful", lambda c: c["pending"][1].update(silent=True)),
            ("invalid_silent_flag", lambda c: c["pending"][1].update(silent=False)),
        ]
        for name, mutate in cases:
            with self.subTest(name=name), tempfile.TemporaryDirectory() as tmp:
                path = Path(tmp)
                valid = {"version": 1, "initialized": True,
                         "entities": {"objective:#1": {"state": states[2], "generation": 3}},
                         "pending": [make_event(states[0], states[1], 2),
                                     make_event(states[1], states[2], 3)]}
                monitor.validate_cursor(valid, path)
                malformed = copy.deepcopy(valid)
                mutate(malformed)
                # Keep mutated transition IDs internally consistent to isolate
                # chain validation from identity validation.
                for event in malformed["pending"]:
                    event["id"] = monitor.event_id(event["entity"], event["previous"],
                                                   event["current"], event["generation"])
                original = json.dumps(malformed).encode()
                cursor_path = path / "learnrise-monitor-cursor.json"
                cursor_path.write_bytes(original)
                with patch.object(monitor, "bounded_run", side_effect=AssertionError("bridge or queue ran")):
                    self.assertEqual(monitor.main(["--apply", "--state-dir", str(path)]), 2)
                self.assertEqual(cursor_path.read_bytes(), original)
                self.assertFalse((path / "learnrise-monitor-snapshot.json").exists())
                self.assertTrue((path / "learnrise-monitor-fault.json").exists())

    def test_suppressed_transitions_survive_queue_failure_restart(self):
        task_id = "01a0d565-c171-7120-b828-b04db384021f"
        cases = [("task_reappears", "task:" + task_id),
                 ("objective_benign_readd", "objective:#1")]
        for name, entity in cases:
            with self.subTest(name=name), tempfile.TemporaryDirectory() as tmp:
                path = Path(tmp)
                now = monitor.now_utc()
                report = self.fixture(path, now)
                if name == "task_reappears":
                    report["inventory"]["tasks"] = [{"id": task_id, "status": "running"}]
                    self.refresh_shadow_fixture(path, now, report)
                args = ["--apply", "--state-dir", str(path),
                        "--observed", str(path / "boss-observed-ledger.json"),
                        "--outbox", str(path / "boss-action-outbox.json"),
                        "--supervisor", str(path / "pr-supervisor-state.json")]
                queued = []
                def runner(argv, timeout=120):
                    if "queue" in argv:
                        queued.append(json.loads(argv[-1]))
                        return Result(1 if len(queued) in {2, 3, 4} else 0)
                    return Result(3, json.dumps(report))
                with patch.object(monitor, "bounded_run", side_effect=runner):
                    self.assertEqual(monitor.main(args), 0)
                    if name == "task_reappears":
                        report["inventory"]["tasks"][0]["status"] = "completed"
                    else:
                        observed_path = path / "boss-observed-ledger.json"
                        observed = monitor.read_json(observed_path)
                        observed["objectives"][0]["last_activity_utc"] = (now + timedelta(minutes=1)).isoformat()
                        monitor.atomic(observed_path, observed)
                    self.assertEqual(monitor.main(args), 2)
                    if name == "task_reappears":
                        report["inventory"]["tasks"] = []
                    else:
                        observed = monitor.read_json(observed_path)
                        observed["objectives"] = []
                        monitor.atomic(observed_path, observed)
                    self.assertEqual(monitor.main(args), 2)
                    if name == "task_reappears":
                        report["inventory"]["tasks"] = [{"id": task_id, "status": "running"}]
                    else:
                        observed = monitor.read_json(observed_path)
                        observed["objectives"] = [{"id": "#1", "github_state": "OPEN",
                                                   "human_gate": False, "pull_requests": [],
                                                   "pull_request_states": {}}]
                        monitor.atomic(observed_path, observed)
                    # This scan appends a suppressed transition while the prior
                    # queued event is still pending. A restarted scan must accept it.
                    self.assertEqual(monitor.main(args), 2)
                    cursor_path = path / "learnrise-monitor-cursor.json"
                    cursor = monitor.read_json(cursor_path)
                    self.assertEqual([event.get("silent", False) for event in cursor["pending"]],
                                     [False, False, True])
                    monitor.validate_cursor(cursor, path)
                    self.assertEqual(monitor.main(args), 0)
                    self.assertEqual([event["entity"] for event in queued],
                                     ["baseline", entity, entity, entity, entity, entity])
                    self.assertEqual(monitor.read_json(cursor_path)["pending"], [])

    def test_multiple_generations_retry_in_order_after_queue_failures(self):
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
                    return Result(1 if len(queued) in {2, 3} else 0)
                return Result(3, json.dumps(report))
            with patch.object(monitor, "bounded_run", side_effect=runner):
                self.assertEqual(monitor.main(args), 0)
                observed_path = path / "boss-observed-ledger.json"
                for minutes in (1, 2):
                    observed = monitor.read_json(observed_path)
                    observed["objectives"][0]["last_activity_utc"] = (now + timedelta(minutes=minutes)).isoformat()
                    monitor.atomic(observed_path, observed)
                    self.assertEqual(monitor.main(args), 2)
                cursor_path = path / "learnrise-monitor-cursor.json"
                cursor = monitor.read_json(cursor_path)
                self.assertEqual([event["generation"] for event in cursor["pending"]], [1, 2])
                monitor.validate_cursor(cursor, path)
                self.assertEqual(monitor.main(args), 0)
                self.assertEqual([event["entity"] for event in queued],
                                 ["baseline", "objective:#1", "objective:#1",
                                  "objective:#1", "objective:#1"])
                self.assertEqual(queued[1]["event_id"], queued[2]["event_id"])
                self.assertEqual(queued[2]["event_id"], queued[3]["event_id"])
                self.assertNotEqual(queued[3]["event_id"], queued[4]["event_id"])
                self.assertEqual(monitor.read_json(cursor_path)["pending"], [])

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
                self.assertTrue((path / "learnrise-monitor-cutover-attempt.json").exists())
                nested_results.append(monitor.main(args))
                fault_calls = []
                with redirect_stdout(io.StringIO()):
                    self.assertEqual(watchdog.check(path, HUB, now=monitor.now_utc(),
                                                    queue=lambda command: fault_calls.append(command) or Result()), 0)
                self.assertEqual(fault_calls, [])
                return Result(3, json.dumps(report))
            with patch.object(monitor, "bounded_run", side_effect=runner):
                self.assertEqual(monitor.main(args), 0)
            self.assertEqual(nested_results, [2])
            self.assertEqual(len(bridge_calls), 1)
            self.assertEqual([event["entity"] for event in queued], ["baseline"])
            self.assertTrue(monitor.read_json(path / "learnrise-monitor-cursor.json")["initialized"])
            self.assertFalse((path / "learnrise-monitor-cutover-attempt.json").exists())

    def test_interrupted_first_apply_disarms_scheduled_retry_table(self):
        for interruption in (KeyboardInterrupt, SystemExit):
            with self.subTest(interruption=interruption.__name__), tempfile.TemporaryDirectory() as tmp:
                path = Path(tmp)
                report = self.fixture(path, monitor.now_utc() - timedelta(minutes=2))
                args = ["--apply", "--state-dir", str(path),
                        "--observed", str(path / "boss-observed-ledger.json"),
                        "--outbox", str(path / "boss-action-outbox.json"),
                        "--supervisor", str(path / "pr-supervisor-state.json")]
                with patch.object(monitor, "bounded_run", side_effect=interruption):
                    with self.assertRaises(interruption):
                        monitor.main(args)
                attempt_path = path / "learnrise-monitor-cutover-attempt.json"
                attempt = monitor.read_json(attempt_path)
                self.assertEqual(attempt["baseline_id"], monitor.read_json(path / "learnrise-monitor-cutover-ready.json")["shadow"]["baseline_id"])
                self.assertFalse((path / "learnrise-monitor-cursor.json").exists())
                outage = []
                with redirect_stdout(io.StringIO()):
                    self.assertEqual(watchdog.check(path, HUB, now=monitor.now_utc(),
                                                    queue=lambda argv: outage.append(json.loads(argv[-1])) or Result()), 2)
                self.assertEqual(outage[0]["reason"], "monitor cutover attempt abandoned")
                calls = []
                with patch.object(monitor, "bounded_run", side_effect=lambda *a, **k: calls.append(a) or Result()):
                    self.assertEqual(monitor.main(args), 2)
                self.assertEqual(calls, [])
                self.assertTrue((path / "learnrise-monitor-rearm-required.json").exists())
                self.refresh_shadow_fixture(path, monitor.now_utc() + timedelta(seconds=1), report)
                self.rearm_fixture(path)
                queued = []
                def runner(argv, timeout=120):
                    if "queue" in argv:
                        queued.append(json.loads(argv[-1]))
                        return Result()
                    return Result(3, json.dumps(report))
                with patch.object(monitor, "bounded_run", side_effect=runner):
                    self.assertEqual(monitor.main(["--rearm", *args]), 0)
                self.assertEqual([event["event_id"] for event in queued], [attempt["baseline_id"]])
                self.assertFalse(attempt_path.exists())

    def test_shadow_baseline_encoded_message_boundary_table(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp)
            now = monitor.now_utc()
            report = self.fixture(path, now)
            ledger_path = path / "boss-observed-ledger.json"
            ledger = monitor.read_json(ledger_path)
            base = monitor.read_json(path / "learnrise-monitor-snapshot.json")["entities"]
            def gates(count):
                return [{"id": f"gate-{index:03d}-" + "x" * 91, "satisfied": False}
                        for index in range(count)]
            def snapshot(count):
                return {**base, **{f"dependency:{gate['id']}": {"satisfied": False}
                                 for gate in gates(count)}}
            first_over = next(count for count in range(1, 101)
                              if self.baseline_too_large(snapshot(count), path))
            self.assertGreater(first_over, 1)
            args = ["--shadow", "--state-dir", str(path), "--observed", str(ledger_path),
                    "--outbox", str(path / "boss-action-outbox.json"),
                    "--supervisor", str(path / "pr-supervisor-state.json")]
            for count, expected in ((first_over - 1, 0), (first_over, 2)):
                with self.subTest(count=count, expected=expected):
                    ledger["dependency_gates"] = gates(count)
                    monitor.atomic(ledger_path, ledger)
                    before_receipt = (path / "learnrise-monitor-receipt.json").read_bytes()
                    before_snapshot = (path / "learnrise-monitor-snapshot.json").read_bytes()
                    calls = []
                    def runner(argv, timeout=120):
                        calls.append(argv)
                        return Result(3, json.dumps(report))
                    with patch.object(monitor, "bounded_run", side_effect=runner):
                        self.assertEqual(monitor.main(args), expected)
                    self.assertEqual(len(calls), 1)
                    self.assertFalse((path / "learnrise-monitor-cursor.json").exists())
                    if expected == 2:
                        self.assertEqual((path / "learnrise-monitor-receipt.json").read_bytes(), before_receipt)
                        self.assertEqual((path / "learnrise-monitor-snapshot.json").read_bytes(), before_snapshot)
                    else:
                        current = monitor.baseline_current(snapshot(count), path)
                        encoded = monitor.message({"id": monitor.event_id("baseline", None, current, 1),
                                                   "entity": "baseline", "previous": None, "current": current})
                        self.assertLessEqual(len(encoded), 6000)

    def baseline_too_large(self, snapshot, path):
        try:
            monitor.baseline_current(snapshot, path)
            return False
        except monitor.MonitorError as exc:
            self.assertIn("message exceeds bound", str(exc))
            return True

    def test_baseline_cursor_commit_recovers_interrupted_marker_cleanup_table(self):
        for leftover_rearm in (False, True):
            with self.subTest(leftover_rearm=leftover_rearm), tempfile.TemporaryDirectory() as tmp:
                path = Path(tmp)
                now = monitor.now_utc()
                report = self.fixture(path, now)
                args = ["--apply", "--state-dir", str(path),
                        "--observed", str(path / "boss-observed-ledger.json"),
                        "--outbox", str(path / "boss-action-outbox.json"),
                        "--supervisor", str(path / "pr-supervisor-state.json")]
                attempt_path = path / "learnrise-monitor-cutover-attempt.json"
                original_unlink = Path.unlink
                def interrupted_unlink(target, *pos, **kw):
                    if target == attempt_path:
                        raise SystemExit("interrupted after cursor commit")
                    return original_unlink(target, *pos, **kw)
                queued = []
                def runner(argv, timeout=120):
                    if "queue" in argv:
                        queued.append(json.loads(argv[-1]))
                        return Result()
                    return Result(3, json.dumps(report))
                with patch.object(monitor, "bounded_run", side_effect=runner), patch.object(Path, "unlink", interrupted_unlink):
                    with self.assertRaises(SystemExit):
                        monitor.main(args)
                cursor = monitor.read_json(path / "learnrise-monitor-cursor.json")
                self.assertTrue(cursor["initialized"])
                self.assertEqual(cursor["pending"], [])
                self.assertTrue(attempt_path.exists())
                self.assertEqual([event["entity"] for event in queued], ["baseline"])
                if leftover_rearm:
                    monitor.atomic(path / "learnrise-monitor-rearm-required.json",
                                   {"version": 1, "failed_at": now.isoformat(),
                                    "baseline_id": queued[0]["event_id"]})
                outbox_path = path / "boss-action-outbox.json"
                outbox = monitor.read_json(outbox_path)
                new_id = "c" * 24
                new_action = {"id": new_id, "repo": monitor.REPO, "verb": "RECOVER_OWNER",
                              "objective": "#1", "pr": 2, "head": "a" * 40}
                outbox["actions"][new_id] = {"state": "pending", "first_seen": now.isoformat(),
                                               "action": new_action}
                monitor.atomic(outbox_path, outbox)
                report["actions"].append(new_action)
                with patch.object(monitor, "bounded_run", side_effect=runner):
                    self.assertEqual(monitor.main(args), 0)
                self.assertEqual([event["entity"] for event in queued], ["baseline", "boss:" + new_id])
                self.assertFalse(attempt_path.exists())
                self.assertFalse((path / "learnrise-monitor-rearm-required.json").exists())

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
                self.cutover_fixture(path,
                                     monitor.read_json(path / "learnrise-monitor-receipt.json")["as_of"],
                                     monitor.read_json(path / "learnrise-monitor-snapshot.json")["entities"])
                self.assertEqual(monitor.main(["--apply", *args]), 2)
                cursor = monitor.read_json(path / "learnrise-monitor-cursor.json")
                self.assertFalse(cursor["initialized"])
                self.assertEqual(len(cursor["pending"]), 1)
                baseline = queues[0]
                self.assertEqual(baseline["current"]["counts"], {"boss": 1, "supervisor": 1, "human_gates": 1, "dependency_gates": 0})
                self.assertTrue(baseline["current"]["reconcile_only"])
                self.assertEqual(monitor.main(["--apply", *args]), 2)
                self.assertEqual(len(queues), 1)
                self.assertEqual(monitor.main(["--shadow", *args]), 2)
                self.rearm_fixture(path)
                self.assertEqual(monitor.main(["--apply", "--rearm", *args]), 0)
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
                immutable_path = Path(queued[0]["current"]["snapshot"])
                immutable_before = immutable_path.read_bytes()
                outbox_path = path / "boss-action-outbox.json"
                outbox = monitor.read_json(outbox_path)
                new_id = "c" * 24
                new_action = {"id": new_id, "repo": monitor.REPO, "verb": "RECOVER_OWNER",
                              "objective": "#1", "pr": 2, "head": "a" * 40}
                outbox["actions"][new_id] = {"state": "pending", "first_seen": now.isoformat(),
                                              "action": new_action}
                monitor.atomic(outbox_path, outbox)
                report["actions"].append(new_action)
                self.assertEqual(monitor.main(args), 2)
                self.assertEqual(len(queued), 1)
                self.assertEqual(monitor.main(["--shadow", *args[1:]]), 2)
                self.assertEqual(immutable_path.read_bytes(), immutable_before)
                self.assertNotIn("boss:" + new_id, monitor.read_json(immutable_path)["entities"])
                self.assertIn("boss:" + new_id,
                              monitor.read_json(path / "learnrise-monitor-snapshot.json")["entities"])
                self.rearm_fixture(path)
                self.assertEqual(monitor.main(["--rearm", *args]), 0)
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

    def test_rearm_keeps_original_baseline_snapshot_when_shadow_state_changes(self):
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
                original_id = queued[0]["event_id"]
                original_path = Path(queued[0]["current"]["snapshot"])
                original_bytes = original_path.read_bytes()
                outbox_path = path / "boss-action-outbox.json"
                outbox = monitor.read_json(outbox_path)
                outbox["actions"]["a" * 24]["state"] = "acknowledged"
                outbox["actions"]["a" * 24]["acknowledged_at"] = now.isoformat()
                monitor.atomic(outbox_path, outbox)
                self.assertEqual(monitor.main(args), 2)
                self.assertEqual(len(queued), 1)
                preview_output = io.StringIO()
                with redirect_stdout(preview_output):
                    self.assertEqual(monitor.main(["--shadow", *args[1:]]), 2)
                preview = json.loads(preview_output.getvalue())
                self.assertEqual(preview["baseline_event_id"], original_id)
                self.assertNotEqual(preview["baseline"]["snapshot"], str(original_path))
                self.assertEqual(original_path.read_bytes(), original_bytes)
                self.assertEqual(monitor.read_json(original_path)["entities"]["boss:" + "a" * 24]["status"], "pending")
                self.assertEqual(monitor.read_json(path / "learnrise-monitor-snapshot.json")["entities"]["boss:" + "a" * 24]["status"], "acknowledged")
                self.rearm_fixture(path)
                self.assertEqual(monitor.main(["--rearm", *args]), 0)
                self.assertEqual(queued[1]["event_id"], original_id)
                self.assertEqual(queued[1]["current"]["snapshot"], str(original_path))
                self.assertEqual([event["entity"] for event in queued],
                                 ["baseline", "baseline", "boss:" + "a" * 24])
                self.assertEqual(original_path.read_bytes(), original_bytes)

    def test_rearm_evidence_table_blocks_scheduled_retry_and_preserves_event_id(self):
        cases = [("missing_rearm_review", lambda ready: ready.pop("rearm")),
                 ("wrong_pending_id", lambda ready: ready["rearm"].update(pending_event_ids=["a" * 32])),
                 ("unpaused_coverage", lambda ready: ready["coverage"].update(paused_heartbeat_ids=[])),
                 ("valid", None)]
        for name, mutate in cases:
            with self.subTest(name=name), tempfile.TemporaryDirectory() as tmp:
                path = Path(tmp)
                report = self.fixture(path, monitor.now_utc())
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
                    original_id = queued[0]["event_id"]
                    marker = monitor.read_json(path / "learnrise-monitor-rearm-required.json")
                    self.assertEqual(marker["baseline_id"], original_id)
                    self.assertEqual(monitor.main(args), 2)
                    self.assertEqual(len(queued), 1)
                    status_output = io.StringIO()
                    with redirect_stdout(status_output):
                        self.assertEqual(monitor.main(["--status", "--state-dir", str(path)]), 2)
                    self.assertTrue(json.loads(status_output.getvalue())["rearm_required"])
                    self.assertEqual(monitor.main(["--shadow", *args[1:]]), 2)
                    self.rearm_fixture(path)
                    if mutate:
                        ready_path = path / "learnrise-monitor-cutover-ready.json"
                        ready = monitor.read_json(ready_path)
                        mutate(ready)
                        monitor.atomic(ready_path, ready)
                        self.assertEqual(monitor.main(["--rearm", *args]), 2)
                        self.assertEqual(len(queued), 1)
                        self.assertEqual(monitor.main(["--shadow", *args[1:]]), 2)
                        self.rearm_fixture(path)
                    self.assertEqual(monitor.main(["--rearm", *args]), 0)
                    self.assertEqual([event["event_id"] for event in queued],
                                     [original_id, original_id])
                    self.assertFalse((path / "learnrise-monitor-rearm-required.json").exists())

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
                self.assertEqual(len(queued), 1)
                self.assertEqual(monitor.main(["--shadow", *args[1:]]), 2)
                self.rearm_fixture(path)
                self.assertEqual(monitor.main(["--rearm", *args]), 2)
                pending = monitor.read_json(path / "learnrise-monitor-cursor.json")["pending"]
                self.assertEqual([event["entity"] for event in pending], ["baseline", "boss:" + new_id])
                self.assertEqual(monitor.main(args), 2)
                self.assertEqual(len(queued), 2)
                self.assertEqual(monitor.main(["--shadow", *args[1:]]), 2)
                self.rearm_fixture(path)
                self.assertEqual(monitor.main(["--rearm", *args]), 0)
                self.assertEqual([event["entity"] for event in queued],
                                 ["baseline", "baseline", "baseline", "boss:" + new_id])
                self.assertEqual(len({event["event_id"] for event in queued[:3]}), 1)

    def test_queue_timeout_does_not_clear_watchdog_outage_during_retry(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp)
            now = monitor.now_utc()
            report = self.fixture(path, now)
            ledger = monitor.read_json(path / "boss-observed-ledger.json")
            outbox = monitor.read_json(path / "boss-action-outbox.json")
            supervisor = monitor.read_json(path / "pr-supervisor-state.json")
            snapshot = monitor.semantic(ledger, outbox, supervisor, report["inventory"], now)
            as_of = now.isoformat()
            monitor.atomic(path / "learnrise-monitor-snapshot.json",
                           {"as_of": as_of, "repository": monitor.REPO, "entities": snapshot})
            monitor.atomic(path / "learnrise-monitor-receipt.json", {"as_of": as_of, "mode": "shadow"})
            baseline = monitor.baseline_current(snapshot, path)
            monitor.atomic(path / "learnrise-monitor-cutover-ready.json",
                           {"version": 1, "as_of": as_of, "hub": HUB,
                            "coverage": {"covered_heartbeat_ids": [HUB], "paused_heartbeat_ids": [HUB],
                                         "objectives": ["#1"], "complete": True, "paused_at": as_of},
                            "drain": {"confirmed_at": as_of, "method": "observed_empty", "no_prior_turns": True},
                            "shadow": {"receipt_as_of": as_of,
                                       "baseline_id": monitor.event_id("baseline", None, baseline, 1),
                                       "reviewed": True},
                            "canaries": {"queue": True, "watchdog": True, "preview": True}})
            (path / "learnrise-monitor-mode").write_text("apply\n")
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
                self.assertEqual(monitor.read_json(path / "learnrise-monitor-receipt.json")["mode"], "shadow")
                self.assertEqual(watchdog.check(path, HUB, now=now, queue=fault_queue), 2)
                first_alert = monitor.read_json(path / "learnrise-watchdog-alert.json")["event_id"]
                self.assertEqual(hub_faults[0]["reason"], "monitor cutover rearm required")
                self.assertEqual(len(hub_faults), 1)
                self.assertEqual(monitor.main(args), 2)
                self.assertEqual(len(queue_attempts), 1)
                self.assertEqual(monitor.main(["--shadow", *args[1:]]), 2)
                self.rearm_fixture(path)
                self.assertEqual(monitor.main(["--rearm", *args]), 2)
                self.assertEqual(alert_ids_during_queue, [first_alert])
                self.assertTrue((path / "learnrise-monitor-fault.json").exists())
                self.assertEqual(monitor.read_json(path / "learnrise-monitor-receipt.json")["mode"], "shadow")
                self.assertEqual(watchdog.check(path, HUB, now=now + timedelta(minutes=1),
                                                queue=fault_queue), 2)
                self.assertEqual(len(hub_faults), 1)
                self.assertEqual(monitor.main(args), 2)
                self.assertEqual(len(queue_attempts), 2)
                self.assertEqual(monitor.main(["--shadow", *args[1:]]), 2)
                self.rearm_fixture(path)
                self.assertEqual(monitor.main(["--rearm", *args]), 0)
                self.assertFalse((path / "learnrise-monitor-fault.json").exists())
                self.assertIsNotNone(monitor.read_json(path / "learnrise-monitor-receipt.json"))
                self.assertEqual(watchdog.check(path, HUB, now=monitor.now_utc(), queue=fault_queue), 0)
                self.assertFalse((path / "learnrise-watchdog-alert.json").exists())
                self.assertEqual(len({event["event_id"] for event in queue_attempts}), 1)

    def test_bridge_failure_persists_fault_without_queue(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp)
            self.fixture(path, monitor.now_utc())
            calls = []
            def runner(argv, timeout=120):
                calls.append(argv)
                return Result(2, "bad")
            with patch.object(monitor, "bounded_run", side_effect=runner):
                self.assertEqual(monitor.main(["--apply", "--state-dir", str(path)]), 2)
                self.assertEqual(monitor.main(["--apply", "--state-dir", str(path)]), 2)
            self.assertEqual(len(calls), 1)
            self.assertTrue((path / "learnrise-monitor-rearm-required.json").exists())
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
