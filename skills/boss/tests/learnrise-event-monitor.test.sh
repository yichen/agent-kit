#!/usr/bin/env bash
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
PYTHONDONTWRITEBYTECODE=1 python3 "$HERE/learnrise_event_monitor.test.py"
PYTHONDONTWRITEBYTECODE=1 python3 "$HERE/install_learnrise_monitor.test.py"
