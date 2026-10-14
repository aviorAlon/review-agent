import json

import httpx
import pytest

from review_agent.providers.base import (
    SUMMARY_MARKER,
    extract_fingerprints,
    fingerprint,
    format_finding,
    fp_marker,
    partition_findings,
)
from review_agent.providers.github import GitHubProvider
from review_agent.providers.gitlab import GitLabProvider
from review_agent.schemas import Category, Finding, Hunk, Severity

HUNKS = [
    Hunk(file="app/auth.py", start_line=1, end_line=11),
    Hunk(file="app/auth.py", start_line=40, end_line=45),
    Hunk(file="app/db.py", start_line=5, end_line=9),
]


def _f(file, line, agent="security", merged=None, sev=Severity.WARNING, msg="msg"):
    return Finding(file=file, line=line, severity=sev, category=Category.SECURITY, message=msg,
                   reasoning="why", confidence=0.8, source_agent=agent, merged_from=merged or [])


def _calls(calls, method, suffix):
    return [c for c in calls if c.method == method and c.url.path.endswith(suffix)]


def test_partition_by_hunk_ranges():
    findings = [
        _f("app/auth.py", 3),        # inside first hunk
        _f("app/auth.py", 42),       # inside second hunk
        _f("app/auth.py", 20),       # between hunks -> summary
        _f("app/db.py", 7),          # inside
        _f("app/other.py", 1),       # file not in diff -> summary
        _f("UNKNOWN", 0),            # external-agent fallback -> summary
    ]
    inline, summary = partition_findings(findings, HUNKS)
    assert [(f.file, f.line) for f in inline] == [("app/auth.py", 3), ("app/auth.py", 42), ("app/db.py", 7)]
    assert [(f.file, f.line) for f in summary] == [("app/auth.py", 20), ("app/other.py", 1), ("UNKNOWN", 0)]


def test_unstructured_never_goes_inline_and_is_labelled_in_summary():
    from review_agent.providers.base import format_summary
    raw = Finding(file="app/auth.py", line=3, severity=Severity.INFO, category=Category.OTHER,
                  message="some free text", source_agent="claude-code", unstructured=True)
    inline, summary = partition_findings([raw], HUNKS)   # line 3 IS inside a hunk, still not inline
    assert inline == [] and summary == [raw]
    body = format_summary(summary, 0)
    assert "unstructured output from `claude-code`" in body and "some free text" in body
    assert body.startswith(SUMMARY_MARKER)


def test_format_finding_shows_agreement_and_carries_fingerprint():
    f = _f("a.py", 1, agent="security", merged=["logic"])
    body = format_finding(f)
    assert "[WARNING] security" in body
    assert "security (+logic agree)" in body
    assert "why" in body
    assert body.endswith(fp_marker(f))
    assert extract_fingerprints([body]) == {fingerprint(f)}


def test_fingerprint_is_stable_and_specific():
    a, b = _f("a.py", 1, msg="x"), _f("a.py", 1, msg="x")
    assert fingerprint(a) == fingerprint(b)
    assert fingerprint(a) != fingerprint(_f("a.py", 2, msg="x"))
    assert fingerprint(a) != fingerprint(_f("a.py", 1, msg="y"))
    assert fingerprint(a) == fingerprint(a.model_copy(update={"reasoning": "different", "confidence": 0.1}))


# ---------------- GitLab ----------------

def _gitlab_mock(calls: list, existing_notes: list[dict] | None = None):
    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        if request.method == "GET" and request.url.path.endswith("/merge_requests/7"):
            return httpx.Response(200, json={"diff_refs": {"base_sha": "B", "start_sha": "S", "head_sha": "H"}})
        if request.method == "GET" and request.url.path.endswith("/notes"):
            return httpx.Response(200, json=existing_notes or [])
        if request.method == "POST" and request.url.path.endswith("/discussions"):
            body = json.loads(request.content)
            if body["position"]["new_line"] == 42:
                return httpx.Response(400, json={"message": "line_code can't be blank"})
            return httpx.Response(201, json={"id": "d1"})
        if request.method == "POST" and request.url.path.endswith("/notes"):
            return httpx.Response(201, json={"id": 1})
        if request.method == "PUT" and "/notes/" in request.url.path:
            return httpx.Response(200, json={"id": 77})
        return httpx.Response(404)
    return httpx.MockTransport(handler)


