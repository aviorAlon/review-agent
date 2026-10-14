from types import SimpleNamespace

import pytest

from review_agent.agents.fake_agent import FakeAgent
from review_agent.agents.heuristic_agent import HeuristicAgent
from review_agent.agents.verifier import (
    FakeVerifier,
    LiteLLMVerifier,
    Verdict,
    VerifierResult,
    apply_verdicts,
)
from review_agent.bench.cases import load_cases
from review_agent.bench.runner import run_bench
from review_agent.config import AgentSpec, Config, build_agents, build_verifiers
from review_agent.pipeline import run_review
from review_agent.schemas import Category, ContextBundle, Finding, Hunk, Severity


def _f(line, msg="m", agent="a", cat=Category.LOGIC):
    return Finding(file="a.py", line=line, severity=Severity.WARNING, category=cat, message=msg, source_agent=agent)


# ---------------------------------------------------------------- fail-open

def test_only_explicit_drop_removes_anything():
    findings = [_f(1), _f(2), _f(3)]
    results = [VerifierResult("v", [Verdict(0, keep=True), Verdict(1, keep=False, reason="speculative")])]
    kept, dropped = apply_verdicts(findings, results)
    assert [f.line for f in kept] == [1, 3]          # index 2 omitted -> kept
    assert [f.line for f in dropped] == [2]
    assert dropped[0].dropped_reason == "v: speculative"


def test_verifier_error_drops_nothing():
    findings = [_f(1), _f(2)]
    results = [VerifierResult("v", [Verdict(0, keep=False)], error="boom")]
    kept, dropped = apply_verdicts(findings, results)
    assert len(kept) == 2 and dropped == []


def test_out_of_range_index_is_ignored():
    kept, dropped = apply_verdicts([_f(1)], [VerifierResult("v", [Verdict(7, keep=False)])])
    assert len(kept) == 1 and dropped == []


def test_first_verifier_to_drop_owns_the_reason():
    results = [VerifierResult("v1", [Verdict(0, keep=False, reason="r1")]),
               VerifierResult("v2", [Verdict(0, keep=False, reason="r2")])]
    _, dropped = apply_verdicts([_f(1)], results)
    assert dropped[0].dropped_reason == "v1: r1"


# -------------------------------------------------------------- LLM backend

HUNK = Hunk(file="a.py", start_line=1, end_line=3, added_lines=["x = 1", "y = eval(s)", "z = 3"], added_line_numbers=[1, 2, 3])


@pytest.mark.asyncio
async def test_llm_verifier_parses_verdicts_and_shows_candidates(monkeypatch):
    captured = {}

    async def fake_acompletion(**kwargs):
        captured.update(kwargs)
        content = '[{"index":0,"verdict":"keep","reason":"real"},{"index":1,"verdict":"DROP","reason":"nit"},{"index":"bad"},{"verdict":"drop"}]'
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=content))],
                               usage=SimpleNamespace(prompt_tokens=300, completion_tokens=40))

    import litellm
    monkeypatch.setattr(litellm, "acompletion", fake_acompletion)

    findings = [_f(2, "eval on input", "sec", Category.SECURITY), _f(3, "naming", "style", Category.STYLE)]
    v = LiteLLMVerifier("ver", model="openai/m", api_base="https://p/v1", api_key="k")
    result = await v.verify(findings, ContextBundle(hunks=[HUNK]))

    assert result.error is None
    assert (result.prompt_tokens, result.completion_tokens, result.tokens) == (300, 40, 340)
    assert [(d.index, d.keep) for d in result.verdicts] == [(0, True), (1, False)]   # malformed entries skipped
    user = captured["messages"][1]["content"]
    assert "+ 2: y = eval(s)" in user                                                # diff with line numbers
    assert "[0] a.py:2 [security/warning] eval on input" in user                      # numbered candidates
    assert "[1] a.py:3 [style/warning] naming" in user
    assert captured["drop_params"] is True and captured["api_base"] == "https://p/v1"


@pytest.mark.asyncio
async def test_llm_verifier_skips_call_when_nothing_to_verify(monkeypatch):
    import litellm

    async def boom(**kwargs):
        raise AssertionError("should not be called")

    monkeypatch.setattr(litellm, "acompletion", boom)
    result = await LiteLLMVerifier("ver").verify([], ContextBundle(hunks=[HUNK]))
    assert result.verdicts == [] and result.error is None


# ---------------------------------------------------------------- pipeline

