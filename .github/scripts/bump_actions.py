"""Bump SHA-pinned GitHub Actions in workflow files to their latest release.

Scans ``.github/workflows/*.y*ml`` for lines of the form::

    uses: owner/repo[/path]@<40-hex-sha> # <version>

For each distinct ``owner/repo`` it looks up the latest release (falling back to
the highest semver tag when the repo has no releases), dereferences that tag to
a commit SHA, and rewrites both the SHA and the trailing version comment.

Writes a markdown summary to the path given by ``--summary`` (default: stdout)
so it can be used as a pull request body.
"""

import argparse
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

API = "https://api.github.com"
USES_RE = re.compile(
    r"^(?P<prefix>\s*-?\s*uses:\s*)"
    r"(?P<repo>[\w.-]+/[\w.-]+)(?P<path>/[^@\s]+)?"
    r"@(?P<sha>[0-9a-f]{40})"
    r"(?P<gap>\s*#\s*)(?P<version>\S+)"
    r"(?P<rest>.*)$"
)
SEMVER_RE = re.compile(r"^v?\d+(\.\d+){0,2}$")


def api_get(path: str, token: str | None, retries: int = 4) -> dict | list | None:
    req = urllib.request.Request(f"{API}{path}")
    req.add_header("Accept", "application/vnd.github+json")
    req.add_header("X-GitHub-Api-Version", "2022-11-28")
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    for attempt in range(retries):
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                return json.load(resp)
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                return None
            if exc.code < 500 or attempt == retries - 1:
                raise
        except (urllib.error.URLError, TimeoutError):
            if attempt == retries - 1:
                raise
        time.sleep(2**attempt)
    return None


def semver_key(tag: str) -> tuple[int, ...]:
    return tuple(int(part) for part in tag.lstrip("v").split("."))


def latest_tag(repo: str, token: str | None) -> str | None:
    release = api_get(f"/repos/{repo}/releases/latest", token)
    if isinstance(release, dict) and release.get("tag_name"):
        return release["tag_name"]
    tags = api_get(f"/repos/{repo}/tags?per_page=100", token) or []
    candidates = [t["name"] for t in tags if SEMVER_RE.match(t["name"])]
    return max(candidates, key=semver_key) if candidates else None


def tag_sha(repo: str, tag: str, token: str | None) -> str | None:
    ref = api_get(f"/repos/{repo}/git/ref/tags/{tag}", token)
    if not isinstance(ref, dict):
        return None
    obj = ref["object"]
    if obj["type"] == "tag":  # annotated tag -> dereference to the commit
        tag_obj = api_get(f"/repos/{repo}/git/tags/{obj['sha']}", token)
        return tag_obj["object"]["sha"] if isinstance(tag_obj, dict) else None
    return obj["sha"]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workflows", default=".github/workflows", type=Path)
    parser.add_argument("--summary", type=Path)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    token = os.environ.get("GITHUB_TOKEN")

    files = sorted(
        list(args.workflows.glob("*.yml")) + list(args.workflows.glob("*.yaml"))
    )
    resolved: dict[str, tuple[str, str] | None] = {}
    changes: dict[str, tuple[str, str, str]] = {}  # repo -> (old_ver, new_ver, sha)

    for path in files:
        original = path.read_text()
        lines = original.splitlines(keepends=True)
        for i, line in enumerate(lines):
            m = USES_RE.match(line)
            if not m:
                continue
            repo = m["repo"]
            if repo not in resolved:
                tag = latest_tag(repo, token)
                sha = tag_sha(repo, tag, token) if tag else None
                resolved[repo] = (tag, sha) if tag and sha else None
                if resolved[repo] is None:
                    print(
                        f"warning: could not resolve latest for {repo}", file=sys.stderr
                    )
            if resolved[repo] is None:
                continue
            tag, sha = resolved[repo]
            if sha == m["sha"]:
                continue
            lines[i] = (
                f"{m['prefix']}{repo}{m['path'] or ''}@{sha}"
                f"{m['gap']}{tag}{m['rest']}\n"
            )
            changes[repo] = (m["version"], tag, sha)
        updated = "".join(lines)
        if updated != original and not args.dry_run:
            path.write_text(updated)

    if changes:
        rows = "\n".join(
            f"| `{repo}` | {old} | **{new}** | `{sha[:12]}` |"
            for repo, (old, new, sha) in sorted(changes.items())
        )
        summary = "| Action | From | To | SHA |\n|---|---|---|---|\n" + rows + "\n"
    else:
        summary = "All pinned actions are already at their latest release.\n"

    if args.summary:
        args.summary.write_text(summary)
    print(summary)
    return 0


if __name__ == "__main__":
    sys.exit(main())
