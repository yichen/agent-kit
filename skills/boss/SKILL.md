---
name: boss
description: Coordinate one repository's coding tickets, pull requests, and monitoring handoff. Use when the user invokes /boss or asks for a repo-level coding coordinator.
---

# Boss

Coordinate one repository at a time. The state is host scoped under `${AGENTS_ARTIFACTS_ROOT:-$HOME/agents-artifacts}/boss/`, keyed by the canonical GitHub origin owner/repository so sibling worktrees share one master. Use `scripts/boss.py` for deterministic state changes and reports. This skill is an agent workflow: the helper records decisions and evidence; the agent uses its available task and automation tools to do actual work.

## `/boss code` operating loop

An explicit `/boss init` or `/boss code` authorizes a separate coordinator task, a monitoring hub task, and a recurring heartbeat for the named repository. Reuse verified existing ones when present; otherwise create them through the host's task and automation tools. Keep the entry task free after a brief handoff. Record real task IDs with `init` and the real heartbeat ID with `monitor set`; do not create placeholder IDs. If a required host capability is unavailable, report the exact missing setup and leave its health unverified.

For `/boss code`, add the requested issues, kinds, and dependencies. Keep the task and PR mapping in state and check the host's task list before each launch. Use `reconcile` to fill available parallel slots with ready issues. Dispatch actual coding work through a configured idempotent adapter or the host's task/subagent tool. Record the returned task ID and PR link. Do not block the entry task while children work; the coordinator owns follow-up.

The recurring monitor should inspect all open PRs for this repository, the current boss state, and the exact current head; report failures, conflicts, missing checks, review gaps, unowned PRs, and stalled owners. Ask it to stay quiet while healthy and alert on material changes. Save the real automation ID with `monitor set`; verify it with the automation tool. The helper does not install an automation by itself, so if the host tool is unavailable, report the missing monitor as an open gap.

For each PR, verify current-head CI, independent review, dependencies, and any human gate. A green check or merged PR alone does not prove user acceptance or deployment. Merge only within the user's authorization and repository rules. After a verified merge, link and resolve the ticket, update or close the issue through that repository's required issue workflow, then archive finished child tasks if the host supports it. Run `reconcile` again to release newly ready issues. Send a handoff to the monitoring hub and obtain acknowledgment before ending ownership of the master task. Stop repeated dispatch on a failure rather than starting duplicates.

Start with `python3 scripts/boss.py init --repo <absolute-repo-path> --master <task-id> [--hub <task-id>]`. Repeating the same master is safe, including from another worktree of the same GitHub repository. A different master is rejected; use `adopt --repo ... --master ... --from <old-master>` only after checking the old coordinator is no longer active. Do not create a second live master.

Record work with `ticket add --repo ... --issue <number> --kind feature|testability --depends <comma-separated-issues>`. For product features, also supply `--summary`, `--availability unknown|testable|not_testable`, `--test-environment`, and `--human-gate` when known. Status reports `unknown` for facts not recorded; do not infer testability from a merge. Ticket numbers are unique per repository. A duplicate with different fields is rejected. Dependencies must be registered and verified as resolved before launch. Link a PR with `ticket pr --repo ... --issue ... --pr <number>`; mark completion with `ticket resolve --repo ... --issue ... --pr <linked-merged-pr> --note ...`. The helper checks that the PR is actually merged before it unblocks a dependent ticket. A merged PR is code evidence, not proof that a feature is usable in production; record human acceptance, release, and other gates in the coordinating task's handoff.

Run `reconcile --repo ...` to get ready tickets, dependency waits, launches in progress, launched tickets, unlinked PRs, and monitoring gaps. Work the ready queue in parallel up to the actual capacity of the chosen harness and its repo rules. Before starting each task, check for an existing task or PR for that issue in the state and in the host task list. Follow the repository's worktree instructions. After each child returns, review the exact PR and record its link. Keep the master task alive until its tracked work is resolved or explicitly handed off.

