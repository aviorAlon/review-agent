from types import SimpleNamespace

import pytest

from review_agent.agents.litellm_agent import LiteLLMAgent
from review_agent.config import AgentSpec, Config, build_agents
from review_agent.schemas import Category, ContextBundle, Hunk

HUNK = Hunk(file="a.py", start_line=1, end_line=1, added_lines=["x = eval(s)"], added_line_numbers=[1])


def _spec(**kw):
    return AgentSpec(id="sec", type="llm", category=Category.SECURITY, focus="security", **kw)


def test_api_key_env_is_resolved_from_environment(monkeypatch):
    monkeypatch.setenv("MY_PROXY_TOKEN", "tok-123")
    (agent,) = build_agents(Config(agents=[_spec(api_base="https://proxy/v1", api_key_env="MY_PROXY_TOKEN")]))
    assert isinstance(agent, LiteLLMAgent)
    assert (agent.api_base, agent.api_key) == ("https://proxy/v1", "tok-123")


def test_missing_api_key_env_fails_fast_with_var_name(monkeypatch):
    monkeypatch.delenv("MY_PROXY_TOKEN", raising=False)
    with pytest.raises(ValueError, match="agent 'sec': env var MY_PROXY_TOKEN is not set"):
        build_agents(Config(agents=[_spec(api_key_env="MY_PROXY_TOKEN")]))


def test_no_api_fields_means_litellm_defaults():
    (agent,) = build_agents(Config(agents=[_spec()]))
    assert agent.api_base is None and agent.api_key is None


@pytest.mark.asyncio
async def test_agent_passes_proxy_params_and_parses_findings(monkeypatch):
    captured = {}

    async def fake_acompletion(**kwargs):
        captured.update(kwargs)
        content = ('[{"file":"a.py","line":1,"category":"logic","severity":"critical","message":"eval","reasoning":"r","confidence":0.9},'
                   ' {"file":"a.py","line":1,"severity":"info","message":"no category given"},'
                   ' {"file":"a.py","line":1,"category":"not-a-category","severity":"info","message":"bad category"}]')
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=content))],
                               usage=SimpleNamespace(prompt_tokens=120, completion_tokens=45))

    import litellm
    monkeypatch.setattr(litellm, "acompletion", fake_acompletion)

    agent = LiteLLMAgent("sec", focus="security", category=Category.SECURITY,
                         model="openai/some-model", api_base="https://proxy/v1", api_key="tok")
    result = await agent.review(ContextBundle(hunks=[HUNK]))

    assert captured["model"] == "openai/some-model"
    assert captured["api_base"] == "https://proxy/v1" and captured["api_key"] == "tok"
    assert captured["drop_params"] is True and captured["temperature"] == 0
    assert "+ 1: x = eval(s)" in captured["messages"][1]["content"]  # real line numbers reach the model
    assert '"category":' in captured["messages"][0]["content"]        # the model is asked to classify
    assert result.error is None
    # category comes from the model per finding; the agent's own is only the fallback
    assert [f.category for f in result.findings] == [Category.LOGIC, Category.SECURITY, Category.SECURITY]
    assert (result.findings[0].file, result.findings[0].line, result.findings[0].confidence) == ("a.py", 1, 0.9)
    assert (result.prompt_tokens, result.completion_tokens, result.tokens) == (120, 45, 165)


def test_usage_is_reset_between_calls_and_zero_when_absent(monkeypatch):
    import litellm

    async def no_usage(**kwargs):
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content="[]"))])

    monkeypatch.setattr(litellm, "acompletion", no_usage)
    agent = LiteLLMAgent("sec", focus="x")
    agent.last_usage = (999, 999)  # stale value from an imagined earlier call
    import asyncio
    result = asyncio.run(agent.review(ContextBundle(hunks=[HUNK])))
    assert (result.prompt_tokens, result.completion_tokens) == (0, 0)


def test_default_config_is_generalist_plus_verifier_with_specialists_opt_in():
    from review_agent.config import DEFAULT_CONFIG, build_verifiers

    roles = [(a.id, a.role) for a in DEFAULT_CONFIG.agents if a.enabled]
    assert roles == [("generalist", "baseline"), ("verifier", "verifier")]
    assert not any(a.role == "specialist" and a.enabled for a in DEFAULT_CONFIG.agents)
    agents = build_agents(DEFAULT_CONFIG)
    assert [a.agent_id for a in agents] == ["generalist"] and agents[0].role == "baseline"
    assert [v.verifier_id for v in build_verifiers(DEFAULT_CONFIG)] == ["verifier"]