def test_gitlab_posts_inline_discussions_and_summary_note():
    calls: list[httpx.Request] = []
    provider = GitLabProvider("https://gitlab.example.com/", "123", "7", "tok", transport=_gitlab_mock(calls))

    result = provider.post_findings([_f("app/auth.py", 3), _f("app/other.py", 1)], HUNKS)

    assert result.inline_posted == 1 and result.summary_posted == 1 and result.errors == []
    assert calls[0].headers["PRIVATE-TOKEN"] == "tok"
    assert _calls(calls, "GET", "/merge_requests/7")[0].url.path == "/api/v4/projects/123/merge_requests/7"

    (disc,) = _calls(calls, "POST", "/discussions")
    body = json.loads(disc.content)
    assert body["position"] == {
        "position_type": "text", "base_sha": "B", "start_sha": "S", "head_sha": "H",
        "new_path": "app/auth.py", "old_path": "app/auth.py", "new_line": 3,
    }
    assert "[WARNING] security" in body["body"] and fp_marker(_f("app/auth.py", 3)) in body["body"]

    (note,) = _calls(calls, "POST", "/notes")
    note_body = json.loads(note.content)["body"]
    assert "1 inline finding(s) posted" in note_body and "`app/other.py:1`" in note_body
    assert note_body.startswith(SUMMARY_MARKER)


def test_gitlab_rerun_skips_existing_and_updates_summary_in_place():
    f = _f("app/auth.py", 3)
    existing = [{"id": 5, "body": format_finding(f)}, {"id": 77, "body": SUMMARY_MARKER + "\nold summary"}]
    calls: list[httpx.Request] = []
    provider = GitLabProvider("https://gitlab.example.com", "123", "7", "tok", transport=_gitlab_mock(calls, existing))

    result = provider.post_findings([f, _f("app/db.py", 7)], HUNKS, notes=["excluded 1 file(s)"])

    assert result.skipped_existing == 1 and result.inline_posted == 1
    assert result.summary_updated and result.summary_posted == 1
    posted_lines = [json.loads(c.content)["position"]["new_line"] for c in _calls(calls, "POST", "/discussions")]
    assert posted_lines == [7]
    assert _calls(calls, "POST", "/notes") == []            # no second summary
    (put,) = [c for c in calls if c.method == "PUT"]
    assert put.url.path.endswith("/notes/77")
    assert "2 inline finding(s) posted" in json.loads(put.content)["body"]   # counts skipped ones too
    assert "excluded 1 file(s)" in json.loads(put.content)["body"]


def test_gitlab_rejected_inline_is_demoted_to_summary_not_lost():
    calls: list[httpx.Request] = []
    provider = GitLabProvider("https://gitlab.example.com", "123", "7", "tok", transport=_gitlab_mock(calls))

    result = provider.post_findings([_f("app/auth.py", 42)], HUNKS)

    assert result.inline_posted == 0 and result.summary_posted == 1
    assert len(result.errors) == 1 and "400" in result.errors[0]
    (note,) = _calls(calls, "POST", "/notes")
    assert "`app/auth.py:42`" in json.loads(note.content)["body"]


def test_gitlab_nothing_to_post_makes_no_requests():
    calls: list[httpx.Request] = []
    provider = GitLabProvider("https://gitlab.example.com", "123", "7", "tok", transport=_gitlab_mock(calls))
    result = provider.post_findings([], HUNKS)
    assert result.inline_posted == 0 and result.summary_posted == 0 and calls == []


