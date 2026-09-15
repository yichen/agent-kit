#!/usr/bin/env bash
# Delegate bounded, prose-only composition to the already-loaded local Ollama model.
#
# This script must be executed directly. Its PID ownership check is not reliable
# when the file is sourced through `bash -c` or command substitution.
#
# Exit codes:
#   0: validated model output is on stdout
#   1: invalid usage
#   2: safe fallback is required
#
# Usage:
#   ollama-delegate.sh --prompt-file <path> [options]
#
# Options:
#   --model <tag>             Must equal the pinned qwen3.8:27b-mlx tag
#   --caller <name>           Calling skill or phase name for the event log
#   --task-type-tag <name>    Optional bounded-work category for aggregate reporting
#   --require-marker <text>   Required output text; may be repeated
#   --min-chars <count>       Minimum non-whitespace output characters (default: 80)
#   --timeout <seconds>       Request timeout for the warm model (default: 60)
#   --max-output-tokens <n>   Server-side generation ceiling (optional, maximum 2048)
#   --receipt-file <path>     Write the successful attempt ID for later consumption tracking
#   --mark-consumed <id>      Record that a previously validated result was actually used
#   --record-result <id>      Record a V2 result lifecycle event
#   --result-outcome <value>  delivered, verified_used, verified_rejected, or superseded
#   --warm                    Load the pinned model and keep it resident for eight hours
#
# Environment:
#   OLLAMA_DELEGATE_DISABLE=1 forces a safe fallback.
#   OLLAMA_DELEGATE_URL overrides http://127.0.0.1:11434.
#   OLLAMA_DELEGATE_LOCK_DIR overrides $HOME/.local/state/sharedanchor-ollama-delegate/lock.
#   (Not /tmp: some setups clear /tmp on reboot, sleep, or a scheduled sweep,
#   and the reclaim protocol is safer built once, on durable storage.)
#   OLLAMA_DELEGATE_LOG_FILE overrides the durable host event log.
#   OLLAMA_DELEGATE_SESSION_ID overrides the session identifier used only to derive a hash.

set -u

MODEL="qwen3.8:27b-mlx"
PINNED_MODEL="qwen3.8:27b-mlx"
CALLER="unknown"
TASK_TYPE_TAG=""
PROMPT_FILE=""
MIN_CHARS=80
TIMEOUT_SEC="${OLLAMA_DELEGATE_TIMEOUT:-60}"
OLLAMA_URL="${OLLAMA_DELEGATE_URL:-http://127.0.0.1:11434}"
LOCK_DIR="${OLLAMA_DELEGATE_LOCK_DIR:-$HOME/.local/state/sharedanchor-ollama-delegate/lock}"
LOG_FILE="${OLLAMA_DELEGATE_LOG_FILE:-$HOME/.local/state/sharedanchor-ollama-delegate/events.log}"
MAX_LOCK_SECONDS=600
MAX_REQUEST_SECONDS=60
REQUIRED_MARKERS=()
REQUIRED_MARKER_COUNT=0
LOCK_HELD=0
LOCK_PID=""
LOCK_NONCE=""
TMP_PS=""
TMP_REQUEST=""
TMP_RESPONSE=""
RECEIPT_FILE=""
MARK_CONSUMED_ID=""
RECORD_RESULT_ID=""
RESULT_OUTCOME=""
MAX_OUTPUT_TOKENS=""
WARM=0
EVENT_ID=""
NODE_BIN=""
START_MS=0

# shellcheck disable=SC2329 # Invoked indirectly by the EXIT trap.
cleanup() {
  local reclaim_dir current_pid current_nonce
  rm -f "${TMP_PS:-}" "${TMP_REQUEST:-}" "${TMP_RESPONSE:-}" 2>/dev/null || true
  if [ "$LOCK_HELD" -eq 1 ]; then
    reclaim_dir="${LOCK_DIR}.reclaim"
    if ! mkdir "$reclaim_dir" 2>/dev/null; then
      echo "ollama-delegate: WARNING: lock release could not acquire the reclaim guard; leaving the current lock intact." >&2
      return
    fi
    current_pid="$(cat "$LOCK_DIR/owner-pid" 2>/dev/null || true)"
    current_nonce="$(cat "$LOCK_DIR/owner-nonce" 2>/dev/null || true)"
    if [ "$current_pid" = "$LOCK_PID" ] && [ "$current_nonce" = "$LOCK_NONCE" ]; then
      rm -f "$LOCK_DIR/owner-pid" "$LOCK_DIR/owner-nonce" 2>/dev/null || true
      rmdir "$LOCK_DIR" 2>/dev/null || true
    else
      echo "ollama-delegate: WARNING: lock ownership changed before cleanup; leaving the current owner's lock intact." >&2
    fi
    rmdir "$reclaim_dir" 2>/dev/null || true
  fi
}
trap cleanup EXIT

