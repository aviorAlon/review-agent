"""GitHub: one Pull Request Review carrying the new inline comments, plus
one issue comment as the summary (reviews cannot be edited in place, issue
comments can). The review is a single request, so it is atomic -- GitHub
422s the whole review if any comment line is outside the diff, which is
exactly why partition_findings runs first.

Idempotent: existing review comments are listed first; findings whose
fingerprint is already on the PR are skipped, and the summary comment is
updated in place.

Auth: GITHUB_TOKEN (the Actions-provided one works for PRs from the same
repo). PR number is read from GITHUB_REF (`refs/pull/<n>/merge`).
"""
from __future__ import annotations

import os
import re

import httpx

from review_agent.providers.base import (
    SUMMARY_MARKER,
    PostResult,
    VCSProvider,
    extract_fingerprints,
    fingerprint,
    format_finding,
    format_summary,
    partition_findings,
)
from review_agent.schemas import Finding, Hunk

_PR_REF = re.compile(r"refs/pull/(\d+)/")


class GitHubProvider(VCSProvider):
    def __init__(
        self,
        repo: str,
        pr_number: int,
        token: str,
        api_url: str = "https://api.github.com",
        transport: httpx.BaseTransport | None = None,
    ):
        api = api_url.rstrip("/")
        self.base = f"{api}/repos/{repo}/pulls/{pr_number}"
        self.issue_base = f"{api}/repos/{repo}/issues/{pr_number}"
        self.comments_base = f"{api}/repos/{repo}/issues/comments"
        self.client = httpx.Client(
            headers={"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json"},
            timeout=30.0,
            transport=transport,
        )

    @classmethod
    def from_env(cls) -> "GitHubProvider":
        missing = [k for k in ("GITHUB_REPOSITORY", "GITHUB_TOKEN", "GITHUB_REF") if not os.environ.get(k)]
        if missing:
            raise RuntimeError(f"GitHub posting needs env vars: {', '.join(missing)}")
        m = _PR_REF.search(os.environ["GITHUB_REF"])
        if not m:
            raise RuntimeError(f"GITHUB_REF={os.environ['GITHUB_REF']!r} is not a pull request ref")
        return cls(
            repo=os.environ["GITHUB_REPOSITORY"],
            pr_number=int(m.group(1)),
            token=os.environ["GITHUB_TOKEN"],
            api_url=os.environ.get("GITHUB_API_URL", "https://api.github.com"),
        )

    def _head_sha(self) -> str:
        r = self.client.get(self.base)
        r.raise_for_status()
        return r.json()["head"]["sha"]

    def _existing(self) -> tuple[set[str], int | None]:
        fps: set[str] = set()
        r = self.client.get(f"{self.base}/comments", params={"per_page": 100})
        if r.is_success:
            fps |= extract_fingerprints([c.get("body") or "" for c in r.json()])
        summary_id: int | None = None
        r = self.client.get(f"{self.issue_base}/comments", params={"per_page": 100})
        if r.is_success:
            for c in r.json():
                if SUMMARY_MARKER in (c.get("body") or ""):
                    summary_id = c.get("id")
        return fps, summary_id

    def post_findings(self, findings: list[Finding], hunks: list[Hunk], notes: list[str] | None = None) -> PostResult:
        inline, summary = partition_findings(findings, hunks)
        result = PostResult()
        if not inline and not summary and not notes:
            return result

        existing, summary_id = self._existing()
        new = [f for f in inline if fingerprint(f) not in existing]
        result.skipped_existing = len(inline) - len(new)

        if new:
            payload = {
                "commit_id": self._head_sha(),
                "event": "COMMENT",
                "comments": [
                    {"path": f.file, "line": f.line, "side": "RIGHT", "body": format_finding(f)} for f in new
                ],
            }
            r = self.client.post(f"{self.base}/reviews", json=payload)
            if r.is_success:
                result.inline_posted = len(new)
            else:
                result.errors.append(f"review -> {r.status_code} {r.text[:200]}")
                summary = summary + new  # don't lose them

        body = format_summary(summary, result.inline_posted + result.skipped_existing, notes)
        if summary_id is not None:
            r = self.client.patch(f"{self.comments_base}/{summary_id}", json={"body": body})
            result.summary_updated = r.is_success
        else:
            r = self.client.post(f"{self.issue_base}/comments", json={"body": body})
        if r.is_success:
            result.summary_posted = 1
        else:
            result.errors.append(f"summary -> {r.status_code} {r.text[:200]}")
        return result