`ticket launch --repo ... --issue ... --adapter <absolute-executable> --apply` calls a configured adapter with one JSON request on stdin; it expects `{"task_id":"..."}` on stdout. Without `--apply`, it returns an eligibility preview and writes nothing. The request carries a stable `action_id`; an adapter MUST deduplicate on that ID before starting a task. The helper saves a `launching` reservation before calling the adapter. An interrupted or failed call stays in `launching` and is never silently retried. Check the host task list by action ID and issue. If the task exists, run `ticket confirm --repo ... --issue ... --action-id ... --task-id ...`. If it does not, run `ticket abandon --repo ... --issue ... --action-id ... --evidence '<what was checked>'` before another launch. The adapter must apply the repository's worktree and authorization rules. When no suitable adapter exists, use the host's available task creation tool for an explicitly requested new task, or a subagent for work kept in the current task; do not claim an unsupported launch. Do not launch a live coding task merely to test this skill.

Run `status --repo ...` for open PRs, PR IDs created and merged by hour in the last five hours, feature versus testability merge counts, merged product feature summaries and test availability, human decision gates, dependency waits, current PR actions, and the last 24 hours of verified ticket resolutions and resolved PR actions. It reads GitHub through `gh pr list` and writes nothing. `scan --repo ...` performs the same inventory and records a successful scan timestamp and action transitions. Action IDs are stable for a repository, PR, head, kind, and blocker. `doctor --repo ...` shows current PR actions and monitoring configuration. A manual scan does not prove a recurring monitor is running, so helper health remains `unverified` even after `monitor set --repo ... --name ...`. Inspect the live automation with the host tool when answering `doctor`; do not call a monitor healthy from local state alone. The helper classifies CI failures, pending or stale checks, missing checks, conflicts, review gaps, unknown heads, and unlinked PRs. It cannot attest that a reviewer inspected the current head; the agent must verify that separately.

`handoff --repo ...` prints a short report for the configured monitoring hub, including the exact repo, master, active tickets, open PRs, monitor state, and unresolved gaps. Send that report to the hub task only when the user authorized the communication or the monitoring workflow itself calls for it. Request an acknowledgment from the hub; retain the master task's ownership until the hub has accepted monitoring. `status`, `doctor`, `reconcile`, and `handoff` are read-only; they never start background jobs or make GitHub writes.

The helper's `--prs-file` fixture option is for local canary tests only. Do not pass it when reporting live GitHub status.

## Deterministic legacy-ledger transition

For a repository still using a separate ownership ledger, run the repository's
GitHub audit first, then supply a **fresh** Codex task inventory to
`scripts/reconcile_ledger.py`. Its `plan` command is read-only. `scan` writes a
durable outbox of action contracts and exits 3 when an action remains
unacknowledged after one 15-minute cycle, or when an acknowledged action has
not changed live state by the next cycle. The next monitor run must treat exit
3 as a failure, not a quiet status. Stale observations and unknown task states
exit 2 and block dispatch. The script does not trust `owner_active`, free-form
`next_gate`, or a parent epic's OPEN status for a PR-completed phase.

The task inventory format is `{"as_of":"2026-09-25T14:00:00Z","tasks":[{"id":"<Codex UUID>","status":"queued|running|completed|interrupted|blocked"}]}`.
Create it from the host's actual task API immediately before each scan; never
invent a status. A scope collision is a structured ledger field
`"dispatch_hold":{"until":"#542","reason":"overlapping session files"}`.
The `until` objective must be verified complete before dispatch. Keep human
acceptance in `human_gate`; the script never converts a human gate to code
completion.

Each action has a stable `id`, exact repository/objective/issue, task and PR
identifiers where known, exact current PR head where applicable, and a verb:
`LAUNCH_TASK`, `RESUME_TASK`, `REPAIR_PR`, `RECOVER_OWNER`,
`RECONCILE_ISSUE`, or `VERIFY_MERGE`.
The host worker may execute only that verb with its existing task tools and
authorization checks. After the tool succeeds, run `ack --outbox <absolute-path>
--id <id> --evidence '<task ID or other concrete result>'`. Acknowledgment is
compare-and-set: an absent or superseded action cannot be acknowledged. The
next scan verifies the action disappeared from live state; acknowledgment
alone never completes the ticket. `RECONCILE_ISSUE` means code is already
merged and the worker must inspect issue acceptance, rollout, and human gates;
it must not start another coding task merely because the issue is open.
`VERIFY_MERGE` is an inspection task, **not merge authorization**: the worker
must independently verify every required CI context on the exact current PR
head, current-head independent review, repository merge rules, and human gates
before deciding whether any merge is allowed. The script's `passed_count` is
only a signal to inspect; it is never proof that required contexts passed.

