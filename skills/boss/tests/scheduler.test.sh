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
mkdir -p "$TMP/codex<bin"
cat >"$TMP/codex<bin/codex" <<'SH'
#!/bin/sh
exit 0
SH
chmod +x "$TMP/codex<bin/codex"
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
tool_cases=(gh codex)
path_failure_cases=(missing relative non_executable)
for tool in "${tool_cases[@]}"; do
  for path_failure in "${path_failure_cases[@]}"; do
    bad_home="$TMP/bad-$tool-$path_failure"
    case "$tool:$path_failure" in
      gh:missing)
        bad_path="$TMP/codex<bin:$TMP/bin:/usr/bin:/bin:/usr/sbin:/sbin"
        bad_cwd="$ROOT"
        expected_error="cannot find the gh executable"
        ;;
      codex:missing)
        bad_path="$TMP/gh&bin:$TMP/bin:/usr/bin:/bin:/usr/sbin:/sbin"
        bad_cwd="$ROOT"
        expected_error="cannot find the codex executable"
        ;;
      gh:relative)
        mkdir -p "$TMP/relative-gh"
        cp "$TMP/gh&bin/gh" "$TMP/relative-gh/gh"
        bad_path="relative-gh:$TMP/codex<bin:$TMP/bin:/usr/bin:/bin:/usr/sbin:/sbin"
        bad_cwd="$TMP"
        expected_error="gh resolved to an invalid or non-executable path"
        ;;
      codex:relative)
        mkdir -p "$TMP/relative-codex"
        cp "$TMP/codex<bin/codex" "$TMP/relative-codex/codex"
        bad_path="relative-codex:$TMP/gh&bin:$TMP/bin:/usr/bin:/bin:/usr/sbin:/sbin"
        bad_cwd="$TMP"
        expected_error="codex resolved to an invalid or non-executable path"
        ;;
      gh:non_executable)
        mkdir -p "$TMP/nonexec-gh"
        printf '#!/bin/sh\nexit 0\n' >"$TMP/nonexec-gh/gh"
        chmod -x "$TMP/nonexec-gh/gh"
        bad_path="$TMP/nonexec-gh:$TMP/codex<bin:$TMP/bin:/usr/bin:/bin:/usr/sbin:/sbin"
        bad_cwd="$ROOT"
        expected_error="gh resolved to an invalid or non-executable path"
        ;;
      codex:non_executable)
        mkdir -p "$TMP/nonexec-codex"
        printf '#!/bin/sh\nexit 0\n' >"$TMP/nonexec-codex/codex"
        chmod -x "$TMP/nonexec-codex/codex"
        bad_path="$TMP/nonexec-codex:$TMP/gh&bin:$TMP/bin:/usr/bin:/bin:/usr/sbin:/sbin"
        bad_cwd="$ROOT"
        expected_error="codex resolved to an invalid or non-executable path"
        ;;
    esac
    : >"$TMP/calls"
    if (cd "$bad_cwd" && CALL_LOG="$TMP/calls" PATH="$bad_path" AGENT_KIT_USER_HOME="$bad_home" AGENT_KIT_PRIMARY_SKILL_PATH="$TMP/skill" "$INSTALLER" install >"$TMP/bad-tool-output" 2>&1); then
      echo "installer accepted $path_failure $tool path" >&2
      exit 1
    fi
    grep -Fq "$expected_error" "$TMP/bad-tool-output"
    if [ -e "$bad_home/Library/LaunchAgents" ] || [ -s "$TMP/calls" ]; then
      echo "invalid $path_failure $tool path wrote launchd files or called launchctl" >&2
      exit 1
    fi
  done
done
: >"$TMP/calls"
CALL_LOG="$TMP/calls" PATH="$TMP/gh&bin:$TMP/codex<bin:$TMP/bin:/usr/bin:/bin:/usr/sbin:/sbin" AGENT_KIT_USER_HOME="$TMP/home" AGENT_KIT_PRIMARY_SKILL_PATH="$TMP/skill" "$INSTALLER" install >/dev/null
grep -Eq 'StartInterval</key><integer>900</integer>' "$TMP/home/Library/LaunchAgents/com.yichen.boss.agent-kit.scheduler.plist"
grep -Eq 'StartInterval</key><integer>60</integer>' "$TMP/home/Library/LaunchAgents/com.yichen.boss.agent-kit.watchdog.plist"
grep -Eq 'bootstrap gui/.*/com.yichen.boss.agent-kit.scheduler.plist' "$TMP/calls"
grep -Eq 'bootstrap gui/.*/com.yichen.boss.agent-kit.watchdog.plist' "$TMP/calls"
grep -Fq -- '--write-last-success' "$TMP/home/Library/LaunchAgents/com.yichen.boss.agent-kit.scheduler.plist"
grep -Fq "$TMP/gh&amp;bin" "$TMP/home/Library/LaunchAgents/com.yichen.boss.agent-kit.scheduler.plist"
grep -Fq "$TMP/codex&lt;bin" "$TMP/home/Library/LaunchAgents/com.yichen.boss.agent-kit.scheduler.plist"
python3 - "$TMP/home/Library/LaunchAgents/com.yichen.boss.agent-kit.scheduler.plist" "$TMP/gh&bin" "$TMP/codex<bin" <<'PY'
import plistlib
import sys

with open(sys.argv[1], "rb") as stream:
    scheduler = plistlib.load(stream)
assert scheduler["EnvironmentVariables"]["PATH"] == f"/usr/bin:/bin:/usr/sbin:/sbin:{sys.argv[2]}:{sys.argv[3]}"
assert scheduler["EnvironmentVariables"]["PATH"].count(":") == 5
PY
echo "boss scheduler installer canaries passed"
