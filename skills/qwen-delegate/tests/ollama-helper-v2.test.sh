#!/usr/bin/env bash
set -euo pipefail

HELPER="$(cd "$(dirname "$0")/../scripts" && pwd)/ollama-delegate.sh"
TEST_ROOT="$(mktemp -d)"
trap 'rm -rf "$TEST_ROOT"' EXIT
FAKE_BIN="$TEST_ROOT/bin"
mkdir -p "$FAKE_BIN"

cat > "$FAKE_BIN/curl" <<'FAKE'
#!/usr/bin/env bash
set -u
output=""
data_file=""
url=""
while [ $# -gt 0 ]; do
  case "$1" in
    -o) output="$2"; shift 2 ;;
    --data-binary) data_file="${2#@}"; shift 2 ;;
    -w|-H|--connect-timeout|--max-time) shift 2 ;;
    -sS) shift ;;
    *) url="$1"; shift ;;
  esac
done
case "$url" in
  */api/ps)
    printf '%s' '{"models":[{"name":"qwen3.8:27b-mlx","context_length":65536}]}' > "$output"
    ;;
  */api/generate)
    cp "$data_file" "${CAPTURE_REQUEST:?}"
    printf '%s' '{"response":"BODY_BEGIN\nSUMMARY:\n- bounded\nEVIDENCE:\n- supplied\nOMISSIONS:\n- NONE\nBODY_END","prompt_eval_count":42,"eval_count":21,"done":true,"done_reason":"stop","load_duration":1000000,"prompt_eval_duration":2000000,"eval_duration":3000000}' > "$output"
    ;;
  *) exit 7 ;;
esac
printf '200'
FAKE
chmod +x "$FAKE_BIN/curl"

printf '%s\n' 'bounded evidence' > "$TEST_ROOT/prompt.txt"
export CAPTURE_REQUEST="$TEST_ROOT/request.json"
export OLLAMA_DELEGATE_LOCK_DIR="$TEST_ROOT/lock"
export OLLAMA_DELEGATE_LOG_FILE="$TEST_ROOT/events.log"
export OLLAMA_DELEGATE_URL="http://fake-ollama"

PATH="$FAKE_BIN:$PATH" "$HELPER" \
  --prompt-file "$TEST_ROOT/prompt.txt" \
  --caller test/helper-v2 \
  --task-type-tag summarize \
  --max-output-tokens 123 \
  --timeout 5 \
  --min-chars 40 \
  --require-marker BODY_BEGIN \
  --require-marker BODY_END \
  --receipt-file "$TEST_ROOT/receipt" \
  > "$TEST_ROOT/output"

node -e '
const fs=require("fs");
const request=JSON.parse(fs.readFileSync(process.argv[1],"utf8"));
if (request.options?.num_predict !== 123) process.exit(1);
if (request.stream !== false || request.think !== false) process.exit(1);
' "$CAPTURE_REQUEST"

receipt="$(tr -d '\r\n' < "$TEST_ROOT/receipt")"
PATH="$FAKE_BIN:$PATH" "$HELPER" --record-result "$receipt" --result-outcome delivered --caller test/helper-v2
PATH="$FAKE_BIN:$PATH" "$HELPER" --record-result "$receipt" --result-outcome delivered --caller test/helper-v2
PATH="$FAKE_BIN:$PATH" "$HELPER" --record-result "$receipt" --result-outcome verified_used --caller test/helper-v2
PATH="$FAKE_BIN:$PATH" "$HELPER" --record-result "$receipt" --result-outcome verified_used --caller test/helper-v2

set +e
PATH="$FAKE_BIN:$PATH" "$HELPER" --record-result "$receipt" --result-outcome verified_rejected --caller test/helper-v2 >/dev/null 2>&1
conflict_status=$?
set -e
test "$conflict_status" -eq 2

EVENTS_FILE="$TEST_ROOT/events.log" RECEIPT="$receipt" node <<'NODE'
const fs=require("fs");
const events=fs.readFileSync(process.env.EVENTS_FILE,"utf8").trim().split("\n").map(JSON.parse);
const attempt=events.find((event)=>event.event_type==="attempt"&&event.event_id===process.env.RECEIPT);
if (!attempt || attempt.task_type!=="summarize" || attempt.load_duration_ns!==1000000 || attempt.prompt_eval_duration_ns!==2000000 || attempt.eval_duration_ns!==3000000) process.exit(1);
if (JSON.stringify(events).includes("bounded evidence") || JSON.stringify(events).includes("bounded\\n")) process.exit(1);
const lifecycle=events.filter((event)=>event.event_type==="result"&&event.event_id===process.env.RECEIPT).map((event)=>event.outcome);
if (JSON.stringify(lifecycle)!==JSON.stringify(["delivered","verified_used"])) process.exit(1);
NODE

printf '%s\n' 'Shared helper V2 tests passed'
