"""GitLab: inline findings become MR Discussions, the rest one MR Note.

The `position` object needs base/start/head SHAs. We fetch them from the
MR's `diff_refs` instead of reconstructing from CI env vars -- the env
vars are easy to get subtly wrong (start_sha != base_sha on rebased MRs)
and the API answer is correct by construction.

Idempotent: existing notes are listed first; findings whose fingerprint is
already on the MR are skipped, and the summary note is updated in place.

Auth is a project/personal access token in GITLAB_TOKEN. CI_JOB_TOKEN
cannot create discussions, so we don't fall back to it silently.
"""
from __future__ import annotations

import os

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


class GitLabProvider(VCSProvider):
    def __init__(
        self,
        server_url: str,
        project_id: str,
        mr_iid: str,
        token: str,
        transport: httpx.BaseTransport | None = None,
    ):
        self.base = f"{server_url.rstrip('/')}/api/v4/projects/{project_id}/merge_requests/{mr_iid}"
        self.client = httpx.Client(headers={"PRIVATE-TOKEN": token}, timeout=30.0, transport=transport)

    @classmethod
    def from_env(cls) -> "GitLabProvider":
        missing = [k for k in ("CI_PROJECT_ID", "CI_MERGE_REQUEST_IID", "GITLAB_TOKEN") if not os.environ.get(k)]
        if missing:
            raise RuntimeError(f"GitLab posting needs env vars: {', '.join(missing)}")
        return cls(
            server_url=os.environ.get("CI_SERVER_URL", "https://gitlab.com"),
            project_id=os.environ["CI_PROJECT_ID"],
            mr_iid=os.environ["CI_MERGE_REQUEST_IID"],
            token=os.environ["GITLAB_TOKEN"],
        )

    def _diff_refs(self) -> dict:
        r = self.client.get(self.base)
        r.raise_for_status()
        refs = r.json().get("diff_refs") or {}
        for k in ("base_sha", "start_sha", "head_sha"):
            if not refs.get(k):
                raise RuntimeError(f"MR diff_refs missing {k}; is the MR still being prepared?")
        return refs

    def _existing(self) -> tuple[set[str], int | None]:
        """Fingerprints already on the MR and the id of our summary note, if any."""
        fps: set[str] = set()
        summary_id: int | None = None
        page = 1
        while page and page <= 10:
            r = self.client.get(f"{self.base}/notes", params={"per_page": 100, "page": page})
            if not r.is_success:
                break
            for note in r.json():
                body = note.get("body") or ""
                fps |= extract_fingerprints([body])
                if SUMMARY_MARKER in body:
                    summary_id = note.get("id")
            nxt = r.headers.get("X-Next-Page", "")
            page = int(nxt) if nxt.isdigit() else 0
        return fps, summary_id

    def post_findings(self, findings: list[Finding], hunks: list[Hunk], notes: list[str] | None = None) -> PostResult:
        inline, summary = partition_findings(findings, hunks)
        result = PostResult()
        if not inline and not summary and not notes:
            return result

        existing, summary_id = self._existing()
        refs = self._diff_refs()
        for f in inline:
            if fingerprint(f) in existing:
                result.skipped_existing += 1
                continue
            payload = {
                "body": format_finding(f),
                "position": {
                    "position_type": "text",
                    "base_sha": refs["base_sha"],
                    "start_sha": refs["start_sha"],
                    "head_sha": refs["head_sha"],
                    "new_path": f.file,
                    "old_path": f.file,
                    "new_line": f.line,
                },
            }
            r = self.client.post(f"{self.base}/discussions", json=payload)
            if r.is_success:
                result.inline_posted += 1
            else:
                # Don't lose it: demote to the summary note instead.
                result.errors.append(f"{f.file}:{f.line} -> {r.status_code} {r.text[:200]}")
                summary.append(f)

        body = format_summary(summary, result.inline_posted + result.skipped_existing, notes)
        if summary_id is not None:
            r = self.client.put(f"{self.base}/notes/{summary_id}", json={"body": body})
            result.summary_updated = r.is_success
        else:
            r = self.client.post(f"{self.base}/notes", json={"body": body})
        if r.is_success:
            result.summary_posted = 1
        else:
            result.errors.append(f"summary note -> {r.status_code} {r.text[:200]}")
        return result
