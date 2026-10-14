"""ContextEngine: turns raw hunks into a ContextBundle with structural
context (enclosing symbol, caller code, callee definitions), not just diff
text.

Python is handled by the stdlib `ast` index. JavaScript/TypeScript, Go and
Java go through tree-sitter when the optional grammars are installed
(`pip install "review-agent[languages]"`); otherwise, and for every other
language, the file gets diff-only context and `unsupported_files` says so.
Agents only ever see the language-agnostic ContextBundle/SymbolRef shapes.
"""
from __future__ import annotations

from pathlib import Path

from review_agent.context import treesitter_index as ts
from review_agent.context.symbol_index import (
    find_caller_refs,
    find_definition,
    find_enclosing_symbol,
)
from review_agent.schemas import ContextBundle, Hunk, SymbolRef

SUPPORTED_EXTENSIONS = {".py"}  # always; tree-sitter languages are added when available
MAX_CALLEE_DEFS = 5
MAX_CONTEXT_CHARS_PER_SYMBOL = 3000
MAX_SYMBOLS = 12  # real PRs can touch dozens of functions; keep the bundle bounded


def language_support(ext: str) -> str:
    """'python' | 'tree-sitter' | 'none'."""
    if ext == ".py":
        return "python"
    if ts.supported(ext):
        return "tree-sitter"
    return "none"


class ContextEngine:
    def __init__(self, repo_root: Path):
        self.repo_root = Path(repo_root)
        self.unsupported_files: list[str] = []

    def _tools(self, ext: str):
        """(enclosing(text, line), definition(name), caller search extensions) for a file extension."""
        if ext == ".py":
            return find_enclosing_symbol, lambda name: find_definition(self.repo_root, name), (".py",)
        if ts.supported(ext):
            return (lambda text, line: ts.find_enclosing_symbol(text, line, ext),
                    lambda name: ts.find_definition(self.repo_root, name, ext),
                    ts.family(ext))
        return None

    def build(self, hunks: list[Hunk], repo_rules: dict | None = None) -> ContextBundle:
        symbols: list[SymbolRef] = []
        seen: set[tuple[str, str]] = set()  # (file, symbol name) de-dup

        for hunk in hunks:
            if len(symbols) >= MAX_SYMBOLS:
                break
            file_path = self.repo_root / hunk.file
            tools = self._tools(file_path.suffix)
            if tools is None:
                if hunk.file not in self.unsupported_files:
                    self.unsupported_files.append(hunk.file)
                continue
            if not file_path.exists():
                continue
            enclosing_fn, definition_fn, exts = tools
            try:
                source = file_path.read_text(encoding="utf-8", errors="ignore")
            except OSError:
                continue

            # Try the changed lines first: with three lines of leading context the
            # hunk's start (and often its midpoint) sits above the function the
            # change is actually in.
            mid_line = (hunk.start_line + hunk.end_line) // 2 or hunk.start_line
            candidates = list(hunk.added_line_numbers) + [mid_line, hunk.start_line, hunk.end_line]
            enclosing = next((e for e in (enclosing_fn(source, ln) for ln in candidates) if e), None)
            if enclosing is None:
                continue

            key = (hunk.file, enclosing.name)
            if key in seen:
                continue
            seen.add(key)

            refs = find_caller_refs(self.repo_root, enclosing.name, exclude_file=hunk.file,
                                    exts=exts, enclosing_fn=enclosing_fn)
            caller_snippets: list[str] = []
            budget = MAX_CONTEXT_CHARS_PER_SYMBOL
            for ref in refs:
                where = f"{ref.file}:{ref.line}" + (f" in `{ref.enclosing}`" if ref.enclosing else "")
                block = f"{where}\n{ref.snippet}"
                if budget - len(block) < 0:
                    break
                budget -= len(block)
                caller_snippets.append(block)

            callee_defs: list[str] = []
            for name in enclosing.callees:
                if len(callee_defs) >= MAX_CALLEE_DEFS or budget <= 0:
                    break
                if name == enclosing.name:
                    continue
                definition = definition_fn(name)
                if definition and len(definition) <= budget:
                    budget -= len(definition)
                    callee_defs.append(definition)

            symbols.append(
                SymbolRef(
                    name=enclosing.name,
                    file=hunk.file,
                    line=enclosing.line,
                    signature=enclosing.signature,
                    callers=[f"{r.file}:{r.line}" for r in refs],
                    callees=enclosing.callees,
                    caller_snippets=caller_snippets,
                    callee_defs=callee_defs,
                )
            )

        return ContextBundle(hunks=hunks, symbols=symbols, repo_rules=repo_rules or {})
