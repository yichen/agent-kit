#!/usr/bin/env bash
set -euo pipefail

SKILL_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
BOSS="$SKILL_ROOT/scripts/boss.py"
ROOT="$(mktemp -d)"
trap 'rm -rf "$ROOT"' EXIT
export AGENTS_ARTIFACTS_ROOT="$ROOT/artifacts"
export BOSS_TEST_MODE=1
REPO="$ROOT/repo"
git init -q "$REPO"
git -C "$REPO" remote add origin git@github.com:example/project.git
PRS="$ROOT/prs.json"
python3 - "$PRS" <<'PY'
import datetime,json,sys
time=(datetime.datetime.now(datetime.timezone.utc)-datetime.timedelta(minutes=30)).isoformat()
json.dump([{'number':11,'state':'OPEN','createdAt':time,'mergedAt':None,'url':'https://github.com/example/project/pull/11','title':'feature'},{'number':12,'state':'MERGED','createdAt':time,'mergedAt':time,'url':'https://github.com/example/project/pull/12','title':'test'}],open(sys.argv[1],'w'))
PY

run() { python3 "$BOSS" --prs-file "$PRS" "$@"; }
expect_fail() {
  local expected="$1"
  shift
  if run "$@" > "$ROOT/out" 2> "$ROOT/err"; then
    echo "unexpected success: $*" >&2
    exit 1
  fi
  grep -q "$expected" "$ROOT/err"
}

# No-write guard: read-only commands neither initialize state nor call a launcher.
before="$(find "$AGENTS_ARTIFACTS_ROOT" -print 2>/dev/null || true)"
expect_fail 'not initialized' status --repo "$REPO"
test "$before" = "$(find "$AGENTS_ARTIFACTS_ROOT" -print 2>/dev/null || true)"
run init --repo "$REPO" --master boss-A --hub hub-A > "$ROOT/init"
STATE="$(python3 -c 'import json,sys;print(json.load(open(sys.argv[1]))["state"])' "$ROOT/init")"
if env -u BOSS_TEST_MODE python3 "$BOSS" --prs-file "$PRS" status --repo "$REPO" > "$ROOT/fixture-out" 2> "$ROOT/fixture-err"; then
  echo 'fixture override worked outside test mode' >&2
  exit 1
fi
grep -q 'explicit local test mode' "$ROOT/fixture-err"
run init --repo "$REPO" --master boss-A --hub hub-A > /dev/null
expect_fail 'different boss master' init --repo "$REPO" --master boss-B
OTHER="$ROOT/other-checkout"
git init -q "$OTHER"
git -C "$OTHER" remote add origin https://github.com/example/project.git
run init --repo "$OTHER" --master boss-A --hub hub-A > "$ROOT/other-init"
test "$STATE" = "$(python3 -c 'import json,sys;print(json.load(open(sys.argv[1]))["state"])' "$ROOT/other-init")"
expect_fail 'different boss master' init --repo "$OTHER" --master boss-B

# Table-driven malformed and injection-like identifiers, dependencies, and issue values.
while IFS='|' read -r expected command; do
  # These literal arguments are never evaluated by a shell.
  read -r -a parts <<< "$command"
  expect_fail "$expected" "${parts[@]}"
done <<EOF
invalid master|init --repo $REPO --master bad;touch-owned
invalid dependency list|ticket add --repo $REPO --issue 4 --kind feature --depends 1;touch-owned
positive integers|ticket add --repo $REPO --issue -1 --kind feature
duplicate or self dependency|ticket add --repo $REPO --issue 4 --kind feature --depends 4
EOF
test ! -e "$ROOT/touch-owned"

run ticket add --repo "$REPO" --issue 1 --kind feature --summary 'Reading practice' --availability testable --test-environment 'staging iPad' --human-gate 'Parent acceptance required' > /dev/null
run ticket add --repo "$REPO" --issue 2 --kind testability --depends 1 > /dev/null
run ticket add --repo "$REPO" --issue 2 --kind testability --depends 1 > /dev/null
expect_fail 'different fields' ticket add --repo "$REPO" --issue 2 --kind feature --depends 1
expect_fail 'unresolved dependencies' ticket launch --repo "$REPO" --issue 2 --adapter /bin/true

