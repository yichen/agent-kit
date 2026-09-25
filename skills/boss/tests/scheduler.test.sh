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
CALL_LOG="$TMP/calls" PATH="$TMP/bin:$PATH" AGENT_KIT_USER_HOME="$TMP/home" AGENT_KIT_PRIMARY_SKILL_PATH="$TMP/skill" "$INSTALLER" stop >/dev/null
if rg -q 'bootstrap' "$TMP/calls"; then
  echo "rollback unexpectedly bootstrapped a service" >&2
  exit 1
fi
rg -q 'bootout gui/.*/com.yichen.boss.agent-kit.scheduler' "$TMP/calls"
rg -q 'bootout gui/.*/com.yichen.boss.agent-kit.watchdog' "$TMP/calls"
: >"$TMP/calls"
CALL_LOG="$TMP/calls" PATH="$TMP/bin:$PATH" AGENT_KIT_USER_HOME="$TMP/home" AGENT_KIT_PRIMARY_SKILL_PATH="$TMP/skill" "$INSTALLER" install >/dev/null
rg -q 'StartInterval</key><integer>900</integer>' "$TMP/home/Library/LaunchAgents/com.yichen.boss.agent-kit.scheduler.plist"
rg -q 'StartInterval</key><integer>60</integer>' "$TMP/home/Library/LaunchAgents/com.yichen.boss.agent-kit.watchdog.plist"
rg -q 'bootstrap gui/.*/com.yichen.boss.agent-kit.scheduler.plist' "$TMP/calls"
rg -q 'bootstrap gui/.*/com.yichen.boss.agent-kit.watchdog.plist' "$TMP/calls"
echo "boss scheduler installer canaries passed"
