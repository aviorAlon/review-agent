import sys

import pytest

from review_agent.agents.external_cli_agent import ExternalCLIAgent
from review_agent.schemas import Category, ContextBundle, Hunk

HUNK = Hunk(file="a.py", start_line=1, end_line=2, added_lines=["x = 1"])

JSON_ECHO = (
    "import sys; sys.stdin.read(); "
    "print('[{\"file\":\"a.py\",\"line\":1,\"severity\":\"warning\",\"message\":\"bad\",\"confidence\":0.9},"
    "{\"file\":\"a.py\",\"line\":2,\"category\":\"logic\",\"severity\":\"info\",\"message\":\"meh\"}]')"
)
TEXT_ECHO = "import sys; sys.stdin.read(); print('looks sketchy to me')"
FAILING = "import sys; sys.exit(3)"


@pytest.mark.asyncio
async def test_structured_output_is_parsed():
    agent = ExternalCLIAgent("ext", [sys.executable, "-c", JSON_ECHO], category=Category.SECURITY)
    result = await agent.review(ContextBundle(hunks=[HUNK]))
    assert result.error is None
    assert len(result.findings) == 2
    f, g = result.findings
    assert (f.file, f.line, f.confidence, f.category) == ("a.py", 1, 0.9, Category.SECURITY)  # no category -> agent default
    assert g.category == Category.LOGIC                                                        # reported category wins
    assert f.source_agent == "ext" and not f.unstructured


@pytest.mark.asyncio
async def test_free_text_output_falls_back_to_one_low_confidence_finding():
    agent = ExternalCLIAgent("ext", [sys.executable, "-c", TEXT_ECHO])
    result = await agent.review(ContextBundle(hunks=[HUNK]))
    assert len(result.findings) == 1
    f = result.findings[0]
    assert f.confidence == 0.2 and f.unstructured is True
    assert "sketchy" in f.message


@pytest.mark.asyncio
async def test_nonzero_exit_becomes_error():
    agent = ExternalCLIAgent("ext", [sys.executable, "-c", FAILING])
    result = await agent.review(ContextBundle(hunks=[HUNK]))
    assert result.findings == []
    assert "exited 3" in result.error


@pytest.mark.asyncio
async def test_empty_context_skips_subprocess():
    agent = ExternalCLIAgent("ext", ["definitely-not-a-real-binary"])
    result = await agent.review(ContextBundle(hunks=[]))
    assert result.error is None and result.findings == []