def test_gitlab_from_env_reports_missing_vars(monkeypatch):
    for k in ("CI_PROJECT_ID", "CI_MERGE_REQUEST_IID", "GITLAB_TOKEN"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("CI_PROJECT_ID", "1")
    with pytest.raises(RuntimeError, match="CI_MERGE_REQUEST_IID, GITLAB_TOKEN"):
        GitLabProvider.from_env()


# ---------------- GitHub ----------------

def _github_mock(calls: list, review_comments=None, issue_comments=None):
    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        p = request.url.path
        if request.method == "GET" and p == "/repos/o/r/pulls/9":
            return httpx.Response(200, json={"head": {"sha": "HEAD1"}})
        if request.method == "GET" and p == "/repos/o/r/pulls/9/comments":
            return httpx.Response(200, json=review_comments or [])
        if request.method == "GET" and p == "/repos/o/r/issues/9/comments":
            return httpx.Response(200, json=issue_comments or [])
        if request.method == "POST" and p == "/repos/o/r/pulls/9/reviews":
            return httpx.Response(200, json={"id": 1})
        if request.method == "POST" and p == "/repos/o/r/issues/9/comments":
            return httpx.Response(201, json={"id": 2})
        if request.method == "PATCH" and p.startswith("/repos/o/r/issues/comments/"):
            return httpx.Response(200, json={"id": 55})
        return httpx.Response(404)
    return httpx.MockTransport(handler)


def test_github_posts_review_with_comments_and_a_summary_comment():
    calls: list[httpx.Request] = []
    provider = GitHubProvider("o/r", 9, "tok", transport=_github_mock(calls))

    result = provider.post_findings([_f("app/auth.py", 3), _f("app/db.py", 7), _f("UNKNOWN", 0)], HUNKS)

    assert result.inline_posted == 2 and result.summary_posted == 1 and result.errors == []
    assert calls[0].headers["Authorization"] == "Bearer tok"
    (review,) = _calls(calls, "POST", "/pulls/9/reviews")
    body = json.loads(review.content)
    assert body["commit_id"] == "HEAD1" and body["event"] == "COMMENT"
    assert [(c["path"], c["line"], c["side"]) for c in body["comments"]] == [("app/auth.py", 3, "RIGHT"), ("app/db.py", 7, "RIGHT")]
    assert all(FP in c["body"] for c in body["comments"] for FP in ["<!-- review-agent:fp="])
    (summary,) = _calls(calls, "POST", "/issues/9/comments")
    assert "_(no location)_" in json.loads(summary.content)["body"]


def test_github_rerun_skips_existing_and_patches_summary():
    f = _f("app/auth.py", 3)
    calls: list[httpx.Request] = []
    provider = GitHubProvider("o/r", 9, "tok", transport=_github_mock(
        calls, review_comments=[{"body": format_finding(f)}], issue_comments=[{"id": 55, "body": SUMMARY_MARKER + " old"}]))

    result = provider.post_findings([f], HUNKS)

    assert result.skipped_existing == 1 and result.inline_posted == 0
    assert _calls(calls, "POST", "/pulls/9/reviews") == []       # nothing new: no review request at all
    assert result.summary_updated and result.summary_posted == 1
    (patch,) = [c for c in calls if c.method == "PATCH"]
    assert patch.url.path == "/repos/o/r/issues/comments/55"
    assert "1 inline finding(s) posted" in json.loads(patch.content)["body"]


def test_github_from_env_parses_pr_number(monkeypatch):
    monkeypatch.setenv("GITHUB_REPOSITORY", "o/r")
    monkeypatch.setenv("GITHUB_TOKEN", "t")
    monkeypatch.setenv("GITHUB_REF", "refs/pull/42/merge")
    provider = GitHubProvider.from_env()
    assert provider.base.endswith("/repos/o/r/pulls/42")


def test_github_from_env_rejects_non_pr_ref(monkeypatch):
    monkeypatch.setenv("GITHUB_REPOSITORY", "o/r")
    monkeypatch.setenv("GITHUB_TOKEN", "t")
    monkeypatch.setenv("GITHUB_REF", "refs/heads/main")
    with pytest.raises(RuntimeError, match="not a pull request ref"):
        GitHubProvider.from_env()
