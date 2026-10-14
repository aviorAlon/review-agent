from review_agent.context.engine import ContextEngine
from review_agent.context.symbol_index import find_callers, find_enclosing_symbol
from review_agent.diff import parse_diff


def test_enclosing_symbol_picks_innermost():
    src = """class Outer:
    def method(self, a, b):
        return helper(a) + other.call(b)
"""
    sym = find_enclosing_symbol(src, 3)
    assert sym is not None
    assert sym.name == "method"
    assert sym.signature == "def method(self, a, b)"
    assert sym.callees == ["helper", "call"]


def test_enclosing_symbol_none_for_module_level():
    assert find_enclosing_symbol("x = 1\n", 1) is None


def test_enclosing_symbol_survives_syntax_error():
    assert find_enclosing_symbol("def broken(:\n", 1) is None


def test_find_callers_excludes_own_file(fake_repo):
    hits = find_callers(fake_repo, "check_permission", exclude_file="app/auth.py")
    assert hits == ["app/handlers.py:5"]


def test_context_engine_resolves_symbol_and_callers(sample_diff, fake_repo):
    hunks = parse_diff(sample_diff)
    bundle = ContextEngine(fake_repo).build(hunks)
    assert len(bundle.symbols) == 1
    sym = bundle.symbols[0]
    assert sym.name == "check_permission"
    assert sym.file == "app/auth.py"
    assert sym.callers == ["app/handlers.py:5"]
    # the call site comes with numbered surrounding code and its enclosing function
    assert len(sym.caller_snippets) == 1
    block = sym.caller_snippets[0]
    assert block.startswith("app/handlers.py:5 in `def get_resource(user, resource)`")
    assert "> 5:     if not check_permission(user, resource):" in block
    assert "  6:         raise PermissionError" in block


def test_definition_lines_are_not_callers(tmp_path):
    (tmp_path / "a.py").write_text("def helper(x):\n    return x\n", encoding="utf-8")
    (tmp_path / "b.py").write_text("from a import helper\n\nprint(helper(1))\n", encoding="utf-8")
    assert find_callers(tmp_path, "helper", exclude_file="zzz.py") == ["b.py:3"]


def test_callee_definitions_are_included_when_project_defined(tmp_path):
    from review_agent.context.symbol_index import find_definition

    (tmp_path / "util.py").write_text(
        "import os\n\n\ndef safe_join(base, name):\n    # TODO: containment check\n    return os.path.join(base, name)\n", encoding="utf-8")
    (tmp_path / "upload.py").write_text(
        "from util import safe_join\n\n\ndef store(filename):\n    return open(safe_join('/srv', filename))\n", encoding="utf-8")
    definition = find_definition(tmp_path, "safe_join")
    assert definition is not None and definition.startswith("util.py:4")
    assert "return os.path.join(base, name)" in definition
    assert find_definition(tmp_path, "open") is None  # builtin: nothing to show

    diff = """--- a/upload.py
+++ b/upload.py
@@ -2,4 +2,4 @@


 def store(filename):
-    return None
+    return open(safe_join('/srv', filename))
"""
    bundle = ContextEngine(tmp_path).build(parse_diff(diff))
    (sym,) = bundle.symbols
    assert sym.callees == ["open", "safe_join"]
    assert len(sym.callee_defs) == 1 and "safe_join" in sym.callee_defs[0]


def test_vendored_and_venv_dirs_are_skipped(tmp_path):
    (tmp_path / ".venv" / "lib").mkdir(parents=True)
    (tmp_path / ".venv" / "lib" / "x.py").write_text("helper(1)\n", encoding="utf-8")
    (tmp_path / "app.py").write_text("helper(2)\n", encoding="utf-8")
    assert find_callers(tmp_path, "helper", exclude_file="none.py") == ["app.py:1"]


def test_context_engine_ignores_unsupported_or_missing_files(fake_repo):
    diff = """diff --git a/web/app.js b/web/app.js
--- a/web/app.js
+++ b/web/app.js
@@ -1,1 +1,2 @@
 const a = 1;
+const b = 2;
"""
    bundle = ContextEngine(fake_repo).build(parse_diff(diff))
    assert bundle.hunks and bundle.symbols == []
