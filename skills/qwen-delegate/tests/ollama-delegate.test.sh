#!/usr/bin/env bash
set -euo pipefail

SKILL_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
DELEGATE="$SKILL_ROOT/scripts/ollama-delegate.sh"
REPORT="$SKILL_ROOT/scripts/qwen-delegate-report.sh"
TMP_ROOT="$(mktemp -d)"
trap 'rm -rf "$TMP_ROOT"' EXIT

BIN="$TMP_ROOT/bin"
mkdir -p "$BIN"

cat > "$BIN/curl" <<'CURL'
#!/usr/bin/env bash
set -u
output=""
request=""
url=""
while [ $# -gt 0 ]; do
  case "$1" in
    -o) output="$2"; shift 2 ;;
    --data-binary) request="$2"; shift 2 ;;
    -w|-H|--connect-timeout|--max-time) shift 2 ;;
    -sS) shift ;;
    *) url="$1"; shift ;;
  esac
done
if [[ "$url" == */api/ps ]]; then
  printf 'GET\n' >> "${FAKE_CALLS:?}"
  printf '%s' "${FAKE_PS:?}" > "$output"
  printf '%s' "${FAKE_PS_HTTP:-200}"
  exit "${FAKE_PS_STATUS:-0}"
fi
printf 'POST\n' >> "${FAKE_CALLS:?}"
if [ -n "${CAPTURE_REQUEST:-}" ] && [[ "$request" == @* ]]; then
  cp "${request#@}" "$CAPTURE_REQUEST"
fi
if [ -n "${FAKE_POST_READY:-}" ] && [ -n "${FAKE_POST_RELEASE:-}" ]; then
  : > "$FAKE_POST_READY"
  while [ ! -e "$FAKE_POST_RELEASE" ]; do sleep 0.05; done
fi
printf '%s' "${FAKE_RESPONSE:?}" > "$output"
printf '%s' "${FAKE_HTTP:-200}"
exit "${FAKE_CURL_STATUS:-0}"
CURL
chmod +x "$BIN/curl"

cat > "$BIN/osascript" <<'OSASCRIPT'
#!/usr/bin/env bash
printf '%s\n' "$*" >> "${FAKE_OSASCRIPT_CALLS:?}"
OSASCRIPT
chmod +x "$BIN/osascript"

cat > "$BIN/rm" <<'RM'
#!/usr/bin/env bash
set -u
for argument in "$@"; do
  if [ -n "${FAKE_CLEANUP_OWNER_PID:-}" ] && [ "$argument" = "$FAKE_CLEANUP_OWNER_PID" ]; then
    : > "${FAKE_CLEANUP_REMOVE_READY:?}"
    while [ ! -e "${FAKE_CLEANUP_REMOVE_RELEASE:?}" ]; do sleep 0.01; done
    break
  fi
done
exec /bin/rm "$@"
RM
chmod +x "$BIN/rm"

PASS=0
FAIL=0

pass() {
  PASS=$((PASS + 1))
  printf 'PASS: %s\n' "$1"
}

fail() {
  FAIL=$((FAIL + 1))
  printf 'FAIL: %s\n' "$1" >&2
}

assert_eq() {
  local label="$1" expected="$2" actual="$3"
  if [ "$expected" = "$actual" ]; then pass "$label"; else fail "$label (expected=$expected actual=$actual)"; fi
}

assert_contains() {
  local label="$1" file="$2" expected="$3"
  if grep -Fq -- "$expected" "$file"; then pass "$label"; else fail "$label (missing: $expected)"; fi
}

run_delegate() {
  local output_file="$1" error_file="$2"
  shift 2
  set +e
  PATH="$BIN:$PATH" "$DELEGATE" "$@" > "$output_file" 2> "$error_file"
  RUN_STATUS=$?
  set -e
}

fresh_case() {
  CASE_DIR="$(mktemp -d "$TMP_ROOT/case.XXXXXX")"
  export OLLAMA_DELEGATE_LOG_FILE="$CASE_DIR/events.log"
  export OLLAMA_DELEGATE_LOCK_DIR="$CASE_DIR/lock"
  export FAKE_CALLS="$CASE_DIR/calls"
  : > "$FAKE_CALLS"
  printf 'Turn these facts into a complete pull request body.\n' > "$CASE_DIR/prompt.txt"
  export FAKE_PS='{"models":[{"name":"qwen3.8:27b-mlx-128k","context_length":65536}]}'
  export FAKE_RESPONSE='{"response":"## Summary\n- Add local composition with safe fallback and deterministic validation.\n\n## Test plan\n- [x] Run the delegate helper tests and selector tests successfully.\n","prompt_eval_count":27,"eval_count":38,"done":true,"done_reason":"stop"}'
  unset FAKE_PS_HTTP FAKE_PS_STATUS FAKE_HTTP FAKE_CURL_STATUS CAPTURE_REQUEST OLLAMA_DELEGATE_DISABLE FAKE_POST_READY FAKE_POST_RELEASE
  unset FAKE_CLEANUP_OWNER_PID FAKE_CLEANUP_REMOVE_READY FAKE_CLEANUP_REMOVE_RELEASE
}

