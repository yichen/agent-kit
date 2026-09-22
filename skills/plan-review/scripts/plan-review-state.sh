#!/usr/bin/env bash
# Working state and stopping rules for a plan review run.
# The gate subcommand is the authoritative stop decision; callers must obey it.

set -euo pipefail

ROUND_CAP="${PLAN_REVIEW_ROUND_CAP:-3}"
GROWTH_PERCENT="${PLAN_REVIEW_GROWTH_PERCENT:-50}"
# Growth must clear both the percentage and this absolute floor. Percentage alone
# fires on any small plan that gains a sentence, which stops a review that was
# being corrected rather than expanded. The floor is set below the two measured
# cases of real expansion: a plan that grew 1,633 bytes over nine rounds, and one
# that grew 9,934 bytes over four.
GROWTH_MIN_BYTES="${PLAN_REVIEW_GROWTH_MIN_BYTES:-1000}"
WORKROOT="${PLAN_REVIEW_WORKROOT:-/tmp}"

die() { echo "plan-review: $*" >&2; exit 2; }

file_bytes() {
  wc -c < "$1" | tr -d ' '
}

state_get() {
  local workdir="$1" key="$2"
  sed -n "s/^${key}=//p" "$workdir/state" | head -1
}

state_set() {
  local workdir="$1" key="$2" value="$3"
  local tmp="$workdir/state.tmp"
  grep -v "^${key}=" "$workdir/state" > "$tmp" 2>/dev/null || true
  echo "${key}=${value}" >> "$tmp"
  mv "$tmp" "$workdir/state"
}

default_head() {
  local repo="$1" ref
  ref="$(git -C "$repo" symbolic-ref --quiet refs/remotes/origin/HEAD 2>/dev/null || true)"
  if [ -n "$ref" ]; then
    git -C "$repo" rev-parse "$ref" 2>/dev/null && return 0
  fi
  git -C "$repo" rev-parse HEAD 2>/dev/null
}

cmd_init() {
  local plan="" repo="" base=""
  plan="${1:-}"; shift || true
  [ -n "$plan" ] || die "init needs a plan path"
  [ -f "$plan" ] || die "plan file not found: $plan"
  plan="$(cd "$(dirname "$plan")" && pwd)/$(basename "$plan")"

  while [ $# -gt 0 ]; do
    case "$1" in
      --repo) repo="${2:-}"; shift 2 ;;
      --base) base="${2:-}"; shift 2 ;;
      *) die "unknown init option: $1" ;;
    esac
  done

  [ -n "$repo" ] || repo="$PWD"
  if ! git -C "$repo" rev-parse --git-dir >/dev/null 2>&1; then
    repo="-"
  else
    repo="$(git -C "$repo" rev-parse --show-toplevel)"
  fi

  if [ -z "$base" ]; then
    if [ "$repo" = "-" ]; then base="-"; else base="$(default_head "$repo" || echo -)"; fi
  fi

  local slug workdir
  slug="$(basename "$plan" .md | tr -c 'a-zA-Z0-9-' '-' | sed 's/-\{2,\}/-/g; s/^-//; s/-$//')"
  workdir="$WORKROOT/plan-review-$slug"
  mkdir -p "$workdir"

  cat > "$workdir/state" <<EOF
plan=$plan
repo=$repo
base=$base
round=0
bytes_round1=$(file_bytes "$plan")
started=$(date -u +%Y-%m-%dT%H:%M:%SZ)
EOF

  if [ ! -f "$workdir/log.md" ]; then
    {
      echo "# Plan review log"
      echo
      echo "Plan: $plan"
      echo "Repository: $repo"
      echo "Base commit: $base"
      echo "Started: $(date -u +%Y-%m-%dT%H:%M:%SZ)"
      echo
      echo "Reviewers do not read this file. The coordinator reads it once per round,"
      echo "to sort each new finding into new, a repeat, or a reversal."
      echo
    } > "$workdir/log.md"
  fi

  echo "$workdir"
}

cmd_round_start() {
  local workdir="${1:-}"
  [ -d "$workdir" ] || die "workdir not found: $workdir"
  local round
  round=$(( $(state_get "$workdir" round) + 1 ))
  state_set "$workdir" round "$round"
  {
    echo "## Round $round"
    echo
    echo "Started: $(date -u +%Y-%m-%dT%H:%M:%SZ)"
    echo "Plan size: $(file_bytes "$(state_get "$workdir" plan)") bytes"
    echo
  } >> "$workdir/log.md"
  echo "$round"
}

