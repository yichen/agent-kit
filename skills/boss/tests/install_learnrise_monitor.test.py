import os
from pathlib import Path
import subprocess
import tempfile
import unittest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "install-learnrise-monitor.sh"


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
            env = {**os.environ, "PATH": str(bin_dir) + ":" + os.environ["PATH"], "FAKE_LOADED": str(loaded),
                   "FAKE_FAIL_BOOTSTRAP": str(fail_bootstrap), "FAKE_BAD_PLIST": str(bad_plist),
                   "LEARNRISE_MONITOR_HOME": str(home), "LEARNRISE_MONITOR_SKILL_PATH": str(skill),
                   "LEARNRISE_MONITOR_STATE_DIR": str(state)}
            def call(mode):
                return subprocess.run(["bash", str(SCRIPT), mode], env=env, capture_output=True, text=True)
            self.assertEqual(call("install").returncode, 0)
            plist = home / "Library/LaunchAgents/com.yichen.boss.learnrise.event-monitor.plist"
            self.assertIn("--shadow", plist.read_text())
            self.assertEqual(call("check").returncode, 0)
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
            fail_bootstrap.touch()
            self.assertNotEqual(call("activate").returncode, 0)
            self.assertEqual(plist.read_text(), original)
            self.assertEqual(call("check").returncode, 0)
            self.assertEqual(call("activate").returncode, 0)
            self.assertIn("--apply", plist.read_text())
            self.assertEqual(call("check").returncode, 0)
            self.assertEqual(call("stop").returncode, 0)
            self.assertFalse(plist.exists())
            self.assertEqual(unrelated.read_text(), "leave me alone")


if __name__ == "__main__":
    unittest.main()
