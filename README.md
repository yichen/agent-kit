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
