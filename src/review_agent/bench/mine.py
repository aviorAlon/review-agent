"""Mine benchmark cases from a real git repository.

Two modes:

recent  -- the latest non-merge commits touching Python files, within a
           size band, treated as CLEAN. In a mature repository, merged
           changes are mostly defect-free, so every finding on them is a
           candidate false positive: this is the noise pressure the
           synthetic suite lacks, and the only way to measure what a
           verifier drops. "Clean" is an assumption; label the pool.

fixes   -- SZZ-lite. Find commits whose message says they fix something,
           take the lines the fix REMOVED, `git blame` them at the fix's
           parent, and call the commit that wrote them the bug-introducing
           change. That commit is the case; the blamed original line
           numbers are the truths. Truths come from history, not from the
           benchmark's author.

Both are approximations. Fix messages lie, blame points at refactors,
and a bug's "line" is often a judgment call. The point is a first set of
truths that nobody here wrote.
"""
from __future__ import annotations

import re
import subprocess
from collections import Counter

from unidiff import PatchSet

from review_agent.bench.cases import Case, Truth
from review_agent.schemas import Category

FIX_GREP = ["fix", "bug", "crash", "regress", "security", "CVE", "leak", "race"]
# "fix" matches far more than defects. Drop fixes whose subject says they are
# about tooling, typing, docs or style -- the removed lines there are not bugs.
NOT_A_BUG = re.compile(
    r"typo|spelling|grammar|wording|docs?\b|document|docstring|readme|changelog|example|comment|annotation|typing"
    r"|type hint|type ignore|mypy|pyright|lint|ruff|flake8|pep ?8|black|isort|format|style|whitespace|pre-commit"
    r"|ci\b|workflow|bump|upgrade|deprecat",
    re.I,
)
_PORCELAIN_HEADER = re.compile(r"^([0-9a-f]{40}) (\d+) (\d+)(?: (\d+))?$")
_MAX_FILE_CHARS = 60_000


def _git(repo: str, *args: str) -> str:
    return subprocess.run(["git", "-C", repo, *args], check=True, capture_output=True,
                          text=True, encoding="utf-8", errors="replace").stdout


def _show(repo: str, ref: str, path: str) -> str | None:
    try:
        return _git(repo, "show", f"{ref}:{path}")
    except subprocess.CalledProcessError:
        return None


def _changed_py_files(repo: str, sha: str) -> list[tuple[str, str]]:
    out = _git(repo, "show", "--no-renames", "--name-status", "--format=", sha)
    files = []
    for line in out.splitlines():
        parts = line.split("\t")
        if len(parts) == 2 and parts[1].endswith(".py"):
            files.append((parts[0], parts[1]))
    return files


def _py_diff_lines(repo: str, sha: str) -> int:
    out = _git(repo, "show", "--no-renames", "--numstat", "--format=", sha)
    total = 0
    for line in out.splitlines():
        parts = line.split("\t")
        if len(parts) == 3 and parts[2].endswith(".py") and parts[0] != "-":
            total += int(parts[0]) + int(parts[1])
    return total


def _is_merge(repo: str, sha: str) -> bool:
    return len(_git(repo, "rev-list", "--parents", "-n", "1", sha).split()) > 2


def _subject(repo: str, sha: str) -> str:
    return _git(repo, "log", "-1", "--format=%s", sha).strip()


def _snapshot(repo: str, sha: str, files: list[tuple[str, str]]) -> tuple[dict[str, str], dict[str, str]] | None:
    before: dict[str, str] = {}
    after: dict[str, str] = {}
    for status, path in files:
        if status not in ("A", "M"):
            return None  # deletions/renames: skip the whole commit, keep cases simple
        new = _show(repo, sha, path)
        if new is None or len(new) > _MAX_FILE_CHARS or "\x00" in new:
            return None
        after[path] = new
        if status == "M":
            old = _show(repo, f"{sha}^", path)
            if old is None:
                return None
            before[path] = old
    return before, after


def _usable(repo: str, sha: str, min_lines: int, max_lines: int, max_files: int) -> list[tuple[str, str]] | None:
    files = _changed_py_files(repo, sha)
    if not files or len(files) > max_files:
        return None
    size = _py_diff_lines(repo, sha)
    if not (min_lines <= size <= max_lines):
        return None
    return files


# ------------------------------------------------------------------- recent

def recent_cases(repo: str, n: int = 15, min_lines: int = 10, max_lines: int = 400,
                 max_files: int = 6, lookback: int = 400) -> list[Case]:
    shas = _git(repo, "log", "--no-merges", "--format=%H", "-n", str(lookback)).split()
    cases: list[Case] = []
    for sha in shas:
        if len(cases) >= n:
            break
        files = _usable(repo, sha, min_lines, max_lines, max_files)
        if files is None:
            continue
        snap = _snapshot(repo, sha, files)
        if snap is None:
            continue
        before, after = snap
        cases.append(Case(id=f"git-{sha[:10]}", before=before, after=after, git_repo=repo, git_ref=sha,
                          note=_subject(repo, sha)))
    return cases


# -------------------------------------------------------------------- fixes

