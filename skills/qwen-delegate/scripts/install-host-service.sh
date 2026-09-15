#!/usr/bin/env bash
# Install or verify the warm-model LaunchAgent for qwen-delegate.

set -euo pipefail

USER_HOME="${AGENT_KIT_USER_HOME:-$HOME}"
SKILL_PATH="${AGENT_KIT_PRIMARY_SKILL_PATH:-$USER_HOME/.agents/skills/qwen-delegate}"
DELEGATE_PATH="$SKILL_PATH/scripts/ollama-delegate.sh"
REPORT_PATH="$SKILL_PATH/scripts/qwen-delegate-report.sh"
WARM_PLIST_PATH="${QWEN_DELEGATE_WARM_PLIST_PATH:-$USER_HOME/Library/LaunchAgents/com.yichen.sharedanchor-ollama-delegate-warm.plist}"
REPORT_PLIST_PATH="${QWEN_DELEGATE_REPORT_PLIST_PATH:-$USER_HOME/Library/LaunchAgents/com.yichen.sharedanchor-ollama-delegate-report.plist}"
WARM_LABEL="com.yichen.sharedanchor-ollama-delegate-warm"
REPORT_LABEL="com.yichen.sharedanchor-ollama-delegate-report"
MODE="${1:-}"

render_warm_plist() {
  sed "s|__DELEGATE_PATH__|$DELEGATE_PATH|g" <<'PLIST'
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>com.yichen.sharedanchor-ollama-delegate-warm</string>
  <key>ProgramArguments</key>
  <array>
    <string>__DELEGATE_PATH__</string>
    <string>--warm</string>
    <string>--caller</string>
    <string>maintenance/warm</string>
    <string>--timeout</string>
    <string>60</string>
  </array>
  <key>RunAtLoad</key>
  <true/>
  <key>StartInterval</key>
  <integer>3600</integer>
  <key>StandardOutPath</key>
  <string>/tmp/qwen-delegate-warm.log</string>
  <key>StandardErrorPath</key>
  <string>/tmp/qwen-delegate-warm.err</string>
</dict>
</plist>
PLIST
}

render_report_plist() {
  sed "s|__REPORT_PATH__|$REPORT_PATH|g; s|__USER_HOME__|$USER_HOME|g" <<'PLIST'
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>com.yichen.sharedanchor-ollama-delegate-report</string>
  <key>ProgramArguments</key>
  <array>
    <string>__REPORT_PATH__</string>
    <string>--date</string>
    <string>yesterday</string>
    <string>--notify</string>
  </array>
  <key>StartCalendarInterval</key>
  <dict>
    <key>Hour</key>
    <integer>0</integer>
    <key>Minute</key>
    <integer>10</integer>
  </dict>
  <key>ProcessType</key>
  <string>Background</string>
  <key>StandardOutPath</key>
  <string>__USER_HOME__/Library/Logs/SharedAnchor/ollama-delegate-report.log</string>
  <key>StandardErrorPath</key>
  <string>__USER_HOME__/Library/Logs/SharedAnchor/ollama-delegate-report.log</string>
</dict>
</plist>
PLIST
}

check_plist() {
  local description="$1" path="$2" renderer="$3"
  [ -f "$path" ] || { echo "qwen-delegate: $description plist is missing: $path" >&2; return 2; }
  [ "$("$renderer")" = "$(sed -n '1,$p' "$path")" ] || {
    echo "qwen-delegate: $description plist drift detected" >&2
    return 2
  }
}

install_plist() {
  local path="$1" renderer="$2"
  local plist_tmp
  mkdir -p "$(dirname "$path")"
  plist_tmp="$(mktemp "${TMPDIR:-/tmp}/qwen-delegate-plist.XXXXXX")"
  "$renderer" > "$plist_tmp"
  install -m 0644 "$plist_tmp" "$path"
  rm -f "$plist_tmp"
}

case "$MODE" in
  check)
    [ -x "$DELEGATE_PATH" ] || { echo "qwen-delegate: helper is not executable: $DELEGATE_PATH" >&2; exit 2; }
    [ -x "$REPORT_PATH" ] || { echo "qwen-delegate: report is not executable: $REPORT_PATH" >&2; exit 2; }
    check_plist "warm service" "$WARM_PLIST_PATH" render_warm_plist
    check_plist "report service" "$REPORT_PLIST_PATH" render_report_plist
    if [ "${QWEN_DELEGATE_SKIP_LAUNCHCTL:-0}" != "1" ]; then
      launchctl print "gui/$(id -u)/$WARM_LABEL" >/dev/null 2>&1 || { echo "qwen-delegate: warm service is not loaded" >&2; exit 2; }
      launchctl print "gui/$(id -u)/$REPORT_LABEL" >/dev/null 2>&1 || { echo "qwen-delegate: report service is not loaded" >&2; exit 2; }
    fi
    echo "qwen-delegate: host services synchronized"
    ;;
  install)
    [ -x "$DELEGATE_PATH" ] || { echo "qwen-delegate: helper is not executable: $DELEGATE_PATH" >&2; exit 2; }
    [ -x "$REPORT_PATH" ] || { echo "qwen-delegate: report is not executable: $REPORT_PATH" >&2; exit 2; }
    install_plist "$WARM_PLIST_PATH" render_warm_plist
    install_plist "$REPORT_PLIST_PATH" render_report_plist
    if [ "${QWEN_DELEGATE_SKIP_LAUNCHCTL:-0}" != "1" ]; then
      launchctl bootout "gui/$(id -u)/$WARM_LABEL" >/dev/null 2>&1 || true
      launchctl bootout "gui/$(id -u)/$REPORT_LABEL" >/dev/null 2>&1 || true
      launchctl bootstrap "gui/$(id -u)" "$WARM_PLIST_PATH"
      launchctl bootstrap "gui/$(id -u)" "$REPORT_PLIST_PATH"
    fi
    "$0" check
    ;;
  *) echo "usage: $0 install|check" >&2; exit 1 ;;
esac
