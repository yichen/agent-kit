#!/usr/bin/env python3
import importlib.util
import json
from datetime import datetime, timezone
from pathlib import Path
import plistlib
import sqlite3
import tempfile
import unittest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "runtime_bridge.py"
spec = importlib.util.spec_from_file_location("boss_runtime_bridge", SCRIPT)
import sys
sys.path.insert(0, str(SCRIPT.parent))
bridge = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bridge)
TASK = "01a0d8dc-c433-78f1-be9f-b11487102f8a"


def objective(number=523, **updates):
    row = {"id": f"#{number}", "issue_number": number, "work_item": True,
           "pull_requests": [], "github_state": "OPEN"}
    row.update(updates)
    return row


class RuntimeBridgeTests(unittest.TestCase):
    def test_sample_scheduler_uses_existing_hub_and_outbox(self):
        sample = SCRIPT.parents[1] / "examples" / "com.yichen.boss.learnrise.plist"
        config = plistlib.loads(sample.read_bytes())
        args = config["ProgramArguments"]
        self.assertEqual(config["StartInterval"], 900)
        self.assertEqual(args[args.index("--hub-task") + 1],
                         "01a0d565-c171-7120-b828-b04db384021f")
        self.assertEqual(args[args.index("--outbox") + 1],
                         "/Users/yichen/agents-artifacts/learnrise-orchestrator/boss-action-outbox.json")

    def test_live_writer_overrides_completed_or_interrupted_rollout(self):
        with tempfile.TemporaryDirectory() as root:
            root = Path(root)
            db = root / "state.db"
            rollout = root / "rollout.jsonl"
            rollout.write_text(json.dumps({"type": "event_msg", "payload": {"type": "task_complete"}}) + "\n")
            connection = sqlite3.connect(db)
            connection.execute("CREATE TABLE threads(id TEXT, rollout_path TEXT)")
            connection.execute("INSERT INTO threads VALUES(?, ?)", (TASK, str(rollout)))
            connection.commit()
            connection.close()
            ledger = {"objectives": [objective(coding_task_id=TASK)]}
            samples = [
                (f"123 /Users/test/.local/bin/codex exec resume --json {TASK} Work\n", "running"),
                (f"123 /Users/test/.local/bin/codex exec resume --json {TASK} Work\n"
                 f"124 /bin/sh -c echo {TASK}\n", "running"),
                (f"124 /bin/sh -c echo {TASK}\n", "completed"),
                (f"124 /bin/sh -c echo codex exec resume --json {TASK}\n", "completed"),
                (f"124 /bin/sh -c codex exec resume --json $(touch /tmp/owned) {TASK}\n", "completed"),
            ]
            for processes, expected in samples:
                with self.subTest(processes=processes):
                    result = bridge.inventory(ledger, db, processes, datetime.now(timezone.utc))
                    self.assertEqual(result["tasks"][0]["status"], expected)
            rollout.write_text(json.dumps({"type": "event_msg", "payload": {"type": "task_started"}}) + "\n")
            self.assertEqual(bridge.inventory(ledger, db, "", datetime.now(timezone.utc))["tasks"][0]["status"], "blocked")

    def test_inventory_sees_committed_wal_row_without_writing_source_catalog(self):
        with tempfile.TemporaryDirectory() as root:
            root = Path(root)
            db = root / "state.db"
            rollout = root / "rollout.jsonl"
            rollout.write_text(json.dumps({"type": "event_msg", "payload": {"type": "task_complete"}}) + "\n")
            writer = sqlite3.connect(db)
            try:
                writer.execute("PRAGMA journal_mode=WAL")
                writer.execute("CREATE TABLE threads(id TEXT, rollout_path TEXT)")
                writer.commit()
                writer.execute("INSERT INTO threads VALUES(?, ?)", (TASK, str(rollout)))
                writer.commit()
                before = {path.name: (path.read_bytes(), path.stat().st_mtime_ns) for path in root.iterdir()}
                result = bridge.inventory({"objectives": [objective(coding_task_id=TASK)]},
                                          db, "", datetime.now(timezone.utc))
                self.assertEqual(result["tasks"], [{"id": TASK, "status": "completed"}])
                self.assertEqual({path.name: (path.read_bytes(), path.stat().st_mtime_ns)
                                  for path in root.iterdir()}, before)
            finally:
                writer.close()

    def test_pr_discovery_is_explicit_and_ambiguous_reference_fails_closed(self):
        ledger = {"objectives": [objective(), objective(604)]}
        cases = [
            ("Closes #523", "work", "feature", [("#523", 603)], False),
            ("Fixes #523", "work", "feature", [("#523", 603)], False),
            ("Implements the approved scope of #523", "Implement #523", "codex/523-feature", [("#523", 603)], False),
            ("Closes #523\nCloses #604", "work", "feature", [], True),
            ("Closes #523\nCloses #604", "Fix #523", "codex/523-feature", [], True),
            ("Closes #523", "Implement #604", "codex/604-feature", [], True),
            ("Closes #523", "Implement #523", "codex/604-feature", [], True),
            ("Closes #523", "Implement #604", "codex/523-feature", [], True),
            ("See #523", "work", "feature", [], True),
            ("", "Fix #523", "feature", [], True),
            ("", "Fix #523", "codex/604-feature", [], True),
            ("", "work", "feature", [], False),
            ("Closes #999", "work", "feature", [], False),
            ("Closes #523; $(touch /tmp/owned)", "work", "feature", [("#523", 603)], False),
        ]
        for body, title, branch, expected, raises in cases:
            with self.subTest(body=body, title=title):
                prs = [{"number": 603, "state": "OPEN", "body": body,
                        "title": title, "headRefName": branch}]
                if raises:
                    with self.assertRaises(bridge.BridgeError):
                        bridge.discover_open_prs(ledger, prs)
                else:
                    self.assertEqual(bridge.discover_open_prs(ledger, prs), expected)

    def test_existing_pr_link_conflicting_with_live_issue_fails_closed(self):
        ledger = {"objectives": [objective(pull_requests=[603]), objective(604)]}
        cases = [
            ("Closes #604", "Implement #604", "codex/604-feature", True),
            ("Closes #523", "Implement #604", "codex/604-feature", True),
            ("Closes #523", "Implement #523", "codex/523-feature", False),
        ]
        for body, title, branch, raises in cases:
            with self.subTest(body=body, title=title, branch=branch):
                prs = [{"number": 603, "state": "OPEN", "body": body,
                        "title": title, "headRefName": branch}]
                if raises:
                    with self.assertRaisesRegex(bridge.BridgeError, "conflict"):
                        bridge.discover_open_prs(ledger, prs)
                else:
                    self.assertEqual(bridge.discover_open_prs(ledger, prs), [])

    def test_malformed_linked_pr_numbers_fail_before_attachment(self):
        cases = [
            [{"number": 615, "state": "OPEN"}],
            ["615"],
            [True],
            [0],
            -1,
        ]
        for linked in cases:
            with self.subTest(linked=linked):
                ledger = {"objectives": [objective(604, pull_requests=linked)]}
                prs = [{"number": 615, "state": "OPEN", "body": "Closes #604",
                        "title": "Fix #604", "headRefName": "codex/604-fix"}]
                with self.assertRaisesRegex(bridge.BridgeError, "malformed linked PR numbers"):
                    bridge.discover_open_prs(ledger, prs)
                self.assertEqual(ledger["objectives"][0]["pull_requests"], linked)

    def test_malformed_objective_shape_fails_closed(self):
        for ledger in (None, [], {}, {"objectives": None}, {"objectives": [None]},
                       {"objectives": [{"issue_number": 523}]},
                       {"objectives": [objective(523), objective(523)]}):
            with self.subTest(ledger=ledger):
                with self.assertRaises(bridge.BridgeError):
                    bridge.discover_open_prs(ledger, [])

    def test_pr_projection_does_not_change_source_ledger(self):
        ledger = {"objectives": [objective(523), objective(604, pull_requests=[615])]}
        before = json.dumps(ledger, sort_keys=True)
        projected = bridge.project_prs(ledger, [("#523", 603)])
        self.assertEqual(json.dumps(ledger, sort_keys=True), before)
        self.assertEqual(projected["objectives"][0]["pull_requests"], [603])
        self.assertEqual(projected["objectives"][1]["pull_requests"], [615])

    def test_discovered_pr_survives_next_scan_after_merge(self):
        source = {"repository": "example/project", "objectives": [objective(523)]}
        first = bridge.project_prs(source, [("#523", 603)])
        second = bridge.carry_observed_prs(source, first)
        self.assertEqual(second["objectives"][0]["pull_requests"], [603])
        second["objectives"][0]["pull_request_states"] = {"603": "MERGED"}
        actions, _ = bridge.reconcile.decide(second, {})
        self.assertEqual([a["verb"] for a in actions], ["RECONCILE_ISSUE"])
        self.assertEqual(source["objectives"][0]["pull_requests"], [])

    def test_canonical_change_during_audit_stops_action_decision(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / "ownership.json"
            path.write_text('{"owner":"before"}')
            original = path.read_bytes()
            bridge.require_unchanged(path, original)
            path.write_text('{"owner":"new coding task"}')
            with self.assertRaisesRegex(bridge.BridgeError, "changed during audit"):
                bridge.require_unchanged(path, original)

    def test_missing_catalog_and_partial_rollout_fail_closed(self):
        with tempfile.TemporaryDirectory() as root:
            root = Path(root)
            db = root / "state.db"
            connection = sqlite3.connect(db)
            connection.execute("CREATE TABLE threads(id TEXT, rollout_path TEXT)")
            connection.commit()
            connection.close()
            ledger = {"objectives": [objective(coding_task_id=TASK)]}
            with self.assertRaises(bridge.BridgeError):
                bridge.inventory(ledger, db, "", datetime.now(timezone.utc))
            connection = sqlite3.connect(db)
            connection.execute("INSERT INTO threads VALUES(?, ?)", (TASK, None))
            connection.commit()
            connection.close()
            with self.assertRaisesRegex(bridge.BridgeError, "invalid rollout path"):
                bridge.inventory(ledger, db, "", datetime.now(timezone.utc))
            connection = sqlite3.connect(db)
            connection.execute("UPDATE threads SET rollout_path='' WHERE id=?", (TASK,))
            connection.commit()
            connection.close()
            with self.assertRaisesRegex(bridge.BridgeError, "invalid rollout path"):
                bridge.inventory(ledger, db, "", datetime.now(timezone.utc))
            rollout = root / "rollout.jsonl"
            for malformed in ("{incomplete", "null", "[]", '{"type":"event_msg","payload":null}',
                              '{"type":"event_msg","payload":[]}'):
                with self.subTest(malformed=malformed):
                    rollout.write_text(malformed + "\n")
                    with self.assertRaises(bridge.BridgeError):
                        bridge.rollout_status(rollout)

    def test_dry_run_does_not_create_lock_inventory_or_outbox(self):
        with tempfile.TemporaryDirectory() as root:
            root = Path(root)
            ledger = root / "ledger.json"
            prs = root / "prs.json"
            processes = root / "processes.txt"
            db = root / "state.db"
            ledger.write_text(json.dumps({"repository": "example/project", "objectives": [objective()]}))
            prs.write_text("[]")
            processes.write_text("")
            connection = sqlite3.connect(db)
            connection.execute("CREATE TABLE threads(id TEXT, rollout_path TEXT)")
            connection.commit()
            connection.close()
            before = {p.name: p.read_bytes() for p in root.iterdir()}
            rc = bridge.main(["--ledger", str(ledger), "--audit", str(root / "unused.py"),
                              "--state-db", str(db), "--tasks", str(root / "tasks.json"),
                              "--outbox", str(root / "outbox.json"), "--prs-file", str(prs),
                              "--processes-file", str(processes), "--skip-audit", "--dry-run"])
            self.assertEqual(rc, 0)
            self.assertEqual({p.name: p.read_bytes() for p in root.iterdir()}, before)


if __name__ == "__main__":
    unittest.main()
