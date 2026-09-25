#!/usr/bin/env python3
import importlib.util
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
import tempfile
import unittest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "reconcile_ledger.py"
spec = importlib.util.spec_from_file_location("boss_reconcile", SCRIPT)
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)
NOW = datetime.now(timezone.utc)


def row(name, **updates):
    value = {"id": name, "issue_number": int(name.split()[0][1:]), "work_item": True,
             "github_state": "OPEN", "owner_active": True, "active_work": False,
             "pull_requests": [], "pull_request_states": {}}
    value.update(updates)
    return value


class ReconcileTests(unittest.TestCase):
    def test_merged_phase_releases_successor_despite_open_parent_issue(self):
        ledger = {"repository": "example/project", "objectives": [
            row("#486 PR2", pull_requests=[568], pull_request_states={"568": "MERGED"}),
            row("#486 PR3", completion_source="pull_request"),
        ], "dependency_edges": [{"before": "#486 PR2", "after": "#486 PR3", "phase": "start"}]}
        actions, waits = mod.decide(ledger, {})
        self.assertEqual([(a["objective"], a["verb"]) for a in actions], [("#486 PR3", "LAUNCH_TASK")])
        self.assertEqual(waits, [])

    def test_static_owner_flag_never_hides_ready_launch(self):
        ledger = {"repository": "example/project", "objectives": [row(f"#{n}") for n in (356, 542, 523)]}
        actions, _ = mod.decide(ledger, {})
        self.assertEqual({a["issue"] for a in actions}, {356, 542, 523})
        self.assertTrue(all(a["verb"] == "LAUNCH_TASK" for a in actions))
        self.assertEqual(actions, mod.decide(ledger, {})[0])

    def test_merged_code_open_issue_reconciles_acceptance_without_relaunch(self):
        task = "01a0d8dc-b312-70d2-ad86-b58088dd22d8"
        cases = [
            row("#330", pull_requests=[491], pull_request_states={"491": "MERGED"}),
            row("#533", coding_task_id=task, pull_requests=[571], pull_request_states={"571": "MERGED"}),
        ]
        for item in cases:
            with self.subTest(issue=item["issue_number"]):
                actions, waiting = mod.decide({"repository": "example/project", "objectives": [item]},
                                              {task: {"id": task, "status": "completed"}})
                self.assertEqual(waiting, [])
                self.assertEqual(actions[0]["verb"], "RECONCILE_ISSUE")
                self.assertEqual(actions[0]["linked_prs"], item["pull_requests"])
                self.assertNotEqual(actions[0]["verb"], "LAUNCH_TASK")

    def test_ended_turn_is_resumed_and_unknown_turn_fails_closed(self):
        task = "01a0d8dc-b312-70d2-ad86-b58088dd22d8"
        ledger = {"repository": "example/project", "objectives": [row("#356", coding_task_id=task)]}
        for status, expected in (("completed", "RESUME_TASK"), ("interrupted", "RESUME_TASK"),
                                 ("running", None), ("blocked", "RECOVER_OWNER")):
            actions, waits = mod.decide(ledger, {task: {"id": task, "status": status}})
            self.assertEqual(actions[0]["verb"] if actions else None, expected)
            self.assertEqual(bool(waits), expected is None)
        self.assertEqual(mod.decide(ledger, {})[0][0]["verb"], "RECOVER_OWNER")

    def test_explicit_file_scope_holds_release_only_after_verified_closure(self):
        blocker = row("#542")
        held = [row("#465", dispatch_hold={"until": "#542", "reason": "session file overlap"}),
                row("#486 PR3", dispatch_hold={"until": "#542", "reason": "session file overlap"})]
        ledger = {"repository": "example/project", "objectives": [blocker, *held]}
        actions, waiting = mod.decide(ledger, {})
        self.assertEqual([a["objective"] for a in actions], ["#542"])
        self.assertEqual({w["objective"] for w in waiting}, {"#465", "#486 PR3"})
        blocker["github_state"] = "CLOSED"
        actions, _ = mod.decide(ledger, {})
        self.assertEqual({a["objective"] for a in actions}, {"#465", "#486 PR3"})

    def test_current_head_controls_repair_and_merge_verification(self):
        task = "01a0d8dc-b312-70d2-ad86-b58088dd22d8"
        observation = {"head": "a" * 40, "merge": "CLEAN", "check_count": 3,
                       "passed_count": 3, "failed_checks": [], "pending_checks": []}
        item = row("#531", coding_task_id=task, pull_requests=[548], pull_request_states={"548": "OPEN"},
                   pr_observations={"548": observation})
        ledger = {"repository": "example/project", "objectives": [item]}
        tasks = {task: {"id": task, "status": "completed"}}
        self.assertEqual(mod.decide(ledger, tasks)[0][0]["verb"], "VERIFY_MERGE")
        observation["head"] = "b" * 40
        observation["failed_checks"] = ["Focused tooling"]
        repair = mod.decide(ledger, tasks)[0][0]
        self.assertEqual((repair["verb"], repair["head"]), ("REPAIR_PR", "b" * 40))
        observation["head"] = "not-a-head;touch /tmp/owned"
        with self.assertRaises(mod.ReconcileError):
            mod.decide(ledger, tasks)

    def test_freshness_and_input_validation(self):
        with tempfile.TemporaryDirectory() as temp:
            ledger = Path(temp) / "ledger.json"
            tasks = Path(temp) / "tasks.json"
            ledger.write_text(json.dumps({"schema_version": 1, "repository": "example/project",
                                          "last_checked_utc": NOW.isoformat(), "objectives": []}))
            tasks.write_text(json.dumps({"as_of": NOW.isoformat(), "tasks": []}))
            mod.load_inputs(ledger, tasks, NOW)
            for bad in ("not-a-date", (NOW - timedelta(hours=1)).isoformat()):
                tasks.write_text(json.dumps({"as_of": bad, "tasks": []}))
                with self.assertRaises(mod.ReconcileError):
                    mod.load_inputs(ledger, tasks, NOW)
            tasks.write_text(json.dumps({"as_of": NOW.isoformat(), "tasks": [{"id": "$(touch /tmp/owned)", "status": "running"}]}))
            with self.assertRaises(mod.ReconcileError):
                mod.load_inputs(ledger, tasks, NOW)

    def test_plan_is_read_only_even_with_ready_work(self):
        with tempfile.TemporaryDirectory() as temp:
            ledger = Path(temp) / "ledger.json"
            tasks = Path(temp) / "tasks.json"
            outbox = Path(temp) / "outbox.json"
            item = row("#356", last_checked_utc=NOW.isoformat())
            ledger.write_text(json.dumps({"schema_version": 1, "repository": "example/project",
                                          "last_checked_utc": NOW.isoformat(), "objectives": [item]}))
            tasks.write_text(json.dumps({"as_of": NOW.isoformat(), "tasks": []}))
            before = (ledger.read_bytes(), tasks.read_bytes())
            self.assertEqual(mod.main(["--ledger", str(ledger), "--tasks", str(tasks),
                                       "--outbox", str(outbox), "plan"]), 0)
            self.assertFalse(outbox.exists())
            self.assertEqual((ledger.read_bytes(), tasks.read_bytes()), before)

    def test_durable_ack_and_unresolved_action_alert(self):
        with tempfile.TemporaryDirectory() as temp:
            outbox = Path(temp) / "outbox.json"
            action = mod.action("example/project", row("#356"), "LAUNCH_TASK")
            self.assertEqual(mod.sync_outbox(outbox, [action], NOW), [])
            self.assertEqual(mod.sync_outbox(outbox, [action], NOW + timedelta(minutes=16)), [action["id"]])
            mod.acknowledge(outbox, action["id"], "Task launch accepted: task UUID 123", NOW + timedelta(minutes=16))
            self.assertEqual(mod.sync_outbox(outbox, [action], NOW + timedelta(minutes=17)), [])
            self.assertEqual(mod.sync_outbox(outbox, [action], NOW + timedelta(minutes=32)), [action["id"]])
            mod.sync_outbox(outbox, [], NOW + timedelta(minutes=33))
            self.assertEqual(json.loads(outbox.read_text())["actions"][action["id"]]["state"], "resolved")
            self.assertEqual(mod.sync_outbox(outbox, [action], NOW + timedelta(minutes=34)), [])
            self.assertEqual(json.loads(outbox.read_text())["actions"][action["id"]]["state"], "pending")
            for bad in ("bad", "a" * 24):
                with self.assertRaises(mod.ReconcileError):
                    mod.acknowledge(outbox, bad, "different evidence", NOW)


if __name__ == "__main__":
    unittest.main()
