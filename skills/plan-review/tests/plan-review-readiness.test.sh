#!/usr/bin/env bash
# Readiness is a separate question from defects. These tests cover a plan that is
# free of defects but not implementable, which is the case the three reviewer
# briefs cannot detect.

set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
STATE="$ROOT/scripts/plan-review-state.sh"
READY="$ROOT/scripts/plan-review-readiness.sh"

TMP="$(mktemp -d -t plan-review-readiness-test)"
trap 'rm -rf "$TMP"' EXIT
export PLAN_REVIEW_WORKROOT="$TMP/work"
mkdir -p "$PLAN_REVIEW_WORKROOT"

fail() { echo "FAIL: $*" >&2; exit 1; }

make_repo() {
  local r="$TMP/repo-$1"
  mkdir -p "$r"
  git -C "$r" init -q
  git -C "$r" config user.email t@t.test
  git -C "$r" config user.name Test
  echo one > "$r/file.txt"
  git -C "$r" add -A
  git -C "$r" commit -qm one
  echo "$r"
}

block_for() {
  # $1 base sha, $2 extra json fragment replacing the defaults
  cat <<EOF
<!-- CODE-PRE-REVIEWED-PLAN:START -->
\`\`\`json
$2
\`\`\`
<!-- CODE-PRE-REVIEWED-PLAN:END -->
EOF
}

repo="$(make_repo main)"
base="$(git -C "$repo" rev-parse HEAD)"

good_json() {
  cat <<EOF
{
  "schema": "code-pre-reviewed-plan:v1",
  "repository": "github.com/t/t",
  "reviewedBaseSha": "$1",
  "scope": {"modify": ["file.txt"], "add": [], "delete": []},
  "anchors": [{"path": "file.txt", "symbols": ["one"]}],
  "acceptanceCriteria": [{"id": "PC-001", "text": "file.txt says two"}],
  "verification": ["cat file.txt"],
  "visual": {"required": false}
}
EOF
}

# 1. A complete block passes.
plan="$TMP/good.md"
{ echo "# Good plan"; echo; block_for "$base" "$(good_json "$base")"; } > "$plan"
wd="$("$STATE" init "$plan" --repo "$repo")"
out="$("$READY" check "$wd")" || fail "a complete plan block was not accepted: $out"
case "$out" in READY*) ;; *) fail "expected READY, got: $out" ;; esac

# 2. Prose with no block and none of the four facts is not ready.
plan="$TMP/prose.md"
printf '# A plan\n\nWe will improve the thing. It will be better afterwards.\n' > "$plan"
wd="$("$STATE" init "$plan" --repo "$repo")"
set +e
out="$("$READY" check "$wd")"; code=$?
set -e
[ "$code" -eq 11 ] || fail "bare prose was accepted as ready (exit $code)"
case "$out" in NOT-READY*) ;; *) fail "expected NOT-READY, got: $out" ;; esac
printf '%s' "$out" | grep -q "acceptance criteria" || fail "missing acceptance criteria was not reported"
printf '%s' "$out" | grep -q "file:line" || fail "missing file:line anchors was not reported"

# 3. Each required field is load-bearing on its own. Drop one at a time and the
#    plan must stop being ready, naming that field.
for drop in scope acceptanceCriteria verification anchors; do
  plan="$TMP/drop-$drop.md"
  json="$(good_json "$base" | python3 -c "
import json,sys
d=json.load(sys.stdin)
k='$drop'
if k=='scope': d['scope']={'modify':[],'add':[],'delete':[]}
elif k=='anchors': d['anchors']=[]
else: d[k]=[]
print(json.dumps(d))
")"
  { echo "# Plan"; block_for "$base" "$json"; } > "$plan"
  wd="$("$STATE" init "$plan" --repo "$repo")"
  set +e
  out="$("$READY" check "$wd")"; code=$?
  set -e
  [ "$code" -eq 11 ] || fail "a plan with an empty $drop was accepted as ready"
done

# 4. A block reviewed against a different commit is stale.
plan="$TMP/stale.md"
{ echo "# Plan"; block_for "$base" "$(good_json 0000000000000000000000000000000000000000)"; } > "$plan"
wd="$("$STATE" init "$plan" --repo "$repo")"
set +e
out="$("$READY" check "$wd")"; code=$?
set -e
[ "$code" -eq 11 ] || fail "a block pinned to another commit was accepted"
printf '%s' "$out" | grep -q "stale" || fail "a stale block was not reported as stale"

# 5. An unknown schema version blocks, because an implementation run blocks on it.
plan="$TMP/schema.md"
json="$(good_json "$base" | sed 's/code-pre-reviewed-plan:v1/code-pre-reviewed-plan:v99/')"
{ echo "# Plan"; block_for "$base" "$json"; } > "$plan"
wd="$("$STATE" init "$plan" --repo "$repo")"
set +e
"$READY" check "$wd" >/dev/null; code=$?
set -e
[ "$code" -eq 11 ] || fail "an unknown schema version was accepted"

# 6. Malformed JSON is reported, not silently treated as absent.
plan="$TMP/broken.md"
{ echo "# Plan"; block_for "$base" '{"schema": "code-pre-reviewed-plan:v1",,}'; } > "$plan"
wd="$("$STATE" init "$plan" --repo "$repo")"
set +e
out="$("$READY" check "$wd")"; code=$?
set -e
[ "$code" -eq 11 ] || fail "malformed JSON was accepted"
printf '%s' "$out" | grep -qi "json" || fail "malformed JSON was not reported as a JSON problem"

# 7. No-write guard. The check and the scaffold are read-only, so neither the plan
#    nor the repository may change.
plan="$TMP/guard.md"
{ echo "# Good plan"; block_for "$base" "$(good_json "$base")"; } > "$plan"
wd="$("$STATE" init "$plan" --repo "$repo")"
plan_before="$(shasum -a 256 "$plan" | cut -d' ' -f1)"
repo_before="$(git -C "$repo" rev-parse HEAD):$(git -C "$repo" status --porcelain | shasum -a 256 | cut -d' ' -f1)"
"$READY" check "$wd" >/dev/null
"$READY" scaffold "$wd" >/dev/null
plan_after="$(shasum -a 256 "$plan" | cut -d' ' -f1)"
repo_after="$(git -C "$repo" rev-parse HEAD):$(git -C "$repo" status --porcelain | shasum -a 256 | cut -d' ' -f1)"
[ "$plan_before" = "$plan_after" ] || fail "the readiness check modified the plan file"
[ "$repo_before" = "$repo_after" ] || fail "the readiness check modified the repository"

# 8. scaffold fills only what it can derive and leaves the judgments empty, so a
#    generated block cannot pass the check by itself.
scaffold_out="$("$READY" scaffold "$wd")"
printf '%s' "$scaffold_out" | grep -q "$base" || fail "scaffold did not fill the pinned base commit"
printf '%s' "$scaffold_out" | grep -q '"acceptanceCriteria": \[{"id": "PC-001", "text": ""}\]' \
  || fail "scaffold invented acceptance criteria instead of leaving them empty"

# 9. The gate refuses to approve a defect-free plan that is not ready.
plan="$TMP/gated.md"
printf '# A plan\n\nWe will improve the thing.\n' > "$plan"
wd="$("$STATE" init "$plan" --repo "$repo")"
"$STATE" round-start "$wd" >/dev/null
set +e
out="$("$STATE" gate "$wd" --blocking 0 --ready fail)"; code=$?
set -e
[ "$code" -eq 10 ] || fail "gate exit was $code, expected 10 for a defect-free but unready plan"
case "$out" in STOP\ not-ready*) ;; *) fail "expected STOP not-ready, got: $out" ;; esac

# 10. The gate approves only when both are true.
out="$("$STATE" gate "$wd" --blocking 0 --ready pass 2>/dev/null)" || true
case "$out" in STOP\ approved*) ;; *) fail "expected STOP approved with no findings and a ready plan, got: $out" ;; esac

# 11. The gate refuses to decide without a readiness answer, so an unrun check
#     cannot read as approval.
set +e
"$STATE" gate "$wd" --blocking 0 >/dev/null 2>&1; code=$?
set -e
[ "$code" -eq 2 ] || fail "gate decided without --ready (exit $code)"

echo "PASS plan-review-readiness"