@pytest.mark.asyncio
async def test_pipeline_applies_verifier_and_keeps_dropped_for_audit(sample_diff, fake_repo):
    agents = [FakeAgent("noise-a"), FakeAgent("noise-b", Category.SECURITY)]
    # fake_repo has a caller for check_permission, so FakeAgent is silent; use a repo without one
    (fake_repo / "app" / "handlers.py").unlink()
    verifier = FakeVerifier(lambda f: f.category == Category.SECURITY)

    without = await run_review(sample_diff, fake_repo, agents)
    with_v = await run_review(sample_diff, fake_repo, agents, verifiers=[verifier])

    assert len(without.findings) == 2 and without.dropped == []
    assert [f.category for f in with_v.findings] == [Category.LOGIC]
    assert [f.category for f in with_v.dropped] == [Category.SECURITY]
    assert with_v.dropped[0].dropped_reason == "fake-verifier: predicate"
    assert with_v.verifier_results[0].error is None


# ------------------------------------------------------------------- bench

def test_bench_arm_e_scores_what_the_verifier_killed():
    cases = load_cases()
    roster = [HeuristicAgent(role="baseline"), FakeAgent("noise", Category.LOGIC)]
    # drop exactly the fake agent's output: on clean cases that is pure FP removal,
    # on the two cases where it accidentally sits near a truth it is a dropped TP.
    verifier = FakeVerifier(lambda f: f.source_agent == "noise")
    d, e = run_bench(cases, roster, ["D", "E"], n_boot=20, verifiers=[verifier])

    d_tp = sum(s.tp for s in d.scores)
    assert d.fp_on_clean > 0 and e.fp_on_clean == 0
    assert e.findings == 0                            # the only specialist was the noise agent; all dropped
    assert e.dropped_tp + e.dropped_fp == d.findings  # nothing lost: every D finding is accounted for in E
    assert e.dropped_tp == d_tp                       # the accidental near-truth hits count against the verifier
    assert e.dropped_fp == d.findings - d_tp
    assert e.agent_errors == 0


def test_bench_arm_e_without_verifier_raises():
    cases = load_cases()[:2]
    with pytest.raises(ValueError, match="arm E .* needs a verifier"):
        run_bench(cases, [FakeAgent("noise")], ["E"], n_boot=5)


def test_bench_arm_f_runs_baseline_with_verifier():
    cases = load_cases()
    roster = [HeuristicAgent(role="baseline"), FakeAgent("noise", Category.LOGIC)]
    # drops nothing the heuristic says; F must therefore equal B exactly on kept findings
    verifier = FakeVerifier(lambda f: f.source_agent == "noise")
    b, f = run_bench(cases, roster, ["B", "F"], n_boot=20, verifiers=[verifier])
    assert f.arm.roles == frozenset({"baseline"}) and f.arm.use_verifier and f.arm.use_context
    assert (f.recall_strict, f.precision, f.findings) == (b.recall_strict, b.precision, b.findings)
    assert f.dropped_tp == 0 and f.dropped_fp == 0        # the noise agent never ran in F
    with pytest.raises(ValueError, match="arm F .* needs a verifier"):
        run_bench(cases[:2], roster, ["F"], n_boot=5)


# --------------------------------------------------------- pool replay eval

def test_verifier_eval_replays_pool_and_scores_drops(tmp_path):
    from review_agent.bench.runner import export_labels, load_pool, run_verifier_eval_sync

    cases = load_cases()
    # Build a real pool: heuristic baseline (all TP, 0 FP) + noise specialist (FPs on clean cases).
    roster = [HeuristicAgent(role="baseline"), FakeAgent("noise", Category.LOGIC)]
    results = run_bench(cases, roster, ["B", "D"], n_boot=10)
    labels, _ = export_labels(results, tmp_path / "labels.jsonl")
    pool = load_pool(labels)
    assert pool and all({"arm", "case_id", "is_tp", "source_agent", "category"} <= set(r) for r in pool)
    n_fp = sum(1 for r in pool if not r["is_tp"])
    n_tp = sum(1 for r in pool if r["is_tp"])
    assert n_fp > 0 and n_tp > 0

    assert all(r["pool"] == "labels.jsonl" for r in pool)

    # A verifier that drops exactly the noise agent's output.
    ev = run_verifier_eval_sync(pool, cases, FakeVerifier(lambda f: f.source_agent == "noise"))
    noise_rows = [r for r in pool if r["source_agent"] == "noise"]
    assert ev.candidates == len(pool) and ev.tp == n_tp and ev.fp == n_fp and ev.errors == 0
    assert ev.dropped_tp + ev.dropped_fp == len(noise_rows)
    assert ev.dropped_fp == sum(1 for r in noise_rows if not r["is_tp"])
    assert ev.dropped_tp == sum(1 for r in noise_rows if r["is_tp"])
    # Arm D groups hold only the noise agent, so every dropped TP there was the sole
    # coverer of its truth: those are real losses, not dedup.
    assert ev.truths_lost == ev.dropped_tp and ev.dup_removed == 0
    assert ev.truths_lost > 0
    assert ev.precision_after >= ev.precision_before
    assert all(set(d) >= {"pool", "case_id", "source_agent", "is_tp", "kind", "reason"} for d in ev.drops)
    assert {d["kind"] for d in ev.drops} == {"fp", "lost"}

    # A verifier that drops nothing changes nothing.
    inert = run_verifier_eval_sync(pool, cases, FakeVerifier(lambda f: False))
    assert (inert.dropped_tp, inert.dropped_fp, inert.truths_lost, inert.precision_after) == (0, 0, 0, inert.precision_before)


