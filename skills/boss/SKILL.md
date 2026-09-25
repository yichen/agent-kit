---
name: boss
description: Coordinate one repository's coding tickets, pull requests, and monitoring handoff. Use when the user invokes /boss or asks for a repo-level coding coordinator.
---

# Boss

Coordinate one repository at a time. The state is host scoped under `${AGENTS_ARTIFACTS_ROOT:-$HOME/agents-artifacts}/boss/`, keyed by the canonical GitHub origin owner/repository so sibling worktrees share one master. Use `scripts/boss.py` for deterministic state changes and reports. This skill is an agent workflow: the helper records decisions and evidence; the agent uses its available task and automation tools to do actual work.

For each issue phase, use `scripts/operational_store.py` as the atomic claim and action ledger. Its SQLite database is `${AGENTS_ARTIFACTS_ROOT:-$HOME/agents-artifacts}/boss/operations.sqlite3`; rows are keyed by canonical GitHub repository, issue, and phase. A claim returns a monotonic generation. Every reserve, dispatch, acknowledgment, or release must carry the active claim's owner and generation; acknowledgment also checks that both match the reserved action. A stale generation or different owner cannot act. Release is blocked while a launch lacks verified effect. Event history is append-only and `rebuild --inventory <fresh-inventory.json>` reconstructs the projections from that history, then checks task effects against the fresh live inventory. GitHub PR and issue facts continue to come from live GitHub inventories.

Claim before deciding or dispatching: `python3 scripts/operational_store.py claim --repo <absolute-repo-path> --issue <number> --phase <phase> --owner <stable-worker-id>`. To launch, use `dispatch` with the returned generation, a stable verb, and an absolute adapter path. The store commits the stable action ID before invoking the adapter and never automatically repeats an existing action. The adapter must deduplicate on `action_id`. If dispatch is interrupted, inspect the live task inventory and call `scan --inventory <fresh-inventory.json>`; the inventory schema is `{"as_of":"<UTC timestamp>","tasks":[{"id":"<task-id>","action_id":"<64-character action ID>","status":"queued|running|completed|interrupted|blocked"}]}`. Inventory timestamps must be timezone-aware and no more than 15 minutes old. A scan recovers an acknowledgment when it finds the action ID, then verifies that task effect. Missing acknowledgments or effects stay visible in `status` and append to `history`. If a fresh inventory proves the task was never created, explicitly `abandon` that reserved action with the inventory and brief evidence before releasing the claim; the store refuses abandonment while an adapter still holds its action lock and requires the inventory timestamp to follow the reservation. A later attempt receives a new stable action ID. Do not retry a reserved action just because its adapter call failed or timed out.

## `/boss code` operating loop

An explicit `/boss init` or `/boss code` authorizes a separate coordinator task, a monitoring hub task, and a recurring heartbeat for the named repository. Reuse verified existing ones when present; otherwise create them through the host's task and automation tools. Keep the entry task free after a brief handoff. Record real task IDs with `init` and the real heartbeat ID with `monitor set`; do not create placeholder IDs. If a required host capability is unavailable, report the exact missing setup and leave its health unverified.

For `/boss code`, add the requested issues, kinds, and dependencies. Keep the task and PR mapping in state and check the host's task list before each launch. Use `reconcile` to fill available parallel slots with ready issues. Acquire an issue-phase claim before launch. For configured adapters, `ticket launch --apply` requires `--phase`, `--owner`, and the active `--generation`; it reserves the stable SQLite action ID before invoking the adapter, then compare-and-set acknowledges the returned task ID. For a host task tool, reserve the action, call `start` with its action ID immediately before invoking the task tool, pass the action ID as the task's deduplication key when supported, then acknowledge the created task. `start` returns a one-time token; keep it with the caller and pass it to `finish` only after the tool has definitively returned without creating a task. The store retains only a hash of the token, so another coordinator sharing the same owner ID cannot finish this in-flight action. A started action cannot be abandoned while it is in flight; if the coordinator stops before recording the result, keep it reserved until a scan finds the task or the original call's completion is confirmed by its caller. Record and verify the returned task ID against a fresh inventory before releasing the claim. Do not block the entry task while children work; the coordinator owns follow-up.

The recurring monitor should inspect all open PRs for this repository, the current boss state, and the exact current head; report failures, conflicts, missing checks, review gaps, unowned PRs, and stalled owners. Ask it to stay quiet while healthy and alert on material changes. Save the real automation ID with `monitor set`; verify it with the automation tool. The helper does not install an automation by itself, so if the host tool is unavailable, report the missing monitor as an open gap.

For each PR, verify current-head CI, independent review, dependencies, and any human gate. A green check or merged PR alone does not prove user acceptance or deployment. Merge only within the user's authorization and repository rules. After a verified merge, link and resolve the ticket, update or close the issue through that repository's required issue workflow, then archive finished child tasks if the host supports it. Run `reconcile` again to release newly ready issues. Send a handoff to the monitoring hub and obtain acknowledgment before ending ownership of the master task. Stop repeated dispatch on a failure rather than starting duplicates.