def _blame_map(repo: str, ref: str, path: str) -> dict[int, tuple[str, int]]:
    """final line number -> (commit, original line number in that commit)."""
    try:
        out = _git(repo, "blame", "--line-porcelain", ref, "--", path)
    except subprocess.CalledProcessError:
        return {}
    mapping: dict[int, tuple[str, int]] = {}
    for line in out.splitlines():
        m = _PORCELAIN_HEADER.match(line)
        if m:
            mapping[int(m.group(3))] = (m.group(1), int(m.group(2)))
    return mapping


def _removed_source_lines(repo: str, fix_sha: str, path: str) -> list[int]:
    patch_text = _git(repo, "show", "--no-renames", "--format=", fix_sha, "--", path)
    try:
        patch = PatchSet(patch_text)
    except Exception:  # noqa: BLE001 - malformed patch text: skip this file
        return []
    lines: list[int] = []
    for pf in patch:
        for hunk in pf:
            for ln in hunk:
                if ln.is_removed and ln.source_line_no is not None:
                    lines.append(ln.source_line_no)
    return lines


def _is_test_path(path: str) -> bool:
    parts = path.replace("\\", "/").split("/")
    name = parts[-1]
    return (any(p in ("test", "tests", "testing", "__tests__", "spec", "specs") for p in parts[:-1])
            or name.startswith("test_") or name.endswith("_test.py") or name == "conftest.py")


_TRIVIAL = re.compile(r"^\s*(#.*|[\]\)\}\[\(\{,;:]*|pass|else:|finally:|try:|except\b.*:|\"\"\"|'''|@\w+.*)\s*$")


def _snap_line(lines: list[str], ln: int, radius: int = 3) -> int | None:
    """Move a blamed line off brackets, comments and `pass` onto the nearest real code line."""
    def ok(i: int) -> bool:
        return 1 <= i <= len(lines) and bool(lines[i - 1].strip()) and not _TRIVIAL.match(lines[i - 1])
    if ok(ln):
        return ln
    for d in range(1, radius + 1):
        for cand in (ln - d, ln + d):
            if ok(cand):
                return cand
    return None


def _category_for(subject: str) -> Category:
    s = subject.lower()
    if re.search(r"secur|vuln|cve|inject|travers|auth|xss|csrf|secret", s):
        return Category.SECURITY
    if re.search(r"perf|slow|n\+1|memory|leak|cache", s):
        return Category.PERFORMANCE
    return Category.LOGIC


def _collapse(lines: list[int], window: int = 3, cap: int = 3) -> list[int]:
    out: list[int] = []
    for ln in sorted(set(lines)):
        if not out or ln - out[-1] > window:
            out.append(ln)
        if len(out) >= cap:
            break
    return out


def fix_pair_cases(repo: str, n: int = 15, min_lines: int = 5, max_lines: int = 400,
                   max_files: int = 6, lookback: int = 800) -> list[Case]:
    args = ["log", "--no-merges", "--format=%H", "-n", str(lookback), "-i"]
    for word in FIX_GREP:
        args += ["--grep", word]
    fixes = _git(repo, *args).split()

    cases: list[Case] = []
    seen: set[str] = set()
    for fix_sha in fixes:
        if len(cases) >= n:
            break
        fix_subject = _subject(repo, fix_sha)
        if NOT_A_BUG.search(fix_subject):
            continue
        fixed_files = [(s, p) for s, p in _changed_py_files(repo, fix_sha) if s == "M"]
        if not fixed_files:
            continue

        # Attribute every removed line to the commit that wrote it.
        attributions: dict[str, dict[str, list[int]]] = {}  # introducer -> path -> orig lines
        for _, path in fixed_files:
            removed = _removed_source_lines(repo, fix_sha, path)
            if not removed:
                continue
            blame = _blame_map(repo, f"{fix_sha}^", path)
            for ln in removed:
                hit = blame.get(ln)
                if hit:
                    attributions.setdefault(hit[0], {}).setdefault(path, []).append(hit[1])
        if not attributions:
            continue
        introducer = Counter({sha: sum(len(v) for v in files.values()) for sha, files in attributions.items()}).most_common(1)[0][0]
        if introducer == fix_sha or introducer in seen or _is_merge(repo, introducer):
            continue
        if NOT_A_BUG.search(_subject(repo, introducer)):
            continue  # "Bump mypy" did not introduce a bug, whatever later touched its lines

        files = _usable(repo, introducer, min_lines, max_lines, max_files)
        if files is None:
            continue
        snap = _snapshot(repo, introducer, files)
        if snap is None:
            continue
        before, after = snap

        truths: list[Truth] = []
        category = _category_for(fix_subject)
        for path, orig_lines in attributions[introducer].items():
            # A truth on a test fixture is where blame landed, not where a
            # reviewer could see a defect.
            if path not in after or _is_test_path(path):
                continue
            text_lines = after[path].splitlines()
            seen_lines: set[int] = set()
            for ln in _collapse(orig_lines):
                snapped = _snap_line(text_lines, ln)
                if snapped is None or snapped in seen_lines:
                    continue
                seen_lines.add(snapped)
                truths.append(Truth(path, snapped, category, text_lines[snapped - 1].strip(),
                                    note=f"removed by fix {fix_sha[:10]}: {fix_subject}"))
        if not truths:
            continue
        seen.add(introducer)
        cases.append(Case(id=f"fix-{introducer[:10]}", before=before, after=after, truths=tuple(truths[:3]),
                          git_repo=repo, git_ref=introducer, note=f"{_subject(repo, introducer)}  [fixed by {fix_sha[:10]}: {fix_subject}]"))
    return cases
