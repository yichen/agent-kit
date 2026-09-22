#!/usr/bin/env python3
"""Validate a code-pre-reviewed-plan:v1 block read from stdin.

Prints nothing and exits 0 when the block satisfies the contract's required
fields. Otherwise prints one plain sentence per problem and exits 1.

This checks presence and shape only. It does not check that the paths exist at
the base commit, which is the repository controller's job.

Read-only: it reads stdin and writes to stdout. It opens no files.
"""

import json
import re
import sys

REQUIRED_TOP = [
    "schema",
    "repository",
    "reviewedBaseSha",
    "scope",
    "acceptanceCriteria",
    "verification",
    "visual",
]

SHA_RE = re.compile(r"^[0-9a-f]{40}$")


def main() -> int:
    pinned_base = sys.argv[1] if len(sys.argv) > 1 else "-"
    raw = sys.stdin.read().strip()
    problems = []

    if not raw:
        print("the pre-reviewed plan block is empty")
        return 1

    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        print("the pre-reviewed plan block is not valid JSON: %s" % exc.msg)
        return 1

    if not isinstance(data, dict):
        print("the pre-reviewed plan block is not a JSON object")
        return 1

    for key in REQUIRED_TOP:
        if key not in data:
            problems.append("the plan block has no %s field" % key)

    schema = data.get("schema")
    if schema is not None and schema != "code-pre-reviewed-plan:v1":
        problems.append(
            "the plan block declares schema %r, and an unknown schema version blocks an implementation run"
            % schema
        )

    base = data.get("reviewedBaseSha")
    if isinstance(base, str) and not SHA_RE.match(base):
        problems.append(
            "reviewedBaseSha is not 40 lowercase hex characters, so the base commit cannot be resolved"
        )
    elif (
        isinstance(base, str)
        and pinned_base not in ("-", "")
        and base != pinned_base
    ):
        problems.append(
            "the plan block was reviewed against %s but this review pinned %s, so the block is stale"
            % (base[:12], pinned_base[:12])
        )

    scope = data.get("scope")
    if scope is not None:
        if not isinstance(scope, dict):
            problems.append("scope is not an object with modify, add and delete lists")
        else:
            for key in ("modify", "add", "delete"):
                if key not in scope:
                    problems.append("scope has no %s list" % key)
                elif not isinstance(scope[key], list):
                    problems.append("scope.%s is not a list" % key)
            total = sum(
                len(scope[k]) for k in ("modify", "add", "delete")
                if isinstance(scope.get(k), list)
            )
            if total == 0:
                problems.append(
                    "scope names no files at all, so the set of files to change is undetermined"
                )

    anchors = data.get("anchors") or []
    callers = data.get("callerChecks") or []
    if not isinstance(anchors, list):
        anchors = []
    if not isinstance(callers, list):
        callers = []
    if len(anchors) + len(callers) == 0:
        problems.append(
            "the plan block has neither an anchor nor a caller check, so nothing in it can be verified against current source"
        )

    criteria = data.get("acceptanceCriteria")
    if isinstance(criteria, list):
        if not criteria:
            problems.append(
                "acceptanceCriteria is empty, so there is no stated observable result"
            )
        for item in criteria:
            if not isinstance(item, dict) or not str(item.get("text", "")).strip():
                problems.append(
                    "an acceptance criterion has no text, so it states no observable result"
                )
                break
    elif criteria is not None:
        problems.append("acceptanceCriteria is not a list")

    verification = data.get("verification")
    if isinstance(verification, list):
        if not verification:
            problems.append(
                "verification is empty, so there is no stated way to prove the work is correct"
            )
    elif verification is not None:
        problems.append("verification is not a list")

    visual = data.get("visual")
    if visual is not None and (
        not isinstance(visual, dict) or "required" not in visual
    ):
        problems.append("visual does not state whether the change is visible to users")

    if problems:
        for p in problems:
            print(p)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
