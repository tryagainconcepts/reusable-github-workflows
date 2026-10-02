# /// script
# dependencies = ["packaging"]
# ///
"""Review version pins and constraints in ``pyproject.toml``.

A *pin* is any requirement in ``[project.dependencies]``, optional-dependencies,
``[dependency-groups]`` or ``[tool.uv] dev-dependencies`` that carries an
``==``, ``<``, ``<=``, ``~=`` or ``===`` specifier, plus every entry of
``[tool.uv] override-dependencies`` / ``constraint-dependencies``.

Every pin must be documented by a comment block directly above it::

    # PIN: why:    4.18+ breaks Draft4 $ref resolution in app/schemas
    #      remove: after the schemas are migrated to Draft7 (see <issue url>)
    "jsonschema==4.17.3",

Only the ``why:`` and ``remove:`` keys are required. An optional ``tests:``
line narrows which tests judge the pin (everything after ``tests:`` is exported
to the test command as ``$PIN_TESTS``; ``make test-only`` passes it to pytest)::

    #      tests:  tests/test_schemas.py tests/api -k jsonschema

Without it the whole suite runs. With ``--write`` the
review also records its result in that block::

    #      reviewed: 2026-10-02 against 0.2 -> still needed

(date, the newest version it tested, verdict). The record is the cache: while
the newest version is unchanged and the record is younger than
``--max-age-days`` (default 90), a conclusive verdict is reused instead of
re-running the tests. ``--refresh`` ignores it.

Floating references
-------------------
A direct URL / git requirement that follows a branch (``...@master``,
``.../archive/main.zip``) or the default branch (no ``@ref``) is a *floating
reference*: a monthly ``uv lock --upgrade`` can pull whatever the branch holds
that day. Those need either a commit SHA (``...@<sha>``, ``.../archive/<sha>.zip``)
or the same ``# PIN:`` block (``why:`` / ``remove:``) as a version pin. Tags
and commit SHAs are accepted as pinned. The report shows each branch's current
head (``git ls-remote``) and the exact requirement to paste to pin it.
Requirements declared under ``[tool.uv.sources]`` are not inspected.

Modes
-----
``--check``  Offline. Exit 1 if a pin is undocumented. Meant for pre-commit / CI.
``review``   (default) For each pin, relax *only that pin* in a scratch copy of
             the project and ``uv lock --upgrade-package <name>``. If the locked
             version moves, the pin is holding the package back; with
             ``--test-cmd`` the tests are then run against the relaxed
             resolution to say whether the pin is still needed. The working
             tree is never modified.

Writes a markdown report to ``--summary`` (default: stdout) so it can be used
in a pull request body. Registry credentials are taken from the environment
(``UV_INDEX_*``), exactly as for ``uv lock``.

Usage: review_pins.py [--root DIR] [--check] [--test-cmd CMD] [--summary OUT.md]
                      [--write] [--refresh] [--max-age-days N]
"""

import argparse
import os
import re
import shutil
import subprocess
import sys
import tempfile
import tomllib
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

from urllib.parse import urlsplit

from packaging.requirements import InvalidRequirement, Requirement
from packaging.specifiers import SpecifierSet
from packaging.utils import canonicalize_name

PIN_OPERATORS = {"==", "<", "<=", "~=", "==="}
COPY_IGNORE = shutil.ignore_patterns(
    ".git", ".venv", "venv", "node_modules", "__pycache__", ".*_cache", ".idea"
)
REVIEWED_RE = re.compile(
    r"\breviewed:\s*(\d{4}-\d{2}-\d{2})\s+against\s+(\S+)\s*->\s*(.+?)\s*$", re.I | re.M
)
CACHEABLE = {"removable", "still needed"}  # verdicts that came from a test run
LOCK_TIMEOUT = 900
TEST_TIMEOUT = 1800