def test_example_yaml_matches_default_shape():
    from pathlib import Path
    from review_agent.config import load_config

    cfg = load_config(Path(__file__).parent.parent / ".review-agent.example.yaml")
    enabled = {a.id: a.role for a in cfg.agents if a.enabled}
    assert enabled == {"generalist": "baseline", "verifier": "verifier"}
    disabled_specialists = [a.id for a in cfg.agents if not a.enabled and a.role == "specialist"]
    assert {"security", "logic"} <= set(disabled_specialists)


def test_prompt_interleaves_context_and_added_lines_in_target_order():
    from review_agent.agents.litellm_agent import _render_user_prompt
    from review_agent.bench.cases import load_cases
    from review_agent.bench.runner import make_diff
    from review_agent.diff import parse_diff

    case = next(c for c in load_cases() if c.id == "logic-off-by-one")
    prompt = _render_user_prompt(ContextBundle(hunks=parse_diff(make_diff(case))))
    lines = prompt.splitlines()
    i_removed = next(i for i, l in enumerate(lines) if l.startswith("-") and "range(len(prices))" in l)
    i_added = next(i for i, l in enumerate(lines) if l.startswith("+ 3:"))
    i_body = next(i for i, l in enumerate(lines) if l.startswith("  4:"))
    assert "range(len(prices) + 1)" in lines[i_added]
    assert "s += prices[i]" in lines[i_body]           # the unchanged loop body is visible...
    assert i_body == i_added + 1                        # ...directly under the changed bound
    assert i_removed == i_added - 1                     # and the old bound sits where it was, not at the end


def test_prompt_renders_caller_snippets_and_callee_definitions():
    from review_agent.agents.litellm_agent import _render_user_prompt
    from review_agent.schemas import SymbolRef

    sym = SymbolRef(name="store", file="app/upload.py", line=8, signature="def store(filename, data)",
                    callers=["app/web.py:12"], callees=["safe_join"],
                    caller_snippets=["app/web.py:12 in `def upload_view(request)`\n> 12:     store(request.files['f'].name, data)"],
                    callee_defs=["app/util.py:4\n  4: def safe_join(base, name):\n  5:     return os.path.join(base, name)"])
    prompt = _render_user_prompt(ContextBundle(hunks=[HUNK], symbols=[sym]))
    assert "### Call site app/web.py:12 in `def upload_view(request)`" in prompt
    assert "request.files['f'].name" in prompt
    assert "### Definition of a called function, app/util.py:4" in prompt
    assert "return os.path.join(base, name)" in prompt


def test_prompt_keeps_removed_lines_at_their_original_position():
    """A deleted guard must render ABOVE the return it protected, or it reads as dead code."""
    from review_agent.agents.litellm_agent import _render_user_prompt
    from review_agent.bench.cases import load_cases
    from review_agent.bench.runner import make_diff
    from review_agent.diff import parse_diff

    for case_id, guard, protected in (
        ("logic-none-deref", "if user is None:", "return user.email"),
        ("sec-auth-guard-removed", "if not user.is_admin:", "db.delete("),
    ):
        case = next(c for c in load_cases() if c.id == case_id)
        hunks = parse_diff(make_diff(case))
        assert hunks[0].removed_anchors and len(hunks[0].removed_anchors) == len(hunks[0].removed_lines)
        lines = _render_user_prompt(ContextBundle(hunks=hunks)).splitlines()
        i_guard = next(i for i, l in enumerate(lines) if l.startswith("-") and guard in l)
        i_protected = next(i for i, l in enumerate(lines) if not l.startswith("-") and protected in l)
        assert i_guard < i_protected, f"{case_id}: removed guard rendered below the line it protected"


def test_role_propagates_from_spec_and_defaults_to_specialist(monkeypatch):
    (base,) = build_agents(Config(agents=[_spec(role="baseline")]))
    (spec,) = build_agents(Config(agents=[_spec()]))
    assert (base.role, spec.role) == ("baseline", "specialist")
    with pytest.raises(Exception):
        AgentSpec(id="x", type="llm", role="captain")


@pytest.mark.asyncio
async def test_agent_omits_api_kwargs_when_unset(monkeypatch):
    captured = {}

    async def fake_acompletion(**kwargs):
        captured.update(kwargs)
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content="no issues"))])

    import litellm
    monkeypatch.setattr(litellm, "acompletion", fake_acompletion)

    result = await LiteLLMAgent("sec", focus="x", temperature=None).review(ContextBundle(hunks=[HUNK]))
    assert "api_base" not in captured and "api_key" not in captured
    assert "temperature" not in captured          # None means: don't send it at all
    assert captured["drop_params"] is True        # ...but drop_params is always on
    assert result.findings == [] and result.error is None  # free text with no array -> nothing, not a crash


def test_temperature_null_in_yaml_is_omitted(monkeypatch):
    (agent,) = build_agents(Config(agents=[_spec(temperature=None)]))
    assert agent.temperature is None
    (default,) = build_agents(Config(agents=[_spec()]))
    assert default.temperature == 0