cmd_record() {
  local workdir="${1:-}"; shift || true
  [ -d "$workdir" ] || die "workdir not found: $workdir"
  local brief="" verdict="" finding="" edit="" check=""
  while [ $# -gt 0 ]; do
    case "$1" in
      --brief) brief="${2:-}"; shift 2 ;;
      --verdict) verdict="${2:-}"; shift 2 ;;
      --finding) finding="${2:-}"; shift 2 ;;
      --edit) edit="${2:-}"; shift 2 ;;
      --check) check="${2:-}"; shift 2 ;;
      *) die "unknown record option: $1" ;;
    esac
  done
  [ -n "$brief" ] || die "record needs --brief"
  [ -n "$finding" ] || die "record needs --finding"
  case "$brief" in
    claims|consequences|deletion) ;;
    *) die "unknown brief: $brief (expected claims, consequences, or deletion)" ;;
  esac
  [ -n "$verdict" ] || verdict="blocking"
  case "$verdict" in
    blocking|noted) ;;
    *) die "unknown verdict: $verdict (expected blocking or noted)" ;;
  esac
  [ -n "$check" ] || check="NONE"

  {
    echo "- brief: $brief"
    echo "  verdict: $verdict"
    echo "  finding: $finding"
    echo "  edit: ${edit:-none}"
    echo "  check: $check"
  } >> "$workdir/log.md"
}

cmd_gate() {
  local workdir="${1:-}"; shift || true
  [ -d "$workdir" ] || die "workdir not found: $workdir"
  local blocking="" ready=""
  while [ $# -gt 0 ]; do
    case "$1" in
      --blocking) blocking="${2:-}"; shift 2 ;;
      --ready) ready="${2:-}"; shift 2 ;;
      *) die "unknown gate option: $1" ;;
    esac
  done
  [ -n "$blocking" ] || die "gate needs --blocking <count>"
  [ -n "$ready" ] || die "gate needs --ready <pass|fail>, from plan-review-readiness.sh check"
  case "$ready" in
    pass|fail) ;;
    *) die "gate --ready must be pass or fail, got: $ready" ;;
  esac
  case "$blocking" in
    ''|*[!0-9]*) die "gate --blocking must be a number, got: $blocking" ;;
  esac

  local plan repo base round bytes1 bytes_now
  plan="$(state_get "$workdir" plan)"
  repo="$(state_get "$workdir" repo)"
  base="$(state_get "$workdir" base)"
  round="$(state_get "$workdir" round)"
  bytes1="$(state_get "$workdir" bytes_round1)"
  [ -f "$plan" ] || die "plan file disappeared: $plan"
  bytes_now="$(file_bytes "$plan")"

  local decision=""

  # No blocking finding means no defect was found. It does not mean the plan is
  # ready to implement. A plan can be true, consistent, and still be missing the
  # scope, acceptance criteria and verification a run needs, in which case the
  # run re-plans from scratch and this review bought nothing.
  if [ "$blocking" -eq 0 ] && [ "$ready" = "pass" ]; then
    decision="STOP approved no blocking finding in round $round and the plan is ready to implement"
  elif [ "$blocking" -eq 0 ] && [ "$ready" = "fail" ]; then
    decision="STOP not-ready no blocking finding in round $round but the plan is missing what an implementation run requires; see the readiness check in the log"
  fi

  if [ -z "$decision" ] && [ "$repo" != "-" ] && [ "$base" != "-" ]; then
    local head_now
    head_now="$(default_head "$repo" 2>/dev/null || echo "")"
    if [ -n "$head_now" ] && [ "$head_now" != "$base" ]; then
      decision="STOP base-moved pinned $base now $head_now"
    fi
  fi

  if [ -z "$decision" ] && [ "$bytes1" -gt 0 ]; then
    local limit grown
    limit=$(( bytes1 + (bytes1 * GROWTH_PERCENT / 100) ))
    grown=$(( bytes_now - bytes1 ))
    if [ "$bytes_now" -gt "$limit" ] && [ "$grown" -ge "$GROWTH_MIN_BYTES" ]; then
      decision="STOP plan-growth round1 $bytes1 bytes now $bytes_now bytes limit $limit bytes"
    fi
  fi

  if [ -z "$decision" ] && [ "$round" -ge "$ROUND_CAP" ]; then
    decision="STOP round-cap $round rounds completed with $blocking blocking findings open"
  fi

  if [ -z "$decision" ]; then
    echo "CONTINUE round $round closed with $blocking blocking findings"
    echo "gate: CONTINUE round $round blocking $blocking" >> "$workdir/log.md"
    return 0
  fi

  echo "$decision"
  echo "gate: $decision" >> "$workdir/log.md"
  return 10
}

cmd_status() {
  local workdir="${1:-}"
  [ -d "$workdir" ] || die "workdir not found: $workdir"
  cat "$workdir/state"
  echo "bytes_now=$(file_bytes "$(state_get "$workdir" plan)")"
}

main() {
  local sub="${1:-}"; shift || true
  case "$sub" in
    init) cmd_init "$@" ;;
    round-start) cmd_round_start "$@" ;;
    record) cmd_record "$@" ;;
    gate) cmd_gate "$@" ;;
    status) cmd_status "$@" ;;
    *) die "usage: $0 init <plan> [--repo <path>] [--base <sha>] | round-start <workdir> | record <workdir> --brief <b> --verdict <v> --finding <text> [--edit <text>] [--check <cmd>] | gate <workdir> --blocking <n> --ready <pass|fail> | status <workdir>" ;;
  esac
}

main "$@"