SHA_RE = re.compile(r"^[0-9a-f]{7,40}$", re.I)
TAG_RE = re.compile(  # v1.2.3, 2.0rc1, 1.0.0-beta.2 -- but not v3-upgrade
    r"^v?\d+(\.\d+)*([.-]?(a|b|rc|alpha|beta|dev|post|pre)[.-]?\d*)?$", re.I
)
ARCHIVE_RE = re.compile(
    r"^https://github\.com/(?P<owner>[^/]+)/(?P<repo>[^/]+)/archive/"
    r"(?P<heads>refs/heads/)?(?P<tags>refs/tags/)?(?P<ref>.+?)\.(?:zip|tar\.gz)$"
)


@dataclass
class Floating:
    url: str  # the requirement's URL as written
    repo: str  # clone URL for ls-remote
    ref: str | None  # None = default branch
    prefix: str  # url up to where the ref goes
    suffix: str  # url after the ref


def classify_url(url: str) -> Floating | None:
    """Return a Floating if ``url`` follows a branch / default branch, else None.

    Commit SHAs and tags count as pinned; URLs we cannot interpret are ignored.
    """
    m = ARCHIVE_RE.match(url)
    if m:
        ref = m["ref"]
        if m["tags"] or SHA_RE.match(ref) or TAG_RE.match(ref):
            return None
        repo = f"https://github.com/{m['owner']}/{m['repo']}.git"
        start = m.start("heads") if m["heads"] else m.start("ref")  # drop refs/heads/ too
        return Floating(url, repo, ref, url[:start], url[m.end("ref") :])
    if url.startswith("git+"):
        parts = urlsplit(url[4:])
        path, _, ref = parts.path.rpartition("@") if "@" in parts.path else (parts.path, "", "")
        if ref and (SHA_RE.match(ref) or TAG_RE.match(ref)):
            return None
        base = f"{parts.scheme}://{parts.netloc}{path}"
        frag = f"#{parts.fragment}" if parts.fragment else ""
        return Floating(url, base, ref or None, f"git+{base}@", frag)
    return None


@dataclass
class Pin:
    raw: str
    section: str
    name: str
    is_constraint: bool  # override/constraint entries are dropped, not loosened
    relaxed: str | None = None  # replacement text; None = drop the entry
    why: bool = False
    remove: bool = False
    line: int | None = None
    verdict: str = ""
    detail: str = field(default="", repr=False)
    locked: str | None = None
    newest: str | None = None
    tests: str | None = None  # from "tests:" in the PIN block -> $PIN_TESTS
    rev_date: date | None = None  # last recorded review (from the comment block)
    rev_version: str | None = None
    rev_verdict: str | None = None
    cached: bool = False  # verdict reused from the record, tests not re-run
    record: bool = False  # this run produced a result worth writing back
    kind: str = "version"  # "version" or "floating"
    floating: Floating | None = None
    head: str | None = None  # current commit of the floating ref
    locked_sha: str | None = None  # commit uv.lock resolved it to (git sources only)

    @property
    def documented(self) -> bool:
        return self.why and self.remove


def collect(data: dict) -> list[Pin]:
    pins: list[Pin] = []

    def add(raw: str, section: str, constraint: bool) -> None:
        try:
            req = Requirement(raw)
        except InvalidRequirement:
            pins.append(Pin(raw, section, raw, constraint, verdict="unparsable"))
            return
        if req.url:
            fl = classify_url(req.url)
            if fl:
                pins.append(
                    Pin(raw, section, canonicalize_name(req.name), False, kind="floating", floating=fl)
                )
            return
        spec_pinned = any(s.operator in PIN_OPERATORS for s in req.specifier)
        if not (constraint or spec_pinned):
            return
        relaxed = None
        if not constraint:
            req.specifier = SpecifierSet()
            relaxed = str(req)
        pins.append(Pin(raw, section, canonicalize_name(req.name), constraint, relaxed))

    project = data.get("project", {})
    for raw in project.get("dependencies", []):
        add(raw, "dependencies", False)
    for group, reqs in project.get("optional-dependencies", {}).items():
        for raw in reqs:
            add(raw, f"optional-dependencies.{group}", False)
    for group, reqs in data.get("dependency-groups", {}).items():
        for raw in reqs:
            if isinstance(raw, str):
                add(raw, f"dependency-groups.{group}", False)
    uv = data.get("tool", {}).get("uv", {})
    for raw in uv.get("dev-dependencies", []):
        add(raw, "tool.uv.dev-dependencies", False)
    for key in ("override-dependencies", "constraint-dependencies"):
        for raw in uv.get(key, []):
            add(raw, f"tool.uv.{key}", True)
    return pins