usage_error() {
  echo "ollama-delegate: $1" >&2
  finish 1 error "usage_error" 0 0
}

now_ms() {
  "$NODE_BIN" -e 'process.stdout.write(String(Date.now()))'
}

find_node() {
  local candidate
  for candidate in "${OLLAMA_DELEGATE_NODE:-}" "$(command -v node 2>/dev/null || true)" /opt/homebrew/bin/node /usr/local/bin/node; do
    if [ -n "$candidate" ] && [ -x "$candidate" ]; then
      NODE_BIN="$candidate"
      return 0
    fi
  done
  return 1
}

session_hash() {
  local raw="${OLLAMA_DELEGATE_SESSION_ID:-${CODEX_THREAD_ID:-${CODEX_SESSION_ID:-${CLAUDE_SESSION_ID:-${PI_SESSION_ID:-}}}}}"
  if [ -z "$raw" ]; then
    printf '%s' ""
    return 0
  fi
  printf '%s' "$raw" | shasum -a 256 | awk '{print $1}'
}

new_event_id() {
  /usr/bin/openssl rand -hex 16 2>/dev/null
}

json_log_line() {
  EVENT_TYPE="$1" EVENT_TIMESTAMP="$2" EVENT_ID_VALUE="$3" EVENT_CALLER="$4" \
    EVENT_OUTCOME="$5" EVENT_PROMPT_TOKENS="$6" EVENT_COMPLETION_TOKENS="$7" \
    EVENT_DURATION_MS="$8" EVENT_REASON="$9" EVENT_SESSION_HASH="${10}" \
    EVENT_LOAD_NS="${11:-0}" EVENT_PROMPT_EVAL_NS="${12:-0}" EVENT_EVAL_NS="${13:-0}" EVENT_TASK_TYPE="$TASK_TYPE_TAG" \
    "$NODE_BIN" -e '
const event = {
  event_type: process.env.EVENT_TYPE,
  timestamp: process.env.EVENT_TIMESTAMP,
  event_id: process.env.EVENT_ID_VALUE,
  caller: process.env.EVENT_CALLER,
  outcome: process.env.EVENT_OUTCOME,
  prompt_tokens: Number(process.env.EVENT_PROMPT_TOKENS),
  completion_tokens: Number(process.env.EVENT_COMPLETION_TOKENS),
  duration_ms: Number(process.env.EVENT_DURATION_MS),
  reason: process.env.EVENT_REASON,
};
if (process.env.EVENT_SESSION_HASH) event.session_hash = process.env.EVENT_SESSION_HASH;
if (process.env.EVENT_TASK_TYPE) event.task_type = process.env.EVENT_TASK_TYPE;
for (const [field, value] of [
  ["load_duration_ns", process.env.EVENT_LOAD_NS],
  ["prompt_eval_duration_ns", process.env.EVENT_PROMPT_EVAL_NS],
  ["eval_duration_ns", process.env.EVENT_EVAL_NS],
]) {
  const parsed = Number(value);
  if (Number.isInteger(parsed) && parsed > 0) event[field] = parsed;
}
process.stdout.write(JSON.stringify(event));'
}

log_event() {
  local event_type="$1" event_id="$2" outcome="$3" reason="$4" prompt_tokens="$5" completion_tokens="$6"
  local load_ns="${7:-0}" prompt_eval_ns="${8:-0}" eval_ns="${9:-0}"
  local end_ms duration timestamp log_dir line hashed_session
  end_ms="$(now_ms)"
  duration=$((end_ms - START_MS))
  timestamp="$(date -u '+%Y-%m-%dT%H:%M:%SZ')"
  log_dir="$(dirname "$LOG_FILE")"
  mkdir -p "$log_dir" 2>/dev/null || {
    echo "ollama-delegate: could not create event log directory: $log_dir" >&2
    return 1
  }
  hashed_session="$(session_hash)"
  line="$(json_log_line "$event_type" "$timestamp" "$event_id" "$CALLER" "$outcome" "$prompt_tokens" "$completion_tokens" "$duration" "$reason" "$hashed_session" "$load_ns" "$prompt_eval_ns" "$eval_ns" 2>/dev/null)" || line=""
  if [ -n "$line" ]; then
    if ! printf '%s\n' "$line" >> "$LOG_FILE" 2>/dev/null; then
      echo "ollama-delegate: could not append event log: $LOG_FILE" >&2
      return 1
    fi
  else
    echo "ollama-delegate: could not encode event log entry" >&2
    return 1
  fi
  return 0
}

finish() {
  local status="$1" outcome="$2" reason="$3" prompt_tokens="$4" completion_tokens="$5"
  [ -n "$EVENT_ID" ] || EVENT_ID="$(new_event_id)"
  log_event attempt "$EVENT_ID" "$outcome" "$reason" "$prompt_tokens" "$completion_tokens" || true
  exit "$status"
}

