from __future__ import annotations

from pathlib import Path

import pytest

FIXTURES = Path(__file__).parent / "fixtures"

AUTH_PY = '''def check_permission(user, resource):
    # fast path
    if user.role in ("admin", "owner"):
        return True
    if resource.owner_id == user.id:
        return True
    return resource.is_public


def load_user(user_id):
    return db.get(user_id)
'''

HANDLERS_PY = '''from app.auth import check_permission


def get_resource(user, resource):
    if not check_permission(user, resource):
        raise PermissionError
    return resource
'''


@pytest.fixture
def sample_diff() -> str:
    return (FIXTURES / "sample.diff").read_text(encoding="utf-8")


@pytest.fixture
def fake_repo(tmp_path: Path) -> Path:
    """A tiny Python repo matching sample.diff's post-change state."""
    (tmp_path / "app").mkdir()
    (tmp_path / "app" / "auth.py").write_text(AUTH_PY, encoding="utf-8")
    (tmp_path / "app" / "handlers.py").write_text(HANDLERS_PY, encoding="utf-8")
    return tmp_path
