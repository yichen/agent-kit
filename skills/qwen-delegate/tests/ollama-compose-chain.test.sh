#!/usr/bin/env bash
set -euo pipefail

SKILL_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
CHAIN="$SKILL_ROOT/scripts/ollama-compose-chain.sh"
TMP_ROOT="$(mktemp -d)"
trap 'rm -rf "$TMP_ROOT"' EXIT

FAKE_ROOT="$TMP_ROOT/repo"
mkdir -p "$FAKE_ROOT/.agents/tools"
printf 'Draft this from the facts below.\n- Fact\n' > "$TMP_ROOT/prompt.txt"

cat > "$FAKE_ROOT/.agents/tools/ollama-delegate.sh" <<'SH'
#!/usr/bin/env bash
printf 'ollama\n' >> "${CHAIN_CALLS:?}"
printf 'ollama-args=%s\n' "$*" >> "${CHAIN_ARGS:?}"
if [ "${1:-}" = "--mark-consumed" ]; then
  exit "${FAKE_MARK_STATUS:-0}"
fi
receipt=""
while [ $# -gt 0 ]; do
  if [ "$1" = "--receipt-file" ]; then receipt="$2"; shift 2; else shift; fi
done
if [ -n "$receipt" ]; then printf '0123456789abcdef0123456789abcdef\n' > "$receipt"; fi
if [ "${FAKE_OLLAMA_STATUS:-0}" -eq 0 ]; then printf '%s\n' "${FAKE_OLLAMA_OUTPUT:-Composed text}"; fi
exit "${FAKE_OLLAMA_STATUS:-0}"
SH
chmod +x "$FAKE_ROOT/.agents/tools/ollama-delegate.sh"

PASS=0
FAIL=0
pass() { PASS=$((PASS + 1)); printf 'PASS: %s\n' "$1"; }
fail() { FAIL=$((FAIL + 1)); printf 'FAIL: %s\n' "$1" >&2; }
assert_eq() {
  if [ "$2" = "$3" ]; then pass "$1"; else fail "$1 (expected=$2 actual=$3)"; fi
}
assert_contains() {
  if grep -Fq -- "$3" "$2"; then pass "$1"; else fail "$1 (missing: $3)"; fi
}

fresh_case() {
  export CHAIN_CALLS="$TMP_ROOT/calls"
  export CHAIN_ARGS="$TMP_ROOT/args"
  : > "$CHAIN_CALLS"
  : > "$CHAIN_ARGS"
  rm -f "$TMP_ROOT/out.txt"
  unset CLAUDE_COMPOSE
  unset FAKE_OLLAMA_OUTPUT
  unset FAKE_MARK_STATUS
}
run_chain() {
  set +e
  QWEN_DELEGATE_HELPER="$FAKE_ROOT/.agents/tools/ollama-delegate.sh" "$CHAIN" --repo-root "$FAKE_ROOT" --prompt-file "$TMP_ROOT/prompt.txt" \
    --output-file "$TMP_ROOT/out.txt" --caller test-caller "$@" \
    > "$TMP_ROOT/stdout" 2> "$TMP_ROOT/stderr"
  CHAIN_STATUS=$?
  set -e
}

fresh_case
export FAKE_OLLAMA_STATUS=0
run_chain
assert_eq "success exits 0" 0 "$CHAIN_STATUS"
assert_eq "success calls generation and consumption tracking" ollamaollama "$(tr -d '\n' < "$CHAIN_CALLS")"
assert_contains "success publishes the composed text" "$TMP_ROOT/out.txt" 'Composed text'
assert_contains "delegate receives the caller name" "$CHAIN_ARGS" '--caller test-caller'
assert_contains "delegate defaults to the 60-second cap" "$CHAIN_ARGS" '--timeout 60'
assert_contains "delegate defaults to 80 min-chars" "$CHAIN_ARGS" '--min-chars 80'
assert_contains "delegate receives a receipt file" "$CHAIN_ARGS" '--receipt-file'
assert_contains "consumption receipt is marked" "$CHAIN_ARGS" '--mark-consumed 0123456789abcdef0123456789abcdef'

fresh_case
export FAKE_OLLAMA_STATUS=0
run_chain --require-marker "## Summary" --min-chars 5 --timeout 30
assert_eq "custom flags still exit 0" 0 "$CHAIN_STATUS"
assert_contains "custom require-marker is forwarded" "$CHAIN_ARGS" '--require-marker ## Summary'
assert_contains "custom min-chars is forwarded" "$CHAIN_ARGS" '--min-chars 5'
assert_contains "custom timeout is forwarded" "$CHAIN_ARGS" '--timeout 30'

fresh_case
export FAKE_OLLAMA_STATUS=0 FAKE_MARK_STATUS=2
run_chain
assert_eq "consumption logging failure falls back" 2 "$CHAIN_STATUS"
assert_eq "consumption logging failure removes published output" 0 "$([ -e "$TMP_ROOT/out.txt" ] && echo 1 || echo 0)"

fresh_case
export FAKE_OLLAMA_STATUS=0
export FAKE_OLLAMA_OUTPUT='This first line is too long'
run_chain --max-first-line-chars 10 --min-chars 1
assert_eq "overlong first line falls back" 2 "$CHAIN_STATUS"
assert_eq "overlong first line is never marked consumed" ollama "$(tr -d '\n' < "$CHAIN_CALLS")"

fresh_case
export FAKE_OLLAMA_STATUS=2
run_chain
assert_eq "delegate failure reaches caller-owned fallback" 2 "$CHAIN_STATUS"
assert_contains "fallback is explicit" "$TMP_ROOT/stderr" 'CLAUDE_COMPOSE_REQUIRED:'
assert_eq "fallback leaves no stale output file" 0 "$([ -e "$TMP_ROOT/out.txt" ] && echo 1 || echo 0)"

fresh_case
export CLAUDE_COMPOSE=1 FAKE_OLLAMA_STATUS=0
run_chain
assert_eq "CLAUDE_COMPOSE requests caller-owned fallback" 2 "$CHAIN_STATUS"
assert_eq "CLAUDE_COMPOSE skips the delegate entirely" 0 "$(wc -l < "$CHAIN_CALLS" | tr -d ' ')"
assert_contains "CLAUDE_COMPOSE names the caller in its fallback message" "$TMP_ROOT/stderr" "caller 'test-caller'"

MALFORMED_SENTINEL="$TMP_ROOT/malformed-sentinel"
mkdir "$MALFORMED_SENTINEL"
set +e
TMPDIR="$MALFORMED_SENTINEL" "$CHAIN" --repo-root "$FAKE_ROOT" > "$TMP_ROOT/stdout" 2> "$TMP_ROOT/stderr"
MALFORMED_STATUS=$?
set -e
assert_eq "missing required args exit 1" 1 "$MALFORMED_STATUS"
assert_eq "missing required args create no temp files" 0 "$(find "$MALFORMED_SENTINEL" -mindepth 1 | wc -l | tr -d ' ')"

set +e
"$CHAIN" --unknown-flag foo > "$TMP_ROOT/stdout" 2> "$TMP_ROOT/stderr"
UNKNOWN_STATUS=$?
set -e
assert_eq "unknown flag exits 1" 1 "$UNKNOWN_STATUS"
assert_contains "unknown flag names itself" "$TMP_ROOT/stderr" 'unknown arg: --unknown-flag'

set +e
"$CHAIN" --repo-root "$FAKE_ROOT" --prompt-file "$TMP_ROOT/prompt.txt" --output-file "$TMP_ROOT/out.txt" --caller test-caller --max-first-line-chars '1;touch nope' > "$TMP_ROOT/stdout" 2> "$TMP_ROOT/stderr"
BAD_LIMIT_STATUS=$?
set -e
assert_eq "shell-like first-line limit exits 1" 1 "$BAD_LIMIT_STATUS"

if [ "$FAIL" -ne 0 ]; then
  printf 'ollama compose chain tests: FAIL (%s passed, %s failed)\n' "$PASS" "$FAIL" >&2
  exit 1
fi
printf 'ollama compose chain tests: PASS (%s assertions)\n' "$PASS"