fresh_case
run_delegate "$CASE_DIR/out" "$CASE_DIR/err" --unknown-option
assert_eq "unknown option exits 1" 1 "$RUN_STATUS"
assert_eq "bad usage does not call Ollama" 0 "$(wc -l < "$FAKE_CALLS" | tr -d ' ')"
assert_contains "bad usage is logged" "$OLLAMA_DELEGATE_LOG_FILE" '"outcome":"error"'

fresh_case
MODEL_INJECTION_TARGET="$CASE_DIR/model-must-not-exist"
run_delegate "$CASE_DIR/out" "$CASE_DIR/err" --prompt-file "$CASE_DIR/prompt.txt" --model "\$(touch $MODEL_INJECTION_TARGET)"
assert_eq "unpinned model exits 1" 1 "$RUN_STATUS"
assert_eq "unpinned model performs no network call" 0 "$(wc -l < "$FAKE_CALLS" | tr -d ' ')"
assert_eq "shell-like model text remains inert" absent "$([ -e "$MODEL_INJECTION_TARGET" ] && echo present || echo absent)"

fresh_case
run_delegate "$CASE_DIR/out" "$CASE_DIR/err" --prompt-file "$CASE_DIR/prompt.txt" --require-marker ''
assert_eq "empty structural marker exits 1" 1 "$RUN_STATUS"
assert_eq "empty structural marker performs no network call" 0 "$(wc -l < "$FAKE_CALLS" | tr -d ' ')"

fresh_case
run_delegate "$CASE_DIR/out" "$CASE_DIR/err" --prompt-file "$CASE_DIR/prompt.txt" --timeout 61
assert_eq "request timeout cannot approach stale-lock age" 1 "$RUN_STATUS"
assert_eq "overlong timeout performs no network call" 0 "$(wc -l < "$FAKE_CALLS" | tr -d ' ')"

while IFS='|' read -r label caller expected_caller; do
  fresh_case
  run_delegate "$CASE_DIR/out" "$CASE_DIR/err" --prompt-file "$CASE_DIR/missing.txt" --caller "$caller"
  assert_eq "$label exits 1" 1 "$RUN_STATUS"
  assert_contains "$label records the safe caller value" "$OLLAMA_DELEGATE_LOG_FILE" "\"caller\":\"$expected_caller\""
  assert_eq "$label performs no network call" 0 "$(wc -l < "$FAKE_CALLS" | tr -d ' ')"
done <<'CALLER_CASES'
valid slash caller|code/diff-screen|code/diff-screen
valid underscore caller|pr/create_title|pr/create_title
uppercase caller|Code/Diff|invalid
leading slash caller|/code/diff|invalid
shell-like caller|$(touch /tmp/ollama-caller-injection)|invalid
CALLER_CASES

fresh_case
DISABLE_INJECTION_TARGET="$CASE_DIR/disable-must-not-exist"
export OLLAMA_DELEGATE_DISABLE="\$(touch $DISABLE_INJECTION_TARGET)"
run_delegate "$CASE_DIR/out" "$CASE_DIR/err" --prompt-file "$CASE_DIR/prompt.txt"
assert_eq "malformed kill switch exits 1" 1 "$RUN_STATUS"
assert_eq "malformed kill switch performs no network call" 0 "$(wc -l < "$FAKE_CALLS" | tr -d ' ')"
assert_eq "shell-like kill switch text remains inert" absent "$([ -e "$DISABLE_INJECTION_TARGET" ] && echo present || echo absent)"

fresh_case
export OLLAMA_DELEGATE_DISABLE=1
run_delegate "$CASE_DIR/out" "$CASE_DIR/err" --prompt-file "$CASE_DIR/does-not-exist.txt" --caller pr/create
assert_eq "kill switch exits 2" 2 "$RUN_STATUS"
assert_eq "kill switch performs no network call" 0 "$(wc -l < "$FAKE_CALLS" | tr -d ' ')"
assert_contains "kill switch logs unavailable" "$OLLAMA_DELEGATE_LOG_FILE" '"reason":"disabled"'

