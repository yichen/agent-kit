#!/usr/bin/env bash
# Run every tracked shell test without requiring a live Ollama service.

set -euo pipefail

ROOT="$(cd "$(dirname "$0")" && pwd)"

for test_file in "$ROOT"/tests/*.test.sh "$ROOT"/skills/*/tests/*.test.sh; do
  [ -f "$test_file" ] || continue
  echo "RUN $(realpath "$test_file")"
  bash "$test_file"
done

echo "agent-kit: all tests passed"
