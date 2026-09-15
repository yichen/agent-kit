#!/usr/bin/env bash
# Generic bounded composition chain: Ollama, then caller fallback.
#
# Shared by any call site that wants the same shape as pr-compose-delegates.sh
# (attempt local Ollama first, validate its output, fall back to the calling
# agent otherwise) without owning its own template/bullets assembly. The
# caller builds the full prompt text itself and passes it via --prompt-file.
#
# Exit codes:
#   0: validated model output was copied to --output-file
#   1: invalid usage
#   2: CLAUDE_COMPOSE_REQUIRED — caller must compose the text itself
#
# Usage:
#   ollama-compose-chain.sh --repo-root <path> \
#     --prompt-file <path> --output-file <path> --caller <name> \
#     [--require-marker <text>]... [--min-chars <count>] [--timeout <seconds>] \
#     [--max-first-line-chars <count>]

set -u

OLLAMA_TIMEOUT_SECONDS=60
MIN_CHARS=80
REPO_ROOT=""
PROMPT_FILE=""
OUTPUT_FILE=""
CALLER="unknown"
MAX_FIRST_LINE_CHARS=0
REQUIRE_MARKER_ARGS=()

while [ $# -gt 0 ]; do
  case "$1" in
    --repo-root)
      [ $# -ge 2 ] || { echo "ollama-compose-chain: --repo-root requires a value" >&2; exit 1; }
      REPO_ROOT="$2"; shift 2 ;;
    --prompt-file)
      [ $# -ge 2 ] || { echo "ollama-compose-chain: --prompt-file requires a value" >&2; exit 1; }
      PROMPT_FILE="$2"; shift 2 ;;
    --output-file)
      [ $# -ge 2 ] || { echo "ollama-compose-chain: --output-file requires a value" >&2; exit 1; }
      OUTPUT_FILE="$2"; shift 2 ;;
    --caller)
      [ $# -ge 2 ] || { echo "ollama-compose-chain: --caller requires a value" >&2; exit 1; }
      CALLER="$2"; shift 2 ;;
    --require-marker)
      [ $# -ge 2 ] || { echo "ollama-compose-chain: --require-marker requires a value" >&2; exit 1; }
      REQUIRE_MARKER_ARGS+=(--require-marker "$2"); shift 2 ;;
    --min-chars)
      [ $# -ge 2 ] || { echo "ollama-compose-chain: --min-chars requires a value" >&2; exit 1; }
      MIN_CHARS="$2"; shift 2 ;;
    --timeout)
      [ $# -ge 2 ] || { echo "ollama-compose-chain: --timeout requires a value" >&2; exit 1; }
      OLLAMA_TIMEOUT_SECONDS="$2"; shift 2 ;;
    --max-first-line-chars)
      [ $# -ge 2 ] || { echo "ollama-compose-chain: --max-first-line-chars requires a value" >&2; exit 1; }
      MAX_FIRST_LINE_CHARS="$2"; shift 2 ;;
    *) echo "ollama-compose-chain: unknown arg: $1" >&2; exit 1 ;;
  esac
done

if [ -z "$REPO_ROOT" ] || [ ! -d "$REPO_ROOT" ] || [ -z "$PROMPT_FILE" ] || [ ! -f "$PROMPT_FILE" ] || [ -z "$OUTPUT_FILE" ]; then
  echo "usage: $0 --repo-root <path> --prompt-file <path> --output-file <path> --caller <name> [--require-marker <text>]... [--min-chars <count>] [--timeout <seconds>] [--max-first-line-chars <count>]" >&2
  exit 1
fi
if ! [[ "$MAX_FIRST_LINE_CHARS" =~ ^[0-9]+$ ]]; then
  echo "ollama-compose-chain: --max-first-line-chars must be a non-negative integer" >&2
  exit 1
fi

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
OLLAMA_DELEGATE="${QWEN_DELEGATE_HELPER:-$SCRIPT_DIR/ollama-delegate.sh}"
OLLAMA_OUT="$(mktemp "${TMPDIR:-/tmp}/ollama-compose-out.XXXXXX")" || exit 1
OLLAMA_ERR="$(mktemp "${TMPDIR:-/tmp}/ollama-compose-err.XXXXXX")" || exit 1
OLLAMA_RECEIPT="$(mktemp "${TMPDIR:-/tmp}/ollama-compose-receipt.XXXXXX")" || exit 1

# shellcheck disable=SC2329 # Invoked indirectly by the EXIT trap.
cleanup() {
  rm -f "$OLLAMA_OUT" "$OLLAMA_ERR" "$OLLAMA_RECEIPT"
}
trap cleanup EXIT
rm -f "$OUTPUT_FILE"

if [ -n "${CLAUDE_COMPOSE:-}" ]; then
  echo "CLAUDE_COMPOSE_REQUIRED: delegate chain disabled for caller '$CALLER'." >&2
  exit 2
fi

if [ -x "$OLLAMA_DELEGATE" ]; then
  "$OLLAMA_DELEGATE" --prompt-file "$PROMPT_FILE" --caller "$CALLER" \
    ${REQUIRE_MARKER_ARGS[@]+"${REQUIRE_MARKER_ARGS[@]}"} \
    --min-chars "$MIN_CHARS" --timeout "$OLLAMA_TIMEOUT_SECONDS" \
    --receipt-file "$OLLAMA_RECEIPT" > "$OLLAMA_OUT" 2> "$OLLAMA_ERR"
  OLLAMA_STATUS=$?
  if [ "$OLLAMA_STATUS" -eq 0 ]; then
    FIRST_LINE_LENGTH="$(awk 'NR == 1 { print length; exit }' "$OLLAMA_OUT")"
    if [ "$MAX_FIRST_LINE_CHARS" -gt 0 ] && { [ "${FIRST_LINE_LENGTH:-0}" -eq 0 ] || [ "$FIRST_LINE_LENGTH" -gt "$MAX_FIRST_LINE_CHARS" ]; }; then
      echo "ollama-compose-chain: Ollama output first line must contain 1-${MAX_FIRST_LINE_CHARS} characters" >&2
    elif cp "$OLLAMA_OUT" "$OUTPUT_FILE"; then
      RECEIPT_ID="$(tr -d '\r\n' < "$OLLAMA_RECEIPT")"
      if "$OLLAMA_DELEGATE" --mark-consumed "$RECEIPT_ID" --caller "$CALLER" >/dev/null 2>> "$OLLAMA_ERR"; then
        exit 0
      fi
      rm -f "$OUTPUT_FILE"
      echo "ollama-compose-chain: could not record consumed Ollama output for caller '$CALLER'" >&2
    fi
    echo "ollama-compose-chain: could not publish Ollama output for caller '$CALLER'" >&2
  fi
fi

echo "CLAUDE_COMPOSE_REQUIRED: local delegate did not produce usable output for caller '$CALLER'." >&2
exit 2