fresh_case
export FAKE_PS='{"models":[]}'
run_delegate "$CASE_DIR/out" "$CASE_DIR/err" --prompt-file "$CASE_DIR/prompt.txt"
assert_eq "unloaded model exits 2" 2 "$RUN_STATUS"
assert_eq "unloaded model stops after preflight" GET "$(tr -d '\n' < "$FAKE_CALLS")"
assert_contains "unloaded model outcome" "$OLLAMA_DELEGATE_LOG_FILE" '"reason":"model_not_loaded"'

fresh_case
export CAPTURE_REQUEST="$CASE_DIR/warm-request.json"
run_delegate "$CASE_DIR/out" "$CASE_DIR/err" --warm --caller maintenance/warm
assert_eq "warm mode exits 0" 0 "$RUN_STATUS"
assert_eq "warm mode posts without a loaded-model preflight" POST "$(tr -d '\n' < "$FAKE_CALLS")"
node -e 'const r=require(process.argv[1]); if(r.model!=="qwen3.8:27b-mlx-128k"||r.prompt!==""||r.keep_alive!=="8h"||r.stream!==false||r.think!==false)process.exit(1)' "$CAPTURE_REQUEST"
pass "warm mode pins the model and eight-hour residency"
assert_contains "warm mode is maintenance telemetry" "$OLLAMA_DELEGATE_LOG_FILE" '"event_type":"maintenance"'

fresh_case
run_delegate "$CASE_DIR/out" "$CASE_DIR/err" --warm --prompt-file "$CASE_DIR/prompt.txt" --caller maintenance/warm
assert_eq "warm mode rejects generation arguments" 1 "$RUN_STATUS"
assert_eq "invalid warm request performs no network call" 0 "$(wc -l < "$FAKE_CALLS" | tr -d ' ')"

fresh_case
export FAKE_PS='{"models":[{"name":"qwen3.8:27b-mlx-128k","context_length":1}]}'
run_delegate "$CASE_DIR/out" "$CASE_DIR/err" --prompt-file "$CASE_DIR/prompt.txt"
assert_eq "oversized prompt exits 2" 2 "$RUN_STATUS"
assert_eq "oversized prompt is never posted" GET "$(tr -d '\n' < "$FAKE_CALLS")"
assert_contains "oversized prompt has its own outcome" "$OLLAMA_DELEGATE_LOG_FILE" '"outcome":"prompt_too_large"'

while IFS='|' read -r label prompt_text; do
  fresh_case
  printf '%s\n' "$prompt_text" > "$CASE_DIR/prompt.txt"
  export FAKE_PS='{"models":[{"name":"qwen3.8:27b-mlx-128k","context_length":40}]}'
  run_delegate "$CASE_DIR/out" "$CASE_DIR/err" --prompt-file "$CASE_DIR/prompt.txt"
  assert_eq "$label exits 2 under byte-safe prompt bound" 2 "$RUN_STATUS"
  assert_eq "$label is rejected before POST" GET "$(tr -d '\n' < "$FAKE_CALLS")"
done <<'PROMPT_CASES'
dense punctuation|!@#$%^&*()_+-=[]{};:,.<>/?~`!@#$%^&*()
non-Latin text|共同养育费用记录需要保持完整并且不得截断上下文
PROMPT_CASES

fresh_case
export CAPTURE_REQUEST="$CASE_DIR/request.json"
export OLLAMA_DELEGATE_SESSION_ID='private-session-value'
run_delegate "$CASE_DIR/out" "$CASE_DIR/err" --prompt-file "$CASE_DIR/prompt.txt" --caller pr/create --require-marker '## Summary' --require-marker '## Test plan' --min-chars 120 --receipt-file "$CASE_DIR/receipt"
assert_eq "valid response exits 0" 0 "$RUN_STATUS"
assert_contains "valid response reaches stdout" "$CASE_DIR/out" '## Summary'
assert_eq "success releases the lock" absent "$([ -e "$OLLAMA_DELEGATE_LOCK_DIR" ] && echo present || echo absent)"
assert_contains "success logs Ollama prompt tokens" "$OLLAMA_DELEGATE_LOG_FILE" '"prompt_tokens":27'
node -e 'const r=require(process.argv[1]); if(r.model!=="qwen3.8:27b-mlx-128k"||r.stream!==false||r.think!==false)process.exit(1)' "$CAPTURE_REQUEST"
pass "request pins the model and disables thinking and streaming"
RECEIPT_ID="$(tr -d '\r\n' < "$CASE_DIR/receipt")"
assert_eq "success returns a receipt ID" 32 "${#RECEIPT_ID}"
if grep -Fq 'private-session-value' "$OLLAMA_DELEGATE_LOG_FILE"; then fail "raw session ID is never logged"; else pass "raw session ID is never logged"; fi
assert_contains "success logs a hashed session" "$OLLAMA_DELEGATE_LOG_FILE" '"session_hash":"'
run_delegate "$CASE_DIR/mark-out" "$CASE_DIR/mark-err" --mark-consumed "$RECEIPT_ID" --caller pr/create
assert_eq "valid receipt records consumption" 0 "$RUN_STATUS"
assert_eq "consumption tracking performs no network call" 2 "$(wc -l < "$FAKE_CALLS" | tr -d ' ')"
assert_contains "consumption event links the receipt" "$OLLAMA_DELEGATE_LOG_FILE" '"event_type":"consumption"'
unset OLLAMA_DELEGATE_SESSION_ID

