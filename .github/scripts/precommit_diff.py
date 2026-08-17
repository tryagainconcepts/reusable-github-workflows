# /// script
# dependencies = ["pyyaml"]
# ///
"""Summarise hook revision changes between two ``.pre-commit-config.yaml`` files.

Usage: precommit_diff.py BEFORE.yaml AFTER.yaml [--summary OUT.md]

Prints (and optionally writes) a markdown table of ``repo: old rev -> new rev``
so it can be dropped into a pull request body.
"""

import argparse
import sys
from pathlib import Path

import yaml


def revs(path: Path) -> dict[str, str]:
    config = yaml.safe_load(path.read_text()) or {}
    return {
        r["repo"]: str(r.get("rev", "")) for r in config.get("repos", []) if "repo" in r
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("before", type=Path)
    parser.add_argument("after", type=Path)
    parser.add_argument("--summary", type=Path)
    args = parser.parse_args()

    before, after = revs(args.before), revs(args.after)
    changes = {
        r: (before[r], after[r]) for r in after if r in before and before[r] != after[r]
    }

    if changes:
        rows = "\n".join(
            f"| `{repo}` | {old} | **{new}** |"
            for repo, (old, new) in sorted(changes.items())
        )
        summary = "| Hook repo | From | To |\n|---|---|---|\n" + rows + "\n"
    else:
        summary = "All pre-commit hooks are already at their latest revision.\n"

    if args.summary:
        args.summary.write_text(summary)
    print(summary)
    return 0


if __name__ == "__main__":
    sys.exit(main())
