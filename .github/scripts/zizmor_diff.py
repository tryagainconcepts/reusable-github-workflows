"""Compare two ``zizmor --format json`` reports and list findings new in the second.

Findings are keyed by audit ident, workflow path and the YAML route of their
primary location, so line-number shifts do not produce false positives.

Usage: zizmor_diff.py BEFORE.json AFTER.json [--summary OUT.md]

Exits 1 if AFTER contains findings not present in BEFORE, else 0.
"""

import argparse
import json
import sys
from pathlib import Path


def finding_key(finding: dict) -> tuple[str, str, str]:
    loc = finding["locations"][0]["symbolic"]
    path = next(iter(loc["key"].values()))["verbatim_path"]
    return finding["ident"], path, json.dumps(loc["route"], sort_keys=True)


def load(path: Path) -> dict[tuple[str, str, str], dict]:
    findings = json.loads(path.read_text() or "[]")
    return {finding_key(f): f for f in findings if not f.get("ignored")}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("before", type=Path)
    parser.add_argument("after", type=Path)
    parser.add_argument("--summary", type=Path)
    args = parser.parse_args()

    before, after = load(args.before), load(args.after)
    new = {k: v for k, v in after.items() if k not in before}
    fixed = [k for k in before if k not in after]

    lines = [
        f"zizmor findings: {len(before)} before, {len(after)} after "
        f"({len(new)} new, {len(fixed)} resolved)."
    ]
    if new:
        lines += [
            "",
            "| Severity | Audit | Location | Description |",
            "|---|---|---|---|",
        ]
        for (ident, path, _), f in sorted(new.items()):
            row = f["locations"][0]["concrete"]["location"]["start_point"]["row"] + 1
            sev = f["determinations"]["severity"]
            lines.append(
                f"| {sev} | [`{ident}`]({f['url']}) | `{path}:{row}` | {f['desc']} |"
            )
    summary = "\n".join(lines) + "\n"

    if args.summary:
        args.summary.write_text(summary)
    print(summary)
    return 1 if new else 0


if __name__ == "__main__":
    sys.exit(main())