while IFS='|' read -r label receipt; do
  fresh_case
  run_delegate "$CASE_DIR/out" "$CASE_DIR/err" --mark-consumed "$receipt" --caller pr/create
  assert_eq "$label exits 1" 1 "$RUN_STATUS"
  assert_eq "$label performs no network call" 0 "$(wc -l < "$FAKE_CALLS" | tr -d ' ')"
done <<'RECEIPT_CASES'
path-like receipt|../bad-receipt
uppercase receipt|AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA
short receipt|abcdef
shell-like receipt|$(touch /tmp/ollama-receipt-injection)
RECEIPT_CASES

fresh_case
run_delegate "$CASE_DIR/out" "$CASE_DIR/err" --mark-consumed 'ffffffffffffffffffffffffffffffff' --caller pr/create
assert_eq "unknown well-formed receipt exits 2" 2 "$RUN_STATUS"
assert_eq "unknown receipt performs no network call" 0 "$(wc -l < "$FAKE_CALLS" | tr -d ' ')"
assert_contains "unknown receipt explains the mismatch" "$CASE_DIR/err" 'does not identify a matching successful attempt'

fresh_case
export FAKE_RESPONSE='{"response":"A response long enough to pass the length floor, but without the requested structural heading anywhere in its output body.","prompt_eval_count":10,"eval_count":20,"done":true,"done_reason":"stop"}'
run_delegate "$CASE_DIR/out" "$CASE_DIR/err" --prompt-file "$CASE_DIR/prompt.txt" --require-marker '## Summary' --min-chars 50
assert_eq "missing structural marker exits 2" 2 "$RUN_STATUS"
assert_contains "missing marker logs invalid output" "$OLLAMA_DELEGATE_LOG_FILE" '"outcome":"invalid_output"'

fresh_case
export FAKE_RESPONSE='{"response":"This sentence mentions ## Summary but does not contain the requested heading line. It is deliberately long enough to isolate structural validation.","prompt_eval_count":10,"eval_count":20,"done":true,"done_reason":"stop"}'
run_delegate "$CASE_DIR/out" "$CASE_DIR/err" --prompt-file "$CASE_DIR/prompt.txt" --require-marker '## Summary' --min-chars 50
assert_eq "inline marker text is not accepted as a heading" 2 "$RUN_STATUS"
assert_contains "near-miss marker logs invalid output" "$OLLAMA_DELEGATE_LOG_FILE" '"reason":"missing_required_marker"'

fresh_case
export FAKE_RESPONSE='{"response":"## Summary\nA response with a malformed negative token count must not be accepted even though its body is otherwise long enough.","prompt_eval_count":-1,"eval_count":20,"done":true,"done_reason":"stop"}'
run_delegate "$CASE_DIR/out" "$CASE_DIR/err" --prompt-file "$CASE_DIR/prompt.txt" --require-marker '## Summary' --min-chars 50
assert_eq "negative response token count exits 2" 2 "$RUN_STATUS"
assert_contains "negative token count logs malformed response" "$OLLAMA_DELEGATE_LOG_FILE" '"reason":"malformed_response"'

while IFS='|' read -r label done_value done_reason expected_reason; do
  fresh_case
  export FAKE_RESPONSE="{\"response\":\"## Summary\\nThis response has headings and enough text but must be rejected because generation did not complete normally.\",\"prompt_eval_count\":10,\"eval_count\":20,\"done\":$done_value,\"done_reason\":\"$done_reason\"}"
  run_delegate "$CASE_DIR/out" "$CASE_DIR/err" --prompt-file "$CASE_DIR/prompt.txt" --require-marker '## Summary' --min-chars 50
  assert_eq "$label exits 2" 2 "$RUN_STATUS"
  assert_contains "$label logs incomplete output" "$OLLAMA_DELEGATE_LOG_FILE" "\"reason\":\"$expected_reason\""
done <<'RESPONSE_CASES'
done false|false|stop|incomplete_response_stop
length stop|true|length|incomplete_response_length
RESPONSE_CASES

