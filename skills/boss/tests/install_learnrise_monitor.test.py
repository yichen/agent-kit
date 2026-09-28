import os
import copy
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
from datetime import datetime, timedelta, timezone

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import learnrise_event_monitor as monitor

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "install-learnrise-monitor.sh"
HUB = "01a0d565-c171-7120-b828-b04db384021f"


class InstallerTests(unittest.TestCase):
    def test_install_check_activate_stop_touch_only_own_jobs(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            home = root / "home"
            skill = root / "skill"
            state = root / "state"
            bin_dir = root / "bin"
            for directory in (home / "Library/LaunchAgents", skill / "scripts", state, bin_dir):
                directory.mkdir(parents=True)
            for name in ("learnrise_event_monitor.py", "learnrise_event_watchdog.py", "runtime_bridge.py"):
                (skill / "scripts" / name).write_text("# fixture\n")
            (skill / "scripts" / "learnrise_event_monitor.py").write_bytes(
                (SCRIPT.parent / "learnrise_event_monitor.py").read_bytes())
            for name in ("ownership.json", "audit.py", "pr-supervisor-state.json"):
                (state / name).write_text("{}")
            unrelated = home / "Library/LaunchAgents/unrelated.plist"
            unrelated.write_text("leave me alone")
            for name in ("codex", "gh", "plutil"):
                tool = bin_dir / name
                tool.write_text("#!/bin/sh\nif [ \"$(basename \"$0\")\" = plutil ] && [ -f \"$FAKE_BAD_PLIST\" ]; then exit 1; fi\nexit 0\n")
                tool.chmod(0o755)
            launchctl = bin_dir / "launchctl"
            launchctl.write_text("#!/bin/sh\ncase \"$1\" in\nprint) test -f \"$FAKE_LOADED/${2##*/}\" ;;\nbootstrap) if [ -f \"$FAKE_FAIL_BOOTSTRAP\" ]; then rm -f \"$FAKE_FAIL_BOOTSTRAP\"; exit 1; fi; touch \"$FAKE_LOADED/$(basename \"$3\" .plist)\" ;;\nbootout) rm -f \"$FAKE_LOADED/${2##*/}\" ;;\nesac\n")
            launchctl.chmod(0o755)
            loaded = root / "loaded"
            loaded.mkdir()
            fail_bootstrap = root / "fail-bootstrap"
            bad_plist = root / "bad-plist"
            # A clean macOS runner has no ripgrep; keep only system tools and fixtures.
            env = {**os.environ, "PATH": str(bin_dir) + ":/usr/bin:/bin", "FAKE_LOADED": str(loaded),
                   "FAKE_FAIL_BOOTSTRAP": str(fail_bootstrap), "FAKE_BAD_PLIST": str(bad_plist),
                   "LEARNRISE_MONITOR_HOME": str(home), "LEARNRISE_MONITOR_SKILL_PATH": str(skill),
                   "LEARNRISE_MONITOR_STATE_DIR": str(state)}
            def call(mode):
                return subprocess.run(["bash", str(SCRIPT), mode], env=env, capture_output=True, text=True)
            self.assertEqual(call("install").returncode, 0)
            plist = home / "Library/LaunchAgents/com.yichen.boss.learnrise.event-monitor.plist"
            mode_file = state / "learnrise-monitor-mode"
            self.assertEqual(mode_file.read_text().strip(), "shadow")
            self.assertIn("--shadow", plist.read_text())
            checked = call("check")
            self.assertEqual(checked.returncode, 0, checked.stderr)
            mode_file.write_text("apply\n")
            self.assertNotEqual(call("check").returncode, 0)
            mode_file.write_text("shadow\n")
            original = plist.read_text()
            plist.write_text(original.replace("<integer>900</integer>", "<integer>901</integer>"))
            self.assertNotEqual(call("check").returncode, 0)
            plist.write_text(original)
            bad_plist.touch()
            self.assertNotEqual(call("install").returncode, 0)
            bad_plist.unlink()
            self.assertEqual(plist.read_text(), original)
            self.assertEqual(call("check").returncode, 0)
            fail_bootstrap.touch()
            self.assertNotEqual(call("install").returncode, 0)
            self.assertEqual(plist.read_text(), original)
            self.assertEqual(call("check").returncode, 0)
            self.assertNotEqual(call("activate").returncode, 0)
            now = datetime.now(timezone.utc).isoformat()
            monitor.atomic(state / "learnrise-monitor-receipt.json", {"as_of": now, "mode": "shadow"})
            monitor.atomic(state / "learnrise-monitor-snapshot.json",
                           {"as_of": now, "repository": monitor.REPO, "entities": {}})
            baseline = monitor.baseline_current({}, state)
            ready = {"version": 1, "as_of": now, "hub": HUB,
                     "coverage": {"covered_heartbeat_ids": [HUB],
                                  "paused_heartbeat_ids": [HUB],
                                  "objectives": ["#1"], "complete": True, "paused_at": now},
                     "drain": {"confirmed_at": now, "method": "observed_empty", "no_prior_turns": True},
                     "shadow": {"receipt_as_of": now,
                                "baseline_id": monitor.event_id("baseline", None, baseline, 1),
                                "reviewed": True},
                     "canaries": {"queue": True, "watchdog": True, "preview": True}}
            readiness = state / "learnrise-monitor-cutover-ready.json"
            invalid = [
                ("wrong_hub", lambda doc: doc.update(hub="01a0d565-c171-7120-b828-b04db384021e")),
                ("unpaused_coverage", lambda doc: doc["coverage"].update(paused_heartbeat_ids=[])),
                ("undrained", lambda doc: doc["drain"].update(no_prior_turns=False)),
                ("unreviewed_baseline", lambda doc: doc["shadow"].update(reviewed=False)),
                ("wrong_baseline", lambda doc: doc["shadow"].update(baseline_id="a" * 32)),
                ("failed_canary", lambda doc: doc["canaries"].update(watchdog=False)),
                ("stale", lambda doc: doc.update(as_of=(datetime.now(timezone.utc) - timedelta(hours=1)).isoformat())),
            ]
            for name, mutate in invalid:
                with self.subTest(name=name):
                    bad = copy.deepcopy(ready)
                    mutate(bad)
                    readiness.write_text(json.dumps(bad))
                    self.assertNotEqual(call("activate").returncode, 0)
                    self.assertEqual(plist.read_text(), original)
            readiness.write_text(json.dumps(ready))
            fail_bootstrap.touch()
            self.assertNotEqual(call("activate").returncode, 0)
            self.assertEqual(plist.read_text(), original)
            self.assertEqual(call("check").returncode, 0)
            self.assertEqual(call("activate").returncode, 0)
            self.assertEqual(mode_file.read_text().strip(), "apply")
            self.assertIn("--apply", plist.read_text())
            self.assertIn("<key>RunAtLoad</key><false/>", plist.read_text())
            self.assertEqual(call("check").returncode, 0)
            mode_file.write_text("shadow\n")
            self.assertNotEqual(call("check").returncode, 0)
            mode_file.write_text("apply\n")
            # Baseline queue committed, but the process died before deleting
            # its attempt marker. Exercise the documented rollback sequence.
            monitor.atomic(state / "learnrise-monitor-cursor.json",
                           {"version": 1, "initialized": True, "entities": {}, "pending": []})
            monitor.atomic(state / "learnrise-monitor-cutover-attempt.json",
                           {"version": 1, "started_at": now, "baseline_id": ready["shadow"]["baseline_id"],
                            "pid": 123})
            self.assertEqual(call("stop").returncode, 0)
            self.assertEqual(call("install").returncode, 0)
            refreshed = datetime.now(timezone.utc).isoformat()
            monitor.atomic(state / "learnrise-monitor-receipt.json", {"as_of": refreshed, "mode": "shadow"})
            monitor.atomic(state / "learnrise-monitor-snapshot.json",
                           {"as_of": refreshed, "repository": monitor.REPO, "entities": {}})
            ready["as_of"] = refreshed
            ready["coverage"]["paused_at"] = refreshed
            ready["drain"]["confirmed_at"] = refreshed
            ready["shadow"]["receipt_as_of"] = refreshed
            readiness.write_text(json.dumps(ready))
            self.assertEqual(call("activate").returncode, 0)
            ledger = {"schema_version": 1, "repository": monitor.REPO,
                      "last_checked_utc": refreshed, "dependency_gates": [],
                      "objectives": [], "open_pull_requests": []}
            supervisor = {"schema_version": 1, "repository": monitor.REPO,
                          "last_scan_utc": refreshed, "actions": {}}
            monitor.atomic(state / "boss-observed-ledger.json", ledger)
            monitor.atomic(state / "boss-action-outbox.json", {"version": 1, "actions": {}})
            monitor.atomic(state / "pr-supervisor-state.json", supervisor)
            report = {"actions": [], "overdue": [], "inventory": {"as_of": refreshed, "tasks": []}}
            queued = []
            class Result:
                def __init__(self, code=0, stdout=""):
                    self.returncode, self.stdout = code, stdout
            def runner(argv, timeout=120):
                if "queue" in argv:
                    queued.append(argv)
                    return Result()
                return Result(3, json.dumps(report))
            with patch.object(monitor, "bounded_run", side_effect=runner):
                self.assertEqual(monitor.main(["--apply", "--state-dir", str(state),
                                               "--observed", str(state / "boss-observed-ledger.json"),
                                               "--outbox", str(state / "boss-action-outbox.json"),
                                               "--supervisor", str(state / "pr-supervisor-state.json")]), 0)
            self.assertEqual(queued, [])
            self.assertFalse((state / "learnrise-monitor-cutover-attempt.json").exists())
            pending_cursor = state / "learnrise-monitor-cursor.json"
            rearm_marker = state / "learnrise-monitor-rearm-required.json"
            pending_cursor.write_text('{"pending":[{"id":"original"}]}')
            rearm_marker.write_text('{"baseline_id":"original"}')
            self.assertEqual(call("stop").returncode, 0)
            self.assertFalse(plist.exists())
            self.assertFalse(mode_file.exists())
            self.assertEqual(pending_cursor.read_text(), '{"pending":[{"id":"original"}]}')
            self.assertEqual(rearm_marker.read_text(), '{"baseline_id":"original"}')
            self.assertEqual(unrelated.read_text(), "leave me alone")


if __name__ == "__main__":
    unittest.main()
