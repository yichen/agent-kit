---
name: qwen-delegate
description: Attempt short, bounded, non-authoritative text microtasks with the host's local Ollama Qwen 3.8 27B model, using hard prompt, output-token, and latency limits plus immediate normal-path fallback. Use for cheap-to-verify summaries, extraction, classification, micro-drafts, test candidates, small diff screens, and incident-evidence reviews; do not use for implementation, architecture, high-stakes decisions, final review, approval, or large-context work.
---

# Qwen Delegate

Use the local model as optional spare capacity, never as a dependency or authority. The caller owns the complete user request and independently verifies any local result it uses.

## Route only microtasks

Delegate only a self-contained portion that:

- is text-only and grounded entirely in evidence supplied in the prompt;
- can produce a useful answer within its fixed 10–18 second profile, or the explicit 30-second extended ceiling;
- fits within 256 output tokens normally or 384 with `--extended`;
- is cheap to verify against the same supplied evidence; and
- contains no secrets, credentials, raw personal data, or unnecessary proprietary context.

Suitable results are advisory summaries, extraction, classification, micro-drafts, test-case candidates, small diff screens, and incident-evidence hypotheses.

Keep the normal path for ambiguous or repository-wide investigation, code changes, architecture, security/privacy/legal judgment, authorization or payment logic, migrations, concurrency correctness, final code review, readiness or approval, publication, merge, or anything needing tools or a longer answer. Explicit invocation does not relax these boundaries.

## Preserve the full request

First understand every requested deliverable and constraint. Delegate only eligible, self-contained portions with the minimum necessary evidence. Never send the full user prompt merely because this skill was invoked.

The main agent completes every non-delegated portion, checks every claim, path, identifier, command, and recommendation it might use, and continues normally when delegation falls back or produces an unhelpful candidate. The local model has no tools; never imply it inspected anything not included in its prompt.

## Generate a candidate

Use [scripts/qwen-delegate.sh](scripts/qwen-delegate.sh). It applies task-specific hard ceilings before acquiring the shared lock, sets Ollama `num_predict`, never retries or queues, and publishes only an advisory V2 envelope.

Create a unique artifact directory under `${AGENTS_ARTIFACTS_ROOT:-$HOME/agents-artifacts}/qwen-delegate/`, write a bounded UTF-8 prompt, and choose a nonexistent output path:

```bash
$HOME/.agents/skills/qwen-delegate/scripts/qwen-delegate.sh \
  --task-type test-plan \
  --caller qwen-delegate/test-plan \
  --prompt-file "$artifact_dir/prompt.txt" \
  --output-file "$artifact_dir/result.md"
```

`diff-screen` and `incident-review` also require a stable `--source-id`, such as the exact commit SHA or CI run ID. Use `--extended` only when a 384-token answer remains a cheap-to-verify microtask. Callers may lower timeout or output limits but cannot raise the selected profile.

Read [references/contracts.md](references/contracts.md) when choosing a profile, troubleshooting validation, or recording result disposition.

On exit 0, read and verify the candidate. On any other exit, ignore diagnostics as task output and immediately complete that portion through the normal path. Do not ask the user to fix local-model availability.

## Verify and record use

The V2 envelope is always `AUTHORITY: ADVISORY_ONLY` with `DECISION_AUTHORITY: NONE`. Its wrapper-computed digest binds it to the exact prompt evidence.

Before using a result, verify the same evidence bytes and current source identity:

```bash
$HOME/.agents/skills/qwen-delegate/scripts/qwen-delegate.sh \
  --verify-result "$artifact_dir/result.md" \
  --evidence-file "$artifact_dir/prompt.txt" \
  --current-source-id "$source_id"
```

After independent verification, record exactly one terminal disposition using `--mark-result verified-used`, `verified-rejected`, or `superseded` with `--result-file`. `verified-used` also requires the evidence file and current source ID. Delivery alone is not verification and never authorizes approval, readiness, publication, or merge.

## Shared runtime

The wrapper pins the shared lock and append-only telemetry used by other local-model callers:

- `$HOME/.local/state/sharedanchor-ollama-delegate/lock`
- `$HOME/.local/state/sharedanchor-ollama-delegate/events.log`

Do not override the lock, wait, retry contention, or enqueue work. The model must already be loaded. Telemetry records identity hashes, task type, outcome, token counts, and numerical timing only—not prompt or response content.

Use `$HOME/.agents/skills/qwen-delegate/scripts/qwen-delegate-report.sh --date YYYY-MM-DD` for daily health, latency, throughput, verification yield, and verified-use value.
