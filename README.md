# Agent Kit

This repository is the source of truth for personal host-scoped agent skills.
The checkout stays at `$HOME/work/agent-kit` on `main`.
Each installed skill is an absolute symbolic link from `$HOME/.agents/skills/<name>` to `skills/<name>` in this checkout.
Claude Code and Pi use adapter links that point back to the same primary link.
Codex reads the primary skill directory directly.

## Install the skills

Run the installer after cloning or updating the repository:

```bash
./install.sh install
```

The first migration from an existing copied skill directory requires one explicit adoption:

```bash
./install.sh install --adopt-existing
```

Adoption moves the existing target beside itself with a timestamped suffix before creating the link.
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
