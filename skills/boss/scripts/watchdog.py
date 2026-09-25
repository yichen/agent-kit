#!/usr/bin/env python3
"""Independent freshness watchdog for the shadow scheduler."""
import argparse
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import sys

UTC = timezone.utc


def healthy(path: Path, now: datetime, max_age: timedelta = timedelta(minutes=32)) -> tuple[bool, str]:
    try:
        receipt = json.loads(path.read_text(encoding="utf-8"))
        checked = datetime.fromisoformat(receipt["as_of"].replace("Z", "+00:00"))
        if checked.tzinfo is None:
            raise ValueError("timestamp timezone missing")
        age = now.astimezone(UTC) - checked.astimezone(UTC)
        if age < timedelta(minutes=-2):
            return False, "last-success timestamp is in the future"
        if age > max_age:
            return False, f"last successful scheduler comparison is {int(age.total_seconds() / 60)} minutes old"
        if receipt.get("mode") != "shadow" or receipt.get("assignment_enabled") is not False:
            return False, "scheduler receipt is not shadow-only"
        if receipt.get("disputed") is True:
            return False, "fresh shadow comparison has unresolved ownership disputes"
        return True, "last successful shadow comparison is fresh"
    except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        return False, f"last-success receipt unavailable or invalid: {exc}"


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--last-success", type=Path, required=True)
    parser.add_argument("--max-age-minutes", type=int, default=32)
    args = parser.parse_args(argv)
    if args.max_age_minutes < 15 or args.max_age_minutes > 120:
        print("boss watchdog: max age must be between 15 and 120 minutes", file=sys.stderr)
        return 2
    ok, message = healthy(args.last_success, datetime.now(UTC), timedelta(minutes=args.max_age_minutes))
    print(message if ok else f"boss watchdog: {message}", file=sys.stderr if not ok else sys.stdout)
    return 0 if ok else 2


if __name__ == "__main__":
    sys.exit(main())
