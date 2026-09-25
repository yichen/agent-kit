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

    def test_pending_client_thread_never_launches_duplicate_task(self):
        for pending in ("client-abc", "$(touch /tmp/owned)"):
            with self.subTest(pending=pending):
                ledger = {"repository": "example/project", "objectives": [
                    row("#506", pending_client_thread_id=pending)]}
                actions, waits = mod.decide(ledger, {})
                self.assertEqual(waits, [])
                self.assertEqual([(a["verb"], a["objective"]) for a in actions],
                                 [("RECOVER_OWNER", "#506")])
        for malformed in ("", 123, []):
            with self.subTest(malformed=malformed):
                ledger = {"repository": "example/project", "objectives": [
                    row("#506", pending_client_thread_id=malformed)]}
                with self.assertRaises(mod.ReconcileError):
                    mod.decide(ledger, {})

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
        observation = {"head": "a" * 40, "merge": "CLEAN",
                       "required_checks": ["Focused tooling", "Unit tests"],
                       "checks": [
                           {"name": "Focused tooling", "head": "a" * 40, "state": "SUCCESS"},
                           {"name": "Unit tests", "head": "a" * 40, "state": "SUCCESS"},
                       ], "review": {"state": "APPROVED", "head": "a" * 40}}
        item = row("#531", coding_task_id=task, pull_requests=[548], pull_request_states={"548": "OPEN"},
                   pr_observations={"548": observation})
        ledger = {"repository": "example/project", "objectives": [item]}
        tasks = {task: {"id": task, "status": "completed"}}
        self.assertEqual(mod.decide(ledger, tasks)[0][0]["verb"], "VERIFY_MERGE")
        observation["head"] = "b" * 40
        observation["checks"][0]["state"] = "FAILURE"
        repair = mod.decide(ledger, tasks)[0][0]
        self.assertEqual((repair["verb"], repair["head"]), ("REPAIR_PR", "b" * 40))
        observation["head"] = "not-a-head;touch /tmp/owned"
        with self.assertRaises(mod.ReconcileError):
            mod.decide(ledger, tasks)

    def test_open_pr_prepass_ignores_issue_gates_and_closed_state(self):
        task = "01a0d8dc-b312-70d2-ad86-b58088dd22d8"
        head = "a" * 40
        row_with_pr = row("#531", github_state="CLOSED", human_gate=True,
                          dispatch_hold={"until": "#999", "reason": "blocked"},
                          coding_task_id=task, pull_requests=[548], pull_request_states={"548": "OPEN"})
        obs = {"head": head, "merge": "CLEAN", "required_checks": ["CI"],
               "checks": [{"name": "CI", "head": head, "state": "FAILURE"}],
               "review": {"state": "APPROVED", "head": head}}
        row_with_pr["pr_observations"] = {"548": obs}
        actions, waits = mod.decide({"repository": "example/project", "objectives": [row_with_pr]},
                                    {task: {"id": task, "status": "running"}})
        self.assertEqual([(item["verb"], item["pr"], item["head"]) for item in actions],
                         [("REPAIR_PR", 548, head)])
        self.assertEqual(waits, [{"objective": "#531", "reason": "human_gate", "pr": 548}])

    def test_untracked_and_ambiguous_open_prs_are_quarantined_once(self):
        tracked = row("#531", pull_requests=[548], pull_request_states={"548": "OPEN"})
        ambiguous = row("#532", pull_requests=[548], pull_request_states={"548": "OPEN"})
        ledger = {"repository": "example/project", "objectives": [tracked, ambiguous],
                  "open_pull_requests": [{"number": 548, "head": "a" * 40, "objective": "#531"},
                                         {"number": 777, "head": "b" * 40}]}
        actions, _ = mod.decide(ledger, {})
        self.assertEqual([(a["verb"], a["pr"]) for a in actions],
                         [("QUARANTINE_PR", 548), ("QUARANTINE_PR", 777)])
        self.assertEqual(len({a["id"] for a in actions}), len(actions))

    def test_quarantined_candidate_pr_suppresses_duplicate_issue_launch(self):
        candidate = row("#531")
        ledger = {"repository": "example/project", "objectives": [candidate],
                  "open_pull_requests": [{"number": 777, "head": "a" * 40,
                                          "candidate_objectives": ["#531"],
                                          "quarantine_reason": "ambiguous issue references"}]}
        actions, waiting = mod.decide(ledger, {}, NOW)
        self.assertEqual([(item["verb"], item["pr"]) for item in actions], [("QUARANTINE_PR", 777)])
        self.assertEqual(waiting, [{"objective": "#531", "reason": "quarantined_pr_candidate"}])

    def test_required_contexts_are_exact_complete_and_current_head(self):
        task = "01a0d8dc-b312-70d2-ad86-b58088dd22d8"
        head, other = "a" * 40, "b" * 40
        base = {"head": head, "merge": "CLEAN", "required_checks": ["Build", "Tests"],
                "checks": [{"name": "Build", "head": head, "state": "SUCCESS"},
                          {"name": "Tests", "head": head, "state": "SUCCESS"}],
                "review": {"state": "APPROVED", "head": head}}
        item = row("#531", coding_task_id=task, pull_requests=[548], pull_request_states={"548": "OPEN"})
        ledger = {"repository": "example/project", "objectives": [item]}
        tasks = {task: {"id": task, "status": "running"}}
        cases = [
            (base, "VERIFY_MERGE", None),
            ({**base, "checks": base["checks"][:1]}, "RECOVER_OWNER", None),
            ({**base, "checks": [*base["checks"], {"name": "Tests", "head": head, "state": "SUCCESS"}]}, "RECOVER_OWNER", None),
            ({**base, "checks": [base["checks"][0], {"name": "Tests", "head": other, "state": "SUCCESS"}]}, "REPAIR_PR", None),
            ({**base, "checks": [base["checks"][0], {"name": "Tests", "head": head, "state": "PENDING", "started_at": (NOW - timedelta(minutes=46)).isoformat()}]}, "REPAIR_PR", None),
            ({**base, "checks": [base["checks"][0], {"name": "Tests", "head": head, "state": "PENDING", "started_at": (NOW - timedelta(minutes=3)).isoformat()}]}, None, "required_pr_checks_pending"),
        ]
        for observation, expected_action, expected_wait in cases:
            with self.subTest(observation=observation):
                item["pr_observations"] = {"548": observation}
                actions, waiting = mod.decide(ledger, tasks, NOW)
                self.assertEqual(actions[0]["verb"] if actions else None, expected_action)
                self.assertEqual(waiting[0]["reason"] if waiting else None, expected_wait)
        for required in ([], ["Build", "Build"], ["Build", "$(touch /tmp/owned)"]):
            with self.subTest(required=required):
                item["pr_observations"] = {"548": {**base, "required_checks": required}}
                actions, _ = mod.decide(ledger, tasks, NOW)
                self.assertEqual(actions[0]["verb"], "RECOVER_OWNER")
        self.assertFalse(Path("/tmp/owned").exists())

    def test_review_gate_is_independent_and_bound_to_exact_head(self):
        task, head = "01a0d8dc-b312-70d2-ad86-b58088dd22d8", "a" * 40
        item = row("#531", coding_task_id=task, pull_requests=[548], pull_request_states={"548": "OPEN"})
        ledger = {"repository": "example/project", "objectives": [item]}
        tasks = {task: {"id": task, "status": "completed"}}
        ci = {"head": head, "merge": "CLEAN", "required_checks": ["CI"],
              "checks": [{"name": "CI", "head": head, "state": "SUCCESS"}]}
        cases = [
            ({"state": "MISSING", "head": None}, "VERIFY_REVIEW"),
            ({"state": "STALE", "head": "b" * 40}, "VERIFY_REVIEW"),
            ({"state": "CHANGES_REQUESTED", "head": None}, "VERIFY_REVIEW"),
            ({"state": "APPROVED", "head": "b" * 40}, "VERIFY_REVIEW"),
            ({"state": "APPROVED", "head": head}, "VERIFY_MERGE"),
        ]
        for review, expected in cases:
            with self.subTest(review=review):
                item["pr_observations"] = {"548": {**ci, "review": review}}
                actions, waiting = mod.decide(ledger, tasks, NOW)
                self.assertEqual([action["verb"] for action in actions], [expected])
                self.assertEqual(waiting, [])

    def test_open_pr_inventory_malformed_inputs_fail_closed(self):
        cases = [[{"number": True}], [{"number": 548}, {"number": 548}], {"number": 548},
                 [{"number": "$(touch /tmp/owned)"}]]
        for inventory in cases:
            with self.subTest(inventory=inventory):
                ledger = {"repository": "example/project", "objectives": [], "open_pull_requests": inventory}
                with self.assertRaises(mod.ReconcileError):
                    mod.decide(ledger, {}, NOW)
        self.assertFalse(Path("/tmp/owned").exists())

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
            first_ack = json.loads(outbox.read_text())["actions"][action["id"]]["acknowledged_at"]
            mod.acknowledge(outbox, action["id"], "Task launch accepted: task UUID 123", NOW + timedelta(minutes=32))
            self.assertEqual(json.loads(outbox.read_text())["actions"][action["id"]]["acknowledged_at"], first_ack)
            self.assertEqual(mod.sync_outbox(outbox, [action], NOW + timedelta(minutes=33)), [action["id"]])
            mod.sync_outbox(outbox, [], NOW + timedelta(minutes=34))
            self.assertEqual(json.loads(outbox.read_text())["actions"][action["id"]]["state"], "resolved")
            self.assertEqual(mod.sync_outbox(outbox, [action], NOW + timedelta(minutes=35)), [])
            self.assertEqual(json.loads(outbox.read_text())["actions"][action["id"]]["state"], "pending")
            for bad in ("bad", "a" * 24):
                with self.assertRaises(mod.ReconcileError):
                    mod.acknowledge(outbox, bad, "different evidence", NOW)

    def test_review_action_ack_is_checked_against_next_scan_effect(self):
        with tempfile.TemporaryDirectory() as temp:
            outbox = Path(temp) / "outbox.json"
            head = "a" * 40
            item = row("#531", pull_requests=[548], pull_request_states={"548": "OPEN"},
                       pr_observations={"548": {"head": head, "merge": "CLEAN", "required_checks": ["CI"],
                           "checks": [{"name": "CI", "head": head, "state": "SUCCESS"}],
                           "review": {"state": "STALE", "head": "b" * 40}}})
            ledger = {"repository": "example/project", "objectives": [item]}
            action = mod.decide(ledger, {}, NOW)[0][0]
            self.assertEqual(action["verb"], "VERIFY_REVIEW")
            mod.sync_outbox(outbox, [action], NOW)
            mod.acknowledge(outbox, action["id"], "Requested a new review on the current head", NOW)
            self.assertEqual(mod.sync_outbox(outbox, [action], NOW + timedelta(minutes=16)), [action["id"]])
            item["pr_observations"]["548"]["review"] = {"state": "APPROVED", "head": head}
            next_actions = mod.decide(ledger, {}, NOW + timedelta(minutes=17))[0]
            self.assertEqual([value["verb"] for value in next_actions], ["VERIFY_MERGE"])
            mod.sync_outbox(outbox, next_actions, NOW + timedelta(minutes=17))
            recorded = json.loads(outbox.read_text())["actions"]
            self.assertEqual(recorded[action["id"]]["state"], "resolved")


if __name__ == "__main__":
    unittest.main()
