import os
import sys

sys.path.append(os.path.join(os.path.dirname(__file__), "..", "services", "agent-service"))
import json

import pytest

import grounding
from langchain_core.messages import AIMessage, SystemMessage, ToolMessage
from langgraph.checkpoint.base import empty_checkpoint
from langgraph.checkpoint.memory import InMemorySaver


@pytest.mark.unit
@pytest.mark.asyncio
async def test_check_grounded_flags_unsupported_percentage():
    tool_results = [{"tool": "get_party_distribution",
        "output": {"wahlperiode": 20, "percentages": {"SPD": 25.5, "CDU/CSU": 28.9}}}]
    unfaithful_answer = "The Greens hold an absolute majority with 90% of all seats."
    assert await grounding.check_grounded(unfaithful_answer, tool_results) < 0.5

@pytest.mark.unit
@pytest.mark.asyncio
async def test_check_grounded_passes_faithful_percentages():
    tool_results = [{"tool": "get_party_distribution",
        "output": {"wahlperiode": 20, "percentages": {"SPD": 25.5, "CDU/CSU": 28.9}}}]
    faithful_answer = "In Wahlperiode 20, the SPD holds 25.5% and CDU/CSU holds 28.9%."
    assert await grounding.check_grounded(faithful_answer, tool_results) > 0.8

@pytest.mark.unit
@pytest.mark.asyncio
async def test_check_grounded_flags_missing_data_notes_caveat():
    tool_results = [{"tool": "get_party_distribution", "output": {
        "wahlperiode": 20, "percentages": {"SPD": 25.5}, "unclassified_count": 6,
        "data_notes": "6 of 736 persons had no resolvable party affiliation...",
    }}]
    # No llm passed -- numeric half still runs synchronously/lexically,
    # and the missing "6"/"736" alone is enough to fail this without
    # needing a semantic judge call.
    assert await grounding.check_grounded("In Wahlperiode 20, the SPD holds 25.5%.", tool_results) < 1.0

@pytest.mark.unit
@pytest.mark.asyncio
async def test_check_grounded_person_info_matches_returned_fraktion():
    tool_results = [{"tool": "get_person_info", "output": {
        "person": {"vorname": "Friedrich", "nachname": "Merz", "fraktion": "CDU/CSU"}, "matches": [],
    }}]
    assert await grounding.check_grounded("Friedrich Merz is a member of the CDU/CSU fraktion.", tool_results) == 1.0

@pytest.mark.unit
@pytest.mark.asyncio
async def test_check_grounded_no_tool_results_is_trivially_grounded():
    assert await grounding.check_grounded("I can't help with that.", []) == 1.0


class _FakeLLM:
    def with_structured_output(self, _schema):
        return self

    async def ainvoke(self, _messages):
        from agent_graph import JudgeVerdict
        return JudgeVerdict(score=0.9, rationale="ok")


class _FakeAgentSystem:
    def __init__(self, llm, judge_llm=None):
        self.llm = llm
        self.judge_llm = judge_llm if judge_llm is not None else llm


class _FakeAsyncConnectionPool:
    def __init__(self, *a, **k):
        pass

    async def open(self):
        pass

    async def close(self):
        pass

