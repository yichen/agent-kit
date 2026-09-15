#!/usr/bin/env bash
set -euo pipefail

REPORT="$(cd "$(dirname "$0")/../scripts" && pwd)/qwen-delegate-report.sh"
TEST_ROOT="$(mktemp -d)"
trap 'rm -rf "$TEST_ROOT"' EXIT
LOG="$TEST_ROOT/events.log"

cat > "$LOG" <<'EVENTS'
{"event_type":"attempt","timestamp":"2026-09-15T12:00:00Z","event_id":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa","caller":"legacy/caller","outcome":"success","prompt_tokens":100,"completion_tokens":20,"duration_ms":2000,"reason":""}
{"event_type":"consumption","timestamp":"2026-09-15T12:00:01Z","event_id":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa","caller":"legacy/caller","outcome":"consumed","prompt_tokens":0,"completion_tokens":0,"duration_ms":1,"reason":""}
{"event_type":"attempt","timestamp":"2026-09-15T12:01:00Z","event_id":"bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb","caller":"qwen-delegate/test-plan","task_type":"test-plan","outcome":"success","prompt_tokens":200,"completion_tokens":40,"duration_ms":4000,"reason":"","load_duration_ns":1000000,"prompt_eval_duration_ns":2000000,"eval_duration_ns":1000000000}
{"event_type":"result","timestamp":"2026-09-15T12:01:01Z","event_id":"bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb","caller":"qwen-delegate/test-plan","outcome":"delivered","prompt_tokens":0,"completion_tokens":0,"duration_ms":1,"reason":""}
{"event_type":"result","timestamp":"2026-09-15T12:01:02Z","event_id":"bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb","caller":"qwen-delegate/test-plan","outcome":"verified_used","prompt_tokens":0,"completion_tokens":0,"duration_ms":1,"reason":""}
EVENTS

before="$(shasum -a 256 "$LOG" | awk '{print $1}')"
"$REPORT" --date 2026-09-15 --log-file "$LOG" > "$TEST_ROOT/report.txt"
after="$(shasum -a 256 "$LOG" | awk '{print $1}')"
test "$before" = "$after"
grep -Fxq 'Production attempts: 2' "$TEST_ROOT/report.txt"
grep -Fxq 'Consumed production turns: 1' "$TEST_ROOT/report.txt"
grep -Fxq 'V2 delivered results: 1' "$TEST_ROOT/report.txt"
grep -Fxq 'V2 verified-used results: 1' "$TEST_ROOT/report.txt"
grep -Fxq 'V2 verification yield: 100.0%' "$TEST_ROOT/report.txt"
grep -Fxq 'Verified-use production tokens: 240 (200 prompt, 40 completion)' "$TEST_ROOT/report.txt"
grep -Fq 'Successful wall latency: p50 2.00s, p95 4.00s' "$TEST_ROOT/report.txt"
grep -Fq 'Generation throughput: p50 40.0 tok/s, p95 40.0 tok/s' "$TEST_ROOT/report.txt"
grep -Fq 'Task test-plan: 1 success, p50 4.00s' "$TEST_ROOT/report.txt"

set +e
"$REPORT" --date 2026-02-30 --log-file "$LOG" >/dev/null 2>&1
invalid_status=$?
set -e
test "$invalid_status" -eq 1
test "$before" = "$(shasum -a 256 "$LOG" | awk '{print $1}')"

printf '%s\n' 'Report V2 read-only tests passed'