fresh_case
export OLLAMA_DELEGATE_LOG_FILE="$CASE_DIR"
run_delegate "$CASE_DIR/out" "$CASE_DIR/err" --prompt-file "$CASE_DIR/prompt.txt" --require-marker '## Summary' --min-chars 50
assert_eq "log append failure refuses success" 2 "$RUN_STATUS"
assert_eq "log append failure emits no model output" 0 "$(wc -c < "$CASE_DIR/out" | tr -d ' ')"
assert_contains "log append failure explains unmeasured fallback" "$CASE_DIR/err" 'refusing an unmeasured success'

fresh_case
export FAKE_CURL_STATUS=28
export FAKE_HTTP=000
run_delegate "$CASE_DIR/out" "$CASE_DIR/err" --prompt-file "$CASE_DIR/prompt.txt" --timeout 1
assert_eq "request timeout exits 2" 2 "$RUN_STATUS"
assert_contains "timeout has its own outcome" "$OLLAMA_DELEGATE_LOG_FILE" '"outcome":"timeout"'

fresh_case
mkdir "$OLLAMA_DELEGATE_LOCK_DIR"
printf '%s\n' "$$" > "$OLLAMA_DELEGATE_LOCK_DIR/owner-pid"
run_delegate "$CASE_DIR/out" "$CASE_DIR/err" --prompt-file "$CASE_DIR/prompt.txt"
assert_eq "live lock owner causes immediate fallback" 2 "$RUN_STATUS"
assert_eq "busy path performs no network call" 0 "$(wc -l < "$FAKE_CALLS" | tr -d ' ')"
assert_contains "contention logs busy" "$OLLAMA_DELEGATE_LOG_FILE" '"outcome":"busy"'

fresh_case
mkdir "$OLLAMA_DELEGATE_LOCK_DIR"
printf '999999\n' > "$OLLAMA_DELEGATE_LOCK_DIR/owner-pid"
run_delegate "$CASE_DIR/out" "$CASE_DIR/err" --prompt-file "$CASE_DIR/prompt.txt" --require-marker '## Summary' --min-chars 50
assert_eq "dead lock owner is reclaimed" 0 "$RUN_STATUS"
assert_contains "dead-owner reclaim is loud" "$CASE_DIR/err" 'reclaiming stale lock'

fresh_case
mkdir "$OLLAMA_DELEGATE_LOCK_DIR"
printf '%s\n' "$$" > "$OLLAMA_DELEGATE_LOCK_DIR/owner-pid"
python3 - "$OLLAMA_DELEGATE_LOCK_DIR" <<'PY'
import os
import sys
import time
old = time.time() - 1200
os.utime(sys.argv[1], (old, old))
PY
run_delegate "$CASE_DIR/out" "$CASE_DIR/err" --prompt-file "$CASE_DIR/prompt.txt" --require-marker '## Summary' --min-chars 50
assert_eq "absolute age cap reclaims a live recycled pid" 0 "$RUN_STATUS"
assert_contains "age-cap reclaim is loud" "$CASE_DIR/err" 'WARNING: reclaiming lock older than 600s'

fresh_case
mkdir "$OLLAMA_DELEGATE_LOCK_DIR" "${OLLAMA_DELEGATE_LOCK_DIR}.reclaim"
printf '999999\n' > "$OLLAMA_DELEGATE_LOCK_DIR/owner-pid"
python3 - "${OLLAMA_DELEGATE_LOCK_DIR}.reclaim" <<'PY'
import os
import sys
import time
old = time.time() - 1200
os.utime(sys.argv[1], (old, old))
PY
run_delegate "$CASE_DIR/out" "$CASE_DIR/err" --prompt-file "$CASE_DIR/prompt.txt" --require-marker '## Summary' --min-chars 50
assert_eq "abandoned reclaim guard cannot wedge delegation" 0 "$RUN_STATUS"
assert_contains "abandoned reclaim guard removal is loud" "$CASE_DIR/err" 'WARNING: removing abandoned reclaim guard older than 600s'

fresh_case
export FAKE_POST_READY="$CASE_DIR/post-ready"
export FAKE_POST_RELEASE="$CASE_DIR/post-release"
PATH="$BIN:$PATH" "$DELEGATE" --prompt-file "$CASE_DIR/prompt.txt" --require-marker '## Summary' --min-chars 50 > "$CASE_DIR/out" 2> "$CASE_DIR/err" &
OLD_OWNER_JOB=$!
for _ in $(seq 1 100); do
  [ -e "$FAKE_POST_READY" ] && break
  sleep 0.05
