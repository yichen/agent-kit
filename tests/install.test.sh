#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
INSTALLER="$ROOT/install.sh"
TEST_ROOT="$(mktemp -d)"
trap 'rm -rf "$TEST_ROOT"' EXIT
TEST_HOME="$TEST_ROOT/home"

export AGENT_KIT_USER_HOME="$TEST_HOME"
export QWEN_DELEGATE_SKIP_LAUNCHCTL=1
export AGENT_KIT_BACKUP_SUFFIX=test

mkdir -p "$TEST_HOME"
before="$(find "$TEST_HOME" -mindepth 1 -print | sort)"
set +e
"$INSTALLER" check > "$TEST_ROOT/check.out" 2> "$TEST_ROOT/check.err"
missing_status=$?
set -e
test "$missing_status" -eq 2
test "$before" = "$(find "$TEST_HOME" -mindepth 1 -print | sort)"

mkdir -p "$TEST_HOME/.agents/skills/qwen-delegate"
printf '%s\n' preserved > "$TEST_HOME/.agents/skills/qwen-delegate/sentinel"
set +e
"$INSTALLER" install > "$TEST_ROOT/install.out" 2> "$TEST_ROOT/install.err"
without_adopt_status=$?
set -e
test "$without_adopt_status" -eq 2
test -f "$TEST_HOME/.agents/skills/qwen-delegate/sentinel"

"$INSTALLER" install --adopt-existing > "$TEST_ROOT/adopt.out"
test -L "$TEST_HOME/.agents/skills/qwen-delegate"
test "$(readlink "$TEST_HOME/.agents/skills/qwen-delegate")" = "$ROOT/skills/qwen-delegate"
test -f "$TEST_HOME/.agents/skills/qwen-delegate.pre-agent-kit-test/sentinel"
test "$(readlink "$TEST_HOME/.claude/skills/qwen-delegate")" = "$TEST_HOME/.agents/skills/qwen-delegate"
test "$(readlink "$TEST_HOME/.pi/agent/skills/qwen-delegate")" = "$TEST_HOME/.agents/skills/qwen-delegate"
grep -Fq "$TEST_HOME/.agents/skills/qwen-delegate/scripts/ollama-delegate.sh" "$TEST_HOME/Library/LaunchAgents/com.yichen.sharedanchor-ollama-delegate-warm.plist"

"$INSTALLER" install > "$TEST_ROOT/reinstall.out"
"$INSTALLER" check > "$TEST_ROOT/final-check.out"

rm "$TEST_HOME/.pi/agent/skills/qwen-delegate"
ln -s "$TEST_ROOT/wrong-target" "$TEST_HOME/.pi/agent/skills/qwen-delegate"
set +e
"$INSTALLER" install > "$TEST_ROOT/wrong.out" 2> "$TEST_ROOT/wrong.err"
wrong_status=$?
set -e
test "$wrong_status" -eq 2
test "$(readlink "$TEST_HOME/.pi/agent/skills/qwen-delegate")" = "$TEST_ROOT/wrong-target"
rm "$TEST_HOME/.pi/agent/skills/qwen-delegate"
ln -s "$TEST_HOME/.agents/skills/qwen-delegate" "$TEST_HOME/.pi/agent/skills/qwen-delegate"

printf '\n# drift\n' >> "$TEST_HOME/Library/LaunchAgents/com.yichen.sharedanchor-ollama-delegate-warm.plist"
set +e
"$INSTALLER" check > "$TEST_ROOT/drift.out" 2> "$TEST_ROOT/drift.err"
drift_status=$?
set -e
test "$drift_status" -eq 2
grep -Fq 'warm service plist drift detected' "$TEST_ROOT/drift.err"

while IFS='|' read -r name expected_status; do
  fixture="$TEST_ROOT/name-$expected_status-$(printf '%s' "$name" | shasum -a 256 | cut -c1-8)"
  mkdir -p "$fixture/skills/$name"
  cp "$INSTALLER" "$fixture/install.sh"
  chmod +x "$fixture/install.sh"
  printf '%s\n' '---' 'name: fixture' '---' > "$fixture/skills/$name/SKILL.md"
  fixture_home="$fixture/home"
  mkdir -p "$fixture_home"
  set +e
  AGENT_KIT_USER_HOME="$fixture_home" QWEN_DELEGATE_SKIP_LAUNCHCTL=1 "$fixture/install.sh" install > "$fixture/out" 2> "$fixture/err"
  actual_status=$?
  set -e
  test "$actual_status" -eq "$expected_status"
  test ! -e "$fixture/touch-owned"
done <<'NAME_CASES'
valid-skill|0
skill2|0
Bad|2
bad_skill|2
-bad|2
bad;touch-owned|2
NAME_CASES

printf '%s\n' 'agent-kit installer tests passed'
