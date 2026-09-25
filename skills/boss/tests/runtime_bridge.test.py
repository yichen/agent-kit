#!/usr/bin/env python3
import importlib.util
import json
from datetime import datetime, timezone
from pathlib import Path
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

    def test_pr_discovery_is_explicit_and_ambiguous_reference_fails_closed(self):
        ledger = {"objectives": [objective(), objective(604)]}
        cases = [
            ("Closes #523", "work", "feature", [("#523", 603)], False),
            ("Fixes #523", "work", "feature", [("#523", 603)], False),
            ("Implements the approved scope of #523", "Implement #523", "codex/523-feature", [("#523", 603)], False),
            ("Closes #523\nCloses #604", "work", "feature", [], True),
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
            rollout = root / "rollout.jsonl"
            rollout.write_text("{incomplete\n")
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
