import pytest

from review_agent.agents.fake_agent import FakeAgent
from review_agent.agents.verifier import FakeVerifier
from review_agent.cli import has_blocking_findings
from review_agent.config import DEFAULT_EXCLUDE, LimitsSpec
from review_agent.pipeline import run_review, scope_hunks
from review_agent.providers.base import format_finding, format_summary
from review_agent.schemas import Category, Finding, Hunk, Severity


def _h(file, start=1, added=("x = 1",), ctx=()):
    return Hunk(file=file, start_line=start, end_line=start + len(added) + len(ctx), added_lines=list(added),
                added_line_numbers=list(range(start, start + len(added))), context_lines=list(ctx),
                context_line_numbers=list(range(start + len(added), start + len(added) + len(ctx))))


def test_default_excludes_catch_vendored_lock_and_generated_files():
    hunks = [_h("app/a.py"), _h("vendor/lib/x.py"), _h("web/node_modules/m/i.js"), _h("poetry.lock"),
             _h("proto/api_pb2.py"), _h("static/app.min.js"), _h("deep/sub/dir/thing.lock")]
    kept, notes = scope_hunks(hunks, DEFAULT_EXCLUDE, None)
    assert [h.file for h in kept] == ["app/a.py"]
    assert len(notes) == 1 and "excluded 6 file(s)" in notes[0]


def test_oversized_hunks_and_budget_are_cut_in_order_with_notes():
    limits = LimitsSpec(max_files=2, max_hunks=10, max_hunk_lines=5, max_prompt_chars=200)
    big = _h("a.py", added=tuple("line %d" % i for i in range(10)))
    small1 = _h("a.py", start=50, added=("a",))
    small2 = _h("b.py", added=("b",))
    third_file = _h("c.py", added=("c",))
    kept, notes = scope_hunks([big, small1, small2, third_file], None, limits)
    assert [(h.file, h.start_line) for h in kept] == [("a.py", 50), ("b.py", 1)]
    assert any("max_hunk_lines" in n for n in notes)
    assert any("beyond max_files 2" in n for n in notes)

    tight = LimitsSpec(max_prompt_chars=12)  # each one-char hunk costs 1 + 8 overhead; two exceed 12
    kept, notes = scope_hunks([small1, small2], None, tight)
    assert len(kept) == 1 and any("max_prompt_chars" in n for n in notes)


def test_no_exclude_no_limits_is_a_no_op():
    hunks = [_h("vendor/x.py"), _h("a.py")]
    assert scope_hunks(hunks, None, None) == (hunks, [])


@pytest.mark.asyncio
async def test_flag_mode_keeps_findings_downgraded(sample_diff, fake_repo):
    (fake_repo / "app" / "handlers.py").unlink()  # make FakeAgent speak
    agents = [FakeAgent("noise-a"), FakeAgent("noise-b", Category.SECURITY)]
    verifier = FakeVerifier(lambda f: f.category == Category.SECURITY)

    drop = await run_review(sample_diff, fake_repo, agents, verifiers=[verifier], verifier_mode="drop")
    flag = await run_review(sample_diff, fake_repo, agents, verifiers=[verifier], verifier_mode="flag")

    assert [f.category for f in drop.findings] == [Category.LOGIC] and len(drop.dropped) == 1 and drop.flagged == []
    assert len(flag.findings) == 2 and flag.dropped == []
    (kept, downgraded) = flag.findings
    assert not kept.flagged
    assert downgraded.flagged and downgraded.severity == Severity.INFO
    assert downgraded.dropped_reason == "fake-verifier: predicate"
    assert flag.flagged == [downgraded]


@pytest.mark.asyncio
async def test_pipeline_applies_exclude_and_reports_notes(sample_diff, fake_repo):
    report = await run_review(sample_diff, fake_repo, [FakeAgent("a")], exclude=["app/auth.py"])
    assert report.context.hunks == [] and report.findings == []
    assert report.notes and "excluded 1 file(s)" in report.notes[0]


def test_ci_gate_ignores_flagged_and_unstructured():
    crit = Finding(file="a.py", line=1, severity=Severity.CRITICAL, category=Category.SECURITY, message="m")
    assert has_blocking_findings([crit])
    assert not has_blocking_findings([crit.model_copy(update={"flagged": True, "severity": Severity.INFO})])
    assert not has_blocking_findings([crit.model_copy(update={"unstructured": True})])
    assert not has_blocking_findings([crit.model_copy(update={"severity": Severity.WARNING})])


def test_flagged_finding_and_notes_render_for_the_host():
    f = Finding(file="a.py", line=3, severity=Severity.INFO, category=Category.LOGIC, message="maybe None",
                dropped_reason="verifier: speculative", flagged=True)
    body = format_finding(f)
    assert "[INFO · flagged] logic" in body and "⚑ verifier downgraded this: verifier: speculative" in body
    summary = format_summary([], 2, notes=["excluded 3 file(s) by pattern: vendor/a.py"])
    assert "Not reviewed:" in summary and "vendor/a.py" in summary