find_node || {
  echo "ollama-delegate: Node.js is required for safe JSON handling." >&2
  exit 2
}
START_MS="$(now_ms)"

while [ $# -gt 0 ]; do
  case "$1" in
    --prompt-file)
      [ $# -ge 2 ] || usage_error "--prompt-file requires a value"
      PROMPT_FILE="$2"
      shift 2
      ;;
    --model)
      [ $# -ge 2 ] || usage_error "--model requires a value"
      MODEL="$2"
      shift 2
      ;;
    --caller)
      [ $# -ge 2 ] || usage_error "--caller requires a value"
      CALLER="$2"
      shift 2
      ;;
    --task-type-tag)
      [ $# -ge 2 ] || usage_error "--task-type-tag requires a value"
      TASK_TYPE_TAG="$2"
      shift 2
      ;;
    --require-marker)
      [ $# -ge 2 ] || usage_error "--require-marker requires a value"
      REQUIRED_MARKERS+=("$2")
      REQUIRED_MARKER_COUNT=$((REQUIRED_MARKER_COUNT + 1))
      shift 2
      ;;
    --min-chars)
      [ $# -ge 2 ] || usage_error "--min-chars requires a value"
      MIN_CHARS="$2"
      shift 2
      ;;
    --timeout)
      [ $# -ge 2 ] || usage_error "--timeout requires a value"
      TIMEOUT_SEC="$2"
      shift 2
      ;;
    --max-output-tokens)
      [ $# -ge 2 ] || usage_error "--max-output-tokens requires a value"
      MAX_OUTPUT_TOKENS="$2"
      shift 2
      ;;
    --receipt-file)
      [ $# -ge 2 ] || usage_error "--receipt-file requires a value"
      RECEIPT_FILE="$2"
      shift 2
      ;;
    --mark-consumed)
      [ $# -ge 2 ] || usage_error "--mark-consumed requires a value"
      MARK_CONSUMED_ID="$2"
      shift 2
      ;;
    --record-result)
      [ $# -ge 2 ] || usage_error "--record-result requires an attempt ID"
      RECORD_RESULT_ID="$2"
      shift 2
      ;;
    --result-outcome)
      [ $# -ge 2 ] || usage_error "--result-outcome requires a value"
      RESULT_OUTCOME="$2"
      shift 2
      ;;
    --warm)
      WARM=1
      shift
      ;;
    -h|--help)
      sed -n '2,/^set -u$/p' "$0" | sed '$d'
      finish 0 error "help_requested" 0 0
      ;;
    *) usage_error "unknown arg: $1" ;;
  esac
done

if ! [[ "$CALLER" =~ ^[a-z0-9][a-z0-9/_-]{0,63}$ ]]; then
  CALLER="invalid"
  usage_error "--caller must use 1-64 lowercase letters, digits, slash, underscore, or hyphen"
fi
if [ -n "$TASK_TYPE_TAG" ] && ! [[ "$TASK_TYPE_TAG" =~ ^[a-z0-9][a-z0-9_-]{0,31}$ ]]; then
  usage_error "--task-type-tag must use 1-32 lowercase letters, digits, underscore, or hyphen"
fi
if [ -n "$MARK_CONSUMED_ID" ]; then
  if [ -n "$PROMPT_FILE" ] || [ -n "$RECEIPT_FILE" ] || [ "$REQUIRED_MARKER_COUNT" -ne 0 ] || [ -n "$RECORD_RESULT_ID" ] || [ -n "$RESULT_OUTCOME" ]; then
    usage_error "--mark-consumed cannot be combined with generation options"
  fi
  if ! [[ "$MARK_CONSUMED_ID" =~ ^[0-9a-f]{32}$ ]]; then
    usage_error "--mark-consumed requires a 32-character lowercase hexadecimal attempt ID"
  fi
  if ! LOG_PATH="$LOG_FILE" RECEIPT_ID="$MARK_CONSUMED_ID" RECEIPT_CALLER="$CALLER" "$NODE_BIN" -e '
const fs = require("fs");
try {
  const found = fs.readFileSync(process.env.LOG_PATH, "utf8").split("\n").some((line) => {
    if (!line) return false;
    try {
      const event = JSON.parse(line);
      return (event.event_type || "attempt") === "attempt" && event.event_id === process.env.RECEIPT_ID && event.caller === process.env.RECEIPT_CALLER && event.outcome === "success";
    } catch (_) { return false; }
  });
  process.exit(found ? 0 : 1);
} catch (_) { process.exit(1); }'; then
    echo "OLLAMA_FALLBACK: consumption receipt does not identify a matching successful attempt." >&2
    exit 2
  fi
  EVENT_ID="$MARK_CONSUMED_ID"
  if log_event consumption "$MARK_CONSUMED_ID" consumed "" 0 0; then
    exit 0
  fi
  exit 2
