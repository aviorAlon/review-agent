from review_agent.orchestrator.dedup import aggregate, merge_findings, rank_and_cap
from review_agent.schemas import AgentResult, Category, Finding, Severity


def _f(line, agent, conf=0.5, file="a.py", cat=Category.LOGIC, sev=Severity.WARNING):
    return Finding(file=file, line=line, severity=sev, category=cat, message="m", confidence=conf, source_agent=agent)


def test_merge_same_file_category_within_window():
    merged = merge_findings([_f(10, "x", 0.5), _f(12, "y", 0.8)], line_window=3)
    assert len(merged) == 1
    survivor = merged[0]
    assert survivor.source_agent == "y"  # higher confidence wins
    assert survivor.merged_from == ["x"]
    assert survivor.confidence == 0.9  # agreement bonus


def test_merge_does_not_cross_category_or_file():
    merged = merge_findings([
        _f(10, "x", cat=Category.LOGIC),
        _f(10, "y", cat=Category.SECURITY),
        _f(10, "z", file="b.py"),
    ])
    assert len(merged) == 3


def test_merge_outside_window_kept_separate():
    assert len(merge_findings([_f(10, "x"), _f(20, "y")], line_window=3)) == 2


def test_confidence_bonus_is_capped():
    merged = merge_findings([_f(10, "x", 0.95), _f(10, "y", 0.9)])
    assert merged[0].confidence == 1.0


def test_unstructured_findings_pass_through_unmerged():
    raw = Finding(file="UNKNOWN", line=0, severity=Severity.INFO, category=Category.LOGIC, message="m",
                  confidence=0.2, source_agent="ext", unstructured=True)
    raw2 = raw.model_copy(update={"source_agent": "ext2"})
    merged = merge_findings([raw, raw2, _f(10, "x")])
    assert len(merged) == 3
    assert all(not m.merged_from for m in merged)


def test_rank_severity_then_confidence():
    out = rank_and_cap([
        _f(1, "a", 0.9, sev=Severity.INFO),
        _f(2, "b", 0.3, sev=Severity.CRITICAL),
        _f(3, "c", 0.7, sev=Severity.WARNING),
        _f(4, "d", 0.9, sev=Severity.WARNING),
    ])
    assert [f.source_agent for f in out] == ["b", "d", "c", "a"]


def test_cap_per_category_and_total():
    findings = [_f(i, f"a{i}", cat=Category.LOGIC) for i in range(0, 100, 10)]
    findings += [_f(i, f"s{i}", cat=Category.SECURITY) for i in range(0, 100, 10)]
    out = rank_and_cap(findings, max_per_category=3, max_total=5)
    assert len(out) == 5
    assert sum(1 for f in out if f.category == Category.LOGIC) <= 3


def test_aggregate_end_to_end_with_errored_agent():
    results = [
        AgentResult(agent_id="good", findings=[_f(5, "good", 0.6)]),
        AgentResult(agent_id="bad", findings=[], error="boom"),
        AgentResult(agent_id="also-good", findings=[_f(6, "also-good", 0.7)]),
    ]
    out = aggregate(results)
    assert len(out) == 1
    assert out[0].merged_from == ["good"]
