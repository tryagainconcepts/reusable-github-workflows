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


def render(pins: list[Pin], reviewed: bool) -> str:
    if not pins:
        return "No version pins found in `pyproject.toml`.\n"
    rows = []
    for p in pins:
        docs = "yes" if p.documented else "**missing**"
        loc = f"`pyproject.toml:{p.line}`" if p.line else p.section
        if reviewed:
            versions = f"{p.locked or '?'} -> {p.newest or '?'}"
            if p.record:
                last = f"{date.today()} (this run)"
            elif p.rev_date:
                last = f"{p.rev_date} on {p.rev_version}"
            else:
                last = "never"
            rows.append(
                f"| `{p.raw}` | {loc} | {docs} | {versions} | **{p.verdict}** | {last} | {p.detail} |"
            )
        else:
            rows.append(f"| `{p.raw}` | {loc} | {docs} |")
    if reviewed:
        head = "| Pin | Where | Documented | Locked -> relaxed | Verdict | Last reviewed | Detail |\n|---|---|---|---|---|---|---|\n"
    else:
        head = "| Pin | Where | Documented |\n|---|---|---|\n"
    undocumented = sum(not p.documented for p in pins)
    note = (
        f"\n{undocumented} pin(s) lack a `# PIN:` block with `why:` and `remove:`.\n"
        if undocumented
        else ""
    )
    return head + "\n".join(rows) + "\n" + note


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

    if pins and shutil.which("uv") is None:
        print("uv not found on PATH", file=sys.stderr)
        return 2
    baseline = locked_versions(root / "uv.lock")
    today = date.today()
    with tempfile.TemporaryDirectory(prefix="review-pins-") as tmp:
        gate = TestGate(args.test_cmd, root, Path(tmp))
        for pin in pins:
            if pin.verdict == "unparsable":
                pin.detail = "could not parse requirement"
                continue
            print(f"reviewing {pin.raw} ...", file=sys.stderr)
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