fi
if [ -n "$RECORD_RESULT_ID" ] || [ -n "$RESULT_OUTCOME" ]; then
  if [ -z "$RECORD_RESULT_ID" ] || [ -z "$RESULT_OUTCOME" ]; then
    usage_error "--record-result and --result-outcome must be supplied together"
  fi
  if [ -n "$PROMPT_FILE" ] || [ -n "$RECEIPT_FILE" ] || [ "$REQUIRED_MARKER_COUNT" -ne 0 ] || [ -n "$MARK_CONSUMED_ID" ]; then
    usage_error "result lifecycle recording cannot be combined with generation options"
  fi
  if ! [[ "$RECORD_RESULT_ID" =~ ^[0-9a-f]{32}$ ]]; then
    usage_error "--record-result requires a 32-character lowercase hexadecimal attempt ID"
  fi
  case "$RESULT_OUTCOME" in
    delivered|verified_used|verified_rejected|superseded) ;;
    *) usage_error "--result-outcome must be delivered, verified_used, verified_rejected, or superseded" ;;
  esac
  RESULT_LOG="$LOG_FILE" RESULT_ID="$RECORD_RESULT_ID" RESULT_CALLER="$CALLER" RESULT_VALUE="$RESULT_OUTCOME" "$NODE_BIN" <<'NODE'
const fs = require("fs");
let events;
try {
  events = fs.readFileSync(process.env.RESULT_LOG, "utf8").split("\n").filter(Boolean).map(JSON.parse);
} catch (_) { process.exit(2); }
const matches = (event) => event.event_id === process.env.RESULT_ID && event.caller === process.env.RESULT_CALLER;
if (!events.some((event) => matches(event) && (event.event_type || "attempt") === "attempt" && event.outcome === "success")) process.exit(2);
const lifecycle = events.filter((event) => matches(event) && event.event_type === "result").map((event) => event.outcome);
if (lifecycle.includes(process.env.RESULT_VALUE)) process.exit(0);
const terminal = lifecycle.find((value) => ["verified_used", "verified_rejected", "superseded"].includes(value));
if (terminal || (process.env.RESULT_VALUE !== "delivered" && !lifecycle.includes("delivered"))) process.exit(2);
NODE
  RESULT_STATUS=$?
  if [ "$RESULT_STATUS" -eq 0 ]; then
    # An identical lifecycle event is idempotent. If it does not yet exist, append it.
    if RESULT_LOG="$LOG_FILE" RESULT_ID="$RECORD_RESULT_ID" RESULT_CALLER="$CALLER" RESULT_VALUE="$RESULT_OUTCOME" "$NODE_BIN" -e '
const fs=require("fs");
const found=fs.readFileSync(process.env.RESULT_LOG,"utf8").split("\n").filter(Boolean).some((line)=>{try{const e=JSON.parse(line);return e.event_type==="result"&&e.event_id===process.env.RESULT_ID&&e.caller===process.env.RESULT_CALLER&&e.outcome===process.env.RESULT_VALUE;}catch(_){return false;}});
process.exit(found?0:1);'; then
      exit 0
    fi
    EVENT_ID="$RECORD_RESULT_ID"
    log_event result "$RECORD_RESULT_ID" "$RESULT_OUTCOME" "" 0 0 && exit 0
  fi
  echo "OLLAMA_FALLBACK: result lifecycle transition is invalid or does not match a successful attempt." >&2
  exit 2
fi
if [ "$WARM" -eq 1 ] && { [ -n "$PROMPT_FILE" ] || [ -n "$RECEIPT_FILE" ] || [ "$REQUIRED_MARKER_COUNT" -ne 0 ]; }; then
  usage_error "--warm cannot be combined with generation options"
fi

if [ "${OLLAMA_DELEGATE_DISABLE:-0}" != "0" ] && [ "${OLLAMA_DELEGATE_DISABLE:-0}" != "1" ]; then
  usage_error "OLLAMA_DELEGATE_DISABLE must be 0 or 1"
fi
if [ "${OLLAMA_DELEGATE_DISABLE:-0}" = "1" ]; then
  echo "OLLAMA_FALLBACK: local delegation is disabled." >&2
  finish 2 unavailable "disabled" 0 0
fi
if [ "$WARM" -eq 0 ] && { [ -z "$PROMPT_FILE" ] || [ ! -f "$PROMPT_FILE" ]; }; then
  usage_error "--prompt-file <path> is required and must exist"
fi
if [ "$MODEL" != "$PINNED_MODEL" ]; then
  usage_error "--model is pinned to $PINNED_MODEL"
