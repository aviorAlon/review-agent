"""Structural context for JavaScript/TypeScript, Go and Java via tree-sitter.

Same two questions the Python `ast` index answers -- which function does a
line sit in, and what does that function call -- plus definition lookup for
project-defined callees. Grammars are optional (`pip install
"review-agent[languages]"`); without them these languages fall back to
diff-only context, and the engine says so.
"""
from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from review_agent.context.symbol_index import EnclosingSymbol, _rel, _source_files

# extension -> (module, attribute that returns the language, family of extensions to search)
_GRAMMARS: dict[str, tuple[str, str, tuple[str, ...]]] = {
    ".js": ("tree_sitter_javascript", "language", (".js", ".jsx", ".mjs", ".cjs")),
    ".jsx": ("tree_sitter_javascript", "language", (".js", ".jsx", ".mjs", ".cjs")),
    ".mjs": ("tree_sitter_javascript", "language", (".js", ".jsx", ".mjs", ".cjs")),
    ".cjs": ("tree_sitter_javascript", "language", (".js", ".jsx", ".mjs", ".cjs")),
    ".ts": ("tree_sitter_typescript", "language_typescript", (".ts", ".tsx", ".js", ".jsx")),
    ".tsx": ("tree_sitter_typescript", "language_tsx", (".ts", ".tsx", ".js", ".jsx")),
    ".go": ("tree_sitter_go", "language", (".go",)),
    ".java": ("tree_sitter_java", "language", (".java",)),
}

_FUNCTION_NODES = {
    "function_declaration", "function_expression", "arrow_function", "method_definition",  # js/ts
    "method_declaration", "func_literal",                                                   # go (+ java method)
    "constructor_declaration",                                                              # java
}
_CALL_NODES = {"call_expression", "method_invocation", "new_expression"}


def supported(ext: str) -> bool:
    return ext in _GRAMMARS and _parser(ext) is not None


def family(ext: str) -> tuple[str, ...]:
    return _GRAMMARS[ext][2] if ext in _GRAMMARS else (ext,)


@lru_cache(maxsize=None)
def _parser(ext: str):
    spec = _GRAMMARS.get(ext)
    if spec is None:
        return None
    module_name, attr, _ = spec
    try:
        import importlib

        from tree_sitter import Language, Parser

        module = importlib.import_module(module_name)
        return Parser(Language(getattr(module, attr)()))
    except Exception:  # noqa: BLE001 - missing/incompatible grammar => unsupported, not fatal
        return None


def _text(node, src: bytes) -> str:
    return src[node.start_byte:node.end_byte].decode("utf-8", errors="ignore")


def _node_name(node, src: bytes) -> str | None:
    name = node.child_by_field_name("name")
    if name is not None:
        return _text(name, src)
    # `const f = () => ...` / `const f = function () {}` : name lives on the declarator
    parent = node.parent
    if parent is not None and parent.type in ("variable_declarator", "pair", "assignment_expression", "public_field_definition"):
        for field_name in ("name", "key", "left"):
            n = parent.child_by_field_name(field_name)
            if n is not None:
                return _text(n, src)
    return None


def _call_name(node, src: bytes) -> str | None:
    fn = node.child_by_field_name("function") or node.child_by_field_name("name") or node.child_by_field_name("constructor")
    if fn is None:
        return None
    if fn.type in ("member_expression", "selector_expression", "field_access"):
        prop = fn.child_by_field_name("property") or fn.child_by_field_name("field")
        return _text(prop, src) if prop is not None else None
    return _text(fn, src)


def _walk(node):
    stack = [node]
    while stack:
        n = stack.pop()
        yield n
        stack.extend(reversed(n.children))


def _parse(ext: str, source: str):
    parser = _parser(ext)
    if parser is None:
        return None, b""
    src = source.encode("utf-8", errors="ignore")
    return parser.parse(src), src


def find_enclosing_symbol(source: str, line: int, ext: str) -> EnclosingSymbol | None:
    tree, src = _parse(ext, source)
    if tree is None:
        return None
    best = None
    for node in _walk(tree.root_node):
        if node.type not in _FUNCTION_NODES:
            continue
        start, end = node.start_point[0] + 1, node.end_point[0] + 1
        if start <= line <= end and (best is None or (end - start) < (best.end_point[0] - best.start_point[0])):
            best = node
    if best is None:
        return None
    name = _node_name(best, src) or "<anonymous>"
    first_line = _text(best, src).splitlines()[0].strip()
    signature = first_line if len(first_line) <= 120 else first_line[:117] + "..."
    callees: list[str] = []
    for n in _walk(best):
        if n.type in _CALL_NODES:
            c = _call_name(n, src)
            if c and c not in callees:
                callees.append(c)
    return EnclosingSymbol(name=name, line=best.start_point[0] + 1, signature=signature, callees=callees)


def find_definition(repo_root: Path, name: str, ext: str, max_body_lines: int = 12) -> str | None:
    needle = name.encode("utf-8")
    for path in _source_files(repo_root, family(ext)):
        try:
            source = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        if name not in source:
            continue
        tree, src = _parse(path.suffix, source)
        if tree is None:
            continue
        for node in _walk(tree.root_node):
            if node.type in _FUNCTION_NODES and _node_name(node, src) == name:
                lines = source.splitlines()
                start = node.start_point[0] + 1
                end = min(node.end_point[0] + 1, start + max_body_lines - 1)
                body = "\n".join(f"  {n}: {lines[n - 1]}" for n in range(start, end + 1))
                more = "" if end >= node.end_point[0] + 1 else "\n  ..."
                return f"{_rel(path, repo_root)}:{start}\n{body}{more}"
    return None