def test_verifier_eval_scores_dedup_per_truth_not_per_candidate(tmp_path):
    """Two candidates on one truth, one dropped: that is dedup (truth still covered), not a loss."""
    from review_agent.bench.runner import run_verifier_eval_sync

    cases = load_cases()
    case = next(c for c in cases if c.id == "sec-sql-injection")
    t = case.truths[0]
    row = lambda agent, msg: {"pool": "p", "arm": "D", "case_id": case.id, "file": t.file, "line": t.line,
                              "severity": "critical", "category": "security", "message": msg, "reasoning": "",
                              "source_agent": agent, "merged_from": [], "is_tp": True, "label_id": agent}
    pool = [row("a", "sql injection via f-string"), row("b", "f-string interpolation allows SQL injection")]

    dedup = run_verifier_eval_sync(pool, cases, FakeVerifier(lambda f: f.source_agent == "b"))
    assert (dedup.dropped_tp, dedup.dup_removed, dedup.truths_lost, dedup.truths_covered) == (1, 1, 0, 1)
    assert dedup.drops[0]["kind"] == "dup"

    kill = run_verifier_eval_sync(pool, cases, FakeVerifier(lambda f: True))
    assert (kill.dropped_tp, kill.dup_removed, kill.truths_lost) == (2, 0, 1)
    assert {d["kind"] for d in kill.drops} == {"lost"}


def test_verifier_eval_never_mixes_pools_into_one_group(tmp_path):
    """The same finding from two runs must land in two groups, never be 'duplicates' of each other."""
    from review_agent.bench.runner import run_verifier_eval_sync

    cases = load_cases()
    case = next(c for c in cases if c.id == "sec-sql-injection")
    t = case.truths[0]
    base = {"arm": "B", "case_id": case.id, "file": t.file, "line": t.line, "severity": "critical",
            "category": "security", "message": "same text", "reasoning": "", "source_agent": "generalist",
            "merged_from": [], "is_tp": True, "label_id": "x"}
    pool = [{**base, "pool": "run2.jsonl"}, {**base, "pool": "run3.jsonl"}]

    seen: list[int] = []

    class Counting(FakeVerifier):
        async def _run(self, findings, context):
            seen.append(len(findings))
            return await super()._run(findings, context)

    ev = run_verifier_eval_sync(pool, cases, Counting(lambda f: False))
    assert ev.groups == 2 and seen == [1, 1]


# ------------------------------------------------------------------- config

def test_config_builds_verifiers_separately_from_agents(monkeypatch):
    monkeypatch.setenv("TOK", "t")
    cfg = Config(agents=[
        AgentSpec(id="sec", type="llm", focus="s"),
        AgentSpec(id="ver", type="llm", role="verifier", model="openai/x", api_base="https://p/v1", api_key_env="TOK"),
    ])
    agents = build_agents(cfg)
    verifiers = build_verifiers(cfg)
    assert [a.agent_id for a in agents] == ["sec"]          # verifier spec not built as a reviewer
    assert [v.verifier_id for v in verifiers] == ["ver"]
    assert (verifiers[0].api_base, verifiers[0].api_key) == ("https://p/v1", "t")


def test_non_llm_verifier_is_rejected():
    with pytest.raises(ValueError, match="only type 'llm'"):
        build_verifiers(Config(agents=[AgentSpec(id="v", type="heuristic", role="verifier")]))