done
assert_eq "old owner reaches the held request" present "$([ -e "$FAKE_POST_READY" ] && echo present || echo absent)"
printf '%s\n' "$$" > "$OLLAMA_DELEGATE_LOCK_DIR/owner-pid"
printf '%s\n' 'replacement-owner-nonce' > "$OLLAMA_DELEGATE_LOCK_DIR/owner-nonce"
: > "$FAKE_POST_RELEASE"
set +e
wait "$OLD_OWNER_JOB"
OLD_OWNER_STATUS=$?
set -e
assert_eq "old owner request itself completes" 0 "$OLD_OWNER_STATUS"
assert_eq "old owner cleanup preserves replacement lock" present "$([ -d "$OLLAMA_DELEGATE_LOCK_DIR" ] && echo present || echo absent)"
assert_eq "replacement nonce survives old owner cleanup" replacement-owner-nonce "$(cat "$OLLAMA_DELEGATE_LOCK_DIR/owner-nonce")"
assert_contains "ownership race is logged loudly" "$CASE_DIR/err" 'lock ownership changed before cleanup'
rm -f "$OLLAMA_DELEGATE_LOCK_DIR/owner-pid" "$OLLAMA_DELEGATE_LOCK_DIR/owner-nonce"
rmdir "$OLLAMA_DELEGATE_LOCK_DIR"

fresh_case
export FAKE_CLEANUP_OWNER_PID="$OLLAMA_DELEGATE_LOCK_DIR/owner-pid"
export FAKE_CLEANUP_REMOVE_READY="$CASE_DIR/cleanup-remove-ready"
export FAKE_CLEANUP_REMOVE_RELEASE="$CASE_DIR/cleanup-remove-release"
PATH="$BIN:$PATH" "$DELEGATE" --prompt-file "$CASE_DIR/prompt.txt" --require-marker '## Summary' --min-chars 50 > "$CASE_DIR/out" 2> "$CASE_DIR/err" &
RELEASING_OWNER_JOB=$!
for _ in $(seq 1 100); do
  [ -e "$FAKE_CLEANUP_REMOVE_READY" ] && break
  sleep 0.05
done
assert_eq "cleanup reaches deletion after its ownership read" present "$([ -e "$FAKE_CLEANUP_REMOVE_READY" ] && echo present || echo absent)"
(
  : > "$CASE_DIR/replacement-attempted"
  while ! mkdir "${OLLAMA_DELEGATE_LOCK_DIR}.reclaim" 2>/dev/null; do sleep 0.01; done
  : > "$CASE_DIR/replacement-guard-acquired"
  while [ -d "$OLLAMA_DELEGATE_LOCK_DIR" ]; do sleep 0.01; done
  mkdir "$OLLAMA_DELEGATE_LOCK_DIR"
  printf '%s\n' "$$" > "$OLLAMA_DELEGATE_LOCK_DIR/owner-pid"
  printf '%s\n' 'post-read-replacement-nonce' > "$OLLAMA_DELEGATE_LOCK_DIR/owner-nonce"
  rmdir "${OLLAMA_DELEGATE_LOCK_DIR}.reclaim"
) &
REPLACER_JOB=$!
for _ in $(seq 1 100); do
  [ -e "$CASE_DIR/replacement-attempted" ] && break
  sleep 0.01
done
assert_eq "reclaimer attempts replacement before old-owner deletion" present "$([ -e "$CASE_DIR/replacement-attempted" ] && echo present || echo absent)"
sleep 0.05
assert_eq "reclaimer cannot acquire the guard before old-owner deletion" absent "$([ -e "$CASE_DIR/replacement-guard-acquired" ] && echo present || echo absent)"
: > "$FAKE_CLEANUP_REMOVE_RELEASE"
set +e
wait "$RELEASING_OWNER_JOB"
RELEASING_OWNER_STATUS=$?
wait "$REPLACER_JOB"
REPLACER_STATUS=$?
set -e
assert_eq "old owner completes during serialized release" 0 "$RELEASING_OWNER_STATUS"
assert_eq "post-read replacement completes" 0 "$REPLACER_STATUS"
assert_eq "post-read replacement lock survives cleanup" post-read-replacement-nonce "$(cat "$OLLAMA_DELEGATE_LOCK_DIR/owner-nonce")"
rm -f "$OLLAMA_DELEGATE_LOCK_DIR/owner-pid" "$OLLAMA_DELEGATE_LOCK_DIR/owner-nonce"
rmdir "$OLLAMA_DELEGATE_LOCK_DIR"