class _FakeAsyncPostgresSaver(InMemorySaver):
    """Test double for AsyncPostgresSaver. Subclasses LangGraph's own
    InMemorySaver instead of hand-constructing Checkpoint dicts, so this
    file never encodes assumptions about internal checkpoint schema
    (id format, required metadata keys, version bookkeeping) that
    LangGraph doesn't document or guarantee to keep stable -- any such
    internal changes are absorbed by LangGraph's own InMemorySaver code
    on upgrade, instead of surfacing as a new cryptic error here.

    _pending_seed/_pending_thread_id are class-level staging values:
    create_agent_graph constructs this class itself (via the
    monkeypatched reference), so a test can't get a handle to the
    actual instance beforehand to seed it directly. setup() is always
    awaited by create_agent_graph right after construction, so it's the
    reliable place to consume any pending seed via the real aput() API.
    """
    _pending_seed: list[dict] | None = None
    _pending_thread_id: str | None = None

    def __init__(self, *args, **kwargs):
        super().__init__()

    async def setup(self):
        if _FakeAsyncPostgresSaver._pending_seed is None:
            return
        config = {"configurable": {"thread_id": _FakeAsyncPostgresSaver._pending_thread_id,
                                    "checkpoint_ns": ""}}
        checkpoint = empty_checkpoint()
        # Never invent a version value -- InMemorySaver mints its own
        # string-shaped versions ("{counter:032}.{random:016}"), and a
        # hand-picked int here would later get compared (">") against a
        # real string version elsewhere in the same run, raising a
        # TypeError. Always mint through the saver's own scheme.
        version = self.get_next_version(None, None)
        checkpoint["channel_values"]["tool_results"] = _FakeAsyncPostgresSaver._pending_seed
        checkpoint["channel_versions"]["tool_results"] = version
        metadata = {"source": "update", "step": -1, "writes": {}, "parents": {}}
        await self.aput(config, checkpoint, metadata, {"tool_results": version})

@pytest.mark.unit
@pytest.mark.asyncio
async def test_graph_compiles_without_hitl_or_nli(monkeypatch):
    from agent_graph import create_agent_graph

    monkeypatch.setattr("agent_graph.AsyncConnectionPool", _FakeAsyncConnectionPool)
    monkeypatch.setattr("agent_graph.AsyncPostgresSaver", _FakeAsyncPostgresSaver)

    graph = await create_agent_graph(_FakeAgentSystem(_FakeLLM()), config={
        "enable_reflection": False, "enable_hallucination_guard": True,
        "max_reflection_iterations": 3, "faithfulness_threshold": 0.8,
        "completeness_threshold": 0.7,
    })
    assert graph is not None




class _StructuredOutputStub:
    def __init__(self, schema, intent, raw_date_expression):
        self._schema = schema
        self._intent = intent
        self._raw_date_expression = raw_date_expression

    async def ainvoke(self, _messages):
        from agent_graph import SupervisorExtraction, JudgeVerdict
        if self._schema is SupervisorExtraction:
            return SupervisorExtraction(intent=self._intent, raw_date_expression=self._raw_date_expression)
        if self._schema is JudgeVerdict:
            return JudgeVerdict(score=1.0, rationale="no tool results, trivially complete")
        raise AssertionError(f"unexpected structured-output schema: {self._schema}")


class _FakeLLMFull:
    def __init__(self, intent, raw_date_expression=None, synthesis_answer="ok"):
        self._intent = intent
        self._raw_date_expression = raw_date_expression
        self._synthesis_answer = synthesis_answer

    def with_structured_output(self, schema):
        return _StructuredOutputStub(schema, self._intent, self._raw_date_expression)

    async def ainvoke(self, _messages):
        return AIMessage(content=self._synthesis_answer)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_conversation_meta_intent_bypasses_tools_and_answers_directly(monkeypatch):
    from agent_graph import create_agent_graph, run_agent_query

    monkeypatch.setattr("agent_graph.AsyncConnectionPool", _FakeAsyncConnectionPool)
    monkeypatch.setattr("agent_graph.AsyncPostgresSaver", _FakeAsyncPostgresSaver)

    fake_llm = _FakeLLMFull(intent="conversation_meta",
                             synthesis_answer="Your first question was about Wahlperiode 20.")
    graph = await create_agent_graph(_FakeAgentSystem(fake_llm), config={
        "enable_reflection": True, "enable_hallucination_guard": True,
        "max_reflection_iterations": 3, "faithfulness_threshold": 0.8,
        "completeness_threshold": 0.7,
    })

    result = await run_agent_query(graph, "what was my first question?", thread_id="t-meta")
    assert result["messages"][-1].content == "Your first question was about Wahlperiode 20."
    assert result["tool_results"] == []
    assert result["faithfulness_score"] == 1.0


