# Agent Kit

This repository is the source of truth for personal host-scoped agent skills.
The checkout stays at `$HOME/work/agent-kit` on `main`.
Each installed skill is an absolute symbolic link from `$HOME/.agents/skills/<name>` to `skills/<name>` in this checkout.
Codex, Claude Code, and Pi use adapter links that point back to the same primary link.

## Install the skills

Run the installer after cloning or updating the repository:

```bash
./install.sh install
```

The first migration from an existing copied skill directory requires one explicit adoption:

```bash
./install.sh install --adopt-existing
```

Adoption moves the existing target to `$HOME/.agent-kit/backups/<runtime>/` with a timestamped suffix before creating the link. Backups stay outside skill discovery directories.
The installer is idempotent after the first successful run.

Verify the links and any skill-specific host services without changing host state:

```bash
./install.sh check
```

## Run the tests

Run every tracked skill and installer test:

```bash
./test.sh
```

Develop changes in a sibling Git worktree.
Merge reviewed changes into `main`, then fast-forward this checkout so every host runtime sees the tested version.

## Skills

### boss

Coordinates one repository's coding tickets and PRs through a single master task. `/boss code` tracks dependencies, dispatches ready work through the host's task tools or a configured idempotent adapter, reports PR health and five-hour throughput, and hands monitoring to a verified host automation and hub task. The helper fails closed when task or monitor integration is unavailable.

### plan-review

Reviews a plan file before implementation starts.
Invoke as `/plan-review plan:<absolute-path>`.

It dispatches three reviewers at once holding different briefs, applies the smallest edit that closes each blocking finding, then re-reviews the edit rather than the whole plan.
A hard cap of three rounds cannot be raised by an argument.

It exists because plan reviews that iterate without a stopping rule run for hours.
The measured worst case on this host ran 39 revisions and 67 review dispatches over 27 hours.

### q

Delegates a `$q` prompt to a subagent and returns its answer in the calling Codex task.
It does not post a dispatch notice or save the answer to a separate file.

## Extensions

Pi extensions live in `extensions/<name>/` and install as a symlink from
`$HOME/.pi/agent/extensions/<name>` to this checkout, so a checkout
fast-forward after merge updates every future pi session with no extra steps.
`install.sh check` verifies the link; the first link creation needs one
`./install.sh install` run.

### orchestrator

A long-running pi session that babysits worker coding agents in herdr panes.
It launches one local-model pi worker at a time (`maxWorkers`, default 1),
runs a mechanical watcher, and wakes the orchestrator conversation only on
judgment events. The worker driver, launch gate, stall detection, CI round
cap, dialog allowlist, and wake batching are pure functions in
`extensions/orchestrator/lib.mjs` with table-driven no-token tests; every
herdr/gh/filesystem effect lives in `extensions/orchestrator/executor.mjs`.

Tier 1 (extension, no model tokens): templated CI-failure nudges up to
`ciMaxRounds` (default 5), resume nudges, escape on stalls, allowlisted
dialog auto-answers, pane close after merge, and fast abort recovery — a
worker whose turn died ("Operation aborted") gets an immediate continue
nudge (tail-anchored screen match, capped, then a `WORKER_ABORT_LOOP` wake).
Tier 2 (wakes the orchestrator session): unknown blocked dialogs (fail
closed — the default allowlist is empty), CI rounds exhausted, merge
timeout, persistent stall, worker lost, abort loop, cycle done (triage next
ticket).
Tier 3 (escalate to the human): the orchestrator session decides; prod
data, consent/child-data, and legal boundaries are never auto-answered.

Configuration is per consuming repo in `.pi/orchestrator.json` (all keys
optional except none — defaults encode maxWorkers 1, pi on
`ollama/qwen3.8:27b-mlx-128k`, 15-minute stall window, 5 CI rounds, empty
dialog allowlist). Unknown keys, malformed values, or an invalid regex
disable the watcher instead of guessing. State persists outside any repo at
`${AGENTS_ARTIFACTS_ROOT:-$HOME/agents-artifacts}/orchestrator/state.json`
and is reconciled against live herdr truth every tick; herdr is the source
of truth and pane IDs are never reused, so stale records are always
detectable.

### Multi-repo registry and resume-first (v2)

One orchestrator session can drive workers in any registered repo. Add a
`repos` map to the config:

```json
{
  "repos": {
    "learnrise": { "path": "/Users/me/work/LearnRise" },
    "sharedanchor": { "path": "/Users/me/work/SharedAnchor",
                      "invocation": "/code ticket:{ticket} profile:local" }
  }
}
```

`orch_launch_worker` accepts `repo:<key>` and opens the worker pane in that
repo's path with its per-repo invocation/startArgs overrides; unknown keys
or nonexistent paths refuse the launch. The 1-worker cap stays host-wide —
one queue across all repos, which is what a single local model wants.

Resume-first: every launch records the worker's pi session path, and
`orch_change_worker {provider, model, reason}` changes a task's model or
effort by relaunching the SAME session with `pi --session <path>` — ticket,
repo, worktree, and CI-round count carry over (the round cap cannot be
reset by switching models). A fresh `orch_launch_worker` for a ticket that
has a resumable session is refused unless `forceFresh: true`.

Run the no-token tests and the opt-in live canary (real herdr panes, real
pi worker on ollama, stubbed `gh`; creates and closes its own panes and
fails if any survive):

```bash
node --test extensions/orchestrator/tests/orchestrator-lib.test.mjs
AGENT_KIT_ORCH_CANARY=1 bash extensions/orchestrator/tests/orchestrator-canary.test.sh
```
