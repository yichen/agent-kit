#!/usr/bin/env bash
# LIVE canary for the orchestrator extension. Opt-in only:
#   AGENT_KIT_ORCH_CANARY=1 bash extensions/orchestrator/tests/orchestrator-canary.test.sh
# Requires on this host: herdr, pi (with the ollama qwen model pulled), node.
# Creates and closes its own herdr panes; never touches the real state file
# (uses a scratch config and scratch PR-status stub), and fails loudly if any
# canary pane survives at the end.
set -euo pipefail

if [ "${AGENT_KIT_ORCH_CANARY:-0}" != "1" ]; then
  echo "SKIP orchestrator canary (set AGENT_KIT_ORCH_CANARY=1 to run live)"
  exit 0
fi

DIR="$(cd "$(dirname "$0")" && pwd)"
EXT_DIR="$(cd "$DIR/.." && pwd)"
REPO_ROOT="$(cd "$EXT_DIR/../.." && pwd)"

node "$DIR/orchestrator-canary.mjs" "$EXT_DIR" "$REPO_ROOT"