@pytest.mark.unit
@pytest.mark.asyncio
async def test_out_of_scope_intent_still_returns_canned_refusal(monkeypatch):
    from agent_graph import create_agent_graph, run_agent_query

    monkeypatch.setattr("agent_graph.AsyncConnectionPool", _FakeAsyncConnectionPool)
    monkeypatch.setattr("agent_graph.AsyncPostgresSaver", _FakeAsyncPostgresSaver)

    fake_llm = _FakeLLMFull(intent="out_of_scope")
    graph = await create_agent_graph(_FakeAgentSystem(fake_llm), config={
        "enable_reflection": True, "enable_hallucination_guard": True,
        "max_reflection_iterations": 3, "faithfulness_threshold": 0.8,
        "completeness_threshold": 0.7,
    })

    result = await run_agent_query(graph, "what's the weather today?", thread_id="t-oos")
    assert "German parliamentary data" in result["messages"][-1].content





class _FakeCaveatJudgeLLM:
    def __init__(self, addressed: bool):
        self._addressed = addressed

    def with_structured_output(self, _schema):
        return self

    async def ainvoke(self, _messages):
        from grounding import CaveatVerdict
        return CaveatVerdict(addressed=self._addressed, rationale="stub verdict")

@pytest.mark.unit
@pytest.mark.asyncio
async def test_check_grounded_credits_date_range_caveat_separately():
    tool_results = [{"tool": "get_party_distribution", "output": {
        "wahlperiode": 21, "percentages": {"SPD": 19.08}, "unclassified_count": 0,
        "data_notes": "Note: date_range was supplied but the underlying DIP API has no "
                      "role-tenure-scoped date filter, so this result reflects the full "
                      "Wahlperiode, not the specific date range.",
    }}]
    answer = ("In Wahlperiode 21, the SPD holds 19.08%. Note that this reflects the full "
              "Wahlperiode, not the specific date range you asked about.")
    score = await grounding.check_grounded(answer, tool_results, llm=_FakeCaveatJudgeLLM(addressed=True))
    assert score == 1.0

@pytest.mark.unit
@pytest.mark.asyncio
async def test_check_grounded_flags_missing_date_range_caveat():
    tool_results = [{"tool": "get_party_distribution", "output": {
        "wahlperiode": 21, "percentages": {"SPD": 19.08}, "unclassified_count": 0,
        "data_notes": "Note: date_range was supplied but the underlying DIP API has no "
                      "role-tenure-scoped date filter, so this result reflects the full "
                      "Wahlperiode, not the specific date range.",
    }}]
    score = await grounding.check_grounded(
        "In Wahlperiode 21, the SPD holds 19.08%.", tool_results, llm=_FakeCaveatJudgeLLM(addressed=False))
    assert score < 1.0




class _FakeMCPTool:
    def __init__(self, name: str, handler):
        self.name = name
        self._handler = handler

    async def ainvoke(self, args: dict):
        return self._handler(args)


class _BoundToolsStub:
    def __init__(self, parent: "_CapturingToolCallLLM"):
        self._parent = parent

    async def ainvoke(self, messages):
        self._parent._bind_tools_calls += 1
        pass_n = self._parent._bind_tools_calls
        self._parent.bind_tools_call_history.append(list(messages))
        calls = self._parent._tool_calls_by_pass.get(pass_n, [])
        return AIMessage(content="", tool_calls=[
            {"name": c["name"], "args": c["args"], "id": f"call_p{pass_n}_{i}", "type": "tool_call"}
            for i, c in enumerate(calls)
        ])


class _StructuredOutputCapture:
    def __init__(self, parent: "_CapturingToolCallLLM", schema):
        self.parent = parent
        self.schema = schema

    async def ainvoke(self, messages):
        from agent_graph import SupervisorExtraction, JudgeVerdict, CompletenessVerdict, CacheSufficiencyVerdict
        if self.schema is SupervisorExtraction:
            return SupervisorExtraction(intent=self.parent._intent, raw_date_expression=None)
        if self.schema is JudgeVerdict:
            self.parent._judge_calls += 1
            score = self.parent._completeness_by_pass.get(self.parent._judge_calls, 1.0)
            return JudgeVerdict(score=score, rationale=f"pass {self.parent._judge_calls}")
        if self.schema is CompletenessVerdict:
            self.parent._judge_calls += 1
            score = self.parent._completeness_by_pass.get(self.parent._judge_calls, 1.0)
            return CompletenessVerdict(score=score, rationale=f"pass {self.parent._judge_calls}", missing_calls=[])
        if self.schema is CacheSufficiencyVerdict:
            return CacheSufficiencyVerdict(score=0.0, rationale="no relevant prior evidence", relevant_indices=[])
        raise AssertionError(f"unexpected structured-output schema: {self.schema}")