fresh_case
mkdir "$CASE_DIR/bad-sh"
cat > "$CASE_DIR/bad-sh/sh" <<'SH'
#!/usr/bin/env bash
printf '999999\n'
SH
chmod +x "$CASE_DIR/bad-sh/sh"
set +e
PATH="$CASE_DIR/bad-sh:$BIN:$PATH" "$DELEGATE" --prompt-file "$CASE_DIR/prompt.txt" > "$CASE_DIR/out" 2> "$CASE_DIR/err"
RUN_STATUS=$?
set -e
assert_eq "unverifiable caller pid exits 2" 2 "$RUN_STATUS"
assert_contains "pid failure has a distinct outcome" "$OLLAMA_DELEGATE_LOG_FILE" '"outcome":"lock_identity_error"'

fresh_case
export FAKE_RESPONSE='{"response":"## Summary\n- Safe caller text remains data even with shell punctuation.\n\n## Test plan\n- [x] Nothing executes from the caller field, and this response is sufficiently long for validation.\n","prompt_eval_count":5,"eval_count":6,"done":true,"done_reason":"stop"}'
INJECTION_TARGET="$CASE_DIR/must-not-exist"
run_delegate "$CASE_DIR/out" "$CASE_DIR/err" --prompt-file "$CASE_DIR/prompt.txt" --caller "\$(touch $INJECTION_TARGET)" --require-marker '## Summary' --min-chars 50
assert_eq "shell-like caller text remains inert" absent "$([ -e "$INJECTION_TARGET" ] && echo present || echo absent)"
assert_eq "shell-like caller is rejected" 1 "$RUN_STATUS"
assert_contains "rejected caller is normalized in telemetry" "$OLLAMA_DELEGATE_LOG_FILE" '"caller":"invalid"'

