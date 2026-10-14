"""Python-native structural context: which function/class a hunk touches,
and a best-effort caller/callee list for it.

Deliberately AST-only (stdlib, zero deps) for the MVP. Other languages are
a documented extension point (see ContextEngine docstring) -- not solved
here, and we don't pretend otherwise.
"""
from __future__ import annotations

import ast
import re
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class EnclosingSymbol:
    name: str
    line: int
    signature: str
    callees: list[str] = field(default_factory=list)


def _signature(node: ast.FunctionDef | ast.AsyncFunctionDef) -> str:
    args = [a.arg for a in node.args.args]
    prefix = "async def" if isinstance(node, ast.AsyncFunctionDef) else "def"
    return f"{prefix} {node.name}({', '.join(args)})"


def _callees_of(node: ast.AST) -> list[str]:
    names: list[str] = []
    for child in ast.walk(node):
        if isinstance(child, ast.Call):
            func = child.func
            if isinstance(func, ast.Name):
                names.append(func.id)
            elif isinstance(func, ast.Attribute):
                names.append(func.attr)
    # stable de-dup, keep first-seen order
    seen: set[str] = set()
    out = []
    for n in names:
        if n not in seen:
            seen.add(n)
            out.append(n)
    return out


def find_enclosing_symbol(source: str, line: int) -> EnclosingSymbol | None:
    """Find the innermost function/class def in `source` that contains `line`."""
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return None

    best: ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef | None = None
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            start = node.lineno
            end = getattr(node, "end_lineno", start)
            if start <= line <= end:
                # prefer the innermost (smallest) enclosing range
                if best is None or (end - start) < (getattr(best, "end_lineno", best.lineno) - best.lineno):
                    best = node

    if best is None:
        return None

    if isinstance(best, ast.ClassDef):
        return EnclosingSymbol(name=best.name, line=best.lineno, signature=f"class {best.name}", callees=[])

    return EnclosingSymbol(
        name=best.name,
        line=best.lineno,
        signature=_signature(best),
        callees=_callees_of(best),
    )


@dataclass
class CallerRef:
    file: str
    line: int
    enclosing: str | None   # signature of the function the call sits in
    snippet: str            # numbered lines around the call


_SKIP_DIRS = {".git", ".venv", "venv", "node_modules", "__pycache__", "build", "dist", ".tox", ".eggs"}


def _source_files(repo_root: Path, exts: tuple[str, ...] = (".py",)) -> list[Path]:
    out: list[Path] = []
    try:
        for p in repo_root.rglob("*"):
            if p.suffix in exts and p.is_file() and not (_SKIP_DIRS & set(p.relative_to(repo_root).parts[:-1])):
                out.append(p)
    except OSError:
        return out
    return out


def _py_files(repo_root: Path) -> list[Path]:
    return _source_files(repo_root, (".py",))


def _rel(path: Path, repo_root: Path) -> str:
    try:
        return path.relative_to(repo_root).as_posix()
    except ValueError:
        return path.as_posix()


_JAVA_MODIFIERS = r"(?:(?:public|private|protected|static|final|abstract|synchronized|default|native)\s+)*"


def _is_definition_line(line: str, name: str) -> bool:
    """A line that DEFINES `name` rather than calls it, across the supported languages.

    Each branch is anchored tightly: the Java/JS-method branches require the
    line to END with `{` (a call statement never does), so a Python
    `return email_for(db, uid)` is never mistaken for a definition.
    """
    n = re.escape(name)
    patterns = (
        rf"^\s*(?:async\s+)?def\s+{n}\s*\(",                                              # python
        rf"^\s*(?:export\s+)?(?:default\s+)?(?:async\s+)?function\s*\*?\s*{n}\s*\(",     # js/ts
        rf"^\s*(?:async\s+)?{n}\s*\([^)]*\)\s*\{{\s*$",                                   # js/ts method shorthand
        rf"^\s*func\s+(?:\([^)]*\)\s*)?{n}\s*\(",                                         # go
        rf"^\s*{_JAVA_MODIFIERS}[\w<>\[\],.?\s]+?\s+{n}\s*\([^)]*\)\s*(?:throws[^{{]*)?\{{\s*$",  # java
    )
    return any(re.match(p, line) for p in patterns)


def find_caller_refs(repo_root: Path, symbol_name: str, exclude_file: str,
                     max_results: int = 5, radius: int = 4, exts: tuple[str, ...] = (".py",),
                     enclosing_fn=None) -> list[CallerRef]:
    """Call sites of `symbol_name(` outside `exclude_file`, with surrounding code.

    Grep-grade, not a call graph: cheap enough per PR, and the snippet is what
    lets a reviewer see *how* the changed function is used (what it is handed,
    what is done with its result) rather than merely that it is used.
    `enclosing_fn(text, line)` names the function a call sits in; defaults to
    the Python index, and a language index can pass its own.
    """
    enclosing_fn = enclosing_fn or find_enclosing_symbol
    hits: list[CallerRef] = []
    needle = f"{symbol_name}("
    for path in _source_files(repo_root, exts):
        if len(hits) >= max_results:
            break
        rel = _rel(path, repo_root)
        if rel == exclude_file:
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        lines = text.splitlines()
        for i, line in enumerate(lines, start=1):
            if needle in line and not _is_definition_line(line, symbol_name):
                try:
                    enclosing = enclosing_fn(text, i)
                except Exception:  # noqa: BLE001 - a parser hiccup must not cost the snippet
                    enclosing = None
                lo, hi = max(1, i - radius), min(len(lines), i + radius)
                snippet = "\n".join(f"{'>' if n == i else ' '} {n}: {lines[n - 1]}" for n in range(lo, hi + 1))
                hits.append(CallerRef(file=rel, line=i, enclosing=enclosing.signature if enclosing else None, snippet=snippet))
                if len(hits) >= max_results:
                    break
    return hits


def find_callers(repo_root: Path, symbol_name: str, exclude_file: str, max_results: int = 5) -> list[str]:
    """`file:line` call sites. Kept as the compact form; find_caller_refs has the code."""
    return [f"{c.file}:{c.line}" for c in find_caller_refs(repo_root, symbol_name, exclude_file, max_results, radius=0)]


def find_definition(repo_root: Path, name: str, max_body_lines: int = 12) -> str | None:
    """Source of a project-defined function `name`, truncated. None for builtins/third-party."""
    for path in _py_files(repo_root):
        try:
            text = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        if f"def {name}(" not in text:
            continue
        try:
            tree = ast.parse(text)
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
                lines = text.splitlines()
                end = min(getattr(node, "end_lineno", node.lineno), node.lineno + max_body_lines - 1)
                body = "\n".join(f"  {n}: {lines[n - 1]}" for n in range(node.lineno, end + 1))
                more = "" if end >= getattr(node, "end_lineno", end) else "\n  ..."
                return f"{_rel(path, repo_root)}:{node.lineno}\n{body}{more}"
    return None