ADAPTER="$ROOT/adapter"
cat > "$ADAPTER" <<'SH'
#!/bin/sh
cat > "$BOSS_TEST_REQUEST"
printf '%s\n' '{"task_id":"task-2"}'
SH
chmod +x "$ADAPTER"
export BOSS_TEST_REQUEST="$ROOT/adapter-request"
run ticket pr --repo "$REPO" --issue 1 --pr 12 > /dev/null
expect_fail 'linked merged PR' ticket resolve --repo "$REPO" --issue 1 --pr 11 --note done
run ticket resolve --repo "$REPO" --issue 1 --pr 12 --note done > /dev/null
OPS="$SKILL_ROOT/scripts/operational_store.py"
ops() { python3 "$OPS" --db "$AGENTS_ARTIFACTS_ROOT/boss/operations.sqlite3" "$@" --repo "$REPO"; }
GEN2="$(ops claim --issue 2 --phase implement --owner boss-A | python3 -c 'import json,sys;print(json.load(sys.stdin)["generation"])')"
state_before="$(shasum -a 256 "$STATE")"
run ticket launch --repo "$REPO" --issue 2 --adapter "$ADAPTER" > "$ROOT/preview"
test ! -e "$BOSS_TEST_REQUEST"
test "$state_before" = "$(shasum -a 256 "$STATE")"
for command in status doctor handoff reconcile; do
  run "$command" --repo "$REPO" > "$ROOT/$command"
  test "$state_before" = "$(shasum -a 256 "$STATE")"
done
python3 - "$ROOT/status" <<'PY'
import json,sys
data=json.load(open(sys.argv[1]))
assert data['merged_5h']=={'total':1,'numbers':[12],'feature':1,'testability':0,'unclassified':0}
assert data['unlinked_open_prs']==[11]
assert len(data['resolved_24h'])==1
assert sum(len(row['merged']) for row in data['hourly_5h'])==1
assert data['created_5h']==[11,12]
assert data['merged_features_5h']==[{'issue':1,'pr':12,'summary':'Reading practice','availability':'testable','test_environment':'staging iPad','human_gate':'Parent acceptance required'}]
PY

expect_fail 'absolute executable' ticket launch --repo "$REPO" --issue 2 --adapter 'sh;touch-owned' --apply --phase implement --owner boss-A --generation "$GEN2"
test ! -e "$ROOT/touch-owned"
run ticket launch --repo "$REPO" --issue 2 --adapter "$ADAPTER" --apply --phase implement --owner boss-A --generation "$GEN2" > /dev/null
python3 - "$BOSS_TEST_REQUEST" <<'PY'
import json,sys
assert json.load(open(sys.argv[1]))['issue']==2
PY
expect_fail 'already launched' ticket launch --repo "$REPO" --issue 2 --adapter "$ADAPTER" --apply --phase implement --owner boss-A --generation "$GEN2"
run ticket add --repo "$REPO" --issue 3 --kind feature > /dev/null
GEN3="$(ops claim --issue 3 --phase implement --owner boss-A | python3 -c 'import json,sys;print(json.load(sys.stdin)["generation"])')"
FAIL_ADAPTER="$ROOT/fail-adapter"
cat > "$FAIL_ADAPTER" <<'SH'
#!/bin/sh
exit 1
SH
chmod +x "$FAIL_ADAPTER"
expect_fail 'launch reservation remains' ticket launch --repo "$REPO" --issue 3 --adapter "$FAIL_ADAPTER" --apply --phase implement --owner boss-A --generation "$GEN3"
expect_fail 'launch reservation' ticket launch --repo "$REPO" --issue 3 --adapter "$ADAPTER" --apply --phase implement --owner boss-A --generation "$GEN3"
ACTION_ID="$(python3 - "$STATE" <<'PY'
import json,sys
print(json.load(open(sys.argv[1]))['tickets']['3']['action_id'])
PY
)"
expect_fail 'precondition failed' ticket confirm --repo "$REPO" --issue 3 --action-id wrong --task-id task-3
python3 - "$ROOT/no-tasks.json" <<'PY'
import datetime,json,sys
json.dump({'as_of':datetime.datetime.now(datetime.timezone.utc).isoformat(),'tasks':[]},open(sys.argv[1],'w'))
PY
run ticket abandon --repo "$REPO" --issue 3 --action-id "$ACTION_ID" --phase implement --owner boss-A --generation "$GEN3" --inventory "$ROOT/no-tasks.json" --evidence 'Checked the task list and found no task' > /dev/null
run ticket launch --repo "$REPO" --issue 3 --adapter "$ADAPTER" --apply --phase implement --owner boss-A --generation "$GEN3" > /dev/null
run ticket pr --repo "$REPO" --issue 2 --pr 11 > /dev/null
expect_fail 'already linked' ticket pr --repo "$REPO" --issue 1 --pr 11
expect_fail 'already linked' ticket pr --repo "$REPO" --issue 2 --pr 12
run status --repo "$REPO" > "$ROOT/status"
python3 - "$ROOT/status" <<'PY'
import json,sys
data=json.load(open(sys.argv[1]))
assert data['merged_5h']['feature']==1
assert data['unlinked_open_prs']==[]
PY