Start with `python3 scripts/boss.py init --repo <absolute-repo-path> --master <task-id> [--hub <task-id>]`. Repeating the same master is safe, including from another worktree of the same GitHub repository. A different master is rejected; use `adopt --repo ... --master ... --from <old-master>` only after checking the old coordinator is no longer active. Do not create a second live master.

Record work with `ticket add --repo ... --issue <number> --kind feature|testability --depends <comma-separated-issues>`. For product features, also supply `--summary`, `--availability unknown|testable|not_testable`, `--test-environment`, and `--human-gate` when known. Status reports `unknown` for facts not recorded; do not infer testability from a merge. Ticket numbers are unique per repository. A duplicate with different fields is rejected. Dependencies must be registered and verified as resolved before launch. Link a PR with `ticket pr --repo ... --issue ... --pr <number>`; mark completion with `ticket resolve --repo ... --issue ... --pr <linked-merged-pr> --note ...`. The helper checks that the PR is actually merged before it unblocks a dependent ticket. A merged PR is code evidence, not proof that a feature is usable in production; record human acceptance, release, and other gates in the coordinating task's handoff.

Run `reconcile --repo ...` to get ready tickets, dependency waits, launches in progress, launched tickets, unlinked PRs, and monitoring gaps. Work the ready queue in parallel up to the actual capacity of the chosen harness and its repo rules. Before starting each task, check for an existing task or PR for that issue in the state and in the host task list. Follow the repository's worktree instructions. After each child returns, review the exact PR and record its link. Keep the master task alive until its tracked work is resolved or explicitly handed off.

`ticket launch --repo ... --issue ... --adapter <absolute-executable> --phase <phase> --owner <worker-id> --generation <active-generation> --apply` calls a configured adapter with one JSON request on stdin; it expects `{"task_id":"..."}` on stdout. Without `--apply`, it returns an eligibility preview and writes nothing. The request carries the stable SQLite `action_id`; an adapter MUST deduplicate on that ID before starting a task. The operational store commits the reservation before the adapter call and holds its action lock until the adapter exits. An interrupted or failed call remains recorded and is never silently retried. Inspect a fresh task inventory by action ID. If the task exists, run `scan` to recover acknowledgment and verify the effect, then run `ticket confirm --repo ... --issue ... --phase <phase> --owner <worker-id> --generation <generation> --action-id <action-id> --task-id <task-id>` to reconcile the ticket record; this also performs the owner- and generation-fenced SQLite acknowledgment and is safe to repeat for the same task. If the fresh inventory captured after the reservation shows no task, run `ticket abandon` with the same phase, owner, generation, action ID, inventory, and concise evidence before another launch. For direct host task tools, call `start` after `reserve` and immediately before task creation; keep the one-time token returned by `start`, and include it as `--token` only when a definitive no-task result is passed to `finish`. After `finish`, use a fresh inventory and `abandon`. `finish` without the matching token fails, including for another process using the same owner and generation. Never abandon a `dispatching` action: if the host call's result is unknown, wait for a scan to find its effect or confirm that the call ended before retrying. The adapter must apply the repository's worktree and authorization rules. When no suitable adapter exists, use the host's available task creation tool for an explicitly requested new task, or a subagent for work kept in the current task; do not claim an unsupported launch. Do not launch a live coding task merely to test this skill.

Run `/boss status` (or `python3 scripts/boss.py status --repo ... --json`) for each ticket and linked PR, SQLite claim owner and phase, visible task/session ID and link, blocker, dependencies, human gate, last meaningful action, scan freshness, and open PR actions. Run `/boss metrics` (or `python3 scripts/boss.py metrics --repo ... --json`) for five hourly created/merged PR buckets with numbers, rolling 24-hour resolved-ticket metrics, and testable merged product features. These commands read GitHub through `gh pr list` and the operational SQLite store in read-only mode; they never initialize state, take a write lock, start a job, send a request that changes external state, or wake a session. If GitHub, the claim store, or the last scan is stale or unavailable, affected facts are `unknown` with the error and check timestamp. `--json` emits the machine-readable report.