fi
if ! [[ "$MIN_CHARS" =~ ^[1-9][0-9]*$ ]]; then
  usage_error "--min-chars must be a positive integer"
fi
if ! [[ "$TIMEOUT_SEC" =~ ^[1-9][0-9]*$ ]] || [ "$TIMEOUT_SEC" -gt "$MAX_REQUEST_SECONDS" ]; then
  usage_error "--timeout must be a positive integer no greater than $MAX_REQUEST_SECONDS"
fi
if [ -n "$MAX_OUTPUT_TOKENS" ] && { ! [[ "$MAX_OUTPUT_TOKENS" =~ ^[1-9][0-9]*$ ]] || [ "$MAX_OUTPUT_TOKENS" -gt 2048 ]; }; then
  usage_error "--max-output-tokens must be a positive integer no greater than 2048"
fi
for ((marker_index = 0; marker_index < REQUIRED_MARKER_COUNT; marker_index++)); do
  marker="${REQUIRED_MARKERS[$marker_index]}"
  if [ -z "$marker" ]; then
    usage_error "--require-marker must not be empty"
  fi
done

own_pid() {
  local output_name="$1" candidate
  candidate="$(sh -c 'echo $PPID')"
  if [ -n "$candidate" ] && kill -0 "$candidate" 2>/dev/null; then
    printf -v "$output_name" '%s' "$candidate"
    return 0
  fi
  printf -v "$output_name" '%s' ""
  return 1
}

lock_mtime() {
  local path="${1:-$LOCK_DIR}" value
  value="$(stat -f '%m' "$path" 2>/dev/null)"
  if [[ "$value" =~ ^[0-9]+$ ]]; then
    printf '%s\n' "$value"
    return 0
  fi
  value="$(stat -c '%Y' "$path" 2>/dev/null)"
  if [[ "$value" =~ ^[0-9]+$ ]]; then
    printf '%s\n' "$value"
    return 0
  fi
  return 1
}

new_lock_nonce() {
  /usr/bin/openssl rand -hex 16 2>/dev/null
}

write_lock_identity() {
  local pid="$1" nonce="$2"
  printf '%s\n' "$pid" > "$LOCK_DIR/owner-pid" \
    && printf '%s\n' "$nonce" > "$LOCK_DIR/owner-nonce" \
    && [ "$(cat "$LOCK_DIR/owner-pid" 2>/dev/null)" = "$pid" ] \
    && [ "$(cat "$LOCK_DIR/owner-nonce" 2>/dev/null)" = "$nonce" ]
}

