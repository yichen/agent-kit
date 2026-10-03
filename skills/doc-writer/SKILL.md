---
name: doc-writer
description: Write and review Markdown in plain English, including reports, plans, specifications, tickets, PR descriptions, memory and temporary artifacts. Review wording only; preserve required formats and verified facts.
---

# Write explanations the reader can understand

Adapted from SharedAnchor's document-writing guide and its LearnRise adaptation on October 3, 2026.
Unexplained code terms in a saved report become context for later agents and return in later replies.
Prevent that at the document and the reply, not just in the final summary.

## Who the explanation is for

Write user-facing explanations without requiring knowledge of the project or its implementation.
Technical implementation sections may assume programming knowledge, but must explain project-specific terms.
These rules also apply to agent handoffs, memory and files outside the repository.
Apply the same plain-language review to chat replies when explaining the document or its findings.

## Explain what happened before naming the code

Lead with the finding or action.
Name the actor, such as the user, app, server or reply checker.
Say what it did and what that meant for the person using the app.
Use one main idea per sentence.

Prefer ordinary words over internal names.
When an exact code name is needed, explain it in the same sentence on first use.
Explain a cited decision's actual rule, not just its ID.
Check the source before describing that rule.

Examples of translating code into an explanation:

- Avoid: "The coordinator permits keep with no activity."
  Write: "The server can send a tutor reply without creating an exercise record."
  Then explain the consequence: "In this case, there was no question record the answer checker could use."
- Avoid: "Assessment status not_assessable; resultnull."
  Write: "The server saved the answer but did not check whether it was correct."
  If the stored value matters, add: "The record calls this `not_assessable`, meaning no correctness check was performed."
- Avoid: "A response-review enforcement gap."
  Write: "The reply checker allowed the app to ask for the same answer again."
- Avoid: "Uncommitted failed_recoverable."
  Write: "The server saved the message but could not finish an approved reply."
  Explain whether trying recovery is supported by the actual records.

Do not join words to values, as in "checks10", "resultnull" or "sourceee83cdf2".
Use descriptive headings unless a required template supplies them.
Do not make readers decode a pile of abbreviations, IDs or bold labels.
Place exact technical evidence after its plain-English explanation.
Use measured numbers when available; admit missing measurements rather than inventing them.
Keep facts, possible explanations and unknowns distinct.

## Preserve required formats and meaning

Keep required ticket sections, decision IDs, test IDs, migration numbers and machine-readable values unchanged.
Write plain prose inside those structures.
Do not rewrite quotations, user answers or multilingual teaching content merely to impose English-only typography.
Use one sentence per line for newly written prose where the format permits it.
Use absolute clickable file links in chat and verify paths or line numbers before citing them.
Do not remove evidence or uncertainty to make a report sound simpler or more confident.
This guide does not authorize extra tasks, production changes or independent agents.

## Review the actual wording before publication

After writing a file, read the saved version before linking it or publishing its contents.
For a chat reply, review the exact draft before sending it.
For each paragraph, ask:

1. Can the reader identify who did what and why it matters?
2. Does the explanation work without knowledge of this repository or past conversations?
3. Is each necessary abbreviation, code term or decision explained when first used?
4. Can a long sentence be split without changing its meaning?
5. Are words and values spaced correctly?
6. Does the wording distinguish checked facts from guesses and missing evidence?
7. Are required structures and the source meaning preserved?

Rewrite failed items before presenting the output.
Do not claim this review was performed if it was skipped.
This is an agent review requirement, not an automated readability guarantee.