Run `/boss history <item>` (or `python3 scripts/boss.py history <item> --repo ... --json`) for a tracked issue, PR, task ID, or action ID. It reads append-only SQLite events plus recorded PR action history and never changes either store. `scan --repo ...` performs the live inventory and records a successful scan timestamp and action transitions. Action IDs are stable for a repository, PR, head, kind, and blocker. `doctor --repo ...` shows current PR actions and monitoring configuration. A manual scan does not prove a recurring monitor is running, so helper health remains `unverified` even after `monitor set --repo ... --name ...`. Inspect the live automation with the host tool when answering `doctor`; do not call a monitor healthy from local state alone. The helper classifies CI failures, pending or stale checks, missing checks, conflicts, review gaps, unknown heads, and unlinked PRs. It cannot attest that a reviewer inspected the current head; the agent must verify that separately.

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
`RECONCILE_ISSUE`, `VERIFY_MERGE`, or `QUARANTINE_PR`.
The host worker may execute only that verb with its existing task tools and
authorization checks. After the tool succeeds, run `ack --outbox <absolute-path>
--id <id> --evidence '<task ID or other concrete result>'`. Acknowledgment is
compare-and-set: an absent or superseded action cannot be acknowledged. The
next scan verifies the action disappeared from live state; acknowledgment
alone never completes the ticket. `RECONCILE_ISSUE` means code is already
merged and the worker must inspect issue acceptance, rollout, and human gates;
it must not start another coding task merely because the issue is open.
The reconciler processes the complete `open_pull_requests` inventory before
issue completion, dispatch holds, dependencies, or human gates. Untracked or
ambiguous PRs produce `QUARANTINE_PR`; candidate tracked objectives wait until
ownership is resolved, preventing a duplicate launch. A linked PR needs its
exact head SHA, mergeability, exact `required_checks` names, and one check
observation per name for that same head. Partial pass counts never authorize
`VERIFY_MERGE`. A missing or duplicated required context requests owner
recovery, a failed, stale (over 45 minute), or wrong-head context requests PR
repair, and pending checks wait only while their start time is fresh. The
required-context list comes from active branch rules and branch protection,
independent of check results. If no required-check policy is configured, the PR
stays visible with an owner-recovery action; unrelated PRs and issues continue
through the same scan.
`VERIFY_MERGE` is an inspection task, **not merge authorization**: the worker
must independently verify every required CI context on the exact current PR
head, current-head independent review, repository merge rules, and human gates
before deciding whether any merge is allowed. The script's `passed_count` is
only a signal to inspect; it is never proof that required contexts passed.

LearnRise's `learnrise-code-exec` runs a coding turn synchronously and cannot
serve as the existing `/boss` launch adapter, which expects a task ID within
120 seconds. The checked-in Codex adapter below provides the asynchronous
worker integration when an app-server is already running. Do not claim the
host is autonomous until the app-server launch canary, #18 shadow comparison,
and pilot pass.

`scripts/codex_worker_adapter.py` is the local Codex worker adapter for
`ticket launch --apply`. It connects only to an already running managed
app-server through `codex app-server proxy`; it never starts or restarts a
daemon. Launch checks that the issue is open, no open PR already references
it, the source checkout is clean, and its GitHub default branch can be freshly
fetched. It creates and verifies a sibling worktree and a stable action branch,
then starts one visible Codex thread with `on-request` approvals and
`workspace-write` sandboxing. Its `threadSource` records the SQLite action ID,
so a retry or a caller crash can recover the same real task UUID and
`codex://threads/<UUID>` link instead of starting another task. An uncertain
timeout leaves the action reserved; inspect the `inventory` operation before
any retry.

The read-only `inventory` operation uses `thread/list` with state-DB-only
enumeration and reads tagged tasks with `thread/read`; it does not initialize
or start an app-server daemon. It reports approval/user-input waits as
`blocked`. `resume` requires both the exact action ID and exact task UUID,
checks current app-server status and local writer processes, refuses an
active task or any live writer, and resumes only its verified worktree. If the
app-server socket is unavailable, launch, inventory, and resume fail closed.
Do not report task setup complete while only a pending client ID is known.

### Host runtime bridge

`scripts/runtime_bridge.py` is the 15-minute host entry point for a legacy
LearnRise ledger. It reads every open GitHub PR, links unambiguous `Closes
#N`/`Fixes #N`/`Resolves #N` references, and preserves ambiguous or untracked
PRs as quarantine observations on an isolated ledger snapshot. It reads the
exact required checks and current head for each PR before auditing that
snapshot, then builds a task inventory from the local Codex
catalog and rollout files, checks for live `codex exec resume --json <UUID>`
writer processes, then invokes `reconcile_ledger.py scan`. A live writer wins
over an earlier `task_complete`/`interrupted` record. Missing catalog entries,
ambiguous PR links, malformed rollouts, audit failures, and stale observations
fail closed. The canonical ownership ledger is never overwritten by this bridge;
the audited `boss-observed-ledger.json` snapshot lives next to its task inventory.
The snapshot carries discovered PR links across runs, including after merge.
If the canonical ledger changes during an audit, the bridge stops before deciding
actions and retries from fresh ownership on the next run. It checks the source
again before writing the action outbox and before queuing a hub message. The hub
must still recheck live ownership before executing an action. If an observed PR
link was wrong, stop the bridge, correct or remove its observed snapshot, then
restart; removing it only from the canonical ledger will not remove the carried
link.
It never decides that a PR is safe to merge.

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
  --outbox "$HOME/agents-artifacts/learnrise-orchestrator/boss-action-outbox.json" \
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
and PR links are projected onto its own snapshot. Use `--dry-run --skip-audit
--prs-file <fixture> --processes-file <fixture>` for no-write local canaries.
Verify a live run after installation: fresh GitHub audit, current task status,
exact PR link, durable outbox, and monitoring-hub receipt. The local Codex
catalog applies only to tasks on this host; remote task IDs fail closed until
a supported remote inventory adapter exists.
