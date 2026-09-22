#!/usr/bin/env bash
# The context pack must answer the search a reviewer would otherwise run, including the misses.

set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
STATE="$ROOT/scripts/plan-review-state.sh"
CONTEXT="$ROOT/scripts/plan-review-context.sh"

TMP="$(mktemp -d -t plan-review-context-test)"
trap 'rm -rf "$TMP"' EXIT
export PLAN_REVIEW_WORKROOT="$TMP/work"
mkdir -p "$PLAN_REVIEW_WORKROOT"

fail() { echo "FAIL: $*" >&2; exit 1; }

repo="$TMP/repo"
mkdir -p "$repo/scripts" "$repo/docs" "$repo/nested/deep"
printf '#!/usr/bin/env bash\necho real\n' > "$repo/scripts/real-tool.sh"
printf '# Notes\n\ncontent\n' > "$repo/docs/notes.md"
printf '{}\n' > "$repo/nested/deep/buried-config.json"
git -C "$repo" init -q
git -C "$repo" config user.email t@t.test
git -C "$repo" config user.name Test
git -C "$repo" add -A
git -C "$repo" commit -qm one

plan="$TMP/plan.md"
cat > "$plan" <<'EOF'
# A plan

Step one calls `scripts/real-tool.sh` which already exists.
Step two updates `docs/notes.md`.
Step three calls `scripts/ghost-tool.sh`, which this plan assumes exists.
Step four reads `buried-config.json` by name only.
Step five creates a worktree at `/Users/someone/work/.worktrees/<slug>`.
Step six runs `codex exec -s workspace-write -C /tmp/x "<prompt>"`.
Step seven follows `<parent-of-main-checkout>/.worktrees/<name>`.
Step eight applies `.../truncated/path.md`.
Run `/cleanup` afterwards.
See https://example.com/docs/thing.md for background.
EOF

wd="$("$STATE" init "$plan" --repo "$repo")"
out="$("$CONTEXT" "$plan" "$wd" 2>/dev/null)"
[ -f "$out" ] || fail "context pack was not written"

grep -q "scripts/real-tool.sh" "$out" || fail "context pack omitted a path the plan names"
grep -A3 "^## scripts/real-tool.sh" "$out" | grep -q "status: exists" || fail "an existing path was not marked as existing"
grep -q "echo real" "$out" || fail "context pack did not include the head of an existing file"

grep -A3 "^## scripts/ghost-tool.sh" "$out" | grep -q "status: UNRESOLVED" || fail "a missing path was not marked UNRESOLVED"

# Unresolved must not be stated as a finding on its own. A plan may legitimately
# name a file it will create, and calling that a defect manufactures a round.
grep -A7 "^## scripts/ghost-tool.sh" "$out" | grep -q "not automatically a finding" || fail "an unresolved path was stated as an automatic finding"

# A bare filename must be found by searching the repository, because a reviewer
# told a present file is absent spends a round proving otherwise.
grep -A3 "^## buried-config.json" "$out" | grep -q "status: exists" || fail "a bare filename was not resolved by repository search"

# Every one of these is prose, a template, or a command line, not a path. Each one
# reported as unresolved would be a false finding a reviewer must spend time on.
for junk in '<slug>' '<prompt>' '<parent-of-main-checkout>' 'codex' 'truncated' 'cleanup'; do
  grep -q "^## .*$junk" "$out" && fail "context pack treated a non-path as a path: $junk"
done

# A URL is not a repository path and must not be resolved as one.
grep -q "example.com" "$out" && fail "context pack treated a URL as a repository path"

grep -q "paths resolved: " "$out" || fail "context pack has no summary count"

echo "PASS plan-review-context"
