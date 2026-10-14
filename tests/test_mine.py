import subprocess
from pathlib import Path

import pytest

from review_agent.bench.cases import Case
from review_agent.bench.mine import fix_pair_cases, recent_cases
from review_agent.bench.runner import make_diff, materialize
from review_agent.context.engine import ContextEngine
from review_agent.diff import parse_diff
from review_agent.schemas import Category


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, text=True).stdout


def _commit(repo: Path, msg: str) -> str:
    _git(repo, "add", "-A")
    _git(repo, "-c", "user.name=t", "-c", "user.email=t@example.com", "commit", "-q", "-m", msg)
    return _git(repo, "rev-parse", "HEAD").strip()


@pytest.fixture
def history(tmp_path: Path) -> dict:
    """base -> introduce (rewrites the return, dropping the guard) -> unrelated -> fix (rewrites it again).

    SZZ attributes the lines a fix REMOVES. A fix that only adds a guard back
    removes nothing, so it cannot be blamed -- the bug was an absence. The
    fixture therefore has the fix modify the offending line, which is the
    only shape SZZ-lite can see.
    """
    repo = tmp_path / "repo"
    (repo / "app").mkdir(parents=True)
    (repo / "app" / "__init__.py").write_text("", encoding="utf-8")
    _git(repo, "init", "-q")
    (repo / "app" / "mail.py").write_text(
        "def email_for(db, user_id):\n    user = db.get(user_id)\n    if user is None:\n        return None\n    return user.email\n", encoding="utf-8")
    (repo / "app" / "api.py").write_text("from app.mail import email_for\n\n\ndef contact(db, uid):\n    return email_for(db, uid)\n", encoding="utf-8")
    base = _commit(repo, "initial")

    (repo / "app" / "mail.py").write_text(
        "def email_for(db, user_id):\n    user = db.get(user_id)\n    return user.email.lower()\n", encoding="utf-8")
    introduce = _commit(repo, "simplify email_for and normalise case")

    (repo / "app" / "other.py").write_text("X = 1\nY = 2\nZ = 3\nW = 4\nV = 5\nU = 6\nT = 7\nS = 8\nR = 9\nQ = 10\nP = 11\n", encoding="utf-8")
    unrelated = _commit(repo, "add constants")

    (repo / "app" / "mail.py").write_text(
        "def email_for(db, user_id):\n    user = db.get(user_id)\n    return user.email.lower() if user else None\n", encoding="utf-8")
    fix = _commit(repo, "Fix crash when user is missing")
    return {"repo": repo, "base": base, "introduce": introduce, "unrelated": unrelated, "fix": fix}


def test_fix_that_only_adds_lines_cannot_be_blamed(tmp_path):
    """Documented SZZ blind spot: re-adding a deleted guard removes nothing, so no pair is mined."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    (repo / "m.py").write_text("def f(u):\n    if u is None:\n        return None\n    return u.email\n", encoding="utf-8")
    _commit(repo, "initial")
    (repo / "m.py").write_text("def f(u):\n    return u.email\n", encoding="utf-8")
    _commit(repo, "simplify")
    (repo / "m.py").write_text("def f(u):\n    if u is None:\n        return None\n    return u.email\n", encoding="utf-8")
    _commit(repo, "Fix crash on missing user")
    assert fix_pair_cases(str(repo), n=5, min_lines=1) == []


def test_recent_cases_are_clean_git_backed_and_size_filtered(history):
    cases = recent_cases(str(history["repo"]), n=10, min_lines=1, max_lines=400)
    ids = {c.git_ref for c in cases}
    assert history["fix"] in ids and history["unrelated"] in ids
    for c in cases:
        assert c.clean and c.git_repo and c.id.startswith("git-")
        assert all(p.endswith(".py") for p in c.after)
    small = recent_cases(str(history["repo"]), n=10, min_lines=1, max_lines=3)
    assert history["unrelated"] not in {c.git_ref for c in small}  # 11-line file exceeds the band


def test_fix_pair_cases_blame_the_introducing_commit(history):
    cases = fix_pair_cases(str(history["repo"]), n=5, min_lines=1)
    assert len(cases) == 1
    case = cases[0]
    assert case.git_ref == history["introduce"]
    assert case.id == f"fix-{history['introduce'][:10]}"
    (t,) = case.truths
    # the fix rewrote line 3; blame at fix^ attributes that line to `introduce`,
    # and the blamed original line number is the truth
    assert t.file == "app/mail.py" and t.line == 3
    assert t.snippet == "return user.email.lower()"
    assert t.category == Category.LOGIC
    assert "Fix crash" in t.note
    assert "app/mail.py" in case.after and "app/mail.py" in case.before


def test_materialize_git_case_extracts_full_tree_for_context(history, tmp_path):
    (case,) = fix_pair_cases(str(history["repo"]), n=5, min_lines=1)
    root = materialize(case, tmp_path / "m")
    assert (root / "app" / "api.py").exists()      # untouched by the diff, present for callers
    assert (root / "app" / "mail.py").read_text(encoding="utf-8") == case.after["app/mail.py"]
    assert not (root / "app" / "other.py").exists()  # added only in a LATER commit

    bundle = ContextEngine(root).build(parse_diff(make_diff(case)))
    (sym,) = bundle.symbols
    assert sym.name == "email_for" and sym.callers == ["app/api.py:5"]
    assert "return email_for(db, uid)" in sym.caller_snippets[0]


def test_materialize_synthetic_case_unchanged(tmp_path):
    case = Case(id="s", before={}, after={"a.py": "x = 1\n"})
    root = materialize(case, tmp_path)
    assert (root / "a.py").read_text(encoding="utf-8") == "x = 1\n"
