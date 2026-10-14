import pytest

from review_agent.agents.heuristic_agent import HeuristicAgent
from review_agent.schemas import Category, ContextBundle, Hunk


async def _scan(added: list[tuple[int, str]], removed: list[str] | None = None):
    hunk = Hunk(file="f.py", start_line=added[0][0] if added else 1, end_line=added[-1][0] if added else 1,
                added_lines=[t for _, t in added], added_line_numbers=[n for n, _ in added], removed_lines=removed or [])
    return (await HeuristicAgent().review(ContextBundle(hunks=[hunk]))).findings


@pytest.mark.asyncio
async def test_line_rules_report_exact_line():
    findings = await _scan([(10, "x = 1"), (11, "subprocess.run(cmd, shell=True)"), (12, "y = 2")])
    assert [(f.line, f.category) for f in findings] == [(11, Category.SECURITY)]


@pytest.mark.asyncio
async def test_except_pass_requires_consecutive_lines():
    hit = await _scan([(4, "    except Exception:"), (5, "        pass")])
    assert [f.line for f in hit] == [4]
    miss = await _scan([(4, "    except Exception:"), (9, "        pass")])
    assert miss == []


@pytest.mark.asyncio
async def test_n_plus_one_only_inside_loop_span():
    inside = await _scan([(3, "    for oid in ids:"), (4, '        row = db.query("x", (oid,))')])
    assert [(f.line, f.category) for f in inside] == [(4, Category.PERFORMANCE)]
    outside = await _scan([(3, "    for oid in ids:"), (30, '        row = db.query("x", (oid,))')])
    assert outside == []


@pytest.mark.asyncio
async def test_lambda_must_reference_loop_var():
    hit = await _scan([(3, "    for i in ids:"), (4, "        fns.append(lambda: process(i))")])
    assert [f.line for f in hit] == [4]
    miss = await _scan([(3, "    for i in ids:"), (4, "        fns.append(lambda: process(0))")])
    assert miss == []


@pytest.mark.asyncio
async def test_removed_guard_reports_at_first_added_line():
    findings = await _scan([(7, '    return {"ok": True}')], removed=["    if not user.is_admin:", "        raise PermissionError"])
    assert [(f.line, f.category) for f in findings] == [(7, Category.SECURITY)]
    kept = await _scan([(7, "    if not user.is_admin: raise PermissionError")], removed=["    if not user.is_admin:"])
    assert kept == []


@pytest.mark.asyncio
async def test_clean_lines_produce_nothing():
    assert await _scan([(1, 'log.info("starting job %s", job.id)'), (2, "def calc(width, height):"), (3, "    return os.path.join('/home', user)")]) == []
