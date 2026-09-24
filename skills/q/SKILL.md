---
name: q
description: Answer a prompt through a subagent and show its answer in the same Codex task. Use when the user invokes $q or types /q.
---

# Background question

The text after `$q` (or a literal `/q` if the app passes it through) is the task. Delegate the answer to a subagent. The parent is a quiet relay; it must not answer the prompt itself.

1. If there is no task text, give one short usage example and stop.
2. Spawn exactly one subagent with `fork_turns: "all"`. Give it the exact task text and ask it to complete the task, respect all applicable instructions and authorization boundaries, and return its full user-facing answer in its final message. Do not ask it to write an answer file. Do not use the `$do` single-file queue.
3. Do not post a dispatch confirmation, agent name, progress update, or file path. Keep the parent turn open and wait quietly for the subagent's final answer. Then send that answer in this same task as the parent's final response. The final response should contain only the answer to the `$q` prompt, with no relay preface.
4. If the user sends a correction while the subagent is working, pass it to the subagent. Continue waiting for the answer unless the user cancels the request. If no subagent slot is available, state briefly that the request could not start and why; do not answer it in the parent.

The fork copies the conversation context available at dispatch, not later messages. If the parent's context has already been compacted, `fork_turns: "all"` cannot restore raw turns; have the child consult durable memory or transcripts when that history matters.

Codex's documented direct skill invocation is `$q`. A plain `/q` works only when the app passes it through as message text and selects this skill; this file cannot register a built-in slash command.