def read_docs(text: str, pins: list[Pin]) -> None:
    """Mark each pin documented if a PIN block of comments sits right above it."""
    lines = text.splitlines()
    for pin in pins:
        quoted = re.compile(rf"""["']{re.escape(pin.raw)}["']""")
        idx = next((i for i, ln in enumerate(lines) if quoted.search(ln)), None)
        if idx is None:
            continue
        pin.line = idx + 1
        block = []
        for ln in reversed(lines[:idx]):
            if not ln.lstrip().startswith("#"):
                break
            block.append(ln)
        body = "\n".join(block)
        pin.why = bool(re.search(r"\bwhy:\s*\S", body, re.I))
        pin.remove = bool(re.search(r"\bremove:\s*\S", body, re.I))
        t = re.search(r"\btests:[ \t]*(\S.*?)\s*$", body, re.I | re.M)
        pin.tests = t[1] if t else None
        m = REVIEWED_RE.search(body)
        if m:
            try:
                pin.rev_date = date.fromisoformat(m[1])
            except ValueError:
                pass
            pin.rev_version, pin.rev_verdict = m[2], m[3].lower()


def write_reviews(text: str, pins: list[Pin], today: date) -> str:
    """Insert/replace a ``reviewed:`` line at the end of each pin's PIN block."""
    lines = text.splitlines(keepends=True)
    for pin in pins:
        if not (pin.record and pin.documented):
            continue
        quoted = re.compile(rf"""["']{re.escape(pin.raw)}["']""")
        idx = next((i for i, ln in enumerate(lines) if quoted.search(ln)), None)
        if idx is None or idx == 0 or not lines[idx - 1].lstrip().startswith("#"):
            continue
        start = idx
        while start > 0 and lines[start - 1].lstrip().startswith("#"):
            start -= 1
        indent = re.match(r"\s*", lines[idx - 1])[0]
        version = pin.newest or pin.locked or "?"
        new = f"{indent}#      reviewed: {today.isoformat()} against {version} -> {pin.verdict}\n"
        old = next((k for k in range(start, idx) if REVIEWED_RE.search(lines[k])), None)
        if old is not None:
            lines[old] = new
        else:
            lines.insert(idx, new)
    return "".join(lines)


def relax(text: str, pin: Pin) -> str:
    raw = re.escape(pin.raw)
    if pin.relaxed is not None:
        return re.sub(rf"""(["']){raw}\1""", lambda m: f"{m[1]}{pin.relaxed}{m[1]}", text)
    for pattern in (
        rf"""["']{raw}["']\s*,\s*""",  # first / middle entry
        rf"""\s*,\s*["']{raw}["']""",  # last entry
        rf"""["']{raw}["']""",  # only entry
    ):
        new, n = re.subn(pattern, "", text)
        if n:
            return new
    return text


def locked_versions(lock: Path) -> dict[str, str]:
    if not lock.exists():
        return {}
    pkgs = tomllib.loads(lock.read_text()).get("package", [])
    return {canonicalize_name(p["name"]): p.get("version", "") for p in pkgs}


def run(cmd: list[str], cwd: Path, timeout: int, env: dict | None = None) -> tuple[int, str]:
    try:
        proc = subprocess.run(
            cmd, cwd=cwd, capture_output=True, text=True, timeout=timeout, env=env
        )
    except subprocess.TimeoutExpired:
        return 124, f"timed out after {timeout}s"
    return proc.returncode, (proc.stdout + proc.stderr).strip()


