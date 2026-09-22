#!/usr/bin/env bash
# Build the context pack for a plan review.
# Resolves every repository path the plan names so no reviewer spends a round on its own failed search.

set -euo pipefail

die() { echo "plan-review-context: $*" >&2; exit 2; }

plan="${1:-}"
workdir="${2:-}"
[ -n "$plan" ] || die "usage: $0 <plan-file> <workdir>"
[ -f "$plan" ] || die "plan file not found: $plan"
[ -n "$workdir" ] || die "usage: $0 <plan-file> <workdir>"
[ -d "$workdir" ] || die "workdir not found: $workdir"

repo="$(sed -n 's/^repo=//p' "$workdir/state" | head -1)"
base="$(sed -n 's/^base=//p' "$workdir/state" | head -1)"
[ -n "$repo" ] || repo="-"
[ -n "$base" ] || base="-"

out="$workdir/context.md"

# Candidate paths: backticked tokens, and bare tokens that look like paths.
#
# The filter is deliberately strict. An entry this script reports as NOT FOUND is
# read as a claims finding, so a candidate that was never a path at all costs a
# review round. Prose, slash commands, shell command lines, and <placeholder>
# templates are dropped here rather than surfaced as false findings.
candidates="$(
  {
    grep -o '`[^`]\{2,\}`' "$plan" 2>/dev/null | tr -d '`' || true
    grep -oE '(^|[[:space:](])[A-Za-z0-9_./~-]+\.(sh|ts|tsx|js|mjs|py|md|json|toml|yaml|yml|sql)' "$plan" 2>/dev/null || true
  } \
  | sed 's/^[[:space:](]*//' \
  | sed 's/[),.:;]*$//' \
  | sed 's/:[0-9-]*$//' \
  | grep -vE '^https?://' \
  | grep -vE '[[:space:]]' \
  | grep -vE '[][<>$"'"'"'(){}*?|]' \
  | grep -vE '\.\.\.' \
  | grep -vE '^/[A-Za-z0-9_-]+$' \
  | grep -vE '/$' \
  | grep -E '/|\.(sh|ts|tsx|js|mjs|py|md|json|toml|yaml|yml|sql)$' \
  | grep -vE '^[[:space:]]*$' \
  | sort -u
)"

{
  echo "# Context pack"
  echo
  echo "Plan: $plan"
  echo "Repository: $repo"
  echo "Base commit: $base"
  echo "Generated: $(date -u +%Y-%m-%dT%H:%M:%SZ)"
  echo
  echo "Every path the plan names, resolved against the repository at the base commit."
  echo "A reviewer that needs one of these must use the answer below rather than searching again."
  echo
} > "$out"

found=0
missing=0

while IFS= read -r raw; do
  [ -n "$raw" ] || continue
  path="$raw"
  case "$path" in
    "~"/*) path="$HOME/${path#\~/}" ;;
  esac

  resolved=""
  if [ -e "$path" ]; then
    resolved="$path"
  elif [ "$repo" != "-" ] && [ -e "$repo/$path" ]; then
    resolved="$repo/$path"
  elif [ "$repo" != "-" ] && [ "$path" = "$(basename "$path")" ]; then
    # A bare filename. Search the repository by basename before calling it missing,
    # because a reviewer told a present file is absent spends a round on that.
    resolved="$(find "$repo" -name "$path" -type f \
      -not -path '*/.git/*' -not -path '*/node_modules/*' \
      -print -quit 2>/dev/null || true)"
  fi

  if [ -n "$resolved" ]; then
    found=$((found + 1))
    {
      echo "## $raw"
      echo
      echo "- status: exists"
      echo "- resolved: $resolved"
      if [ -f "$resolved" ]; then
        echo "- size: $(wc -c < "$resolved" | tr -d ' ') bytes, $(wc -l < "$resolved" | tr -d ' ') lines"
        echo
        echo '```'
        head -12 "$resolved"
        echo '```'
      else
        echo "- kind: directory"
      fi
      echo
    } >> "$out"
  else
    missing=$((missing + 1))
    {
      echo "## $raw"
      echo
      echo "- status: UNRESOLVED"
      echo "- searched: $path"
      [ "$repo" != "-" ] && echo "- searched: $repo/$path"
      echo "- note: unresolved is not automatically a finding. A plan may legitimately"
      echo "  name a file it will create. It is a claims finding only where the plan"
      echo "  asserts this path exists today."
      echo
    } >> "$out"
  fi
done <<< "$candidates"

{
  echo "## Summary"
  echo
  echo "- paths resolved: $found"
  echo "- paths unresolved: $missing"
} >> "$out"

echo "$out"
echo "resolved=$found missing=$missing" >&2
