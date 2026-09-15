#!/usr/bin/env bash
# Install or verify the warm-model LaunchAgent for qwen-delegate.

set -euo pipefail

USER_HOME="${AGENT_KIT_USER_HOME:-$HOME}"
SKILL_PATH="${AGENT_KIT_PRIMARY_SKILL_PATH:-$USER_HOME/.agents/skills/qwen-delegate}"
DELEGATE_PATH="$SKILL_PATH/scripts/ollama-delegate.sh"
PLIST_PATH="${QWEN_DELEGATE_WARM_PLIST_PATH:-$USER_HOME/Library/LaunchAgents/com.yichen.sharedanchor-ollama-delegate-warm.plist}"
LABEL="com.yichen.sharedanchor-ollama-delegate-warm"
MODE="${1:-}"

render_plist() {
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

case "$MODE" in
  check)
    [ -x "$DELEGATE_PATH" ] || { echo "qwen-delegate: helper is not executable: $DELEGATE_PATH" >&2; exit 2; }
    [ -f "$PLIST_PATH" ] || { echo "qwen-delegate: warm service plist is missing: $PLIST_PATH" >&2; exit 2; }
    [ "$(render_plist)" = "$(sed -n '1,$p' "$PLIST_PATH")" ] || { echo "qwen-delegate: warm service plist drift detected" >&2; exit 2; }
    if [ "${QWEN_DELEGATE_SKIP_LAUNCHCTL:-0}" != "1" ]; then
      launchctl print "gui/$(id -u)/$LABEL" >/dev/null 2>&1 || { echo "qwen-delegate: warm service is not loaded" >&2; exit 2; }
    fi
    echo "qwen-delegate: warm service synchronized"
    ;;
  install)
    [ -x "$DELEGATE_PATH" ] || { echo "qwen-delegate: helper is not executable: $DELEGATE_PATH" >&2; exit 2; }
    mkdir -p "$(dirname "$PLIST_PATH")"
    plist_tmp="$(mktemp "${TMPDIR:-/tmp}/qwen-delegate-plist.XXXXXX")"
    trap 'rm -f "$plist_tmp"' EXIT
    render_plist > "$plist_tmp"
    install -m 0644 "$plist_tmp" "$PLIST_PATH"
    if [ "${QWEN_DELEGATE_SKIP_LAUNCHCTL:-0}" != "1" ]; then
      launchctl bootout "gui/$(id -u)/$LABEL" >/dev/null 2>&1 || true
      launchctl bootstrap "gui/$(id -u)" "$PLIST_PATH"
    fi
    "$0" check
    ;;
  *) echo "usage: $0 install|check" >&2; exit 1 ;;
esac
