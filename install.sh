#!/usr/bin/env bash
# Link every tracked skill into the host agent directories and install optional host services.

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")" && pwd)"
USER_HOME="${AGENT_KIT_USER_HOME:-$HOME}"
PRIMARY_ROOT="${AGENT_KIT_PRIMARY_SKILLS_ROOT:-$USER_HOME/.agents/skills}"
CODEX_ROOT="${AGENT_KIT_CODEX_SKILLS_ROOT:-$USER_HOME/.codex/skills}"
CLAUDE_ROOT="${AGENT_KIT_CLAUDE_SKILLS_ROOT:-$USER_HOME/.claude/skills}"
PI_ROOT="${AGENT_KIT_PI_SKILLS_ROOT:-$USER_HOME/.pi/agent/skills}"
BACKUP_ROOT="${AGENT_KIT_BACKUP_ROOT:-$USER_HOME/.agent-kit/backups}"
MODE="${1:-}"
ADOPT=0

if [ "${2:-}" = "--adopt-existing" ]; then
  ADOPT=1
elif [ $# -gt 1 ]; then
  echo "usage: $0 install [--adopt-existing] | check" >&2
  exit 1
fi

case "$MODE" in
  install|check) ;;
  *) echo "usage: $0 install [--adopt-existing] | check" >&2; exit 1 ;;
esac

skill_directories=()
for skill_dir in "$REPO_ROOT"/skills/*; do
  [ -d "$skill_dir" ] || continue
  [ -f "$skill_dir/SKILL.md" ] || { echo "agent-kit: missing SKILL.md in $skill_dir" >&2; exit 2; }
  skill_name="$(basename "$skill_dir")"
  [[ "$skill_name" =~ ^[a-z0-9][a-z0-9-]{0,63}$ ]] || {
    echo "agent-kit: invalid skill directory name: $skill_name" >&2
    exit 2
  }
  skill_directories+=("$skill_dir")
done
[ "${#skill_directories[@]}" -gt 0 ] || { echo "agent-kit: no skills found" >&2; exit 2; }

expected_link() {
  local target="$1" expected="$2"
  [ -L "$target" ] && [ "$(readlink "$target")" = "$expected" ]
}

preflight_target() {
  local target="$1" expected="$2"
  if expected_link "$target" "$expected"; then
    return 0
  fi
  if [ -e "$target" ] || [ -L "$target" ]; then
    [ "$ADOPT" -eq 1 ] || {
      echo "agent-kit: refusing to replace existing target without --adopt-existing: $target" >&2
      return 2
    }
  fi
}

backup_and_link() {
  local target="$1" expected="$2" runtime="$3"
  if expected_link "$target" "$expected"; then
    return 0
  fi
  if [ -e "$target" ] || [ -L "$target" ]; then
    local suffix backup
    suffix="${AGENT_KIT_BACKUP_SUFFIX:-$(date -u +%Y%m%dT%H%M%SZ)}"
    backup="$BACKUP_ROOT/$runtime/$(basename "$target").pre-agent-kit-${suffix}"
    [ ! -e "$backup" ] && [ ! -L "$backup" ] || {
      echo "agent-kit: backup already exists: $backup" >&2
      return 2
    }
    mkdir -p "$(dirname "$backup")"
    mv "$target" "$backup"
  fi
  ln -s "$expected" "$target"
}

for skill_dir in "${skill_directories[@]}"; do
  skill_name="$(basename "$skill_dir")"
  primary="$PRIMARY_ROOT/$skill_name"
  preflight_target "$primary" "$skill_dir"
  preflight_target "$CODEX_ROOT/$skill_name" "$primary"
  preflight_target "$CLAUDE_ROOT/$skill_name" "$primary"
  preflight_target "$PI_ROOT/$skill_name" "$primary"
done

if [ "$MODE" = "check" ]; then
  for skill_dir in "${skill_directories[@]}"; do
    skill_name="$(basename "$skill_dir")"
    primary="$PRIMARY_ROOT/$skill_name"
    expected_link "$primary" "$skill_dir" || { echo "agent-kit: primary link mismatch: $primary" >&2; exit 2; }
    expected_link "$CODEX_ROOT/$skill_name" "$primary" || { echo "agent-kit: Codex link mismatch: $CODEX_ROOT/$skill_name" >&2; exit 2; }
    expected_link "$CLAUDE_ROOT/$skill_name" "$primary" || { echo "agent-kit: Claude link mismatch: $CLAUDE_ROOT/$skill_name" >&2; exit 2; }
    expected_link "$PI_ROOT/$skill_name" "$primary" || { echo "agent-kit: Pi link mismatch: $PI_ROOT/$skill_name" >&2; exit 2; }
    if [ -x "$skill_dir/scripts/install-host-service.sh" ]; then
      AGENT_KIT_USER_HOME="$USER_HOME" AGENT_KIT_PRIMARY_SKILL_PATH="$primary" "$skill_dir/scripts/install-host-service.sh" check
    fi
  done
  echo "agent-kit: links and host services are synchronized"
  exit 0
fi

mkdir -p "$PRIMARY_ROOT" "$CODEX_ROOT" "$CLAUDE_ROOT" "$PI_ROOT"
for skill_dir in "${skill_directories[@]}"; do
  skill_name="$(basename "$skill_dir")"
  primary="$PRIMARY_ROOT/$skill_name"
  backup_and_link "$primary" "$skill_dir" agents
  backup_and_link "$CODEX_ROOT/$skill_name" "$primary" codex
  backup_and_link "$CLAUDE_ROOT/$skill_name" "$primary" claude
  backup_and_link "$PI_ROOT/$skill_name" "$primary" pi
  if [ -x "$skill_dir/scripts/install-host-service.sh" ]; then
    AGENT_KIT_USER_HOME="$USER_HOME" AGENT_KIT_PRIMARY_SKILL_PATH="$primary" "$skill_dir/scripts/install-host-service.sh" install
  fi
done
"$0" check
