import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "operational_store.py"


class OperationalStoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.repo = self.root / "repo"
        self.repo.mkdir()
        subprocess.run(["git", "init", "-q", str(self.repo)], check=True)
        subprocess.run(["git", "-C", str(self.repo), "remote", "add", "origin", "git@github.com:Example/Project.git"], check=True)
        self.db = self.root / "operations.sqlite3"

    def tearDown(self):
        self.temp.cleanup()

    def call(self, *args, expected=0):
        result = subprocess.run([sys.executable, str(SCRIPT), "--db", str(self.db), *map(str, args)], text=True, capture_output=True)
        if result.returncode != expected:
            self.fail(f"command exited {result.returncode}, expected {expected}: {result.stderr}\n{result.stdout}")
        return json.loads(result.stdout) if result.stdout.strip() else None

    def command(self, *args):
        return (*args, "--repo", self.repo)

    def claim(self, owner="worker-A", phase="implement"):
        return self.call(*self.command("claim", "--issue", "14", "--phase", phase, "--owner", owner))

    def inventory(self, tasks, name="inventory.json"):
        path = self.root / name
        path.write_text(json.dumps({"as_of": dt.datetime.now(dt.timezone.utc).isoformat(), "tasks": tasks}))
        return path

    def test_separate_processes_allow_only_one_claimant(self):
        command = [sys.executable, str(SCRIPT), "--db", str(self.db), "claim", "--repo", str(self.repo), "--issue", "14", "--phase", "implement"]
        processes = [subprocess.Popen(command + ["--owner", f"worker-{i}"], text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE) for i in range(2)]
        results = [process.communicate(timeout=15) + (process.returncode,) for process in processes]
        self.assertEqual(sum(code == 0 for _, _, code in results), 1, results)
        self.assertEqual(sum(code == 2 and "claim held" in err for _, err, code in results), 1, results)

    def test_same_owner_is_idempotent_and_generation_increments(self):
        first = self.claim()
        self.assertFalse(first["reused"])
        self.assertTrue(self.claim()["reused"])
        self.call(*self.command("release", "--issue", "14", "--phase", "implement", "--owner", "worker-A", "--generation", "1"))
        second = self.claim("worker-B")
        self.assertEqual(second["generation"], 2)
        self.call(*self.command("release", "--issue", "14", "--phase", "implement", "--owner", "worker-B", "--generation", "1"), expected=2)
        self.call(*self.command("reserve", "--issue", "14", "--phase", "implement", "--owner", "worker-B", "--generation", "1", "--verb", "launch"), expected=2)

    def test_lock_contention_serializes_writers(self):
        self.claim()
        lock = sqlite3.connect(self.db, timeout=1, isolation_level=None)
        lock.execute("BEGIN IMMEDIATE")
        command = [sys.executable, str(SCRIPT), "--db", str(self.db), "claim", "--repo", str(self.repo), "--issue", "15", "--phase", "implement", "--owner", "worker-B"]
        process = subprocess.Popen(command, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        time.sleep(0.25)
        self.assertIsNone(process.poll(), "writer did not wait for SQLite lock")
        lock.commit()
        lock.close()
        out, err = process.communicate(timeout=15)
        self.assertEqual(process.returncode, 0, err)
        self.assertEqual(json.loads(out)["issue"], 15)

    def test_crash_before_launch_keeps_reservation_and_never_retries(self):
        claim = self.claim()
        counter = self.root / "calls"
        adapter = self.root / "adapter"
        adapter.write_text("#!/usr/bin/env python3\nimport os,sys\np=os.environ['CALLS']\nopen(p,'a').write('called\\n')\nsys.exit(9)\n")
        adapter.chmod(0o755)
        env = os.environ.copy()
        env["CALLS"] = str(counter)
        cmd = [sys.executable, str(SCRIPT), "--db", str(self.db), "dispatch", "--repo", str(self.repo), "--issue", "14", "--phase", "implement", "--owner", "worker-A", "--generation", str(claim["generation"]), "--verb", "launch", "--adapter", str(adapter)]
        failed = subprocess.run(cmd, env=env, text=True, capture_output=True)
        self.assertEqual(failed.returncode, 2)
        self.assertIn("remains for reconciliation", failed.stderr)
        retry = subprocess.run(cmd, env=env, text=True, capture_output=True)
        self.assertEqual(retry.returncode, 2)
        self.assertIn("already reserved", retry.stderr)
        self.assertEqual(counter.read_text().splitlines(), ["called"])
        reserved = self.call(*self.command("status"))["actions"][0]
        empty = self.inventory([])
        self.call(*self.command("abandon", "--issue", "14", "--phase", "implement", "--owner", "worker-A", "--generation", "1", "--action-id", reserved["action_id"], "--inventory", empty, "--evidence", "Fresh task inventory found no created task"))
        self.call(*self.command("release", "--issue", "14", "--phase", "implement", "--owner", "worker-A", "--generation", "1"))
        next_claim = self.claim("worker-B")
        next_action = self.call(*self.command("reserve", "--issue", "14", "--phase", "implement", "--owner", "worker-B", "--generation", str(next_claim["generation"]), "--verb", "launch"))["action"]
        self.assertNotEqual(next_action["action_id"], reserved["action_id"])
        self.assertEqual(next_action["attempt"], 2)
        status = self.call(*self.command("status"))
        self.assertEqual([row["status"] for row in status["actions"]], ["abandoned", "reserved"])

    def test_crash_after_task_creation_is_recovered_by_action_id_without_duplicate(self):
        claim = self.claim()
        created = self.root / "created-task.json"
        calls = self.root / "calls"
        adapter = self.root / "adapter-after-create"
        adapter.write_text("#!/usr/bin/env python3\nimport json,os,sys\nr=json.load(sys.stdin)\nopen(os.environ['CALLS'],'a').write('called\\n')\njson.dump({'id':'task-14','action_id':r['action_id'],'status':'running'},open(os.environ['CREATED'],'w'))\nsys.exit(8)\n")
        adapter.chmod(0o755)
        env = os.environ.copy(); env.update(CALLS=str(calls), CREATED=str(created))
        cmd = [sys.executable, str(SCRIPT), "--db", str(self.db), "dispatch", "--repo", str(self.repo), "--issue", "14", "--phase", "implement", "--owner", "worker-A", "--generation", str(claim["generation"]), "--verb", "launch", "--adapter", str(adapter)]
        failed = subprocess.run(cmd, env=env, text=True, capture_output=True)
        self.assertEqual(failed.returncode, 2)
        task = json.loads(created.read_text())
        scan = self.call(*self.command("scan", "--inventory", self.inventory([task])))
        self.assertEqual(scan["actions"][0]["status"], "effect_verified")
        self.assertEqual(calls.read_text().splitlines(), ["called"])
        action = self.call(*self.command("status"))["actions"][0]
        self.assertEqual((action["status"], action["task_id"]), ("effect_verified", "task-14"))

    def test_abandon_cannot_race_an_adapter_that_may_still_create_a_task(self):
        claim = self.claim()
        started, finish, created, calls = [self.root / name for name in ("started", "finish", "created.json", "calls")]
        adapter = self.root / "slow-adapter"
        adapter.write_text("#!/usr/bin/env python3\nimport json,os,sys,time\nr=json.load(sys.stdin)\nopen(os.environ['CALLS'],'a').write('called\\n')\nopen(os.environ['STARTED'],'w').write('yes')\nwhile not os.path.exists(os.environ['FINISH']): time.sleep(.02)\njson.dump({'id':'task-slow','action_id':r['action_id'],'status':'running'},open(os.environ['CREATED'],'w'))\nprint(json.dumps({'task_id':'task-slow'}))\n")
        adapter.chmod(0o755)
        env = os.environ.copy(); env.update(STARTED=str(started), FINISH=str(finish), CREATED=str(created), CALLS=str(calls))
        cmd = [sys.executable, str(SCRIPT), "--db", str(self.db), "dispatch", "--repo", str(self.repo), "--issue", "14", "--phase", "implement", "--owner", "worker-A", "--generation", str(claim["generation"]), "--verb", "launch", "--adapter", str(adapter)]
        process = subprocess.Popen(cmd, env=env, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        deadline = time.time() + 10
        while not started.exists() and time.time() < deadline:
            time.sleep(.02)
        self.assertTrue(started.exists(), "adapter did not start")
        process.kill()
        self.assertNotEqual(process.wait(timeout=10), 0, "dispatcher unexpectedly survived simulated crash")
        action = self.call(*self.command("status"))["actions"][0]
        inventory = self.inventory([])
        abandon = subprocess.run([sys.executable, str(SCRIPT), "--db", str(self.db), *map(str, self.command("abandon", "--issue", "14", "--phase", "implement", "--owner", "worker-A", "--generation", "1", "--action-id", action["action_id"], "--inventory", inventory, "--evidence", "Fresh empty inventory while adapter runs"))], text=True, capture_output=True)
        self.assertEqual(abandon.returncode, 2, abandon.stderr)
        self.assertIn("action dispatch is still in progress", abandon.stderr)
        self.assertFalse(finish.exists())
        finish.write_text("done")
        out, err = process.communicate(timeout=15)
        self.assertEqual(process.returncode, -9, err)
        deadline = time.time() + 10
        while not created.exists() and time.time() < deadline:
            time.sleep(.02)
        self.assertTrue(created.exists(), f"adapter did not finish after dispatcher crash: {out!r} {err!r}")
        task = json.loads(created.read_text())
        self.call(*self.command("scan", "--inventory", self.inventory([task], "finished.json")))
        self.assertEqual(calls.read_text().splitlines(), ["called"])
        self.assertEqual(self.call(*self.command("status"))["actions"][0]["status"], "effect_verified")

    def test_missing_ack_and_missing_effect_are_durable_findings(self):
        claim = self.claim()
        reserved = self.call(*self.command("reserve", "--issue", "14", "--phase", "implement", "--owner", "worker-A", "--generation", str(claim["generation"]), "--verb", "launch"))["action"]
        self.call(*self.command("release", "--issue", "14", "--phase", "implement", "--owner", "worker-A", "--generation", str(claim["generation"])), expected=2)
        found = self.call(*self.command("scan", "--inventory", self.inventory([])))
        self.assertEqual(found["actions"][0]["status"], "action_unacknowledged")
        history = self.call(*self.command("history", "--issue", "14"))
        self.assertIn("action_unacknowledged", [event["kind"] for event in history])
        self.assertEqual(reserved["status"], "reserved")

    def test_ack_is_cas_fenced_and_next_scan_verifies_effect(self):
        claim = self.claim()
        reserved = self.call(*self.command("reserve", "--issue", "14", "--phase", "implement", "--owner", "worker-A", "--generation", str(claim["generation"]), "--verb", "launch"))["action"]
        self.call(*self.command("ack", "--issue", "14", "--phase", "implement", "--generation", str(claim["generation"]), "--action-id", reserved["action_id"], "--task-id", "task-14"))
        # Retrying the same CAS result is idempotent; conflicting task IDs are rejected.
        self.call(*self.command("ack", "--issue", "14", "--phase", "implement", "--generation", str(claim["generation"]), "--action-id", reserved["action_id"], "--task-id", "task-14"))
        self.call(*self.command("ack", "--issue", "14", "--phase", "implement", "--generation", str(claim["generation"]), "--action-id", reserved["action_id"], "--task-id", "other-task"), expected=2)
        absent = self.call(*self.command("scan", "--inventory", self.inventory([])))
        self.assertEqual(absent["actions"][0]["status"], "action_effect_missing")
        present = self.call(*self.command("scan", "--inventory", self.inventory([{"id": "task-14", "action_id": reserved["action_id"], "status": "running"}], "present.json")))
        self.assertEqual(present["actions"][0]["status"], "effect_verified")
        self.call(*self.command("release", "--issue", "14", "--phase", "implement", "--owner", "worker-A", "--generation", str(claim["generation"])))
        next_claim = self.claim("worker-B")
        self.assertEqual(next_claim["generation"], 2)
        self.call(*self.command("ack", "--issue", "14", "--phase", "implement", "--generation", "1", "--action-id", "0" * 64, "--task-id", "stale-task"), expected=2)

    def test_bad_inventory_and_injection_like_inputs_are_rejected(self):
        claim = self.claim()
        before = hashlib.sha256(self.db.read_bytes()).hexdigest()
        for phase in ("x;drop", "../other", ""):
            result = subprocess.run([sys.executable, str(SCRIPT), "--db", str(self.db), "claim", "--repo", str(self.repo), "--issue", "14", "--phase", phase, "--owner", "worker-X"], text=True, capture_output=True)
            self.assertEqual(result.returncode, 2)
        bad = self.root / "bad.json"
        bad.write_text('{"as_of":"yesterday","tasks":[{"id":"x;touch-owned","status":"running"}]}')
        self.call(*self.command("scan", "--inventory", bad), expected=2)
        stale = self.inventory([], "stale.json")
        stale.write_text(json.dumps({"as_of": "2000-01-01T00:00:00Z", "tasks": []}))
        self.call(*self.command("scan", "--inventory", stale), expected=2)
        self.assertEqual(hashlib.sha256(self.db.read_bytes()).hexdigest(), before)
        self.assertFalse((self.root / "touch-owned").exists())
        self.assertEqual(claim["generation"], 1)

    def test_read_only_status_history_and_append_only_events(self):
        self.claim()
        before = self.db.stat().st_size, hashlib.sha256(self.db.read_bytes()).hexdigest()
        self.call(*self.command("status"))
        self.call(*self.command("history"))
        after = self.db.stat().st_size, hashlib.sha256(self.db.read_bytes()).hexdigest()
        self.assertEqual(before, after)
        con = sqlite3.connect(self.db)
        with self.assertRaisesRegex(sqlite3.IntegrityError, "append-only"):
            con.execute("UPDATE events SET kind='changed'")
        with self.assertRaisesRegex(sqlite3.IntegrityError, "append-only"):
            con.execute("DELETE FROM events")
        con.close()

    def test_rebuild_replays_append_only_history_then_verifies_live_inventory(self):
        claim = self.claim()
        action = self.call(*self.command("reserve", "--issue", "14", "--phase", "implement", "--owner", "worker-A", "--generation", str(claim["generation"]), "--verb", "launch"))["action"]
        self.call(*self.command("ack", "--issue", "14", "--phase", "implement", "--generation", str(claim["generation"]), "--action-id", action["action_id"], "--task-id", "task-14"))
        inventory = self.inventory([{"id": "task-14", "action_id": action["action_id"], "status": "running"}])
        con = sqlite3.connect(self.db)
        con.execute("DELETE FROM actions")
        con.execute("DELETE FROM claims")
        con.execute("DELETE FROM generations")
        con.commit(); con.close()
        rebuilt = self.call(*self.command("rebuild", "--inventory", inventory))
        self.assertEqual(rebuilt["actions"][0]["status"], "effect_verified")
        status = self.call(*self.command("status"))
        self.assertEqual(status["active_claims"][0]["owner"], "worker-A")
        self.assertEqual(status["actions"][0]["status"], "effect_verified")


if __name__ == "__main__":
    unittest.main(verbosity=2)