class _CapturingToolCallLLM:
    def __init__(self, tool_calls_by_pass: dict[int, list[dict]], completeness_by_pass: dict[int, float],
                 intent="mixed", synthesis_answer="ok"):
        self._tool_calls_by_pass = tool_calls_by_pass
        self._completeness_by_pass = completeness_by_pass
        self._intent = intent
        self._synthesis_answer = synthesis_answer
        self.bind_tools_call_history: list[list] = []
        self._bind_tools_calls = 0
        self._judge_calls = 0

    def bind_tools(self, tools, tool_choice=None):
        return _BoundToolsStub(self)

    def with_structured_output(self, schema):
        return _StructuredOutputCapture(self, schema)

    async def ainvoke(self, _messages):
        return AIMessage(content=self._synthesis_answer)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_reflection_retry_sees_prior_tool_result_in_transcript(monkeypatch):
    from agent_graph import create_agent_graph, run_agent_query

    monkeypatch.setattr("agent_graph.AsyncConnectionPool", _FakeAsyncConnectionPool)
    monkeypatch.setattr("agent_graph.AsyncPostgresSaver", _FakeAsyncPostgresSaver)
    monkeypatch.setattr("agent_graph.resolve_current_wahlperiode", lambda: 21)

    person_output = {
        "person": {"vorname": "Angela", "nachname": "Merkel", "fraktion": None,
                    "person_roles": [{"wahlperiode_nummer": [18, 19], "fraktion": "CDU/CSU"}]},
        "matches": [], "resolved_fraktion": "CDU/CSU",
    }
    party_output = {
        "wahlperiode": 19, "percentages": {"CDU/CSU": 34.1, "SPD": 25.7},
        "counts": {"CDU/CSU": 245, "SPD": 185}, "total_persons": 630,
        "unclassified_count": 0, "data_notes": None,
    }

    fake_tools = [
        _FakeMCPTool("get_person_info", lambda args: person_output),
        _FakeMCPTool("get_party_distribution", lambda args: party_output),
        _FakeMCPTool("get_persons_by_role", lambda args: {"persons": [], "data_notes": None}),
    ]

    async def _fake_get_mcp_tools():
        return fake_tools

    monkeypatch.setattr("agent_graph.get_mcp_tools", _fake_get_mcp_tools)

    fake_llm = _CapturingToolCallLLM(
        tool_calls_by_pass={
            1: [{"name": "get_person_info", "args": {"name": "Angela Merkel"}}],
            2: [{"name": "get_party_distribution", "args": {"wahlperiode": 19}}],
        },
        completeness_by_pass={1: 0.3, 2: 1.0},
        intent="mixed",
        synthesis_answer="Angela Merkel (CDU/CSU); in WP19 CDU/CSU held 34.1%.",
    )

    graph = await create_agent_graph(_FakeAgentSystem(fake_llm), config={
        "enable_reflection": True, "enable_hallucination_guard": False,
        "max_reflection_iterations": 3, "faithfulness_threshold": 0.8,
        "completeness_threshold": 0.7,
    })

    result = await run_agent_query(
        graph, "Who is Angela Merkel and what was her party's share in WP19?", thread_id="t-retry")

    assert fake_llm._bind_tools_calls == 2

    pass_1_messages = fake_llm.bind_tools_call_history[0]
    assert not any(isinstance(m, ToolMessage) for m in pass_1_messages)

    pass_2_messages = fake_llm.bind_tools_call_history[1]
    prior_result_summaries = [m for m in pass_2_messages
                               if isinstance(m, SystemMessage)
                               and "You have already retrieved" in m.content]
    assert len(prior_result_summaries) == 1
    assert json.dumps(person_output) in prior_result_summaries[0].content

    assert result["tool_results"] == [
        {"tool": "get_person_info", "args": {"name": "Angela Merkel"}, "output": person_output},
        {"tool": "get_party_distribution", "args": {"wahlperiode": 19}, "output": party_output},
    ]
    assert result["messages"][-1].content == fake_llm._synthesis_answer


