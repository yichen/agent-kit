#!/usr/bin/env bash
# The failure this skill exists to prevent: every fix creates the next defect, so
# the reviewer never runs out of blocking findings and the loop never ends.
#
# This replays that chain against the gate. The reviewer here always reports a
# blocking finding, exactly as the measured 27 hour loop did. The gate must still
# stop, and must stop for a named reason.

set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
STATE="$ROOT/scripts/plan-review-state.sh"

TMP="$(mktemp -d -t plan-review-loop-test)"
trap 'rm -rf "$TMP"' EXIT
export PLAN_REVIEW_WORKROOT="$TMP/work"
mkdir -p "$PLAN_REVIEW_WORKROOT"

fail() { echo "FAIL: $*" >&2; exit 1; }

# The measured chain, from the row contrast loop:
#   round 1 raises a row's contrast
#   round 2 finds the selected and hovered rows are now identical
#   round 3 finds the border added in round 2 is itself below the contrast bar
# Each edit is small and each one creates the next blocking finding.
plan="$TMP/contrast-plan.md"
cat > "$plan" <<'EOF'
# Table row states

Selected rows use a light fill.
Hovered rows use the same light fill.
EOF

wd="$("$STATE" init "$plan" --repo "$TMP")"

apply_edit() {
  case "$1" in
    1) echo "Raise the selected row fill to a higher contrast value." >> "$plan" ;;
    2) echo "Add a border so a selected row differs from a hovered row." >> "$plan" ;;
    3) echo "Set the border colour so it clears the contrast bar." >> "$plan" ;;
    *) echo "Another correction." >> "$plan" ;;
  esac
}

rounds_run=0
stop_line=""
for n in 1 2 3 4 5 6 7 8 9 10; do
  round="$("$STATE" round-start "$wd")"
  rounds_run="$round"
  apply_edit "$n"
  "$STATE" record "$wd" \
    --brief consequences \
    --verdict blocking \
    --finding "edit $n closed its finding and created the next one" \
    --edit "edit $n" \
    --check "NONE"

  # The reviewer never approves. There is always one blocking finding left.
  set +e
  out="$("$STATE" gate "$wd" --blocking 1 --ready fail)"; code=$?
  set -e
  if [ "$code" -eq 10 ]; then
    stop_line="$out"
    break
  fi
  [ "$code" -eq 0 ] || fail "gate returned an unexpected exit code: $code"
done

[ -n "$stop_line" ] || fail "the gate never stopped: ten rounds ran with a blocking finding every time"
[ "$rounds_run" -le 3 ] || fail "the gate ran $rounds_run rounds, above the cap of 3"

case "$stop_line" in
  STOP\ round-cap*|STOP\ plan-growth*) ;;
  *) fail "the gate stopped without a reason a caller can act on: $stop_line" ;;
esac

# The stop must be recorded, so a later invocation can see why the last one ended.
grep -q "gate: STOP" "$wd/log.md" || fail "the stop was not written to the log"

# Every round must be in the log, so the coordinator can sort repeats from new findings.
for n in $(seq 1 "$rounds_run"); do
  grep -q "^## Round $n$" "$wd/log.md" || fail "round $n is missing from the log"
done

# A second invocation on the same plan starts its own count rather than resuming
# the old one. Continuing past the cap has to be a deliberate act.
wd2="$("$STATE" init "$plan" --repo "$TMP")"
[ "$(sed -n 's/^round=//p' "$wd2/state")" = "0" ] || fail "re-init did not reset the round count"

echo "PASS plan-review-loop (stopped after $rounds_run rounds: $stop_line)"
