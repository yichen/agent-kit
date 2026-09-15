#!/usr/bin/env bash
set -euo pipefail

SCRIPT="$(cd "$(dirname "$0")/../scripts" && pwd)/qwen-delegate.sh"
TEST_ROOT="$(mktemp -d)"
trap 'rm -rf "$TEST_ROOT"' EXIT
FAKE_HELPER="$TEST_ROOT/fake-helper.sh"

cat > "$FAKE_HELPER" <<'FAKE'
#!/usr/bin/env bash
set -u
printf '%s\n' "$*" >> "${CALL_LOG:?}"
if [ "${1:-}" = "--record-result" ]; then
  printf '%s|%s|%s\n' "$2" "$4" "$6" >> "${LIFECYCLE_LOG:?}"
  exit "${FAKE_LIFECYCLE_STATUS:-0}"
fi
if [ "${1:-}" = "--mark-consumed" ]; then
  echo "legacy mark-consumed should not be called" >&2
  exit 9
fi
[ -z "${CAPTURE_LOCK_FILE:-}" ] || printf '%s\n' "${OLLAMA_DELEGATE_LOCK_DIR:-missing}" > "$CAPTURE_LOCK_FILE"
[ -z "${CAPTURE_LOG_FILE:-}" ] || printf '%s\n' "${OLLAMA_DELEGATE_LOG_FILE:-missing}" > "$CAPTURE_LOG_FILE"
task_type=""
receipt=""
while [ $# -gt 0 ]; do
  case "$1" in
    --task-type-tag) task_type="$2"; shift 2 ;;
    --receipt-file) receipt="$2"; shift 2 ;;
    *) if [ $# -ge 2 ] && [[ "$1" == --* ]]; then shift 2; else shift; fi ;;
  esac
done
case "$task_type" in
  classify) h1='RESULT:'; h2='EVIDENCE:'; h3='UNCERTAINTY:' ;;
  extract) h1='ITEMS:'; h2='EVIDENCE:'; h3='MISSING:' ;;
  summarize) h1='SUMMARY:'; h2='EVIDENCE:'; h3='OMISSIONS:' ;;
  draft) h1='DRAFT:'; h2='ASSUMPTIONS:'; h3='OPEN_QUESTIONS:' ;;
  test-plan) h1='CASES:'; h2='RISKS:'; h3='OPEN_QUESTIONS:' ;;
  diff-screen) h1='FINDINGS:'; h2='EVIDENCE:'; h3='LIMITATIONS:' ;;
  incident-review) h1='HYPOTHESES:'; h2='EVIDENCE:'; h3='NEXT_CHECKS:' ;;
  *) h1='SUMMARY:'; h2='EVIDENCE:'; h3='OMISSIONS:' ;;
esac
case "${FAKE_MODE:-success}" in
  busy) echo 'OLLAMA_FALLBACK: local model is busy; falling back immediately.' >&2; exit 2 ;;
  failure) echo 'delegate failed' >&2; exit 1 ;;
  missing-marker) printf '%s\n' 'BODY_BEGIN' "$h1" '- result' "$h2" '- evidence' 'BODY_END' ;;
  wrong-order) printf '%s\n' 'BODY_BEGIN' "$h2" '- evidence' "$h1" '- result' "$h3" '- none' 'BODY_END' ;;
  trailing) printf '%s\n' 'BODY_BEGIN' "$h1" '- result' "$h2" '- evidence' "$h3" '- none' 'BODY_END' 'trailing' ;;
  oversized) printf '%s\n' 'BODY_BEGIN' "$h1"; printf '%05000d\n' 0; printf '%s\n' "$h2" '- evidence' "$h3" '- none' 'BODY_END' ;;
  *) printf '%s\n' 'BODY_BEGIN' "$h1" '- bounded result' "$h2" '- supplied evidence' "$h3" '- NONE' 'BODY_END' ;;
esac
printf '%s\n' '0123456789abcdef0123456789abcdef' > "$receipt"
FAKE
chmod +x "$FAKE_HELPER"

export QWEN_DELEGATE_HELPER="$FAKE_HELPER"
export CALL_LOG="$TEST_ROOT/calls.log"
export LIFECYCLE_LOG="$TEST_ROOT/lifecycle.log"
: > "$CALL_LOG"
: > "$LIFECYCLE_LOG"
printf '%s\n' 'Summarize this bounded evidence.' > "$TEST_ROOT/prompt.txt"

pass=0
fail=0
check() {
  local label="$1"; shift
  if "$@"; then printf 'PASS: %s\n' "$label"; pass=$((pass + 1)); else printf 'FAIL: %s\n' "$label" >&2; fail=$((fail + 1)); fi
}

