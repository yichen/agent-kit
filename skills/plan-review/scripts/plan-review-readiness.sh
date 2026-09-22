#!/usr/bin/env bash
# Decide whether a reviewed plan is ready to hand to an implementation workflow.
#
# Finding no defects is not the same as being ready. A plan can be entirely true,
# internally consistent, and still be missing the scope, acceptance criteria, and
# verification an implementation workflow needs. This script checks for what must
# be present, which the three reviewer briefs never do.
#
# Read-only. It never writes to the plan or to the repository. The scaffold
# subcommand prints to stdout and writes nothing.

set -euo pipefail

die() { echo "plan-review-readiness: $*" >&2; exit 2; }


state_get() {
  sed -n "s/^${2}=//p" "$1/state" | head -1
}

extract_block() {
  # Prints the JSON inside the pre-reviewed plan block, or nothing.
  awk '
    /<!-- CODE-PRE-REVIEWED-PLAN:START -->/ { inblock=1; next }
    /<!-- CODE-PRE-REVIEWED-PLAN:END -->/   { inblock=0 }
    inblock { print }
  ' "$1" | sed '/^[[:space:]]*```/d'
}

cmd_check() {
  local workdir="${1:-}"
  [ -d "$workdir" ] || die "workdir not found: $workdir"

  local plan repo base
  plan="$(state_get "$workdir" plan)"
  repo="$(state_get "$workdir" repo)"
  base="$(state_get "$workdir" base)"
  [ -f "$plan" ] || die "plan file not found: $plan"

  local before_sha
  before_sha="$(shasum -a 256 "$plan" | cut -d' ' -f1)"

  local -a missing=()
  local route="not-checked"
  local block
  block="$(extract_block "$plan")"

  if [ -n "$block" ]; then
    local parsed
    if ! parsed="$(printf '%s' "$block" | python3 "$(dirname "$0")/plan-review-envelope.py" "$base" 2>&1)"; then
      missing+=("$parsed")
    fi
  else
    missing+=("no code-pre-reviewed-plan:v1 block, so an implementation run must re-derive scope and acceptance itself")

    # Without the block, look for the same facts in prose. This is a heuristic on
    # headings, so it can report a section missing that a human would recognise
    # under a different name. A false NOT-READY costs one round; a false READY
    # sends an unimplementable plan to a run that then re-plans from scratch, so
    # the heuristic is deliberately biased toward reporting.
    grep -qE '[A-Za-z0-9_./-]+\.[A-Za-z]+:[0-9]+' "$plan" \
      || missing+=("no file:line reference anywhere, so no anchor can be verified against current source")
    grep -qiE '^#+.*(acceptance|success criteria|done when|definition of done|pass criteria|confirmed means)' "$plan" \
      || missing+=("no acceptance criteria section, so there is no stated observable result")
    grep -qiE '^#+.*(verif|test|how to check|evidence)' "$plan" \
      || missing+=("no verification section, so there is no stated way to prove the work is correct")
    grep -qiE '^#+.*(scope|files|what changes|the change)' "$plan" \
      || missing+=("no scope section, so the set of files to change is undetermined")
    missing+=("the four prose checks above are heuristics on section headings; a code-pre-reviewed-plan:v1 block is the reliable way to answer them")
  fi

  # This check deliberately does not call a repository's own plan controller.
  # SharedAnchor's code-controller.mjs refuses to answer outside an active /code
  # lifecycle ("no host-owned ACTIVE /code lifecycle targets <repo>"), so a call
  # from here can never return a route, and a branch that always reports
  # "unreadable" is worse than no branch. The rules above mirror what that
  # controller requires. It performs its own routing when the plan reaches it.
  if [ -n "$block" ]; then
    route="structured-block"
  else
    route="prose"
  fi

  local after_sha
  after_sha="$(shasum -a 256 "$plan" | cut -d' ' -f1)"
  [ "$before_sha" = "$after_sha" ] || die "internal error: the readiness check modified the plan file"

  {
    echo "## Readiness check"
    echo
    echo "Plan: $plan"
    echo "Pinned base: $base"
    echo "Plan form: $route"
  } >> "$workdir/log.md"

  if [ "${#missing[@]}" -eq 0 ]; then
    echo "READY route=$route"
    echo "readiness: READY route=$route" >> "$workdir/log.md"
    return 0
  fi

  echo "NOT-READY route=$route"
  local m
  for m in "${missing[@]}"; do
    echo "  - $m"
    echo "readiness: missing - $m" >> "$workdir/log.md"
  done
  return 11
}

cmd_scaffold() {
  local workdir="${1:-}"
  [ -d "$workdir" ] || die "workdir not found: $workdir"

  local repo base origin name
  repo="$(state_get "$workdir" repo)"
  base="$(state_get "$workdir" base)"

  name="unknown/unknown"
  if [ "$repo" != "-" ]; then
    origin="$(git -C "$repo" config --get remote.origin.url 2>/dev/null || true)"
    case "$origin" in
      *github.com[:/]*) name="github.com/$(printf '%s' "$origin" | sed -E 's#.*github\.com[:/]##; s#\.git$##')" ;;
    esac
  fi

  # Only the derivable fields are filled. Scope, anchors, acceptance criteria and
  # verification are judgments about the work, and guessing them would produce a
  # block that passes a check while describing the wrong change.
  cat <<EOF
<!-- CODE-PRE-REVIEWED-PLAN:START -->
\`\`\`json
{
  "schema": "code-pre-reviewed-plan:v1",
  "repository": "$name",
  "reviewedBaseSha": "$base",
  "scope": {"modify": [], "add": [], "delete": []},
  "anchors": [],
  "callerChecks": [],
  "acceptanceCriteria": [{"id": "PC-001", "text": ""}],
  "verification": [],
  "visual": {"required": false}
}
\`\`\`
<!-- CODE-PRE-REVIEWED-PLAN:END -->
EOF

  if [ -f "$workdir/context.md" ]; then
    echo
    echo "Paths the context pack resolved, as scope candidates:"
    awk '/^## /{h=substr($0,4); getline; getline; if ($0 ~ /^- status: exists/) print "  " h}' "$workdir/context.md"
  fi
}

main() {
  local sub="${1:-}"; shift || true
  case "$sub" in
    check) cmd_check "$@" ;;
    scaffold) cmd_scaffold "$@" ;;
    *) die "usage: $0 check <workdir> | scaffold <workdir>" ;;
  esac
}

main "$@"
