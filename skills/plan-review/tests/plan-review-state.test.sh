#!/usr/bin/env bash
# Each stopping rule is tested on its own, so deleting any one of them turns a test red.

set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
STATE="$ROOT/scripts/plan-review-state.sh"

TMP="$(mktemp -d -t plan-review-state-test)"
trap 'rm -rf "$TMP"' EXIT
export PLAN_REVIEW_WORKROOT="$TMP/work"
mkdir -p "$PLAN_REVIEW_WORKROOT"

fail() { echo "FAIL: $*" >&2; exit 1; }

make_plan() {
  local name="$1" bytes="${2:-400}"
  local p="$TMP/$name.md"
  { echo "# $name"; head -c "$bytes" /dev/zero | tr '\0' 'x'; } > "$p"
  echo "$p"
}

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

# 1. init records the plan, the starting size, and round zero.
plan="$(make_plan one 400)"
wd="$("$STATE" init "$plan" --repo "$TMP")"
[ -d "$wd" ] || fail "init did not create a workdir"
grep -q "^round=0$" "$wd/state" || fail "init did not start at round 0"
grep -q "^bytes_round1=" "$wd/state" || fail "init did not record the starting size"
[ -f "$wd/log.md" ] || fail "init did not create the log"

# 2. round-start increments and prints the round number.
r="$("$STATE" round-start "$wd")"
[ "$r" = "1" ] || fail "first round-start returned $r, expected 1"
r="$("$STATE" round-start "$wd")"
[ "$r" = "2" ] || fail "second round-start returned $r, expected 2"

# 3. gate continues when there is a blocking finding and no stop condition applies.
plan="$(make_plan cont 400)"
wd="$("$STATE" init "$plan" --repo "$TMP")"
"$STATE" round-start "$wd" >/dev/null
out="$("$STATE" gate "$wd" --blocking 2)" || fail "gate should continue on round 1 with findings"
case "$out" in CONTINUE*) ;; *) fail "expected CONTINUE, got: $out" ;; esac

# 4. STOP on no blocking finding. This is the rule that ends a loop after an approval.
plan="$(make_plan approve 400)"
wd="$("$STATE" init "$plan" --repo "$TMP")"
"$STATE" round-start "$wd" >/dev/null
set +e
out="$("$STATE" gate "$wd" --blocking 0)"; code=$?
set -e
[ "$code" -eq 10 ] || fail "gate exit was $code, expected 10 on zero blocking findings"
case "$out" in STOP\ approved*) ;; *) fail "expected STOP approved, got: $out" ;; esac

# 5. STOP on the round cap, even with findings still open.
plan="$(make_plan cap 400)"
wd="$("$STATE" init "$plan" --repo "$TMP")"
for _ in 1 2 3; do "$STATE" round-start "$wd" >/dev/null; done
set +e
out="$("$STATE" gate "$wd" --blocking 5)"; code=$?
set -e
[ "$code" -eq 10 ] || fail "gate exit was $code, expected 10 at the round cap"
case "$out" in STOP\ round-cap*) ;; *) fail "expected STOP round-cap, got: $out" ;; esac

# 6. The cap cannot be raised by an argument. Only the documented env override moves it.
plan="$(make_plan nocap 400)"
wd="$("$STATE" init "$plan" --repo "$TMP")"
for _ in 1 2 3; do "$STATE" round-start "$wd" >/dev/null; done
set +e
"$STATE" gate "$wd" --blocking 1 --rounds 9 >/dev/null 2>&1; code=$?
set -e
[ "$code" -eq 2 ] || fail "gate accepted an unknown option to raise the cap (exit $code)"

# 7. STOP on plan growth past both the percentage and the absolute floor.
plan="$(make_plan growth 4000)"
wd="$("$STATE" init "$plan" --repo "$TMP")"
"$STATE" round-start "$wd" >/dev/null
head -c 3000 /dev/zero | tr '\0' 'y' >> "$plan"
set +e
out="$("$STATE" gate "$wd" --blocking 1)"; code=$?
set -e
[ "$code" -eq 10 ] || fail "gate exit was $code, expected 10 on plan growth"
case "$out" in STOP\ plan-growth*) ;; *) fail "expected STOP plan-growth, got: $out" ;; esac

# 8. Growth under the percentage does not stop. The percentage bound is real.
plan="$(make_plan nogrowth 4000)"
wd="$("$STATE" init "$plan" --repo "$TMP")"
"$STATE" round-start "$wd" >/dev/null
head -c 1100 /dev/zero | tr '\0' 'y' >> "$plan"
out="$("$STATE" gate "$wd" --blocking 1)" || fail "gate stopped on growth below the percentage"
case "$out" in CONTINUE*) ;; *) fail "expected CONTINUE below the growth percentage, got: $out" ;; esac

# 8b. A small plan past 50 percent but under the absolute floor does not stop.
# Without the floor, any short plan gaining a sentence halts in round 1, which
# stops a review that was being corrected rather than expanded.
plan="$(make_plan smallgrowth 120)"
wd="$("$STATE" init "$plan" --repo "$TMP")"
"$STATE" round-start "$wd" >/dev/null
head -c 300 /dev/zero | tr '\0' 'y' >> "$plan"
out="$("$STATE" gate "$wd" --blocking 1)" || fail "gate stopped a small plan on percentage alone"
case "$out" in CONTINUE*) ;; *) fail "expected CONTINUE for a small plan under the byte floor, got: $out" ;; esac

# 9. STOP when the pinned base moves.
repo="$(make_repo moved)"
plan="$(make_plan based 400)"
wd="$("$STATE" init "$plan" --repo "$repo")"
"$STATE" round-start "$wd" >/dev/null
echo two > "$repo/file2.txt"
git -C "$repo" add -A
git -C "$repo" commit -qm two
set +e
out="$("$STATE" gate "$wd" --blocking 1)"; code=$?
set -e
[ "$code" -eq 10 ] || fail "gate exit was $code, expected 10 when the base moved"
case "$out" in STOP\ base-moved*) ;; *) fail "expected STOP base-moved, got: $out" ;; esac

# 10. An unmoved base does not stop. The base check is real, not always-true.
repo="$(make_repo still)"
plan="$(make_plan stillbased 400)"
wd="$("$STATE" init "$plan" --repo "$repo")"
"$STATE" round-start "$wd" >/dev/null
out="$("$STATE" gate "$wd" --blocking 1)" || fail "gate stopped although the base had not moved"
case "$out" in CONTINUE*) ;; *) fail "expected CONTINUE on an unmoved base, got: $out" ;; esac

# 11. record rejects a brief that is not one of the three.
plan="$(make_plan rec 400)"
wd="$("$STATE" init "$plan" --repo "$TMP")"
"$STATE" record "$wd" --brief claims --verdict blocking --finding "a claim is wrong" --check "grep -q x file"
grep -q "brief: claims" "$wd/log.md" || fail "record did not write the brief"
grep -q "check: grep -q x file" "$wd/log.md" || fail "record did not write the check"
set +e
"$STATE" record "$wd" --brief vibes --finding x >/dev/null 2>&1; code=$?
set -e
[ "$code" -eq 2 ] || fail "record accepted an unknown brief (exit $code)"

# 12. gate refuses to decide without a blocking count, so silence cannot read as approval.
set +e
"$STATE" gate "$wd" >/dev/null 2>&1; code=$?
set -e
[ "$code" -eq 2 ] || fail "gate decided without --blocking (exit $code)"

echo "PASS plan-review-state"
