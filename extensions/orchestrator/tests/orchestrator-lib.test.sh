#!/usr/bin/env bash
# No-token decision-logic tests for the orchestrator extension.
set -euo pipefail
DIR="$(cd "$(dirname "$0")" && pwd)"
node --test "$DIR/orchestrator-lib.test.mjs"