acquire_lock() {
  local pid nonce owner owner_nonce created now age reclaim_dir recheck_owner recheck_nonce
  local guard_created guard_recheck guard_age reclaim_held
  if ! mkdir -p "$(dirname "$LOCK_DIR")" 2>/dev/null; then
    echo "OLLAMA_FALLBACK: could not create the lock directory's parent." >&2
    finish 2 lock_identity_error "lock_parent_dir_unavailable" 0 0
  fi
  own_pid pid || true
  if [ -z "$pid" ]; then
    echo "OLLAMA_FALLBACK: could not identify the direct caller process." >&2
    finish 2 lock_identity_error "caller_pid_unavailable" 0 0
  fi
  nonce="$(new_lock_nonce)"
  if ! [[ "$nonce" =~ ^[0-9a-f]{32}$ ]]; then
    echo "OLLAMA_FALLBACK: could not create a lock ownership nonce." >&2
    finish 2 lock_identity_error "owner_nonce_unavailable" 0 0
  fi

  if mkdir "$LOCK_DIR" 2>/dev/null; then
    if ! write_lock_identity "$pid" "$nonce"; then
      rm -f "$LOCK_DIR/owner-pid" "$LOCK_DIR/owner-nonce" 2>/dev/null || true
      rmdir "$LOCK_DIR" 2>/dev/null || true
      echo "OLLAMA_FALLBACK: could not record lock ownership." >&2
      finish 2 lock_identity_error "owner_pid_write_failed" 0 0
    fi
    LOCK_PID="$pid"
    LOCK_NONCE="$nonce"
    LOCK_HELD=1
    return 0
  fi

  owner="$(cat "$LOCK_DIR/owner-pid" 2>/dev/null || true)"
  owner_nonce="$(cat "$LOCK_DIR/owner-nonce" 2>/dev/null || true)"
  created="$(lock_mtime)" || created=""
  now="$(date +%s)"
  age=0
  if [ -n "$created" ] && [ "$now" -ge "$created" ]; then
    age=$((now - created))
  fi

  if { [ -n "$owner" ] && ! kill -0 "$owner" 2>/dev/null; } || { [ -n "$created" ] && [ "$age" -gt "$MAX_LOCK_SECONDS" ]; }; then
    reclaim_dir="${LOCK_DIR}.reclaim"
    reclaim_held=0
    if mkdir "$reclaim_dir" 2>/dev/null; then
      reclaim_held=1
    else
      guard_created="$(lock_mtime "$reclaim_dir")" || guard_created=""
      guard_age=0
      if [ -n "$guard_created" ] && [ "$now" -ge "$guard_created" ]; then
        guard_age=$((now - guard_created))
      fi
      if [ -n "$guard_created" ] && [ "$guard_age" -gt "$MAX_LOCK_SECONDS" ]; then
        guard_recheck="$(lock_mtime "$reclaim_dir")" || guard_recheck=""
        if [ "$guard_recheck" = "$guard_created" ]; then
          echo "ollama-delegate: WARNING: removing abandoned reclaim guard older than ${MAX_LOCK_SECONDS}s." >&2
          rmdir "$reclaim_dir" 2>/dev/null || true
        fi
      fi
      if mkdir "$reclaim_dir" 2>/dev/null; then
        reclaim_held=1
      fi
    fi
    if [ "$reclaim_held" -eq 1 ]; then
      recheck_owner="$(cat "$LOCK_DIR/owner-pid" 2>/dev/null || true)"
      recheck_nonce="$(cat "$LOCK_DIR/owner-nonce" 2>/dev/null || true)"
      if [ "$recheck_owner" = "$owner" ] && [ "$recheck_nonce" = "$owner_nonce" ]; then
        if [ -n "$created" ] && [ "$age" -gt "$MAX_LOCK_SECONDS" ]; then
          echo "ollama-delegate: WARNING: reclaiming lock older than ${MAX_LOCK_SECONDS}s (owner pid ${owner:-missing}, age ${age}s)." >&2
        else
          echo "ollama-delegate: reclaiming stale lock owned by dead pid ${owner:-missing}." >&2
        fi
        rm -f "$LOCK_DIR/owner-pid" "$LOCK_DIR/owner-nonce" 2>/dev/null || true
        rmdir "$LOCK_DIR" 2>/dev/null || true
      fi
      rmdir "$reclaim_dir" 2>/dev/null || true
      if mkdir "$LOCK_DIR" 2>/dev/null; then
        if write_lock_identity "$pid" "$nonce"; then
          LOCK_PID="$pid"
          LOCK_NONCE="$nonce"
          LOCK_HELD=1
          return 0
        fi
        rm -f "$LOCK_DIR/owner-pid" "$LOCK_DIR/owner-nonce" 2>/dev/null || true
        rmdir "$LOCK_DIR" 2>/dev/null || true
        echo "OLLAMA_FALLBACK: could not record lock ownership after reclaim." >&2
        finish 2 lock_identity_error "reclaimed_owner_pid_write_failed" 0 0
      fi
    fi
  fi

  echo "OLLAMA_FALLBACK: local model is busy; falling back immediately." >&2
  finish 2 busy "lock_contended" 0 0
}

acquire_lock

TMP_PS="$(mktemp)" || finish 2 error "mktemp_failed" 0 0
TMP_REQUEST="$(mktemp)" || finish 2 error "mktemp_failed" 0 0
TMP_RESPONSE="$(mktemp)" || finish 2 error "mktemp_failed" 0 0

if [ "$WARM" -eq 1 ]; then
  MODEL_TAG="$MODEL" REQUEST_PATH="$TMP_REQUEST" "$NODE_BIN" -e '
const fs = require("fs");
fs.writeFileSync(process.env.REQUEST_PATH, JSON.stringify({model: process.env.MODEL_TAG, prompt: "", stream: false, think: false, keep_alive: "8h"}));'
  WARM_CURL_STATUS=0
  WARM_HTTP_STATUS="$(curl -sS --connect-timeout 2 --max-time "$TIMEOUT_SEC" -o "$TMP_RESPONSE" -w '%{http_code}' -H 'Content-Type: application/json' --data-binary "@$TMP_REQUEST" "$OLLAMA_URL/api/generate" 2>/dev/null)" || WARM_CURL_STATUS=$?
  EVENT_ID="$(new_event_id)"
  if [ "$WARM_CURL_STATUS" -eq 0 ] && [ "$WARM_HTTP_STATUS" = "200" ]; then
    if log_event maintenance "$EVENT_ID" warmed "model_loaded" 0 0; then
      printf 'Ollama model %s is warm for eight hours.\n' "$MODEL"
      exit 0
    fi
  fi
  log_event maintenance "$EVENT_ID" warm_failed "curl_${WARM_CURL_STATUS}_http_${WARM_HTTP_STATUS:-none}" 0 0 || true
  echo "OLLAMA_FALLBACK: could not warm the pinned model." >&2
  exit 2
fi