REPORT_LOG="$TMP_ROOT/report-events.log"
cat > "$REPORT_LOG" <<'JSONL'
{"timestamp":"2026-09-13T18:00:00Z","caller":"pr/create","outcome":"success","prompt_tokens":2000,"completion_tokens":1000,"duration_ms":400,"reason":""}
{"timestamp":"2026-09-13T19:00:00Z","caller":"pr/create","outcome":"busy","prompt_tokens":0,"completion_tokens":0,"duration_ms":3,"reason":"lock_contended"}
{"event_type":"attempt","timestamp":"2026-09-13T20:00:00Z","event_id":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa","session_hash":"bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb","caller":"pr/create","outcome":"success","prompt_tokens":1000,"completion_tokens":500,"duration_ms":300,"reason":""}
{"event_type":"consumption","timestamp":"2026-09-13T20:01:00Z","event_id":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa","session_hash":"bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb","caller":"pr/create","outcome":"consumed","prompt_tokens":0,"completion_tokens":0,"duration_ms":0,"reason":""}
{"event_type":"attempt","timestamp":"2026-09-13T20:02:00Z","event_id":"cccccccccccccccccccccccccccccccc","caller":"smoke-test","outcome":"success","prompt_tokens":10,"completion_tokens":5,"duration_ms":10,"reason":""}
{"event_type":"attempt","timestamp":"2026-09-14T06:59:00Z","event_id":"dddddddddddddddddddddddddddddddd","session_hash":"cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc","caller":"code/final-report","outcome":"success","prompt_tokens":400,"completion_tokens":100,"duration_ms":20,"reason":""}
{"event_type":"consumption","timestamp":"2026-09-14T07:01:00Z","event_id":"dddddddddddddddddddddddddddddddd","session_hash":"cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc","caller":"code/final-report","outcome":"consumed","prompt_tokens":0,"completion_tokens":0,"duration_ms":0,"reason":""}
{"event_type":"attempt","timestamp":"not-a-date","event_id":"eeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee","caller":"pr/create","outcome":"success","prompt_tokens":9000,"completion_tokens":9000,"duration_ms":10,"reason":""}
{"event_type":"consumption","timestamp":"also-not-a-date","event_id":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa","caller":"pr/create","outcome":"consumed","prompt_tokens":0,"completion_tokens":0,"duration_ms":0,"reason":""}
{"timestamp":"2026-09-14T18:00:00Z","caller":"pr/create","outcome":"timeout","prompt_tokens":100,"completion_tokens":0,"duration_ms":60000,"reason":"request_timeout"}
not-json
JSONL
REPORT_SENTINEL="$TMP_ROOT/report-sentinel"
mkdir "$REPORT_SENTINEL"
TMPDIR="$REPORT_SENTINEL" TZ=America/Los_Angeles PATH="$BIN:$PATH" "$REPORT" --log-file "$REPORT_LOG" --date 2026-09-13 > "$TMP_ROOT/report.out"
assert_contains "report leads with attempt health" "$TMP_ROOT/report.out" 'Attempts and health'
assert_contains "report distinguishes production attempts" "$TMP_ROOT/report.out" 'Production attempts: 4'
assert_contains "report distinguishes test attempts" "$TMP_ROOT/report.out" 'Test attempts: 1'
assert_contains "report counts successful attempts" "$TMP_ROOT/report.out" 'success: 4'
assert_contains "report computes production success rate" "$TMP_ROOT/report.out" 'Production success rate: 75.0%'
assert_contains "report names the most common failure" "$TMP_ROOT/report.out" 'Most common failure reason: lock_contended (1)'
assert_contains "report counts validated production generations" "$TMP_ROOT/report.out" 'Validated production generations: 3'
assert_contains "report counts consumed production turns" "$TMP_ROOT/report.out" 'Consumed production turns: 2'
assert_contains "report counts consumed test turns" "$TMP_ROOT/report.out" 'Consumed test turns: 0'
assert_contains "report counts hashed production sessions" "$TMP_ROOT/report.out" 'Known production sessions: 2'
assert_contains "report joins a receipt across local midnight" "$TMP_ROOT/report.out" 'Consumed production tokens: 2000 (1400 prompt, 600 completion)'
assert_contains "report labels API-equivalent value" "$TMP_ROOT/report.out" 'API-equivalent value of consumed work'
assert_contains "report includes flat-plan caveat" "$TMP_ROOT/report.out" 'flat subscription'
assert_contains "report rejects malformed attempt and consumption timestamps" "$TMP_ROOT/report.out" 'Malformed event lines ignored: 3'
assert_eq "read-only report creates no temp files" 0 "$(find "$REPORT_SENTINEL" -mindepth 1 | wc -l | tr -d ' ')"

export FAKE_OSASCRIPT_CALLS="$TMP_ROOT/osascript-calls"
: > "$FAKE_OSASCRIPT_CALLS"
TZ=America/Los_Angeles PATH="$BIN:$PATH" "$REPORT" --log-file "$REPORT_LOG" --date 2026-09-13 --notify > /dev/null
assert_eq "below-80-percent day sends one notification" 1 "$(wc -l < "$FAKE_OSASCRIPT_CALLS" | tr -d ' ')"
: > "$FAKE_OSASCRIPT_CALLS"
TZ=America/Los_Angeles PATH="$BIN:$PATH" "$REPORT" --log-file "$REPORT_LOG" --date 2026-09-14 --notify > "$TMP_ROOT/zero-success.out"
assert_contains "zero-success day is explicit" "$TMP_ROOT/zero-success.out" 'Zero production successes: yes'
assert_eq "zero-success day sends one notification" 1 "$(wc -l < "$FAKE_OSASCRIPT_CALLS" | tr -d ' ')"
: > "$FAKE_OSASCRIPT_CALLS"
TZ=America/Los_Angeles PATH="$BIN:$PATH" "$REPORT" --log-file "$REPORT_LOG" --date 2026-09-15 --notify > "$TMP_ROOT/no-attempts.out"
assert_contains "no-attempt day is explicit" "$TMP_ROOT/no-attempts.out" 'n/a (no attempts)'
if grep -Fq 'Zero production successes: yes' "$TMP_ROOT/no-attempts.out"; then
  fail "no-attempt day does not claim zero successes"
else
  pass "no-attempt day does not claim zero successes"
fi
assert_eq "no-attempt day stays quiet" 0 "$(wc -l < "$FAKE_OSASCRIPT_CALLS" | tr -d ' ')"

set +e
"$REPORT" --date '2026-09-13; touch /tmp/nope' > /dev/null 2>&1
INVALID_DATE_STATUS=$?
set -e
assert_eq "malformed date exits 1" 1 "$INVALID_DATE_STATUS"

while IFS='|' read -r label candidate expected_status; do
  set +e
  "$REPORT" --log-file "$REPORT_LOG" --date "$candidate" > /dev/null 2>&1
  CALENDAR_STATUS=$?
  set -e
  assert_eq "$label" "$expected_status" "$CALENDAR_STATUS"
done <<'CALENDAR_CASES'
leap day is accepted|2024-02-29|0
non-leap day is rejected|2026-02-29|1
February 30 is rejected|2026-02-30|1
month zero is rejected|2026-00-01|1
month thirteen is rejected|2026-13-01|1
day zero is rejected|2026-01-00|1
CALENDAR_CASES

if [ "$FAIL" -ne 0 ]; then
  printf 'ollama delegate tests: FAIL (%s passed, %s failed)\n' "$PASS" "$FAIL" >&2
  exit 1
fi
printf 'ollama delegate tests: PASS (%s assertions)\n' "$PASS"
