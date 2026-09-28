#!/usr/bin/env bash
# Install independent LearnRise event monitor and watchdog LaunchAgents.
set -euo pipefail

MODE="${1:-}"
case "$MODE" in check|install|activate|stop) ;; *) echo "usage: $0 check|install|activate|stop" >&2; exit 1 ;; esac
USER_HOME="${LEARNRISE_MONITOR_HOME:-$HOME}"
SKILL_PATH="${LEARNRISE_MONITOR_SKILL_PATH:-$USER_HOME/.agents/skills/boss}"
STATE_DIR="${LEARNRISE_MONITOR_STATE_DIR:-$USER_HOME/agents-artifacts/learnrise-orchestrator}"
HUB="${LEARNRISE_MONITOR_HUB:-01a0d565-c171-7120-b828-b04db384021f}"
LABEL="com.yichen.boss.learnrise.event-monitor"
WATCH_LABEL="com.yichen.boss.learnrise.event-watchdog"
AGENTS_DIR="$USER_HOME/Library/LaunchAgents"
PLIST="$AGENTS_DIR/$LABEL.plist"
WATCH_PLIST="$AGENTS_DIR/$WATCH_LABEL.plist"
MODE_FILE="$STATE_DIR/learnrise-monitor-mode"
UID_NUM="$(id -u)"