# Failed inventory leaves the scan timestamp and state untouched.
run scan --repo "$REPO" > /dev/null
printf '%s\n' '{"bad":true}' > "$PRS"
state_before="$(shasum -a 256 "$STATE")"
expect_fail 'invalid PR inventory' scan --repo "$REPO"
test "$state_before" = "$(shasum -a 256 "$STATE")"
printf '%s\n' '[]' > "$PRS"
run scan --repo "$REPO" > /dev/null
run status --repo "$REPO" > "$ROOT/status"
python3 - "$ROOT/status" <<'PY'
import json,sys
data=json.load(open(sys.argv[1]))
assert data['resolved_actions_24h']
assert all(item['status']=='resolved' for item in data['resolved_actions_24h'])
PY
run doctor --repo "$REPO" > "$ROOT/doctor"
python3 - "$ROOT/doctor" <<'PY'
import json,sys
assert json.load(open(sys.argv[1]))['monitor']['health']=='unconfigured'
PY
run monitor set --repo "$REPO" --name monitor-A > /dev/null
run doctor --repo "$REPO" > "$ROOT/doctor"
python3 - "$ROOT/doctor" <<'PY'
import json,sys
assert json.load(open(sys.argv[1]))['monitor']['health']=='unverified'
PY

# A live-read canary uses a mock gh executable. Only a PR list call is allowed.
MOCK_BIN="$ROOT/mock-bin"
mkdir -p "$MOCK_BIN"
cat > "$MOCK_BIN/gh" <<'SH'
#!/usr/bin/env sh
printf '%s\n' "$*" >> "$BOSS_GH_CALLS"
printf '%s\n' '[]'
SH
chmod +x "$MOCK_BIN/gh"
export BOSS_GH_CALLS="$ROOT/gh-calls"
state_before="$(shasum -a 256 "$STATE")"
PATH="$MOCK_BIN:$PATH" python3 "$BOSS" status --repo "$REPO" > /dev/null
test "$state_before" = "$(shasum -a 256 "$STATE")"
test "$(cat "$BOSS_GH_CALLS")" = 'pr list --repo example/project --state all --limit 500 --json number,state,createdAt,updatedAt,mergedAt,url,title,headRefOid,mergeable,reviewDecision,statusCheckRollup,isDraft'

# Table-driven PR classification: head, CI, conflict, review, and malformed input.
python3 - "$BOSS" <<'PY'
import datetime, importlib.util, sys
spec=importlib.util.spec_from_file_location('boss',sys.argv[1]); boss=importlib.util.module_from_spec(spec); spec.loader.exec_module(boss)
instant=datetime.datetime.now(datetime.timezone.utc)
base={'number':31,'state':'OPEN','headRefOid':'a'*40,'mergeable':'MERGEABLE','reviewDecision':'APPROVED','updatedAt':boss.iso(instant),'statusCheckRollup':[{'conclusion':'SUCCESS'}]}
cases=[
    ({},[]),
    ({'mergeable':'CONFLICTING'},['conflict']),
    ({'statusCheckRollup':[]},['no_checks']),
    ({'statusCheckRollup':[{'conclusion':'FAILURE'}]},['ci_failed']),
    ({'statusCheckRollup':[{'status':'IN_PROGRESS'}]},['ci_pending_age_unknown']),
    ({'statusCheckRollup':[{'status':'IN_PROGRESS','startedAt':boss.iso(instant-datetime.timedelta(minutes=5))}]},['ci_pending']),
    ({'statusCheckRollup':[{'status':'IN_PROGRESS','startedAt':boss.iso(instant-datetime.timedelta(hours=1))}]},['ci_stale']),
    ({'reviewDecision':'CHANGES_REQUESTED'},['changes_requested']),
    ({'headRefOid':'bad;touch-owned'},['head_unknown']),
]
for update,expected in cases:
    pr={**base,**update}
    actual=[item['kind'] for item in boss.pr_actions('example/project',pr,True,instant)]
    assert actual==expected,(update,actual,expected)
for invalid in ([{'status':'evil;touch-owned'}],{'bad':True}):
    try:
        boss.pr_actions('example/project',{**base,'statusCheckRollup':invalid},True,instant)
    except ValueError:
        pass
    else:
        raise AssertionError('malformed check accepted')
assert boss.pr_actions('example/project',base,True,instant)==boss.pr_actions('example/project',base,True,instant)
assert boss.pr_actions('example/project',base,False,instant)[0]['kind']=='unlinked'
PY
printf '%s\n' 'boss tests passed'
