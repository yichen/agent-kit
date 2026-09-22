---
name: plan-review
description: Review a plan file before implementation starts, with a hard cap of three rounds, three reviewers holding different briefs, and re-review of the edit rather than the whole plan. Use when the user invokes /plan-review, $plan-review, or /skill:plan-review with a plan file, or asks to review a plan, proposal, or design document before building it. Do not use it to review code, a pull request, or a branch diff.
---

# Plan Review

Review one plan file and return it either approved or stopped with a named reason.

This skill exists because plan reviews that iterate without a stopping rule run for hours.
The measured worst case on this host ran 39 revisions and 67 review dispatches over 27 hours.
Most of that cost came from four things this skill fixes: no rule that ends the loop, fixes that create the next defect, every round re-checking every earlier round, and a plan that grows while it is being reviewed.

## Scope

Invoke as `/plan-review plan:<absolute-path>`.

Optional arguments:

- `repo:<absolute-path>` names the repository the plan's claims are checked against.
  Defaults to the current working directory when it is a Git repository.
- `base:<sha>` pins the commit the reviewers check against.
  When omitted the skill pins the repository's remote default branch head itself.

This skill edits the named plan file and writes to its own working directory under `/tmp/`.
It never touches repository source, never creates a worktree, never commits, and never opens a pull request.
It runs before `/implement`, and its output is the plan path that `/implement plan:<path>` consumes.

There is no argument that raises the round cap.
Three rounds is absolute within one invocation.
When the cap is reached, stop and report the open findings.
A caller who wants to continue invokes the skill again on the revised plan, which starts a new log carrying forward only findings closed with a re-runnable check.

## The loop

### Step 1. Pin the base and build the context pack

Run `scripts/plan-review-state.sh init <plan> --repo <repo> [--base <sha>]`.
It creates the working directory, pins the base commit, records the plan's starting size, and prints the working directory path.
Use that path for every later command in this run.

Run `scripts/plan-review-context.sh <plan> <workdir>`.
It extracts every file path, script name, and command the plan names, checks each one against the repository at the pinned commit, and writes `<workdir>/context.md`.
Each entry says whether the path exists, and for a path that exists, its size and first lines.

Read `<workdir>/context.md` before dispatching anything.
Its purpose is to stop a reviewer from spending a round on its own failed search.
One measured round was lost to a reviewer reporting that no caller existed for a script when the caller was on disk the whole time, and that false finding then cost a second round to undo.

### Step 2. Round 1 only: dispatch three reviewers at once

Send all three at the same time.
Each one receives the plan, `<workdir>/context.md`, and the pinned commit.
None of them receives the other two briefs.

The exact brief text is in `references/briefs.md`.
Send it verbatim so the three jobs stay distinct across runtimes and across runs.

- **claims** checks only whether each factual statement the plan makes about the repository is true at the pinned commit.
- **consequences** checks only whether the plan's steps, applied in order, produce the stated result, and whether any step undoes an earlier one.
- **deletion** checks only this: for each thing the plan says it will add, if that thing were left out, what would notice.

Three reviewers, always.
Do not compute the count from the plan's size.
The two worst measured loops ran on the smallest inputs, one of them a single 447 byte paragraph that took nine rounds.

Record every finding with the brief that raised it.
That tag is what makes Step 4 possible.

### Step 3. Apply the smallest edit that closes each blocking finding

Only blocking findings get edited.
Everything else goes to the log as noted and unfixed.

Before adding a case to an existing rule, check whether the rule can instead be rewritten to compute its answer.
If the same rule takes a finding in two consecutive rounds, stop editing it and rewrite it.
One measured rule grew 4.7 times over nine rounds by adding a case per round and still had cases open at the end.

Record each edit with `scripts/plan-review-state.sh record`, naming the finding it closes, the brief that raised that finding, and a command that can be re-run to check it.

### Step 4. Round 2 and after: re-review the edit, not the plan

Never repeat the Step 2 dispatch.
Dispatch only these reviewers, and give each one only the edits made since the previous round, the finding each edit was meant to close, and the context pack.

- Every brief that raised a finding edited in this round, to confirm its own finding is closed.
- The consequences brief, always, whenever any edit changed a step, added a step, or changed the order of steps.

No reviewer re-reads the whole plan.
No reviewer is asked to confirm that earlier findings are still closed.

The consequences brief runs on every step change because it asks whether a step undoes an earlier one.
That is the question the measured worst chain failed three times in a row: raise a row's contrast, then find the selected and hovered rows identical, then find the new border below the contrast bar the change itself had set.
Each defect was a consequence of the edit just made, and each was found a full round later by a fresh review of the whole plan.
Re-running one brief against one edit finds it in the same round.

One exception returns to a full three-reviewer dispatch.
If an edit touches a section no finding pointed at, the plan is being expanded rather than corrected.
Repeat the full dispatch once, and count it as a round.

### Step 5. Write the round to the log, then sort

The log is `<workdir>/log.md`, written by `scripts/plan-review-state.sh record`.

Reviewers never read this log.
The coordinator reads it once per round, to sort each new finding into new, a repeat of something already closed, or a reversal of something already decided.

A logged entry with no re-runnable check recorded against it cannot overrule a new finding.

Keeping reviewers out of the log is what stops this from becoming the pattern that made the 27 hour loop expensive, where round 20 was pointed at reviews v1 through v19 and the cost of a round grew with the round number.

### Step 6. Stop

Run `scripts/plan-review-state.sh gate <workdir> --blocking <count>` after every round.
It prints one line and exits 0 to continue or 10 to stop.
Its decision is authoritative.

It stops on any of these.

- No reviewer reported a blocking finding.
  The plan is approved.
  Findings below blocking are written into the plan as known and accepted, and do not start another round.
- Three rounds have completed.
  Report the open findings. Do not silently continue.
- The pinned base has moved.
  Report the new head and ask whether to restart on the new base.
  Do not re-review against a head that moved for unrelated reasons.
- The plan has grown more than 50 percent in bytes since round 1.
  Report that the plan is being expanded rather than corrected, with the byte counts.

## Dispatching reviewers

Use the current runtime's own subagent mechanism.
This skill names no runtime-specific dispatch tool, because it runs under Claude Code, Codex, and Pi from one installed copy.

In Pi, a reviewer is a nested `pi -p` call, and three host behaviors will silently break it.
Use `scripts/plan-review-pi-dispatch.sh <brief-file> <cwd>`, which handles all three.

1. Pi loads a skill in two stages: the description enters the system prompt, and the body is read with the `read` tool.
   A dispatch with `--no-tools` sees no skill at all.
2. `pi -p` in its default text mode prints nothing when tools are enabled.
   Dispatches must use `--mode json` and parse the result.
3. Pi exits with status 0 and no output when the working directory is absent from `~/.pi/agent/trust.json`.
   An empty result is a failed dispatch, never an approval.
