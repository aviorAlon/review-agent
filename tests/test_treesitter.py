from pathlib import Path

import pytest

ts = pytest.importorskip("tree_sitter")
pytest.importorskip("tree_sitter_javascript")
pytest.importorskip("tree_sitter_go")
pytest.importorskip("tree_sitter_java")
pytest.importorskip("tree_sitter_typescript")

from review_agent.context import treesitter_index as tsi  # noqa: E402
from review_agent.context.engine import ContextEngine, language_support  # noqa: E402
from review_agent.diff import parse_diff  # noqa: E402

JS = """import { db } from './db';

export function displayName(user) {
  return user.profile.name || user.email;
}

const whoami = (req) => {
  const user = db.get(req.userId);
  return { name: displayName(user) };
};
"""

GO = """package app

func canEdit(user User, rec Record) bool {
\tif user.IsStaff {
\t\treturn true
\t}
\treturn user.ID == rec.OwnerID
}

func amendInvoice(u User, inv Invoice) error {
\tif !canEdit(u, inv.Record) {
\t\treturn ErrForbidden
\t}
\treturn inv.Apply()
}
"""

JAVA = """public class Perms {
    public static boolean canEdit(User user, Record rec) {
        if (user.isStaff()) {
            return true;
        }
        return user.getId() == rec.getOwnerId();
    }

    public void amend(User u, Invoice inv) {
        if (!canEdit(u, inv.record())) {
            throw new SecurityException("owner only");
        }
        inv.apply();
    }
}
"""


def test_language_support_reports_honestly():
    assert language_support(".py") == "python"
    assert language_support(".js") == "tree-sitter"
    assert language_support(".go") == "tree-sitter"
    assert language_support(".rb") == "none"


def test_js_enclosing_symbol_and_callees():
    sym = tsi.find_enclosing_symbol(JS, 4, ".js")
    assert sym and sym.name == "displayName" and sym.line == 3
    assert "function displayName(user)" in sym.signature  # the node starts at `function`; `export` is its parent
    arrow = tsi.find_enclosing_symbol(JS, 8, ".js")
    assert arrow and arrow.name == "whoami"          # name recovered from the declarator
    assert arrow.callees == ["get", "displayName"]   # member call -> property name


def test_go_enclosing_symbol_and_callees():
    sym = tsi.find_enclosing_symbol(GO, 11, ".go")
    assert sym and sym.name == "amendInvoice" and sym.callees == ["canEdit", "Apply"]


def test_java_enclosing_symbol_and_callees():
    sym = tsi.find_enclosing_symbol(JAVA, 10, ".java")
    assert sym and sym.name == "amend"
    assert "canEdit" in sym.callees and "apply" in sym.callees


def test_definition_lookup_across_files(tmp_path):
    (tmp_path / "perms.go").write_text(GO, encoding="utf-8")
    d = tsi.find_definition(tmp_path, "canEdit", ".go")
    assert d and d.startswith("perms.go:3") and "user.IsStaff" in d
    assert tsi.find_definition(tmp_path, "nope", ".go") is None


def test_engine_builds_cross_file_context_for_typescript(tmp_path):
    (tmp_path / "users.ts").write_text("export function displayName(user: User): string {\n  return user.profile.name;\n}\n", encoding="utf-8")
    (tmp_path / "api.ts").write_text("import { displayName } from './users';\n\nexport function whoami(req: Req) {\n  const user = db.get(req.userId); // may be undefined\n  return { name: displayName(user) };\n}\n", encoding="utf-8")
    diff = """--- a/users.ts
+++ b/users.ts
@@ -1,3 +1,3 @@
 export function displayName(user: User): string {
-  return user.email;
+  return user.profile.name;
 }
"""
    engine = ContextEngine(tmp_path)
    bundle = engine.build(parse_diff(diff))
    (sym,) = bundle.symbols
    assert sym.name == "displayName" and sym.callers == ["api.ts:5"]
    assert "may be undefined" in sym.caller_snippets[0]
    assert sym.caller_snippets[0].startswith("api.ts:5 in `function whoami(req: Req)")
    assert engine.unsupported_files == []


def test_engine_records_unsupported_languages(tmp_path):
    (tmp_path / "x.rb").write_text("def f\n  1\nend\n", encoding="utf-8")
    diff = "--- a/x.rb\n+++ b/x.rb\n@@ -1,3 +1,3 @@\n def f\n-  1\n+  2\n end\n"
    engine = ContextEngine(tmp_path)
    bundle = engine.build(parse_diff(diff))
    assert bundle.symbols == [] and engine.unsupported_files == ["x.rb"]
