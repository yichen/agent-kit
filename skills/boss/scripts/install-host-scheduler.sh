#!/usr/bin/env bash
# Install the read-only shadow scheduler and independent freshness watchdog.
set -euo pipefail

MODE="${1:-}"
USER_HOME="${AGENT_KIT_USER_HOME:-$HOME}"
SKILL_PATH="${AGENT_KIT_PRIMARY_SKILL_PATH:-$USER_HOME/.agents/skills/boss}"
LABEL="com.yichen.boss.agent-kit.scheduler"
WATCHDOG_LABEL="com.yichen.boss.agent-kit.watchdog"
AGENTS_DIR="$USER_HOME/Library/LaunchAgents"
STATE_DIR="$USER_HOME/agents-artifacts/boss"
SCHEDULER_PLIST="$AGENTS_DIR/$LABEL.plist"
WATCHDOG_PLIST="$AGENTS_DIR/$WATCHDOG_LABEL.plist"
LAST_SUCCESS="$STATE_DIR/agent-kit-last-success.json"
STDOUT="$STATE_DIR/agent-kit-scheduler.stdout.log"
STDERR="$STATE_DIR/agent-kit-scheduler.stderr.log"
WATCHDOG_STDOUT="$STATE_DIR/agent-kit-watchdog.stdout.log"
WATCHDOG_STDERR="$STATE_DIR/agent-kit-watchdog.stderr.log"
UID_NUM="$(id -u)"

case "$MODE" in
  check|install|stop) ;;
  *) echo "usage: $0 check|install|stop" >&2; exit 1 ;;
esac

safe_absolute_path() {
  local candidate="$1"
  [[ "$candidate" = /* ]] || return 1
  case "$candidate" in *[!A-Za-z0-9_./-]*) return 1 ;; esac
  case "/$candidate/" in */../*|*//../*|*///../*) return 1 ;; esac
  return 0
}

if ! safe_absolute_path "$USER_HOME" || ! safe_absolute_path "$SKILL_PATH"; then
  echo "boss scheduler: home and skill paths must be absolute and contain only letters, digits, slash, dot, underscore, or hyphen" >&2
  exit 2
fi

if [ "$MODE" != "stop" ] && { [ ! -x "$SKILL_PATH/scripts/scheduler.py" ] || [ ! -x "$SKILL_PATH/scripts/watchdog.py" ]; }; then
  echo "boss scheduler: installed skill scripts are missing at $SKILL_PATH" >&2
  exit 2
fi

loaded() {
  launchctl print "gui/$UID_NUM/$1" >/dev/null 2>&1
}

bootout_if_loaded() {
  local label="$1"
  if loaded "$label"; then
    launchctl bootout "gui/$UID_NUM/$label"
  fi
}

if [ "$MODE" = "check" ]; then
  [ -f "$SCHEDULER_PLIST" ] && [ -f "$WATCHDOG_PLIST" ] || {
    echo "boss scheduler: shadow scheduler/watchdog plists are not installed" >&2
    exit 2
  }
  plutil -lint "$SCHEDULER_PLIST" "$WATCHDOG_PLIST"
  loaded "$LABEL" && loaded "$WATCHDOG_LABEL" || {
    echo "boss scheduler: one or more shadow services are not loaded" >&2
    exit 2
  }
  echo "boss scheduler: shadow services are loaded; assignment remains disabled"
  exit 0
fi

if [ "$MODE" = "stop" ]; then
  # Rollback only stops these shadow readers. It never bootstraps or revives
  # another launcher, so rollback cannot create a second writer.
  bootout_if_loaded "$LABEL"
  bootout_if_loaded "$WATCHDOG_LABEL"
  rm -f "$SCHEDULER_PLIST" "$WATCHDOG_PLIST"
  echo "boss scheduler: shadow services stopped; no assignment writer was enabled"
  exit 0
fi

mkdir -p "$AGENTS_DIR" "$STATE_DIR"
bootout_if_loaded "$LABEL"
bootout_if_loaded "$WATCHDOG_LABEL"

cat >"$SCHEDULER_PLIST" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>$LABEL</string>
  <key>ProgramArguments</key><array>
    <string>/usr/bin/python3</string>
    <string>$SKILL_PATH/scripts/scheduler.py</string>
    <string>--repo</string><string>$USER_HOME/work/agent-kit</string>
    <string>--operations-db</string><string>$STATE_DIR/operations.sqlite3</string>
    <string>--last-success</string><string>$LAST_SUCCESS</string>
    <string>--write-last-success</string>
  </array>
  <key>StartInterval</key><integer>900</integer>
  <key>RunAtLoad</key><true/>
  <key>StandardOutPath</key><string>$STDOUT</string>
  <key>StandardErrorPath</key><string>$STDERR</string>
</dict></plist>
EOF

cat >"$WATCHDOG_PLIST" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>$WATCHDOG_LABEL</string>
  <key>ProgramArguments</key><array>
    <string>/usr/bin/python3</string>
    <string>$SKILL_PATH/scripts/watchdog.py</string>
    <string>--last-success</string><string>$LAST_SUCCESS</string>
    <string>--max-age-minutes</string><string>32</string>
  </array>
  <key>StartInterval</key><integer>60</integer>
  <key>RunAtLoad</key><true/>
  <key>StandardOutPath</key><string>$WATCHDOG_STDOUT</string>
  <key>StandardErrorPath</key><string>$WATCHDOG_STDERR</string>
</dict></plist>
EOF

plutil -lint "$SCHEDULER_PLIST" "$WATCHDOG_PLIST"
launchctl bootstrap "gui/$UID_NUM" "$SCHEDULER_PLIST"
launchctl bootstrap "gui/$UID_NUM" "$WATCHDOG_PLIST"
echo "boss scheduler: one 15-minute shadow scheduler and one 60-second watchdog loaded; assignment remains disabled"