def tail(output: str, n: int = 3) -> str:
    lines = [ln.strip() for ln in output.splitlines() if ln.strip()]
    return " ".join(lines[-n:])[:300]


class TestGate:
    """Hands out the test command for a pin, once its baseline has passed.

    The baseline (tests on the unmodified project) runs lazily and once per
    distinct ``tests:`` selection, so an unrelated red test elsewhere in the
    suite cannot make every pin look "still needed".
    """

    def __init__(self, cmd: str | None, root: Path, scratch: Path):
        self._cmd, self.root, self.scratch = cmd, root, scratch
        self._baseline: dict[str, str] = {}  # selection -> "" if green else failure tail
        self.skipped: list[str] = []

    def cmd(self, selection: str | None) -> str | None:
        if not self._cmd:
            return None
        key = selection or ""
        if key not in self._baseline:
            print(f"running baseline tests ({key or 'full suite'}) ...", file=sys.stderr)
            base = self.scratch / "baseline"
            shutil.copytree(self.root, base, ignore=COPY_IGNORE)
            env = test_env(self.scratch / "venv-base", selection)
            code, out = run(["uv", "sync", "--frozen"], base, LOCK_TIMEOUT, env)
            if code == 0:
                code, out = run(["bash", "-c", self._cmd], base, TEST_TIMEOUT, env)
            shutil.rmtree(base, ignore_errors=True)
            self._baseline[key] = "" if code == 0 else tail(out)
            if code != 0:
                self.skipped.append(f"`{key or 'full suite'}`: {tail(out)}")
        return None if self._baseline[key] else self._cmd


def test_env(venv: Path, selection: str | None) -> dict:
    env = {**os.environ, "UV_PROJECT_ENVIRONMENT": str(venv)}
    if selection:
        env["PIN_TESTS"] = selection
    else:
        env.pop("PIN_TESTS", None)
    return env


def locked_git_shas(lock: Path) -> dict[str, str]:
    if not lock.exists():
        return {}
    out = {}
    for p in tomllib.loads(lock.read_text()).get("package", []):
        git = (p.get("source") or {}).get("git")
        if git and "#" in git:
            out[canonicalize_name(p["name"])] = git.rsplit("#", 1)[1]
    return out


def resolve_floating(pin: Pin, shas: dict[str, str]) -> None:
    fl = pin.floating
    pin.locked_sha = shas.get(pin.name)
    target = fl.ref or "HEAD"
    env = {**os.environ, "GIT_TERMINAL_PROMPT": "0"}
    code, out = run(["git", "ls-remote", fl.repo, target], Path.cwd(), 60, env)
    lines = [ln.split() for ln in out.splitlines() if "\t" in ln]
    pick = next((ln for ln in lines if ln[-1] == f"refs/heads/{target}"), None) or (lines[0] if lines else None)
    where = f"`{fl.ref}` branch" if fl.ref else "default branch"
    if code != 0 or not pick:
        pin.verdict = "floating"
        pin.detail = f"follows the {where}; could not resolve its head ({tail(out)})"
        return
    pin.head = pick[0]
    pin.verdict = "floating"
    pin.detail = f"follows the {where}"
    if pin.locked_sha and pin.locked_sha != pin.head:
        pin.detail += f"; branch has moved since the lock ({pin.locked_sha[:9]} -> {pin.head[:9]})"


def suggested(pin: Pin) -> str | None:
    """The requirement rewritten to point at the branch's current commit."""
    fl = pin.floating
    if not (fl and pin.head):
        return None
    return pin.raw.replace(fl.url, f"{fl.prefix}{pin.head}{fl.suffix}")


