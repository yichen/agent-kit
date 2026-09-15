# Qwen Delegate V2 contracts

Read this reference when selecting limits, diagnosing rejected output, or recording whether a delivered candidate was used.

## Fixed profiles

| Task type | Prompt bytes | Output tokens | Timeout | Required body sections |
|---|---:|---:|---:|---|
| `classify` | 4,096 | 96 | 10s | `RESULT`, `EVIDENCE`, `UNCERTAINTY` |
| `extract` | 5,120 | 160 | 12s | `ITEMS`, `EVIDENCE`, `MISSING` |
| `summarize` | 8,192 | 192 | 15s | `SUMMARY`, `EVIDENCE`, `OMISSIONS` |
| `draft` | 5,120 | 256 | 18s | `DRAFT`, `ASSUMPTIONS`, `OPEN_QUESTIONS` |
| `test-plan` | 8,192 | 256 | 18s | `CASES`, `RISKS`, `OPEN_QUESTIONS` |
| `diff-screen` | 10,240 | 256 | 18s | `FINDINGS`, `EVIDENCE`, `LIMITATIONS` |
| `incident-review` | 10,240 | 256 | 18s | `HYPOTHESES`, `EVIDENCE`, `NEXT_CHECKS` |

`--extended` is available only for `draft`, `test-plan`, `diff-screen`, and `incident-review`. It raises the output ceiling to 384 tokens and the absolute timeout to 30 seconds. It does not raise the prompt-byte ceiling. If useful work cannot fit, keep it on the normal path.

Prompt bytes are a conservative transport bound because Ollama does not expose a tokenizer-only endpoint. The helper retains its separate context-safety check.

## Published envelope

The wrapper, not the model, adds identity and authority fields:

```text
QWEN_DELEGATE_V2
AUTHORITY: ADVISORY_ONLY
TASK_TYPE: <task-type>
CALLER: <caller>
SOURCE_ID: <source identifier or prompt>
EVIDENCE_SHA256: <SHA-256 of exact prompt-file bytes>
BODY_SHA256: <SHA-256 of the validated body>
RECEIPT_ID: <generation event ID>
BODY_BEGIN
<task-specific sections>
BODY_END
DECISION_AUTHORITY: NONE
END_QWEN_DELEGATE_V2
```

`diff-screen` and `incident-review` require an explicit source identifier. Verification recomputes both digests from the evidence file and published body, checks the task-specific fields, and compares the current source identifier. A mismatch is a safe fallback.

## Result lifecycle

Successful generation records `delivered`. Delivery means only that a bounded, structurally valid candidate reached the caller.

After checking the candidate against its evidence, record one terminal state:

- `verified-used`: the evidence and source still match, every used claim was checked, and some result was incorporated.
- `verified-rejected`: the candidate was current but wrong, unhelpful, incomplete, or unused.
- `superseded`: the source or evidence changed before use.

Lifecycle events are append-only and idempotent. A result cannot move between terminal states. None of these states represents code-review approval or permission for an external action.