safe_path() {
  [[ "$1" = /* && "$1" != *'..'* && "$1" != *[!A-Za-z0-9_./-]* ]]
}
for path in "$USER_HOME" "$SKILL_PATH" "$STATE_DIR"; do
  safe_path "$path" || { echo "invalid absolute path" >&2; exit 2; }
done
[[ "$HUB" =~ ^[0-9a-f]{8}(-[0-9a-f]{4}){3}-[0-9a-f]{12}$ ]] || { echo "invalid hub UUID" >&2; exit 2; }
loaded() { launchctl print "gui/$UID_NUM/$1" >/dev/null 2>&1; }
bootout() { if loaded "$1"; then launchctl bootout "gui/$UID_NUM/$1"; fi; }

if [ "$MODE" = stop ]; then
  bootout "$LABEL"
  bootout "$WATCH_LABEL"
  rm -f "$PLIST" "$WATCH_PLIST" "$MODE_FILE"
  echo "LearnRise event monitor jobs stopped"
  exit 0
fi

for script in learnrise_event_monitor.py learnrise_event_watchdog.py runtime_bridge.py; do
  [ -f "$SKILL_PATH/scripts/$script" ] || { echo "missing $script" >&2; exit 2; }
done
for source in ownership.json audit.py pr-supervisor-state.json; do
  [ -f "$STATE_DIR/$source" ] || { echo "missing $source" >&2; exit 2; }
done
command -v codex >/dev/null && command -v gh >/dev/null && command -v plutil >/dev/null && command -v launchctl >/dev/null || { echo "missing host executable" >&2; exit 2; }

if [ "$MODE" = check ]; then
  [ -f "$PLIST" ] && [ -f "$WATCH_PLIST" ] || { echo "monitor plists missing" >&2; exit 2; }
  [ -f "$MODE_FILE" ] || { echo "monitor activation mode missing" >&2; exit 2; }
  EXPECTED_MODE="$(cat "$MODE_FILE")"
  case "$EXPECTED_MODE" in shadow|apply) ;; *) echo "invalid monitor activation mode" >&2; exit 2 ;; esac
  plutil -lint "$PLIST" "$WATCH_PLIST"
  loaded "$LABEL" && loaded "$WATCH_LABEL" || { echo "monitor jobs not loaded" >&2; exit 2; }
  for plist in "$PLIST" "$WATCH_PLIST"; do
    /usr/bin/grep -Fq "<string>$HUB</string>" "$plist" || { echo "hub mismatch" >&2; exit 2; }
    /usr/bin/grep -Fq "<string>$STATE_DIR</string>" "$plist" || { echo "state path mismatch" >&2; exit 2; }
  done
  /usr/bin/grep -Fq "<string>$SKILL_PATH/scripts/learnrise_event_monitor.py</string>" "$PLIST" || { echo "monitor executable mismatch" >&2; exit 2; }
  /usr/bin/grep -Fq "<string>$SKILL_PATH/scripts/learnrise_event_watchdog.py</string>" "$WATCH_PLIST" || { echo "watchdog executable mismatch" >&2; exit 2; }
  /usr/bin/grep -Eq '<string>--(shadow|apply)</string>' "$PLIST" && /usr/bin/grep -Fq "<string>--$EXPECTED_MODE</string>" "$PLIST" || { echo "monitor activation mode mismatch" >&2; exit 2; }
  /usr/bin/grep -Fq '<integer>900</integer>' "$PLIST" && /usr/bin/grep -Fq '<integer>60</integer>' "$WATCH_PLIST" || { echo "monitor interval mismatch" >&2; exit 2; }
  echo "LearnRise monitor and watchdog loaded"
  exit 0
fi

if [ "$MODE" = activate ]; then
  [ -f "$PLIST" ] && [ -f "$WATCH_PLIST" ] || { echo "install shadow first" >&2; exit 2; }
  bash "$0" check >/dev/null
  /usr/bin/grep -Fq '<string>--shadow</string>' "$PLIST" || { echo "monitor is not in shadow mode" >&2; exit 2; }
  CANDIDATE="$(mktemp "$AGENTS_DIR/.learnrise-monitor-activate.XXXXXX")"
  BACKUP="$(mktemp "$AGENTS_DIR/.learnrise-monitor-backup.XXXXXX")"
  MODE_CANDIDATE="$(mktemp "$STATE_DIR/.learnrise-mode.XXXXXX")"
  printf 'apply\n' > "$MODE_CANDIDATE"
  cp "$PLIST" "$BACKUP"
  sed 's/<string>--shadow<\/string>/<string>--apply<\/string>/' "$PLIST" > "$CANDIDATE"
  if ! plutil -lint "$CANDIDATE"; then
    rm -f "$CANDIDATE" "$BACKUP" "$MODE_CANDIDATE"
    exit 2
  fi
  rollback_activate() {
    bootout "$LABEL" || true
    cp "$BACKUP" "$PLIST"
    launchctl bootstrap "gui/$UID_NUM" "$PLIST" || true
    rm -f "$CANDIDATE" "$BACKUP" "$MODE_CANDIDATE"
  }
  if ! bootout "$LABEL" || ! cp "$CANDIDATE" "$PLIST" || ! launchctl bootstrap "gui/$UID_NUM" "$PLIST" || ! mv "$MODE_CANDIDATE" "$MODE_FILE"; then
    rollback_activate
    echo "activation failed; shadow monitor restored" >&2
    exit 2
  fi
  rm -f "$CANDIDATE" "$BACKUP" "$MODE_CANDIDATE"
  echo "LearnRise monitor activated"
  exit 0
fi

CODEX_BIN="$(command -v codex)"
GH_BIN="$(command -v gh)"
for path in "$CODEX_BIN" "$GH_BIN"; do
  safe_path "$path" && [ -x "$path" ] || { echo "invalid executable" >&2; exit 2; }
done
LAUNCH_PATH="/usr/bin:/bin:/usr/sbin:/sbin:${CODEX_BIN%/*}:${GH_BIN%/*}"
mkdir -p "$AGENTS_DIR" "$STATE_DIR"
MODE_CANDIDATE="$(mktemp "$STATE_DIR/.learnrise-mode.XXXXXX")"
printf 'shadow\n' > "$MODE_CANDIDATE"
NEW_PLIST="$(mktemp "$AGENTS_DIR/.learnrise-monitor-new.XXXXXX")"
NEW_WATCH="$(mktemp "$AGENTS_DIR/.learnrise-watchdog-new.XXXXXX")"
cat > "$NEW_PLIST" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
<key>Label</key><string>$LABEL</string>
<key>ProgramArguments</key><array><string>/usr/bin/python3</string><string>$SKILL_PATH/scripts/learnrise_event_monitor.py</string><string>--shadow</string><string>--state-dir</string><string>$STATE_DIR</string><string>--hub</string><string>$HUB</string></array>
<key>EnvironmentVariables</key><dict><key>PATH</key><string>$LAUNCH_PATH</string><key>PYTHONDONTWRITEBYTECODE</key><string>1</string></dict>
<key>StartInterval</key><integer>900</integer><key>RunAtLoad</key><true/>
<key>StandardOutPath</key><string>$STATE_DIR/learnrise-monitor.stdout.log</string><key>StandardErrorPath</key><string>$STATE_DIR/learnrise-monitor.stderr.log</string>
</dict></plist>
EOF
cat > "$NEW_WATCH" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
<key>Label</key><string>$WATCH_LABEL</string>
<key>ProgramArguments</key><array><string>/usr/bin/python3</string><string>$SKILL_PATH/scripts/learnrise_event_watchdog.py</string><string>--state-dir</string><string>$STATE_DIR</string><string>--hub</string><string>$HUB</string></array>
<key>EnvironmentVariables</key><dict><key>PATH</key><string>$LAUNCH_PATH</string><key>PYTHONDONTWRITEBYTECODE</key><string>1</string></dict>
<key>StartInterval</key><integer>60</integer><key>RunAtLoad</key><true/>
<key>StandardOutPath</key><string>$STATE_DIR/learnrise-watchdog.stdout.log</string><key>StandardErrorPath</key><string>$STATE_DIR/learnrise-watchdog.stderr.log</string>
</dict></plist>
EOF
if ! plutil -lint "$NEW_PLIST" "$NEW_WATCH"; then
  rm -f "$NEW_PLIST" "$NEW_WATCH" "$MODE_CANDIDATE"
  exit 2
fi
OLD_MONITOR_LOADED=0
OLD_WATCH_LOADED=0
loaded "$LABEL" && OLD_MONITOR_LOADED=1
loaded "$WATCH_LABEL" && OLD_WATCH_LOADED=1
OLD_PLIST="$(mktemp "$AGENTS_DIR/.learnrise-monitor-old.XXXXXX")"
OLD_WATCH="$(mktemp "$AGENTS_DIR/.learnrise-watchdog-old.XXXXXX")"
OLD_MODE="$(mktemp "$STATE_DIR/.learnrise-mode-old.XXXXXX")"
HAD_PLIST=0
HAD_WATCH=0
HAD_MODE=0
if [ -f "$PLIST" ]; then cp "$PLIST" "$OLD_PLIST"; HAD_PLIST=1; fi
if [ -f "$WATCH_PLIST" ]; then cp "$WATCH_PLIST" "$OLD_WATCH"; HAD_WATCH=1; fi
if [ -f "$MODE_FILE" ]; then cp "$MODE_FILE" "$OLD_MODE"; HAD_MODE=1; fi
rollback_install() {
  bootout "$LABEL" || true
  bootout "$WATCH_LABEL" || true
  if [ "$HAD_PLIST" -eq 1 ]; then cp "$OLD_PLIST" "$PLIST"; else rm -f "$PLIST"; fi
  if [ "$HAD_WATCH" -eq 1 ]; then cp "$OLD_WATCH" "$WATCH_PLIST"; else rm -f "$WATCH_PLIST"; fi
  if [ "$HAD_MODE" -eq 1 ]; then cp "$OLD_MODE" "$MODE_FILE"; else rm -f "$MODE_FILE"; fi
  if [ "$OLD_MONITOR_LOADED" -eq 1 ]; then launchctl bootstrap "gui/$UID_NUM" "$PLIST" || true; fi
  if [ "$OLD_WATCH_LOADED" -eq 1 ]; then launchctl bootstrap "gui/$UID_NUM" "$WATCH_PLIST" || true; fi
  rm -f "$NEW_PLIST" "$NEW_WATCH" "$OLD_PLIST" "$OLD_WATCH" "$OLD_MODE" "$MODE_CANDIDATE"
}
if ! bootout "$LABEL" || ! bootout "$WATCH_LABEL" || ! cp "$NEW_PLIST" "$PLIST" || ! cp "$NEW_WATCH" "$WATCH_PLIST"; then
  rollback_install
  echo "installation failed; previous jobs restored" >&2
  exit 2
fi
touch "$STATE_DIR/learnrise-monitor-installed"
if ! launchctl bootstrap "gui/$UID_NUM" "$PLIST" || ! mv "$MODE_CANDIDATE" "$MODE_FILE" || ! launchctl bootstrap "gui/$UID_NUM" "$WATCH_PLIST"; then
  rollback_install
  echo "installation failed; previous jobs restored" >&2
  exit 2
fi
rm -f "$NEW_PLIST" "$NEW_WATCH" "$OLD_PLIST" "$OLD_WATCH" "$OLD_MODE" "$MODE_CANDIDATE"
echo "LearnRise shadow monitor and watchdog installed"