@pytest.mark.unit
@pytest.mark.asyncio
async def test_reflection_loop_terminates_at_max_iterations_even_if_stuck(monkeypatch):
    from agent_graph import create_agent_graph, run_agent_query

    monkeypatch.setattr("agent_graph.AsyncConnectionPool", _FakeAsyncConnectionPool)
    monkeypatch.setattr("agent_graph.AsyncPostgresSaver", _FakeAsyncPostgresSaver)
    monkeypatch.setattr("agent_graph.resolve_current_wahlperiode", lambda: 21)

    person_output = {"person": {"vorname": "Angela", "nachname": "Merkel", "fraktion": "CDU/CSU"},
                      "matches": [], "resolved_fraktion": "CDU/CSU"}
    fake_tools = [
        _FakeMCPTool("get_person_info", lambda args: person_output),
        _FakeMCPTool("get_party_distribution", lambda args: {}),
        _FakeMCPTool("get_persons_by_role", lambda args: {"persons": [], "data_notes": None}),
    ]

    async def _fake_get_mcp_tools():
        return fake_tools

    monkeypatch.setattr("agent_graph.get_mcp_tools", _fake_get_mcp_tools)

    stuck_calls = {n: [{"name": "get_person_info", "args": {"name": "Angela Merkel"}}] for n in range(1, 5)}
    stuck_scores = {n: 0.2 for n in range(1, 5)}
    fake_llm = _CapturingToolCallLLM(stuck_calls, stuck_scores, intent="person_lookup",
                                      synthesis_answer="best guess given incomplete evidence")

    graph = await create_agent_graph(_FakeAgentSystem(fake_llm), config={
        "enable_reflection": True, "enable_hallucination_guard": False,
        "max_reflection_iterations": 3, "faithfulness_threshold": 0.8,
        "completeness_threshold": 0.7,
    })

    result = await run_agent_query(graph, "who is Angela Merkel?", thread_id="t-stuck")

    assert fake_llm._bind_tools_calls == 3
    assert result["reflection_iterations"] == 3
    assert result["messages"][-1].content == "best guess given incomplete evidence"




@pytest.mark.unit
def test_parse_mcp_tool_result_multiple_blocks_becomes_list():
    from agent_graph import _parse_mcp_tool_result
    blocks = [
        {"type": "text", "text": '{"vorname": "Friedrich", "nachname": "Merz"}'},
        {"type": "text", "text": '{"vorname": "Friedrich", "nachname": "Maier"}'},
    ]
    assert _parse_mcp_tool_result(blocks) == [
        {"vorname": "Friedrich", "nachname": "Merz"},
        {"vorname": "Friedrich", "nachname": "Maier"},
    ]



class _DecliningToolLLM:
    def __init__(self):
        self._bind_calls = 0

    def bind_tools(self, tools, tool_choice=None):
        self._bind_calls += 1
        return self

    async def ainvoke(self, _messages):
        return AIMessage(content="I wasn't able to locate anyone by that name.")

    def with_structured_output(self, schema):
        return _StructuredOutputStub(schema, "person_lookup", None)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_model_declining_tools_gets_template_not_free_text(monkeypatch):
    from agent_graph import RETRIEVAL_FAILURE_ANSWER, create_agent_graph, run_agent_query

    monkeypatch.setattr("agent_graph.AsyncConnectionPool", _FakeAsyncConnectionPool)
    monkeypatch.setattr("agent_graph.AsyncPostgresSaver", _FakeAsyncPostgresSaver)
    monkeypatch.setattr("agent_graph.resolve_current_wahlperiode", lambda: 21)

    async def _fake_get_mcp_tools():
        return []

    monkeypatch.setattr("agent_graph.get_mcp_tools", _fake_get_mcp_tools)

    fake_llm = _DecliningToolLLM()
    graph = await create_agent_graph(_FakeAgentSystem(fake_llm), config={
        "enable_reflection": True, "enable_hallucination_guard": True,
        "max_reflection_iterations": 3, "faithfulness_threshold": 0.8,
        "completeness_threshold": 0.7,
    })

    result = await run_agent_query(graph, "Who is Fredrch Mez?", thread_id="t-decline")
    assert result["messages"][-1].content == RETRIEVAL_FAILURE_ANSWER
    assert result["tool_results"] == []
    assert result["faithfulness_score"] == 1.0


