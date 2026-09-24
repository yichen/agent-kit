---
name: q
description: Fork the current Codex task to answer a prompt in parallel and return its task link. Use when the user invokes $q or types /q.
---

# Parallel question

The text after `$q` (or a literal `/q` if the app passes it through) is the task. The calling task dispatches it to a fork and ends its turn so later prompts can run here. The answer appears in the forked task.

1. If there is no task text, give one short usage example and stop.
2. Call `mcp__codex_app__fork_thread` with `environment: { type: "same-directory" }` and no `threadId` to copy this task's completed history. Do not use a collaboration subagent or keep this turn open waiting for the answer.
3. The fork excludes this active turn. Send the exact task text to the returned `threadId` with `mcp__codex_app__send_message_to_thread`. Include any file paths or URLs supplied with this prompt that the fork needs. A same-directory fork should return `threadId` immediately; if it does not, stop without claiming the request started. Never send to `clientThreadId`.
4. After the prompt is accepted, end this turn immediately with only a link to the new task: `[Open q task](codex://threads/<threadId>)`. Do not poll, monitor, relay its answer here, or post a separate dispatch update. The user can continue this task and follow up with the fork directly.
5. If forking fails, say the request did not start. If the fork succeeds but sending the prompt fails, say that no work started and link to the empty fork. Do not claim that the request is running.

The fork copies completed context available at dispatch, not later messages or attachments from this active turn. If an attachment has no accessible path or URL to pass along, say so rather than claiming the fork has it. Compaction cannot restore raw earlier turns; ask the fork to consult durable memory or transcripts when that history matters.

Codex's documented direct skill invocation is `$q`. A plain `/q` works only when the app passes it through as message text and selects this skill; this file cannot register a built-in slash command.