PS_STATUS="$(curl -sS --connect-timeout 2 --max-time 5 -o "$TMP_PS" -w '%{http_code}' "$OLLAMA_URL/api/ps" 2>/dev/null)"
if [ "$PS_STATUS" != "200" ]; then
  echo "OLLAMA_FALLBACK: Ollama is unavailable (preflight HTTP ${PS_STATUS:-none})." >&2
  finish 2 unavailable "preflight_http_${PS_STATUS:-none}" 0 0
fi

MODEL_CONTEXT="$(PS_FILE="$TMP_PS" MODEL_TAG="$MODEL" "$NODE_BIN" -e '
const fs = require("fs");
try {
  const payload = JSON.parse(fs.readFileSync(process.env.PS_FILE, "utf8"));
  const item = Array.isArray(payload.models) && payload.models.find((entry) => entry && (entry.name === process.env.MODEL_TAG || entry.model === process.env.MODEL_TAG));
  if (item && Number.isInteger(item.context_length) && item.context_length > 0) process.stdout.write(String(item.context_length));
} catch (_) {}'
)"
if ! [[ "$MODEL_CONTEXT" =~ ^[1-9][0-9]*$ ]]; then
  echo "OLLAMA_FALLBACK: pinned model '$MODEL' is not loaded with a usable context length." >&2
  finish 2 unavailable "model_not_loaded" 0 0
fi

PROMPT_BYTES="$(PROMPT_PATH="$PROMPT_FILE" "$NODE_BIN" -e '
const fs = require("fs");
try {
  const raw = fs.readFileSync(process.env.PROMPT_PATH);
  const decoded = new TextDecoder("utf-8", {fatal: true}).decode(raw);
  void decoded;
  process.stdout.write(String(raw.length));
} catch (_) { process.exit(1); }'
)" || usage_error "prompt file must be readable UTF-8 text"
# Ollama exposes no tokenizer-only endpoint. One token per UTF-8 byte is a
# deliberately conservative upper bound even for byte-fallback tokenizers,
# dense punctuation, hashes, and non-Latin text.
ESTIMATED_PROMPT_TOKENS="$PROMPT_BYTES"
PROMPT_TOKEN_CAP=$(( MODEL_CONTEXT * 70 / 100 ))
if [ "$ESTIMATED_PROMPT_TOKENS" -gt "$PROMPT_TOKEN_CAP" ]; then
  echo "OLLAMA_FALLBACK: prompt estimate ${ESTIMATED_PROMPT_TOKENS} exceeds the safe ${PROMPT_TOKEN_CAP}-token cap." >&2
  finish 2 prompt_too_large "estimated_prompt_exceeds_70_percent_context" "$ESTIMATED_PROMPT_TOKENS" 0
fi

PROMPT_PATH="$PROMPT_FILE" MODEL_TAG="$MODEL" REQUEST_PATH="$TMP_REQUEST" MAX_OUTPUT_TOKENS_VALUE="$MAX_OUTPUT_TOKENS" "$NODE_BIN" -e '
const fs = require("fs");
const request = {model: process.env.MODEL_TAG, prompt: fs.readFileSync(process.env.PROMPT_PATH, "utf8"), stream: false, think: false};
if (process.env.MAX_OUTPUT_TOKENS_VALUE) request.options = {num_predict: Number(process.env.MAX_OUTPUT_TOKENS_VALUE)};
fs.writeFileSync(process.env.REQUEST_PATH, JSON.stringify(request));'

CURL_STATUS=0
HTTP_STATUS="$(curl -sS --connect-timeout 2 --max-time "$TIMEOUT_SEC" -o "$TMP_RESPONSE" -w '%{http_code}' -H 'Content-Type: application/json' --data-binary "@$TMP_REQUEST" "$OLLAMA_URL/api/generate" 2>/dev/null)" || CURL_STATUS=$?
if [ "$CURL_STATUS" -eq 28 ]; then
  echo "OLLAMA_FALLBACK: local model timed out after ${TIMEOUT_SEC}s." >&2
  finish 2 timeout "request_timeout" "$ESTIMATED_PROMPT_TOKENS" 0
fi
if [ "$CURL_STATUS" -ne 0 ] || [ "$HTTP_STATUS" != "200" ]; then
  echo "OLLAMA_FALLBACK: local model request failed (curl=$CURL_STATUS HTTP=${HTTP_STATUS:-none})." >&2
  finish 2 error "request_failed_curl_${CURL_STATUS}_http_${HTTP_STATUS:-none}" "$ESTIMATED_PROMPT_TOKENS" 0
fi

