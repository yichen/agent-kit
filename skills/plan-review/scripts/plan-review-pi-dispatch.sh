#!/usr/bin/env bash
# Dispatch one reviewer brief to Pi and print its final text.
# Works around three Pi behaviors that otherwise fail silently:
#   1. skills load through the read tool, so --no-tools hides the skill entirely
#   2. pi -p prints nothing in text mode when tools are enabled, so json mode is required
#   3. pi exits 0 with no output when the working directory is untrusted

set -euo pipefail

die() { echo "plan-review-pi-dispatch: $*" >&2; exit 2; }

brief_file="${1:-}"
cwd="${2:-$PWD}"
timeout_s="${PLAN_REVIEW_PI_TIMEOUT:-600}"

[ -n "$brief_file" ] || die "usage: $0 <brief-file> [cwd]"
[ -f "$brief_file" ] || die "brief file not found: $brief_file"
[ -d "$cwd" ] || die "working directory not found: $cwd"

command -v pi >/dev/null 2>&1 || die "pi not on PATH"

trust_file="$HOME/.pi/agent/trust.json"
abs_cwd="$(cd "$cwd" && pwd)"
if [ -f "$trust_file" ]; then
  trusted=0
  probe="$abs_cwd"
  while [ -n "$probe" ] && [ "$probe" != "/" ]; do
    if grep -q "\"$probe\"[[:space:]]*:[[:space:]]*true" "$trust_file"; then
      trusted=1
      break
    fi
    probe="$(dirname "$probe")"
  done
  [ "$trusted" -eq 1 ] || die "untrusted working directory: $abs_cwd is not in $trust_file, and pi exits 0 with no output there. Add it to pi's trust list or dispatch from a trusted directory. An empty result is a failed dispatch, never an approval."
fi

raw="$(mktemp -t plan-review-pi)"
trap 'rm -f "$raw"' EXIT

set +e
(cd "$abs_cwd" && timeout "$timeout_s" pi -p --mode json --no-session "$(cat "$brief_file")") > "$raw" 2>/dev/null
status=$?
set -e

if [ "$status" -eq 124 ]; then
  die "pi timed out after ${timeout_s}s"
fi

if [ ! -s "$raw" ]; then
  die "pi produced no output (exit $status). This is a failed dispatch, never an approval."
fi

python3 - "$raw" <<'PY'
import json, sys

final = None
for line in open(sys.argv[1]):
    line = line.strip()
    if not line.startswith('{'):
        continue
    try:
        d = json.loads(line)
    except Exception:
        continue
    if d.get('type') != 'message_end':
        continue
    m = d.get('message', {})
    if m.get('role') != 'assistant':
        continue
    for c in m.get('content', []):
        if c.get('type') == 'text' and c.get('text', '').strip():
            final = c['text']
        elif c.get('type') == 'toolCall' and c.get('name') == 'pq_done':
            out = (c.get('arguments') or {}).get('output')
            if out:
                final = out

if not final:
    sys.stderr.write('plan-review-pi-dispatch: pi returned no final text. This is a failed dispatch, never an approval.\n')
    sys.exit(3)

print(final)
PY