No universal CLI adapter for the Codex task API is installed by this skill.
In particular, LearnRise's `learnrise-code-exec` runs a coding turn
synchronously and cannot serve as the existing `/boss` launch adapter, which
expects a task ID within 120 seconds. Do not claim autonomous dispatch until
the host has a tested asynchronous worker that consumes this outbox,
deduplicates by action ID, records the real task ID, and acknowledges it.

### Host runtime bridge

`scripts/runtime_bridge.py` is the 15-minute host entry point for a legacy
LearnRise ledger. It reads open GitHub PRs, attaches only unambiguous `Closes
#N`/`Fixes #N`/`Resolves #N` links to the exact `#N` objective, refreshes the
ledger with its repository audit, builds a task inventory from the local Codex
catalog and rollout files, checks for live `codex exec resume --json <UUID>`
writer processes, then invokes `reconcile_ledger.py scan`. A live writer wins
over an earlier `task_complete`/`interrupted` record. Missing catalog entries,
ambiguous PR links, malformed rollouts, audit failures, and stale observations
fail closed. It never decides that a PR is safe to merge.

The bridge has no safe noninteractive API for creating desktop Codex tasks. Its
`--hub-task` option queues structured action IDs to an existing monitoring hub
with `codex queue`, then exits 3 while actions remain. This is an alert and
handoff, **not** automatic repair or task launch. The hub must execute the
action with its task tools, verify the returned task/PR ID, and call the
reconciler's `ack` command. If the action stays unchanged after 15 minutes,
the outbox remains overdue. Never report this installation as fully autonomous.

After this version is installed on the host, use a 15-minute launchd job or
equivalent host scheduler. Example command (replace home path and monitoring
hub UUID with verified values):

```sh
python3 "$HOME/.codex/skills/boss/scripts/runtime_bridge.py" \
  --ledger "$HOME/agents-artifacts/learnrise-orchestrator/ownership.json" \
  --audit "$HOME/agents-artifacts/learnrise-orchestrator/audit.py" \
  --state-db "$HOME/.codex/state_5.sqlite" \
  --tasks "$HOME/agents-artifacts/learnrise-orchestrator/codex-tasks.json" \
  --outbox "$HOME/agents-artifacts/learnrise-orchestrator/boss-outbox.json" \
  --hub-task '<verified-hub-uuid>'
```

Schedule every 900 seconds. Capture stdout and stderr in host logs and alert
on exits 2 or 3; `StartInterval` alone does not surface failures to a human.
For the current LearnRise host, the checked-in
`examples/com.yichen.boss.learnrise.plist` shows the exact paths and 900-second
schedule. Review its hub UUID against the live monitoring task, copy it to
`$HOME/Library/LaunchAgents/com.yichen.boss.learnrise.plist`, validate with
`plutil -lint`, then run `launchctl bootstrap gui/$(id -u)
$HOME/Library/LaunchAgents/com.yichen.boss.learnrise.plist`. Run
`launchctl print gui/$(id -u)/com.yichen.boss.learnrise` to verify it loaded;
inspect the stdout/stderr logs and a live scan before declaring it deployed.
The example must not be loaded before this code is merged and host skill
installation is updated.
Do not run a second bridge for the same outbox; the bridge uses a host lock,
and ledger PR attachment uses compare-and-set. Use `--dry-run --skip-audit
--prs-file <fixture> --processes-file <fixture>` for no-write local canaries.
Verify a live run after installation: fresh GitHub audit, current task status,
exact PR link, durable outbox, and monitoring-hub receipt. The local Codex
catalog applies only to tasks on this host; remote task IDs fail closed until
a supported remote inventory adapter exists.