PARSED="$(RESPONSE_PATH="$TMP_RESPONSE" "$NODE_BIN" -e '
const fs = require("fs");
try {
  const payload = JSON.parse(fs.readFileSync(process.env.RESPONSE_PATH, "utf8"));
  const validInt = (value) => Number.isInteger(value) && value >= 0;
  if (typeof payload.response !== "string" || payload.response.includes("\0") || !validInt(payload.prompt_eval_count) || !validInt(payload.eval_count) || typeof payload.done !== "boolean" || typeof payload.done_reason !== "string") throw new Error("shape");
  for (const field of ["load_duration", "prompt_eval_duration", "eval_duration"]) if (!validInt(payload[field])) payload[field] = 0;
  process.stdout.write([Buffer.from(payload.response).toString("base64"), payload.prompt_eval_count, payload.eval_count, payload.done ? "1" : "0", Buffer.from(payload.done_reason).toString("base64"), payload.load_duration, payload.prompt_eval_duration, payload.eval_duration].join("\n"));
} catch (_) { process.exit(1); }'
)" || {
  echo "OLLAMA_FALLBACK: local model returned malformed JSON." >&2
  finish 2 error "malformed_response" "$ESTIMATED_PROMPT_TOKENS" 0
}

OUTPUT_B64="$(printf '%s\n' "$PARSED" | sed -n '1p')"
PROMPT_TOKENS="$(printf '%s\n' "$PARSED" | sed -n '2p')"
COMPLETION_TOKENS="$(printf '%s\n' "$PARSED" | sed -n '3p')"
RESPONSE_DONE="$(printf '%s\n' "$PARSED" | sed -n '4p')"
DONE_REASON_B64="$(printf '%s\n' "$PARSED" | sed -n '5p')"
DONE_REASON="$(printf '%s' "$DONE_REASON_B64" | base64 --decode 2>/dev/null || printf '%s' "$DONE_REASON_B64" | base64 -D 2>/dev/null)"
LOAD_DURATION_NS="$(printf '%s\n' "$PARSED" | sed -n '6p')"
PROMPT_EVAL_DURATION_NS="$(printf '%s\n' "$PARSED" | sed -n '7p')"
EVAL_DURATION_NS="$(printf '%s\n' "$PARSED" | sed -n '8p')"
OUTPUT="$(printf '%s' "$OUTPUT_B64" | base64 --decode 2>/dev/null || printf '%s' "$OUTPUT_B64" | base64 -D 2>/dev/null)"
OUTPUT_LENGTH="$(printf '%s' "$OUTPUT" | "$NODE_BIN" -e 'let text=""; process.stdin.setEncoding("utf8"); process.stdin.on("data", chunk => text += chunk); process.stdin.on("end", () => process.stdout.write(String(text.trim().length)));')"

if [ "$RESPONSE_DONE" != "1" ] || [ "$DONE_REASON" != "stop" ]; then
  echo "OLLAMA_FALLBACK: local model response did not finish with a normal stop (done=$RESPONSE_DONE reason=${DONE_REASON:-missing})." >&2
  finish 2 invalid_output "incomplete_response_${DONE_REASON:-not_done}" "$PROMPT_TOKENS" "$COMPLETION_TOKENS"
fi

if [ "$OUTPUT_LENGTH" -lt "$MIN_CHARS" ]; then
  echo "OLLAMA_FALLBACK: local model output was too short (${OUTPUT_LENGTH} chars; minimum ${MIN_CHARS})." >&2
  finish 2 invalid_output "output_too_short" "$PROMPT_TOKENS" "$COMPLETION_TOKENS"
fi
for ((marker_index = 0; marker_index < REQUIRED_MARKER_COUNT; marker_index++)); do
  marker="${REQUIRED_MARKERS[$marker_index]}"
  if ! printf '%s\n' "$OUTPUT" | grep -Fxq -- "$marker"; then
    echo "OLLAMA_FALLBACK: local model output is missing required marker '$marker'." >&2
    finish 2 invalid_output "missing_required_marker" "$PROMPT_TOKENS" "$COMPLETION_TOKENS"
  fi
done

EVENT_ID="$(new_event_id)"
if ! [[ "$EVENT_ID" =~ ^[0-9a-f]{32}$ ]]; then
  echo "OLLAMA_FALLBACK: could not create an event receipt ID." >&2
  exit 2
fi
if ! log_event attempt "$EVENT_ID" success "" "$PROMPT_TOKENS" "$COMPLETION_TOKENS" "$LOAD_DURATION_NS" "$PROMPT_EVAL_DURATION_NS" "$EVAL_DURATION_NS"; then
  echo "OLLAMA_FALLBACK: validated output could not be recorded; refusing an unmeasured success." >&2
  exit 2
fi
if [ -n "$RECEIPT_FILE" ]; then
  if ! printf '%s\n' "$EVENT_ID" > "$RECEIPT_FILE" 2>/dev/null; then
    echo "OLLAMA_FALLBACK: validated output receipt could not be recorded." >&2
    exit 2
  fi
fi
printf '%s\n' "$OUTPUT"
exit 0