def baseline_lock_error(root: Path, scratch: Path) -> str | None:
    """Run ``uv lock`` on the untouched project; a failure here (missing registry
    credentials, no network) would otherwise make every pin look "still needed"."""
    work = scratch / "lock-baseline"
    shutil.copytree(root, work, ignore=COPY_IGNORE)
    # --refresh: an up-to-date lock is otherwise accepted offline, which would
    # hide missing registry credentials until the first relaxed lock.
    code, out = run(["uv", "lock", "--refresh"], work, LOCK_TIMEOUT)
    shutil.rmtree(work, ignore_errors=True)
    return None if code == 0 else tail(out)


def review(
    pin: Pin, root: Path, scratch: Path, gate: TestGate, baseline: dict,
    refresh: bool, max_age: int, today: date,
) -> None:
    pyproject, lock = root / "pyproject.toml", root / "uv.lock"
    work = scratch / "work"
    if work.exists():
        shutil.rmtree(work)
    shutil.copytree(root, work, ignore=COPY_IGNORE)
    (work / "pyproject.toml").write_text(relax(pyproject.read_text(), pin))
    if lock.exists():
        shutil.copy2(lock, work / "uv.lock")

    pin.locked = baseline.get(pin.name)
    code, out = run(["uv", "lock", "--upgrade-package", pin.name], work, LOCK_TIMEOUT)
    if code != 0:
        pin.verdict = "still needed"
        pin.detail = f"relaxing it breaks resolution: {tail(out)}"
        return

    pin.newest = locked_versions(work / "uv.lock").get(pin.name)
    if pin.newest in (None, pin.locked):
        pin.verdict = "not holding anything back"
        pin.detail = "newest allowed version equals the locked one"
        pin.record = True
        return

    if (
        not refresh
        and pin.rev_version == pin.newest
        and pin.rev_verdict in CACHEABLE
        and pin.rev_date
        and (today - pin.rev_date).days <= max_age
    ):
        pin.verdict, pin.cached = pin.rev_verdict, True
        pin.detail = f"reused: already tested on {pin.newest} on {pin.rev_date}"
        return

    test_cmd = gate.cmd(pin.tests)
    pin.record = True
    if not test_cmd:
        pin.verdict = "holding back"
        pin.detail = f"{pin.locked} -> {pin.newest} is available; run tests to decide"
        return

    env = test_env(scratch / "venv", pin.tests)
    code, out = run(["uv", "sync", "--frozen"], work, LOCK_TIMEOUT, env)
    if code == 0:
        code, out = run(["bash", "-c", test_cmd], work, TEST_TIMEOUT, env)
    if code == 0:
        pin.verdict = "removable"
        pin.detail = f"tests pass on {pin.newest}"
    else:
        pin.verdict = "still needed"
        pin.detail = f"tests fail on {pin.newest}: {tail(out)}"


def floats_on(p: Pin) -> str:
    return f"`{p.floating.ref}`" if p.floating.ref else "default branch"


