#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/../../.." && pwd)"
PYTHONDONTWRITEBYTECODE=1 python3 "$ROOT/skills/boss/tests/scheduler.test.py"

INSTALLER="$ROOT/skills/boss/scripts/install-host-scheduler.sh"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT
mkdir -p "$TMP/home/work" "$TMP/home/Library/LaunchAgents" "$TMP/bin" "$TMP/skill/scripts"
ln -s "$ROOT/skills/boss/scripts/scheduler.py" "$TMP/skill/scripts/scheduler.py"
ln -s "$ROOT/skills/boss/scripts/watchdog.py" "$TMP/skill/scripts/watchdog.py"
cat >"$TMP/bin/launchctl" <<'SH'
#!/bin/sh
echo "$*" >>"$CALL_LOG"
case "$1" in
  print) exit 0 ;;
  *) exit 0 ;;
esac
SH
cat >"$TMP/bin/plutil" <<'SH'
#!/bin/sh
exit 0
SH
chmod +x "$TMP/bin/launchctl" "$TMP/bin/plutil"
mkdir -p "$TMP/gh&bin"
cat >"$TMP/gh&bin/gh" <<'SH'
#!/bin/sh
exit 0
SH
chmod +x "$TMP/gh&bin/gh"
CALL_LOG="$TMP/calls" PATH="$TMP/bin:$PATH" AGENT_KIT_USER_HOME="$TMP/home" AGENT_KIT_PRIMARY_SKILL_PATH="$TMP/skill" "$INSTALLER" stop >/dev/null
if grep -Eq 'bootstrap' "$TMP/calls"; then
  echo "rollback unexpectedly bootstrapped a service" >&2
  exit 1
fi
grep -Eq 'bootout gui/.*/com.yichen.boss.agent-kit.scheduler' "$TMP/calls"
grep -Eq 'bootout gui/.*/com.yichen.boss.agent-kit.watchdog' "$TMP/calls"
: >"$TMP/calls"
invalid_paths=("$TMP/home&evil" "$TMP/home<evil" "$TMP/home\"evil" "$TMP/home;touch" "$TMP/home/../outside")
for invalid in "${invalid_paths[@]}"; do
  if CALL_LOG="$TMP/calls" PATH="$TMP/bin:$PATH" AGENT_KIT_USER_HOME="$invalid" AGENT_KIT_PRIMARY_SKILL_PATH="$TMP/skill" "$INSTALLER" install >/dev/null 2>&1; then
    echo "unsafe home path was accepted: $invalid" >&2
    exit 1
  fi
  if [ -e "$invalid/Library/LaunchAgents" ] || [ -s "$TMP/calls" ]; then
    echo "unsafe home path caused a write or launchctl call: $invalid" >&2
    exit 1
  fi
done
invalid_paths=("$TMP/skill&evil" "$TMP/skill<evil" "$TMP/skill\"evil" "$TMP/skill;touch")
for invalid in "${invalid_paths[@]}"; do
  if CALL_LOG="$TMP/calls" PATH="$TMP/bin:$PATH" AGENT_KIT_USER_HOME="$TMP/home" AGENT_KIT_PRIMARY_SKILL_PATH="$invalid" "$INSTALLER" install >/dev/null 2>&1; then
    echo "unsafe skill path was accepted: $invalid" >&2
    exit 1
  fi
  if [ -s "$TMP/calls" ] || [ -e "$TMP/injected" ]; then
    echo "unsafe skill path caused a write or launchctl call: $invalid" >&2
    exit 1
  fi
  if [ -e "$TMP/home/Library/LaunchAgents/com.yichen.boss.agent-kit.scheduler.plist" ] || [ -e "$TMP/home/Library/LaunchAgents/com.yichen.boss.agent-kit.watchdog.plist" ]; then
    echo "unsafe skill path wrote a plist: $invalid" >&2
    exit 1
  fi
done
: >"$TMP/calls"
bad_gh_cases=(missing relative)
for bad_gh in "${bad_gh_cases[@]}"; do
  bad_home="$TMP/bad-gh-$bad_gh"
  case "$bad_gh" in
    missing)
      bad_path="$TMP/bin:/usr/bin:/bin:/usr/sbin:/sbin"
      bad_cwd="$ROOT"
      expected_error="cannot find the gh executable"
      ;;
    relative)
      mkdir -p "$TMP/relative-gh"
      cp "$TMP/gh&bin/gh" "$TMP/relative-gh/gh"
      bad_path="relative-gh:$TMP/bin:/usr/bin:/bin:/usr/sbin:/sbin"
      bad_cwd="$TMP"
      expected_error="invalid or non-executable path"
      ;;
  esac
  : >"$TMP/calls"
  if (cd "$bad_cwd" && CALL_LOG="$TMP/calls" PATH="$bad_path" AGENT_KIT_USER_HOME="$bad_home" AGENT_KIT_PRIMARY_SKILL_PATH="$TMP/skill" "$INSTALLER" install >"$TMP/bad-gh-output" 2>&1); then
    echo "installer accepted $bad_gh gh path" >&2
    exit 1
  fi
  grep -Fq "$expected_error" "$TMP/bad-gh-output"
  if [ -e "$bad_home/Library/LaunchAgents" ] || [ -s "$TMP/calls" ]; then
    echo "invalid $bad_gh gh path wrote launchd files or called launchctl" >&2
    exit 1
  fi
done
: >"$TMP/calls"
CALL_LOG="$TMP/calls" PATH="$TMP/gh&bin:$TMP/bin:/usr/bin:/bin:/usr/sbin:/sbin" AGENT_KIT_USER_HOME="$TMP/home" AGENT_KIT_PRIMARY_SKILL_PATH="$TMP/skill" "$INSTALLER" install >/dev/null
grep -Eq 'StartInterval</key><integer>900</integer>' "$TMP/home/Library/LaunchAgents/com.yichen.boss.agent-kit.scheduler.plist"
grep -Eq 'StartInterval</key><integer>60</integer>' "$TMP/home/Library/LaunchAgents/com.yichen.boss.agent-kit.watchdog.plist"
grep -Eq 'bootstrap gui/.*/com.yichen.boss.agent-kit.scheduler.plist' "$TMP/calls"
grep -Eq 'bootstrap gui/.*/com.yichen.boss.agent-kit.watchdog.plist' "$TMP/calls"
grep -Fq -- '--write-last-success' "$TMP/home/Library/LaunchAgents/com.yichen.boss.agent-kit.scheduler.plist"
grep -Fq "$TMP/gh&amp;bin" "$TMP/home/Library/LaunchAgents/com.yichen.boss.agent-kit.scheduler.plist"
python3 - "$TMP/home/Library/LaunchAgents/com.yichen.boss.agent-kit.scheduler.plist" "$TMP/gh&bin" <<'PY'
import plistlib
import sys

with open(sys.argv[1], "rb") as stream:
    scheduler = plistlib.load(stream)
assert scheduler["EnvironmentVariables"]["PATH"] == f"/usr/bin:/bin:/usr/sbin:/sbin:{sys.argv[2]}"
assert scheduler["EnvironmentVariables"]["PATH"].count(":") == 4
PY
echo "boss scheduler installer canaries passed"