contains() {
  [[ "$1" == *"$2"* ]]
}

run_case() {
  local mode="$1" task_type="$2" output="$3"; shift 3
  local source_id="prompt"
  case "$task_type" in diff-screen|incident-review) source_id="source-a" ;; esac
  set +e
  FAKE_MODE="$mode" "$SCRIPT" --task-type "$task_type" --caller "qwen-delegate/$task_type" \
    --prompt-file "$TEST_ROOT/prompt.txt" --output-file "$output" --source-id "$source_id" "$@" \
    > "$TEST_ROOT/stdout" 2> "$TEST_ROOT/stderr"
  status=$?
  set -e
}

while IFS='|' read -r task_type expected_tokens expected_timeout; do
  output="$TEST_ROOT/result-$task_type.md"
  before="$(wc -l < "$CALL_LOG")"
  run_case success "$task_type" "$output"
  call="$(sed -n "$((before + 1))p" "$CALL_LOG")"
  check "$task_type succeeds" test "$status" -eq 0
  check "$task_type publishes V2" grep -Fxq 'QWEN_DELEGATE_V2' "$output"
  check "$task_type is advisory only" grep -Fxq 'AUTHORITY: ADVISORY_ONLY' "$output"
  check "$task_type has no decision authority" grep -Fxq 'DECISION_AUTHORITY: NONE' "$output"
  check "$task_type passes token profile" contains "$call" "--max-output-tokens $expected_tokens"
  check "$task_type passes timeout profile" contains "$call" "--timeout $expected_timeout"
  check "$task_type records delivery" grep -Fq '|delivered|qwen-delegate/' "$LIFECYCLE_LOG"
done <<'PROFILES'
classify|96|10
extract|160|12
summarize|192|15
draft|256|18
test-plan|256|18
diff-screen|256|18
incident-review|256|18
PROFILES

export CAPTURE_LOCK_FILE="$TEST_ROOT/captured-lock"
export CAPTURE_LOG_FILE="$TEST_ROOT/captured-log"
export OLLAMA_DELEGATE_LOCK_DIR="$TEST_ROOT/wrong-lock"
export OLLAMA_DELEGATE_LOG_FILE="$TEST_ROOT/wrong-events.log"
run_case success summarize "$TEST_ROOT/pinned-lock.md"
check 'wrapper pins shared host lock' grep -Fxq "$HOME/.local/state/sharedanchor-ollama-delegate/lock" "$CAPTURE_LOCK_FILE"
check 'wrapper pins shared telemetry log' grep -Fxq "$HOME/.local/state/sharedanchor-ollama-delegate/events.log" "$CAPTURE_LOG_FILE"
unset CAPTURE_LOCK_FILE CAPTURE_LOG_FILE OLLAMA_DELEGATE_LOCK_DIR OLLAMA_DELEGATE_LOG_FILE

run_case success test-plan "$TEST_ROOT/extended.md" --extended
extended_call="$(grep -- '--max-output-tokens 384' "$CALL_LOG" | tail -1)"
check 'extended profile uses 384 tokens' test -n "$extended_call"
check 'extended profile uses 30-second ceiling' contains "$extended_call" '--timeout 30'
run_case success classify "$TEST_ROOT/bad-extended.md" --extended
check 'classify rejects extended mode' test "$status" -eq 1
run_case success summarize "$TEST_ROOT/lower.md" --timeout 8 --max-output-tokens 80
check 'callers may lower limits' test "$status" -eq 0
run_case success summarize "$TEST_ROOT/raise-time.md" --timeout 16
check 'callers cannot raise timeout' test "$status" -eq 1
run_case success summarize "$TEST_ROOT/raise-tokens.md" --max-output-tokens 193
check 'callers cannot raise output tokens' test "$status" -eq 1

set +e
"$SCRIPT" --task-type diff-screen --caller qwen-delegate/diff --prompt-file "$TEST_ROOT/prompt.txt" --output-file "$TEST_ROOT/no-source.md" >/dev/null 2>&1
no_source_status=$?
set -e
check 'freshness-sensitive task requires source ID' test "$no_source_status" -eq 1

head -c 8192 /dev/zero | tr '\0' x > "$TEST_ROOT/limit.txt"
set +e
FAKE_MODE=success "$SCRIPT" --task-type summarize --caller qwen-delegate/limit --prompt-file "$TEST_ROOT/limit.txt" --output-file "$TEST_ROOT/limit.md" >/dev/null 2>&1
at_limit_status=$?
set -e
check 'prompt at byte ceiling is attempted' test "$at_limit_status" -eq 0
printf 'x' >> "$TEST_ROOT/limit.txt"
before_calls="$(wc -l < "$CALL_LOG")"
set +e
FAKE_MODE=success "$SCRIPT" --task-type summarize --caller qwen-delegate/over --prompt-file "$TEST_ROOT/limit.txt" --output-file "$TEST_ROOT/over.md" >/dev/null 2>&1
over_status=$?
set -e
after_calls="$(wc -l < "$CALL_LOG")"
check 'one byte over ceiling falls back' test "$over_status" -eq 2
check 'oversized prompt does not invoke helper' test "$before_calls" -eq "$after_calls"
check 'oversized prompt publishes no result' test ! -e "$TEST_ROOT/over.md"

