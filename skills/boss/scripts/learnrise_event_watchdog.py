#!/usr/bin/env python3
"""Wake the LearnRise hub once per monitor outage, retrying the same alert."""
import argparse
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
from datetime import datetime, timedelta, timezone

from learnrise_event_monitor import ARTIFACTS, MonitorError, UUID, atomic, attempt_as_rearm_marker, instant, read_json

UTC = timezone.utc


def check(state_dir, hub, max_age=35, now=None, queue=None, notify=None):
    now = now or datetime.now(UTC)
    if not UUID.fullmatch(hub):
        raise ValueError("invalid hub UUID")
    state_dir.mkdir(parents=True, exist_ok=True)
    with (state_dir / "learnrise-watchdog.lock").open("a+") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        receipt_invalid = False
        try:
            receipt = read_json(state_dir / "learnrise-monitor-receipt.json", optional=True)
        except (MonitorError, OSError, ValueError):
            receipt, receipt_invalid = None, True
        mode_error = None
        expected_mode = None
        mode_path = state_dir / "learnrise-monitor-mode"
        try:
            if not mode_path.exists():
                mode_error = "monitor activation mode missing"
            elif mode_path.stat().st_size > 16:
                mode_error = "monitor activation mode malformed"
            else:
                expected_mode = mode_path.read_text().strip()
                if expected_mode not in {"shadow", "apply"}:
                    mode_error = "monitor activation mode malformed"
        except OSError:
            mode_error = "monitor activation mode unreadable"
        fault_invalid = False
        try:
            monitor_fault = read_json(state_dir / "learnrise-monitor-fault.json", optional=True)
        except (MonitorError, OSError, ValueError):
            monitor_fault, fault_invalid = None, True
        alert_path = state_dir / "learnrise-watchdog-alert.json"
        alert_invalid = False
        try:
            alert = read_json(alert_path, optional=True)
            if alert is not None and (not re.fullmatch(r"[0-9a-f]{32}", str(alert.get("event_id", ""))) or type(alert.get("delivered")) is not bool or not isinstance(alert.get("reason"), str)):
                raise MonitorError("invalid watchdog alert")
        except (MonitorError, OSError, ValueError):
            alert, alert_invalid = None, True
        reason = "monitor receipt malformed" if receipt_invalid else None
        if receipt is None and not receipt_invalid:
            installed = state_dir / "learnrise-monitor-installed"
            if not installed.exists() or now.timestamp() - installed.stat().st_mtime > max_age * 60:
                reason = "monitor receipt absent"
        elif receipt is not None:
            try:
                age = now - instant(receipt.get("as_of"), "monitor receipt")
                if age > timedelta(minutes=max_age) or age < -timedelta(minutes=2):
                    reason = "monitor receipt stale"
            except Exception:
                reason = "monitor receipt malformed"
        if monitor_fault or fault_invalid:
            reason = "monitor scan fault"
        if (state_dir / "learnrise-monitor-rearm-required.json").exists():
            reason = "monitor cutover rearm required"
        active_attempt = False
        attempt_path = state_dir / "learnrise-monitor-cutover-attempt.json"
        if attempt_path.exists():
            try:
                attempt = read_json(attempt_path)
                attempt_as_rearm_marker(attempt)
                age = now - instant(attempt["started_at"], "cutover attempt")
                active = False
                lock_path = state_dir / "learnrise-monitor.lock"
                with lock_path.open("r") as monitor_lock:
                    try:
                        fcntl.flock(monitor_lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    except BlockingIOError:
                        active = True
                    else:
                        fcntl.flock(monitor_lock, fcntl.LOCK_UN)
                if (active and timedelta(0) <= age <= timedelta(minutes=5) and
                        reason in (None, "monitor receipt absent", "monitor receipt stale")):
                    # Apply has not emitted its first receipt yet. The held
                    # monitor lock proves this is still the same live scan.
                    reason = None
                    mode_error = None
                    active_attempt = True
                elif not (state_dir / "learnrise-monitor-rearm-required.json").exists():
                    reason = "monitor cutover attempt abandoned"
            except (MonitorError, OSError, ValueError, TypeError, KeyError, RecursionError):
                reason = "monitor cutover attempt malformed"
        if alert_invalid:
            reason = "watchdog alert state malformed"
        if reason is None and mode_error:
            reason = mode_error
        if reason is None and receipt is not None and receipt.get("mode") != expected_mode and not active_attempt:
            reason = "monitor receipt mode mismatch"
        if reason is None:
            if alert_path.exists():
                alert_path.unlink()
            print(json.dumps({"healthy": True}))
            return 0
        if alert is None:
            event_id = hashlib.sha256(f"yichen/LearnRise|monitor_outage|{now.isoformat()}|{reason}".encode()).hexdigest()[:32]
            alert = {"event_id": event_id, "reason": reason, "created_at": now.isoformat(), "delivered": False}
            atomic(alert_path, alert)
        if not alert["delivered"]:
            message = json.dumps({"kind": "learnrise_monitor_fault", "event_id": alert["event_id"], "reason": alert["reason"], "instruction": "Deduplicate this event_id before acting. Inspect monitor status and live repository state."}, separators=(",", ":"))
            queue = queue or (lambda argv: subprocess.run(argv, capture_output=True, timeout=30, check=False))
            try:
                result = queue(["codex", "queue", "--thread", hub, "--message", message])
                if result.returncode:
                    raise RuntimeError("queue failed")
                alert["delivered"] = True
                atomic(alert_path, alert)
            except (RuntimeError, subprocess.TimeoutExpired, OSError):
                notify = notify or (lambda argv: subprocess.run(argv, capture_output=True, timeout=10, check=False))
                notify(["osascript", "-e", 'display notification "LearnRise monitor fault; hub queue pending" with title "LearnRise monitor"'])
                print(json.dumps({"healthy": False, "pending": True, "event_id": alert["event_id"]}))
                return 2
        print(json.dumps({"healthy": False, "pending": False, "event_id": alert["event_id"]}))
        return 2


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--state-dir", type=Path, default=ARTIFACTS)
    ap.add_argument("--hub", default="01a0d565-c171-7120-b828-b04db384021f")
    ap.add_argument("--max-age-minutes", type=int, default=35)
    args = ap.parse_args(argv)
    if not 1 <= args.max_age_minutes <= 120:
        ap.error("invalid max age")
    try:
        return check(args.state_dir, args.hub, args.max_age_minutes)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"learnrise watchdog: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