def render(pins: list[Pin], reviewed: bool) -> str:
    if not pins:
        return "No version pins or floating references found in `pyproject.toml`.\n"
    versions = [p for p in pins if p.kind == "version"]
    floating = [p for p in pins if p.kind == "floating"]
    out = []

    if versions:
        rows = []
        for p in versions:
            docs = "yes" if p.documented else "**missing**"
            loc = f"`pyproject.toml:{p.line}`" if p.line else p.section
            if reviewed:
                moved = f"{p.locked or '?'} -> {p.newest or '?'}"
                if p.record:
                    last = f"{date.today()} (this run)"
                elif p.rev_date:
                    last = f"{p.rev_date} on {p.rev_version}"
                else:
                    last = "never"
                rows.append(
                    f"| `{p.raw}` | {loc} | {docs} | {moved} | **{p.verdict}** | {last} | {p.detail} |"
                )
            else:
                rows.append(f"| `{p.raw}` | {loc} | {docs} |")
        if reviewed:
            head = "| Pin | Where | Documented | Locked -> relaxed | Verdict | Last reviewed | Detail |\n|---|---|---|---|---|---|---|\n"
        else:
            head = "| Pin | Where | Documented |\n|---|---|---|\n"
        out.append(head + "\n".join(rows) + "\n")

    if floating:
        out.append("\n**Floating references** (follow a branch; pin to a commit SHA or document with `# PIN:`)\n\n")
        rows = []
        for p in floating:
            docs = "yes" if p.documented else "**missing**"
            loc = f"`pyproject.toml:{p.line}`" if p.line else p.section
            if reviewed:
                sha = f"`{p.head[:9]}`" if p.head else "?"
                rows.append(f"| `{p.name}` | {loc} | {floats_on(p)} | {sha} | {docs} | {p.detail} |")
            else:
                rows.append(f"| `{p.name}` | {loc} | {floats_on(p)} | {docs} |")
        if reviewed:
            head = "| Dependency | Where | Floats on | Head now | Documented | Detail |\n|---|---|---|---|---|---|\n"
        else:
            head = "| Dependency | Where | Floats on | Documented |\n|---|---|---|---|\n"
        out.append(head + "\n".join(rows) + "\n")
        if reviewed:
            fixes = [(p, suggested(p)) for p in floating if suggested(p)]
            if fixes:
                out.append("\nTo pin a floating reference to its current commit, use:\n\n")
                out.extend(f"- `{fix}`\n" for _, fix in fixes)

    undocumented = sum(not p.documented for p in pins)
    if undocumented:
        out.append(f"\n{undocumented} entr(ies) lack a `# PIN:` block with `why:` and `remove:`.\n")
    return "".join(out)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--root", type=Path, default=Path("."))
    parser.add_argument("--check", action="store_true", help="offline docs check only")
    parser.add_argument("--test-cmd", help="shell command that runs the test suite")
    parser.add_argument("--summary", type=Path)
    parser.add_argument("--write", action="store_true", help="record results in pyproject.toml")
    parser.add_argument("--refresh", action="store_true", help="ignore recorded reviews")
    parser.add_argument("--max-age-days", type=int, default=90)
    args = parser.parse_args()

    root = args.root.resolve()
    pyproject = root / "pyproject.toml"
    if not pyproject.exists():
        print("no pyproject.toml; nothing to review")
        return 0
    text = pyproject.read_text()
    pins = collect(tomllib.loads(text))
    read_docs(text, pins)

    if args.check:
        report = render(pins, reviewed=False)
        print(report)
        return 1 if any(not p.documented for p in pins) else 0

    if any(p.kind == "version" for p in pins) and shutil.which("uv") is None:
        print("uv not found on PATH", file=sys.stderr)
        return 2
    baseline = locked_versions(root / "uv.lock")
    git_shas = locked_git_shas(root / "uv.lock")
    today = date.today()
    with tempfile.TemporaryDirectory(prefix="review-pins-") as tmp:
        gate = TestGate(args.test_cmd, root, Path(tmp))
        lock_error = None
        if any(p.kind == "version" for p in pins):
            lock_error = baseline_lock_error(root, Path(tmp))
        for pin in pins:
            if pin.verdict == "unparsable":
                pin.detail = "could not parse requirement"
                continue
            print(f"reviewing {pin.raw} ...", file=sys.stderr)
            if pin.kind == "floating":
                resolve_floating(pin, git_shas)
                continue
            if lock_error:
                pin.verdict = "not reviewed"
                pin.detail = f"`uv lock` fails before any change (credentials? network?): {lock_error}"
                continue
            review(pin, root, Path(tmp), gate, baseline, args.refresh, args.max_age_days, today)

    if args.write and any(p.record for p in pins):
        pyproject.write_text(write_reviews(text, pins, today))
    skipped = ""
    if gate.skipped:
        skipped = "\nBaseline tests fail without any change, so these pins were not tested:\n" + "".join(
            f"- {m}\n" for m in gate.skipped
        )

    report = "### Pin review\n\n" + render(pins, reviewed=True) + skipped
    if args.summary:
        args.summary.write_text(report)
    print(report)
    return 0


if __name__ == "__main__":
    sys.exit(main())