@pytest.mark.unit
def test_parse_mcp_tool_result_multiple_blocks_becomes_list():
    from agent_graph import _parse_mcp_tool_result
    blocks = [
        {"type": "text", "text": '{"vorname": "Friedrich", "nachname": "Merz"}'},
        {"type": "text", "text": '{"vorname": "Friedrich", "nachname": "Maier"}'},
    ]
    assert _parse_mcp_tool_result(blocks) == [
        {"vorname": "Friedrich", "nachname": "Merz"},
        {"vorname": "Friedrich", "nachname": "Maier"},
    ]


@pytest.mark.unit
def test_parse_mcp_tool_result_error_block_raises_value_error():
    from agent_graph import _parse_mcp_tool_result
    blocks = [{"type": "text", "text": "Error executing tool x: Event loop is closed"}]
    with pytest.raises(ValueError):
        _parse_mcp_tool_result(blocks)




class _FakeDeclineWithPriorEvidenceLLM:
    def bind_tools(self, tools, tool_choice=None):
        return self

    async def ainvoke(self, _messages):
        return AIMessage(content="Based on what we discussed, CDU/CSU holds 34.1%.")

    def with_structured_output(self, schema):
        return _StructuredOutputStub(schema, "person_lookup", None)

@pytest.mark.unit
@pytest.mark.asyncio
async def test_hallucination_guard_awaits_check_grounded_for_direct_answer(monkeypatch):
    from agent_graph import create_agent_graph, run_agent_query

    monkeypatch.setattr("agent_graph.AsyncConnectionPool", _FakeAsyncConnectionPool)
    monkeypatch.setattr("agent_graph.AsyncPostgresSaver", _FakeAsyncPostgresSaver)
    monkeypatch.setattr("agent_graph.resolve_current_wahlperiode", lambda: 21)

    async def _fake_get_mcp_tools():
        return []

    monkeypatch.setattr("agent_graph.get_mcp_tools", _fake_get_mcp_tools)

    prior_evidence = [{"tool": "get_party_distribution",
                        "output": {"wahlperiode": 19, "percentages": {"CDU/CSU": 34.1}}}]
    _FakeAsyncPostgresSaver._pending_seed = prior_evidence
    _FakeAsyncPostgresSaver._pending_thread_id = "t-guard-await"
    try:
        fake_llm = _FakeDeclineWithPriorEvidenceLLM()
        graph = await create_agent_graph(_FakeAgentSystem(fake_llm), config={
            "enable_reflection": True, "enable_hallucination_guard": True,
            "max_reflection_iterations": 3, "faithfulness_threshold": 0.8,
            "completeness_threshold": 0.7,
        })

        result = await run_agent_query(graph, "and CDU/CSU's share?", thread_id="t-guard-await")
    finally:
        _FakeAsyncPostgresSaver._pending_seed = None
        _FakeAsyncPostgresSaver._pending_thread_id = None

    assert isinstance(result["faithfulness_score"], float)
    # tool_selector_node carries the seeded prior evidence forward as-is
    # since the model declines to call any further tool -- this confirms
    # the hallucination_guard_node branch that awaits check_grounded
    # against non-empty prior evidence actually ran, not just the
    # trivial score=1.0 empty-evidence shortcut.
    assert result["tool_results"] == prior_evidence