for item in 'busy|busy|local model is busy' 'missing marker|missing-marker|V2 body contract' 'wrong order|wrong-order|V2 body contract' 'trailing output|trailing|V2 body contract' 'oversized output|oversized|V2 body contract' 'helper failure|failure|delegate failed'; do
  IFS='|' read -r label mode expected <<< "$item"
  output="$TEST_ROOT/negative-${label// /-}.md"
  start="$(date +%s)"
  run_case "$mode" summarize "$output"
  elapsed=$(( $(date +%s) - start ))
  check "$label exits with fallback" test "$status" -eq 2
  check "$label publishes no file" test ! -e "$output"
  check "$label reports reason" grep -Fq "$expected" "$TEST_ROOT/stderr"
  check "$label does not wait" test "$elapsed" -lt 2
done

fresh="$TEST_ROOT/fresh-result.md"
run_case success summarize "$fresh" --source-id source-a
set +e
"$SCRIPT" --verify-result "$fresh" --evidence-file "$TEST_ROOT/prompt.txt" --current-source-id source-a >/dev/null 2>&1
verify_status=$?
set -e
check 'identical evidence and source verifies' test "$verify_status" -eq 0
set +e
"$SCRIPT" --verify-result "$fresh" --evidence-file "$TEST_ROOT/prompt.txt" --current-source-id source-b >/dev/null 2>&1
stale_status=$?
set -e
check 'changed source is rejected' test "$stale_status" -eq 2
printf '%s\n' 'changed' >> "$TEST_ROOT/prompt.txt"
set +e
"$SCRIPT" --verify-result "$fresh" --evidence-file "$TEST_ROOT/prompt.txt" --current-source-id source-a >/dev/null 2>&1
digest_status=$?
set -e
check 'changed evidence is rejected' test "$digest_status" -eq 2
printf '%s\n' 'Summarize this bounded evidence.' > "$TEST_ROOT/prompt.txt"

cp "$fresh" "$TEST_ROOT/tampered-result.md"
sed -i.bak 's/bounded result/tampered result/' "$TEST_ROOT/tampered-result.md"
rm -f "$TEST_ROOT/tampered-result.md.bak"
set +e
"$SCRIPT" --verify-result "$TEST_ROOT/tampered-result.md" --evidence-file "$TEST_ROOT/prompt.txt" --current-source-id source-a >/dev/null 2>&1
tampered_status=$?
set -e
check 'changed result body is rejected' test "$tampered_status" -eq 2

set +e
"$SCRIPT" --mark-result verified-used --result-file "$fresh" --evidence-file "$TEST_ROOT/prompt.txt" --current-source-id source-a >/dev/null 2>&1
mark_status=$?
set -e
check 'verified-used accepts fresh evidence' test "$mark_status" -eq 0
check 'verified-used lifecycle is recorded' grep -Fq '|verified_used|qwen-delegate/summarize' "$LIFECYCLE_LOG"

existing="$TEST_ROOT/existing.md"
printf 'preserve me\n' > "$existing"
run_case success summarize "$existing"
check 'existing output is rejected' test "$status" -eq 1
check 'existing output is preserved' grep -Fxq 'preserve me' "$existing"

injection_target="$TEST_ROOT/must-not-exist"
injection_value="$(printf '\044(touch %s)' "$injection_target")"
set +e
"$SCRIPT" --task-type summarize --caller "$injection_value" --prompt-file "$TEST_ROOT/prompt.txt" --output-file "$TEST_ROOT/injection.md" >/dev/null 2>&1
injection_status=$?
set -e
check 'shell-like caller is rejected' test "$injection_status" -eq 1
check 'shell-like caller remains inert' test ! -e "$injection_target"

export FAKE_LIFECYCLE_STATUS=2
run_case success summarize "$TEST_ROOT/unmeasured.md"
check 'delivery telemetry failure falls back' test "$status" -eq 2
check 'delivery telemetry failure removes output' test ! -e "$TEST_ROOT/unmeasured.md"
unset FAKE_LIFECYCLE_STATUS

printf 'Qwen delegate V2 tests: %s passed, %s failed\n' "$pass" "$fail"
test "$fail" -eq 0
