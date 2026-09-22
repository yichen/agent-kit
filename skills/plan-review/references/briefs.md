# Reviewer briefs

Send one of these verbatim to each reviewer in round 1.
Do not send a reviewer more than one brief, and do not tell a reviewer what the other briefs are.
Substitute `<PLAN>`, `<CONTEXT>`, and `<BASE>` before sending.

Every brief ends with the same output contract so the coordinator can sort findings without parsing prose.

## Shared output contract

```
Report each finding on its own, in this exact shape:

FINDING: <one sentence saying what is wrong>
WHERE: <section heading or line number in the plan>
EVIDENCE: <file:line in the repository, or the plan's own text>
BLOCKING: yes | no
CHECK: <a command that can be re-run to confirm this is fixed, or NONE>

Mark BLOCKING yes only when building from this plan would produce the wrong
result or fail. Everything else is BLOCKING no.

If you have no findings, reply with exactly: NO FINDINGS
```

## claims

```
You are reviewing a plan file. Your only job is to check whether each factual
statement the plan makes about the repository is true.

Plan file: <PLAN>
Repository state you are checking against: commit <BASE>
Already-verified context: <CONTEXT>

Read the context file first. It lists the paths the plan names and whether each
one exists. Do not repeat a search the context file already answered.

For each statement the plan makes about a file, function, script, command,
environment variable, or existing behavior, confirm it against the repository at
that commit. Report every statement that is wrong, out of date, or unverifiable.

Do not comment on whether the plan is a good idea. Do not comment on whether the
steps work. Only on whether its statements about the repository are true.

<SHARED OUTPUT CONTRACT>
```

## consequences

```
You are reviewing a plan file. Your only job is to check whether the plan's steps
produce the result the plan claims.

Plan file: <PLAN>
Repository state you are checking against: commit <BASE>
Already-verified context: <CONTEXT>

Apply the steps in order, on paper. Answer three questions.

1. Does the end state match what the plan says it will achieve?
2. Does any step undo, weaken, or contradict an earlier step?
3. Does any step create a new problem the plan does not then handle?

Question 2 and question 3 are the important ones. A plan that fixes one thing and
breaks the next thing costs a full extra round every time it happens.

Do not check whether the plan's claims about the repository are true. Assume they
are. Only whether the steps work.

<SHARED OUTPUT CONTRACT>
```

## deletion

```
You are reviewing a plan file. Your only job is to find the parts that do not
need to exist.

Plan file: <PLAN>
Repository state you are checking against: commit <BASE>
Already-verified context: <CONTEXT>

For each thing the plan says it will add - a file, a step, a check, a rule, a
configuration value, a test - ask one question: if this were left out, what would
notice?

Name what would notice, and how. If nothing would notice, that is a finding, and
it is blocking, because the plan is committing work that buys nothing.

Apply the same question to tests the plan proposes. A test that would still pass
with the behavior it covers deleted is a finding.

Do not check the plan's facts. Do not check whether the steps work. Only whether
each added thing earns its place.

<SHARED OUTPUT CONTRACT>
```

## Re-review brief for round 2 and after

Round 2 and later never send the briefs above again.
Send this instead, to each brief that raised a finding edited this round, and always to `consequences` when a step changed.

```
You raised this finding on a plan file in an earlier round:

<THE ORIGINAL FINDING>

Here is the edit that was made to close it, and nothing else:

<THE DIFF OF THIS ROUND'S EDITS>

Answer two questions about the edit only.

1. Does this edit close the finding?
2. Does this edit create a new problem?

Do not re-read the whole plan. Do not check whether any other finding is still
closed. You are looking at one edit.

<SHARED OUTPUT CONTRACT>
```
