import json
import logging
import os
from datetime import date
from typing import Annotated, Literal

import grounding
from date_utils import (
    DateResolutionAmbiguousError,
    WahlperiodeResolutionError,
    resolve_current_wahlperiode,
    resolve_date_expression,
    resolve_wahlperiode_from_date,
)
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from langgraph.graph import END, START, StateGraph
from langgraph.graph.message import add_messages
from mcp_client import get_mcp_tools
from psycopg_pool import AsyncConnectionPool
from pydantic import BaseModel
from tracing import observe, update_current_trace
from typing_extensions import TypedDict

logger = logging.getLogger(__name__)

MAX_LLM_ATTEMPTS = 3

# Single source of truth for which tool a single-tool intent forces. Used
# to force the tool call in tool_selector_node. Note: person_lookup is
# omitted because it can map to either get_person_info (biography) or 
# get_person_activities (speeches/actions). The LLM dynamically chooses.
INTENT_TO_TOOL_NAME = {
    "party_distribution": "get_party_distribution",
    "role_lookup": "get_persons_by_role",   # NEW
}


# Verbatim substring from Groq's actual error message when tool_choice="required"
# is set and the model has nothing further to call (confirmed from production logs).
# Used to distinguish "model is legitimately done" from a real transport/API failure.
# Phrases OpenAI-compatible servers use when tool_choice is
# forced but the model returns plain text instead of a tool call.
_TOOL_CHOICE_DECLINE_MARKERS = (
    "did not call a tool",
    "did not call any tools",
    "tool_choice",
)


def _extract_failed_generation(error: Exception) -> str | None:
    """Extract the model's intended text from a Groq 'tool_choice=required'
    400 error. Groq embeds the generation the model WANTED to produce (but
    couldn't, because it contained no tool call) in error.failed_generation."""
    for attr in ("body", "response"):
        body = getattr(error, attr, None)
        if isinstance(body, dict):
            err = body.get("error", body)
            if isinstance(err, dict) and err.get("failed_generation"):
                return err["failed_generation"]
    try:
        error_str = str(error)
        json_start = error_str.find("{")
        if json_start >= 0:
            data = json.loads(error_str[json_start:])
            err = data.get("error", data)
            if isinstance(err, dict):
                return err.get("failed_generation")
    except (json.JSONDecodeError, ValueError):
        pass
    return None


# Shown when the model refuses to call a tool and there is no evidence to
# fall back on. We deliberately do NOT surface the model's own-knowledge
# answer here: this agent only speaks from DIP-retrieved data.
RETRIEVAL_FAILURE_ANSWER = (
    "I couldn't retrieve that from the parliamentary data just now. "
    "Please rephrase the question or try again."
)

FORCE_TOOL_USE_MESSAGE = SystemMessage(content=(
    "MANDATORY: Answer this question ONLY by calling the provided tool. "
    "Do not answer from your own knowledge \u2014 the user requires facts "
    "retrieved from the DIP parliamentary database."
))

MULTI_ENTITY_TOOL_USE_HINT = SystemMessage(content=(
    "If the user's question refers to more than one distinct entity of the "
    "same kind (e.g. multiple Wahlperioden/election periods, multiple named "
    "people), call this tool multiple times in this SAME response -- once "
    "per distinct entity -- instead of answering for only one and waiting "
    "for a follow-up question. Call it only once if the question names a "
    "single entity."
))


def _with_tool_use_directive(llm_input: list) -> list:
    """Append the force-tool directive once, so retried attempts after a
    tool_choice decline actually produce a tool call."""
    if any(isinstance(m, SystemMessage) and m.content == FORCE_TOOL_USE_MESSAGE.content
           for m in llm_input):
        return llm_input
    return [*llm_input, FORCE_TOOL_USE_MESSAGE]


class ModelDegenerateResponseError(Exception):
    pass


SUPERVISOR_PROMPT = """\
This assistant answers questions about the German Bundestag (federal
parliament) using the DIP database: individual politicians' biographical
and party info, and aggregate party-composition statistics.

You classify a user's message into one of five intents:
- person_lookup: about a specific named individual's parliamentary bio/party
  (e.g. "Who is Angela Merkel?", "What party is Olaf Scholz in?")
- party_distribution: an aggregate/statistical question about party composition
  (e.g. "Party distribution in Wahlperiode 20?")
- mixed: needs both of the above
- conversation_meta: about THIS conversation OR the assistant's own capabilities
  — e.g. "summarize our chat", "what was my first question", "what plots can
  you create?", "what data do you have access to". This includes any meta
  question about how to use the assistant or what visualizations it supports.
- out_of_scope: unrelated to parliamentary data, unrelated to the assistant's
  capabilities, and not about the conversation (e.g. "What's the weather?")

- role_lookup: about WHO holds a role or title, without naming the person
  (e.g. "Who is the current Bundeskanzler?", "Who is the Bundestag president?",
  "who are the ministers?"). If the user NAMES the person, that is person_lookup
  instead.

CRITICAL: You are a strict intent classifier. You must NEVER answer the 
user's question directly. Never generate code, charts, ASCII art, or 
markdown tables. Only output the JSON classification.

Any "who is <name>" or "tell me about <name>" question about a named person
should default to person_lookup, even if you don't personally recognize
the name -- the lookup tool itself will report if that person isn't found
in the data, so err toward person_lookup rather than out_of_scope for any
named-person question. Prefer out_of_scope only when the question is neither
about a person/party/parliament nor about the conversation itself.

GENERAL CONVERSATIONAL RULE: If the user's message is a short follow-up 
referring to previous context (e.g. asking to reformat, visualize, clarify, 
or modify previous data), inherit the intent from the previous turn.

You will be provided with the recent conversation history. Use it to 
understand conversational context (e.g. if the user replies to a 
clarification, infer what they are referring to).

Output: intent, raw_date_expression (verbatim, or null). Do not resolve
dates yourself.

Set raw_date_expression ONLY for calendar-date expressions ("since 2021",
"bis März 2023", "between 2020 and 2022"). Wahlperiode references
("Wahlperiode 20", "20th legislative session", "election period 20") are
NOT dates — for those, set raw_date_expression to null; the Wahlperiode
number is already captured by the intent/arguments."""

CONVERSATION_META_PROMPT = """\
Answer the user's question about THIS conversation or your own capabilities.
This covers: summarizing the chat, recalling an earlier message, explaining
what kinds of plots/tables/charts you can produce, reformatting or visualizing
prior results, or any other meta follow-up about what has been discussed.

Use the conversation transcript as the primary source. Prior tool-output data
is provided as additional context if available — reference it when the user
asks to reformat or visualize earlier results, but never invent parliamentary
facts that aren't in that data. 

If the user asks what visualizations you can create, answer based on your
actual capabilities (bar charts, scatter plots, and grouped bar charts for
comparing multiple legislative periods side by side). Do not tell the user
how to plot or visualize the data yourself; simply provide the data or
description. The UI will automatically render the visualization alongside
your text.

Never generate code, ASCII art, or image data. If the transcript doesn't
contain what they're asking about, say so honestly."""

SYNTHESIS_PROMPT = """\
Formulate a natural-language answer using ONLY the facts and numbers
present in the provided tool-output JSON. Never calculate, estimate, or
infer a number that is not already present in the data. If the data
includes a data_notes field, include that caveat in plain language. For
get_person_info results, prefer resolved_fraktion for the person's party
when the raw person.fraktion field is empty -- resolved_fraktion is
already computed from their role history for exactly that case. If no
tool output is provided, say so honestly rather than answering from
general knowledge.

Write like you're briefing someone, not printing a report: open with a
short conversational sentence or two stating the headline finding in
plain language, THEN give the precise figures as a markdown table or
bullet list. Never lead with a bare table and no framing, and never
replace the structured numbers with prose alone -- both parts belong
together in every answer.

CRITICAL: You are a data retrieval agent, not a coding assistant. Never 
generate Python code, ASCII art, or image data. If the user asks for a 
plot, chart, or visualization, present the requested data clearly as 
text or a markdown table. DO NOT tell the user how to plot or visualize 
the data yourself (e.g. "you can plot this by..."). The UI will 
automatically render the visualization alongside your text.

Only attribute an office, title, or role to a person if that office or
title appears verbatim in the tool output (funktion, person_roles, or
persons[].funktion). Never state who currently holds an office unless the
tool output says so -- if the output does not establish it, say the data
does not show it."""

SYNTHESIS_PROMPT_STRICT = SYNTHESIS_PROMPT + """

IMPORTANT: your previous answer was flagged as not well-grounded in the
tool output. This time, for every factual claim, only use a number or
fact that is verbatim present in the provided tool-output JSON. If you
are not sure a claim is supported, omit it rather than guessing."""

COMPLETENESS_RUBRIC = """\
Given the conversation and the tool-call evidence gathered so far, score
0.0-1.0 how completely the evidence addresses the UNDERLYING DATA the
question needs. A party-distribution question needs a distribution object
with counts and percentages; a person-lookup or role-lookup question needs
either a resolved person/persons, or an explicit disambiguation/not-found
result (e.g. an empty persons list together with a data_notes explanation)
-- the empty-plus-explanation case counts as COMPLETE evidence, not as a
gap to keep retrying.

Presentation and formatting requests -- what chart type (pie, bar, scatter,
donut, ...), table vs. prose, sort order, units, styling, or any other way
the user wants the SAME underlying data displayed -- are not part of this
score and are never a reason to mark evidence incomplete. That data already
being present in the evidence, however it was originally requested, is
sufficient; a different display of it is a downstream rendering concern,
not new data to retrieve.

Base your judgment ONLY on the tool_results JSON and the reference fact
when one is provided. Do not use your own knowledge about who holds which
office or which Wahlperiode is current, and never reject or second-guess a
tool result because it conflicts with something you believe to be true
about a real-world officeholder -- the tool's data is authoritative for
this task, even if it looks wrong to you. If the user says "current" or
"most recent" and the reference states the current Wahlperiode is N,
evidence about Wahlperiode N is what counts; evidence about an older
Wahlperiode is incomplete even when present.

If the score is below 1.0, ALSO return missing_calls: the specific tool
call(s) still needed to fill the gap, as {"tool": <name>, "args": {...}}.
The available tools and their exact argument names are:
- get_party_distribution: {"wahlperiode": <int 1-21>, "date_range": null}
- get_persons_by_role: {"funktion": <str>, "wahlperiode": <int 1-21>}
- get_person_info: {"name": <str>}
Only include a call whose exact (tool, args) pair is NOT already present
in tool_results -- never repeat one already made. If nothing concrete can
be identified, return an empty list rather than guessing.

Return the score, a one-sentence rationale, and missing_calls."""

CACHE_SUFFICIENCY_RUBRIC = """\
You will be shown the recent conversation and a JSON list of evidence
already retrieved earlier in this same session (existing_evidence). Each
entry is tagged with its position as `index` and includes the exact
`args` it was originally retrieved with (e.g. the `name` passed to
get_person_info, or the `wahlperiode` passed to get_party_distribution)
alongside its `output`.

Decide whether that existing evidence ALREADY fully answers the user's
most recent message, such that no new retrieval is needed -- e.g. the
user is repeating, rephrasing, or asking a pronoun/follow-up question
about the exact same person, party, or Wahlperiode/date-range already
covered by that evidence.

Ground this decision in each entry's `args`, not in the surface wording
of the conversation: only treat an entry as covering the current
question when its `args` refer to the same entity the user now means
(allowing for the user's own rephrasing, pronouns, or minor typos of
THAT entry's args) -- not merely because both messages are broadly
about "a person" or "a party."

An entry whose `output` reports "not found" (a null/empty person, match,
or distribution) is evidence ONLY that THAT entry's specific `args`
failed to resolve -- it says nothing about any other name, party, or
Wahlperiode, however similar-looking. Never reuse a not-found result for
args you cannot verify are the same.

Score 0 if the question is about a different person, a different party,
a different Wahlperiode or date-range, or asks for any detail not
already present in the evidence -- even if the topic looks superficially
similar. If you cannot verify the `args` match, score 0: a redundant
tool call is cheap, but silently reusing an unverified not-found result
is a false claim. Score 1 only when reusing the existing evidence, with
no further tool call, answers the question as well as a fresh retrieval
would.

ALSO return relevant_indices: the `index` values of ONLY the specific
entries whose `args` you actually verified ground the answer. Even if
the score is 0 (evidence is incomplete), include any entries that ARE
partially relevant to the user's current question (e.g. if asking for
WP 20 vs 21, and only WP 21 is in evidence, return the index of the
WP 21 entry). 

CRITICAL: Partial relevance requires the SAME TYPE of data. If the user
asks about party distribution, do NOT include entries about specific
persons, even if that person was discussed earlier. If absolutely no
existing entries are relevant, return an empty list. When score is 1,
relevant_indices must contain every entry actually needed to answer --
usually one, occasionally more for a question spanning several earlier
lookups (e.g. "compare his party to the one from before").

Judge ONLY from each entry's args and output plus the reference fact when
one is provided. Never use your own knowledge of who holds which office or
which Wahlperiode is current: if the user says "current"/"most recent" and
the reference says the current Wahlperiode is N, an entry for an older
Wahlperiode does NOT cover the question. Return the score, a one-sentence
rationale, and relevant_indices."""


def _current_turn_messages(messages: list) -> list:
    for i in range(len(messages) - 1, -1, -1):
        if isinstance(messages[i], HumanMessage):
            return messages[i:]
    return messages


def _human_readable_transcript(messages: list, limit: int | None = None) -> str:
    relevant = [m for m in messages if isinstance(m, (HumanMessage, AIMessage)) and m.content]
    if limit is not None:
        relevant = relevant[-limit:]
    return "\n".join(f"{'User' if isinstance(m, HumanMessage) else 'Agent'}: {m.content}" for m in relevant)


def _parse_mcp_tool_result(raw_result) -> dict | list:
    if isinstance(raw_result, str):
        return json.loads(raw_result)
    if isinstance(raw_result, dict):
        return raw_result
    if isinstance(raw_result, (list, tuple)):
        if not raw_result:
            return []
        parsed = []
        for block in raw_result:
            text = (block.get("text", "") if isinstance(block, dict)
                    else getattr(block, "text", "")).strip()
            if not text:
                continue
            if text.startswith("Error executing tool"):
                raise ValueError(text)
            parsed.append(json.loads(text))
        if not parsed:
            raise ValueError(f"could not extract text from MCP content blocks: {raw_result!r}")
        return parsed[0] if len(parsed) == 1 else parsed
    raise TypeError(f"unexpected MCP tool result type: {type(raw_result)!r}")


def deterministic_fallback_answer(tool_results: list[dict]) -> str:
    """Presentation-only, LLM-free formatter used when the synthesis model
    call itself fails (rate limit, timeout, outage) but valid structured
    tool evidence already exists. Generalizes by output SHAPE (counts/
    percentages vs. person fields), not by intent or wording, so it
    covers any current or future tool -- not just party_distribution."""
    if not tool_results:
        return ("I retrieved no data for this request and the answer-generation "
                "step is temporarily unavailable. Please try again shortly.")

    lines = [("The AI summarizer is temporarily unavailable, so here is the raw "
              "retrieved data:")]
    for entry in tool_results:
        output = entry.get("output", {})
        if not isinstance(output, dict) or "error" in output:
            continue
        if "counts" in output and "percentages" in output:
            label = f"Wahlperiode {output.get('wahlperiode')}" if output.get("wahlperiode") \
                else str(output.get("date_range") or "")
            lines.append(f"\n**{label}**")
            for party, pct in output.get("percentages", {}).items():
                count = output.get("counts", {}).get(party)
                lines.append(f"- {party}: {count} seats ({pct}%)")
            if output.get("data_notes"):
                lines.append(f"- Note: {output['data_notes']}")
        elif output.get("person"):
            p = output["person"]
            name = " ".join(part for part in (p.get("vorname"), p.get("nachname")) if part)
            lines.append(f"\n**{name}**: {json.dumps(p, default=str)[:500]}")
        else:
            lines.append(f"\n{json.dumps(output, default=str)[:500]}")
    return "\n".join(lines)


# NOTE: The deterministic completeness short-circuit was removed.
# It incorrectly assumed a single tool call satisfied multi-entity queries
# (e.g. "compare WP 20 and 21"), preventing the LLM judge from triggering
# necessary reflection retries. The LLM judge handles completeness safely.


def load_agent_config(overrides: dict | None = None) -> dict:
    cfg = {
        "enable_reflection": os.getenv("ENABLE_REFLECTION").lower() == "true",
        "enable_hallucination_guard": os.getenv("ENABLE_HALLUCINATION_GUARD").lower() == "true",
        "max_reflection_iterations": int(os.getenv("MAX_REFLECTION_ITERATIONS")),
        "faithfulness_threshold": float(os.getenv("FAITHFULNESS_THRESHOLD")),
        "completeness_threshold": float(os.getenv("COMPLETENESS_THRESHOLD")),
    }
    if overrides:
        cfg.update(overrides)
    return cfg


class AgentState(TypedDict):
    messages: Annotated[list, add_messages]
    intent: Literal["person_lookup", "party_distribution", "mixed", "conversation_meta",
                    "out_of_scope", "role_lookup"] | None
    raw_date_expression: str | None
    needs_clarification: str | None
    tool_results: list[dict]
    reflection_iterations: int
    completeness_score: float | None
    completeness_rationale: str | None
    faithfulness_score: float | None
    retrieval_feedback: str | None  # completeness judge rationale, fed back
                                    # into tool_selector on reflection retries
    missing_calls: list[dict] | None  # structured (tool, args) gaps the
                                       # completeness judge identified; lets
                                       # tool_selector fill them deterministically
                                       # when the LLM doesn't act on retrieval_feedback
    regenerated: bool
    needs_regenerate_this_pass: bool
    direct_answer: str | None  # set when the model answers without a tool call
    turn_tool_results: list[dict]
    thinking_log: list[str]   


class SupervisorExtraction(BaseModel):
    intent: Literal["person_lookup", "party_distribution", "mixed", "conversation_meta",
                    "out_of_scope", "role_lookup"]
    raw_date_expression: str | None


class JudgeVerdict(BaseModel):
    score: float
    rationale: str


class CacheSufficiencyVerdict(BaseModel):
    score: float
    rationale: str
    relevant_indices: list[int] = []


class MissingToolCall(BaseModel):
    tool: str
    args: dict


class CompletenessVerdict(BaseModel):
    score: float
    rationale: str
    # Structured, not just prose: the SPECIFIC tool+args still missing.
    # Lets tool_selector fill the gap deterministically if the LLM fails
    # to act on the rationale text -- generalizes to any tool/argument,
    # not one hardcoded case.
    missing_calls: list[MissingToolCall] = []


async def _judge(llm, rubric: str, payload: str, schema: type[BaseModel] = JudgeVerdict):
    return await llm.with_structured_output(schema).ainvoke(
        [("system", rubric), ("user", payload)])


async def create_agent_graph(agent_system, config: dict | None = None,
                              pool: AsyncConnectionPool | None = None):
    cfg = config or load_agent_config()

    @observe()
    async def supervisor_node(state: AgentState) -> dict:
        # Pass conversational context (Human/AI text only) so the LLM can 
        # understand short follow-up replies like "yes". We exclude ToolMessages 
        # to avoid confusing intent extraction and crashing Groq's harmony formatter.
        history = [
            m for m in state["messages"] 
            if isinstance(m, (HumanMessage, AIMessage)) and m.content
        ]
        llm_input = [("system", SUPERVISOR_PROMPT)] + history

        try:
            extraction: SupervisorExtraction = await agent_system.judge_llm.with_structured_output(
    SupervisorExtraction).ainvoke(llm_input)
        except Exception as e:
            if any(m in str(e) for m in _TOOL_CHOICE_DECLINE_MARKERS):
                logger.warning("supervisor declined to call structured output tool, defaulting to conversation_meta")
                extraction = SupervisorExtraction(intent="conversation_meta", raw_date_expression=None)
            else:
                raise

        # The out-of-scope double-check retry was removed: Groq's TPM
        # ceiling (8K/min) barely covers one full turn's call chain, so a
        # rare-path safety retry isn't worth its token cost. If false
        # out_of_scope becomes a real problem, tighten SUPERVISOR_PROMPT's
        # examples instead of re-adding a second call.
        logger.info("intent=%s date_expr=%r", extraction.intent, extraction.raw_date_expression)
        thinking = [f"🧭 Classified as **{extraction.intent}**"]
        if extraction.raw_date_expression:
            thinking.append(f"📅 Detected date expression: \"{extraction.raw_date_expression}\"")
        return {"intent": extraction.intent, "raw_date_expression": extraction.raw_date_expression,
                "reflection_iterations": 0, "regenerated": False,
                "retrieval_feedback": None, "thinking_log": thinking}

    @observe()
    async def tool_selector_node(state: AgentState) -> dict:
        if state["intent"] in ("out_of_scope", "conversation_meta"):
            # Preserve prior tool results so follow-up UI/clarification requests
            # can still be answered or visualized by the frontend.
            return {"tool_results": state.get("tool_results", []), "needs_clarification": None,
                    "direct_answer": None}

        date_range = None
        if state["raw_date_expression"]:
            try:
                date_range = await resolve_date_expression(agent_system.judge_llm, state["raw_date_expression"])
            except DateResolutionAmbiguousError as err:
                logger.warning("ambiguous date: %r", err.expression)
                return {"needs_clarification": f"Could you clarify the date you mean by '{err.expression}'?"}

        tool_results = list(state.get("tool_results") or [])
        prior_count = len(tool_results)
        relevant_prior = []  # Captures partially relevant prior evidence for mixed-retrieval turns

        # Session-state reuse: before spending a real DIP API call, ask
        # whether evidence already gathered earlier in this thread already
        # answers the current message. SKIPPED on reflection retries:
        # the completeness judge just declared this turn's evidence
        # insufficient, so re-asking "is the cache enough?" is provably
        # wasted work (and wasted judge-LLM quota on the free tier).
        if tool_results and state.get("reflection_iterations", 0) == 0:
            history_for_cache = [
                m for m in state["messages"]
                if isinstance(m, (HumanMessage, AIMessage)) and m.content
            ]
            try:
                indexed_evidence = [{"index": i, **tr} for i, tr in enumerate(tool_results)]
                try:
                    current_wp = resolve_current_wahlperiode()
                    cache_reference = ("As of today, the current/most recently "
                                       f"constituted Wahlperiode is {current_wp}.")
                except WahlperiodeResolutionError:
                    cache_reference = None
                cache_verdict = await _judge(agent_system.judge_llm, CACHE_SUFFICIENCY_RUBRIC, json.dumps({
                    "conversation": _human_readable_transcript(history_for_cache, limit=6),
                    "existing_evidence": indexed_evidence,
                    "reference": cache_reference,
                }), schema=CacheSufficiencyVerdict)

                logger.info("cache_sufficiency=%.2f (%s) relevant_indices=%s",
                            cache_verdict.score, cache_verdict.rationale, cache_verdict.relevant_indices)
                
                # Extract partially relevant prior evidence even if the cache 
                # score is < 1.0. This ensures turns that span multiple retrievals 
                # (e.g. "compare 11 and 21" where 21 was fetched previously) retain 
                # the prior context needed for charts and synthesis.
                relevant_prior = [tool_results[i] for i in cache_verdict.relevant_indices
                                 if 0 <= i < len(tool_results)]
                
                if cache_verdict.score >= cfg["completeness_threshold"] and relevant_prior:
                    # Only reuse cached evidence when the judge gave us
                    # SPECIFIC, verified indices. A score/indices mismatch
                    # (score says "sufficient" but indices come back empty)
                    # must fall through to a real tool call below, never
                    # substitute the entire cross-turn history -- that was
                    # exactly what caused an unrelated WP21/WP12/WP10
                    # comparison chart to appear on a plain "WP 20" question.
                    return {"tool_results": tool_results, "needs_clarification": None,
                            "direct_answer": None, "turn_tool_results": relevant_prior,
                            "thinking_log": (state.get("thinking_log") or [])
                                + [(f"♻️ Reused {len(relevant_prior)} already-fetched result(s) "
                                    f"from this conversation — no new API call needed "
                                    f"({cache_verdict.rationale})")]}
            except Exception:
                # Fail open toward a real (slightly redundant) tool call
                # rather than risk answering from evidence we couldn't
                # confirm is actually relevant to the new question.
                logger.exception("cache sufficiency check failed; proceeding with a live tool call")

        tools = await get_mcp_tools()

        # Provide context of tools already called THIS turn so the LLM doesn't
        # blindly repeat the exact same arguments. Reflection loops retry the
        # tool_selector; without this, the LLM re-reads the original user query,
        # forgets it just called the tool, and fetches the exact same data again.
        prior_turn_tools = state.get("turn_tool_results") or []
        if prior_turn_tools:
            tool_summary = "You have already retrieved the following data during this turn:\n"
            for tr in prior_turn_tools:
                args_str = json.dumps(tr.get("args", {}))
                # Truncate output to prevent token bloat and harmony formatter crashes
                output_brief = json.dumps(tr.get("output", {}))
                if len(output_brief) > 500:
                    output_brief = output_brief[:500] + "..."
                tool_summary += f"- {tr.get('tool')}({args_str}) -> {output_brief}\n"
            tool_summary += (
                "\nCRITICAL: Do NOT repeat any of the tool calls listed above. "
                "If the user's question requires data you haven't fetched yet "
                "(e.g. comparing multiple election periods or multiple people), "
                "call the tool again with the NEW, missing arguments."
            )
            prior_tools_msg = SystemMessage(content=tool_summary)
        else:
            prior_tools_msg = None

        try:
            current_wp = resolve_current_wahlperiode()
            anchor = SystemMessage(content=(
                f"Reference fact: as of today, the current/most recently constituted "
                f"Wahlperiode is {current_wp}. Use this number whenever the user refers "
                f"to 'now', 'current', 'the last election', 'the current government', or "
                f"similar relative phrasing about which Wahlperiode they mean -- never "
                f"guess a Wahlperiode number from memory for a relative reference like that.\n"
                f"TOOL SELECTION: if the user asks WHO currently holds a role or title "
                f"(chancellor/Bundeskanzler, president, minister, mayor, ...), call "
                f"get_persons_by_role with funktion=<the role> and wahlperiode={current_wp}. "
                f"If the user NAMES a specific person, call get_person_info(name=...). "
                f"Never name the person who holds a role from your own knowledge -- "
                f"the tool resolves it from the data."
            ))
            # Include text history for context, but skip raw ToolMessages to prevent 
            # Groq harmony formatter crashes on previous tool outputs.
            history = [
                m for m in state["messages"] 
                if isinstance(m, (HumanMessage, AIMessage)) and m.content
            ]
            llm_input = [anchor, *history]
            if prior_tools_msg:
                llm_input.append(prior_tools_msg)
            if state.get("retrieval_feedback"):
                # Directed reflection: the completeness judge told us WHAT
                # is missing. Give that directive to the selector so the
                # retry fetches the gap instead of repeating itself.
                llm_input.append(SystemMessage(content=(
                    "The evidence judge reviewed the data retrieved so far this "
                    f"turn and found it INCOMPLETE: {state['retrieval_feedback']}\n"
                    "Fetch exactly the missing data now with a NEW tool call -- a "
                    "different tool and/or different arguments. Do NOT repeat any "
                    "call whose output is already listed above."
                )))
        except WahlperiodeResolutionError:
            logger.warning("could not resolve current Wahlperiode; proceeding without anchor")
            history = [
                m for m in state["messages"]
                if isinstance(m, (HumanMessage, AIMessage)) and m.content
            ]
            llm_input = list(history)
            if prior_tools_msg:
                llm_input.append(prior_tools_msg)
            if state.get("retrieval_feedback"):
                llm_input.append(SystemMessage(content=(
                    "The evidence judge reviewed the data retrieved so far this "
                    f"turn and found it INCOMPLETE: {state['retrieval_feedback']}\n"
                    "Fetch exactly the missing data now with a NEW tool call -- a "
                    "different tool and/or different arguments. Do NOT repeat any "
                    "call whose output is already listed above."
                )))

        # Encourage resolving multi-entity requests ("compare WP 21 and 12")
        # in ONE model turn via multiple tool calls, instead of depending on
        # the reflection loop to discover the second entity a full round
        # trip later. Domain-agnostic -- any repeated-entity phrasing, not
        # a hardcoded Wahlperiode number. The reflection loop stays in
        # place underneath as a safety net for whatever this doesn't catch.
        llm_input.append(MULTI_ENTITY_TOOL_USE_HINT)

        # Map the supervisor's intent to a specific tool name.
        # This prevents the LLM from hallucinating an answer instead of calling the tool.
        forced_tool_name = INTENT_TO_TOOL_NAME.get(state["intent"])
        
        # Groq expects a specific format to force a tool: 
        # {"type": "function", "function": {"name": "..."}}
        # If we know the exact tool, force it. Otherwise, fall back to "required".
        if forced_tool_name:
            tool_choice_config = {"type": "function", "function": {"name": forced_tool_name}}
        else:
            tool_choice_config = "required"

        response = None
        direct_answer = None
        for attempt in range(1, MAX_LLM_ATTEMPTS + 1):
            try:
                candidate = await agent_system.llm.bind_tools(
                    tools, tool_choice=tool_choice_config).ainvoke(llm_input)
            except Exception as e:
                if any(m in str(e) for m in _TOOL_CHOICE_DECLINE_MARKERS):
                    logger.info("model declined to call a further tool (intent=%s): %s",
                               state["intent"], e)
                    decline_text = _extract_failed_generation(e)
                    if decline_text and tool_results:
                        # Follow-up question: the answer may already exist in
                        # prior tool evidence. Accept it ONLY if it actually
                        # grounds against that evidence.
                        score = await grounding.check_grounded(decline_text, tool_results, llm=agent_system.llm)
                        logger.info("declined answer vs prior evidence: faithfulness=%.3f", score)
                        if score >= cfg["faithfulness_threshold"]:
                            direct_answer = decline_text
                            break
                        logger.warning("declined answer not grounded in prior evidence "
                                       "(score=%.3f) -- forcing a real tool call", score)
                    elif decline_text:
                        logger.info("model answered from its own knowledge with no "
                                    "prior tool evidence -- refusing to surface it; "
                                    "forcing a real tool call")
                    # Never return ungrounded text to the user: insist on an
                    # actual tool call on the next attempt.
                    llm_input = _with_tool_use_directive(llm_input)
                    continue
                logger.exception("unexpected error invoking LLM with tool_choice='required' "
                                 "(intent=%s), attempt %d/%d", state["intent"], attempt, MAX_LLM_ATTEMPTS)
                if attempt == MAX_LLM_ATTEMPTS:
                    raise
                continue
            if candidate.tool_calls:
                response = candidate
                break
            logger.warning("model produced no tool call despite tool_choice='required' "
                           "(intent=%s), retry %d/%d", state["intent"], attempt, MAX_LLM_ATTEMPTS)

        # Short-circuit: the model answered directly (typically a follow-up
        # whose answer is already in the conversation from a prior tool call).
        if direct_answer:
            return {"tool_results": tool_results, "needs_clarification": None,
                    "direct_answer": direct_answer}

        if response is None:
            if tool_results:
                logger.info("no further tool call available/needed (intent=%s); "
                           "proceeding to synthesis with existing tool evidence", state["intent"])
                return {"tool_results": tool_results, "needs_clarification": None,
                        "direct_answer": None}
            # Model refused all forced attempts and there is no evidence to
            # fall back on. Tell the user honestly instead of 503ing -- and
            # never surface the model's ungrounded answer.
            logger.error("LLM refused to call a tool with no evidence "
                         "(intent=%s); returning honest failure", state["intent"])
            return {"tool_results": [], "needs_clarification": RETRIEVAL_FAILURE_ANSWER,
                    "direct_answer": None}

        new_messages: list = [response]
        needs_clarification = None
        call_thinking: list[str] = []

        def _display_name(p: dict) -> str:
            return " ".join(part for part in (p.get("vorname", ""), p.get("namenszusatz", ""),
                                                p.get("nachname", "")) if part)

        for call in response.tool_calls:
            if call["name"] not in {t.name for t in tools}:
                logger.warning("ignoring unknown tool call: %r", call["name"])
                new_messages.append(ToolMessage(
                    content=f"error: unknown tool {call['name']!r}", tool_call_id=call["id"], name=call["name"]))
                continue

            args = dict(call["args"])

            if date_range is not None:
                args.setdefault("date_range", date_range)

                # get_persons_by_role / get_party_distribution take a
                # wahlperiode int, not a date -- resolve it deterministically
                # from the anchor date instead of trusting the model's guess.
                if call["name"] in ("get_persons_by_role", "get_party_distribution"):
                    anchor_date_str = date_range.get("start") or date_range.get("end")
                    if anchor_date_str:
                        try:
                            resolved_wp = resolve_wahlperiode_from_date(date.fromisoformat(anchor_date_str))
                        except ValueError:
                            resolved_wp = None
                        if resolved_wp is not None:
                            args["wahlperiode"] = resolved_wp
            # Deterministic backstop: if the model repeats a call with the
            # exact same (tool, args) already made this turn, that means it
            # failed to act on retrieval_feedback -- a generic LLM compliance
            # failure, not something specific to any one tool or phrasing.
            # Before falling back to stale reused evidence, check whether the
            # completeness judge already told us EXACTLY what's missing
            # (state["missing_calls"]) and, if so, fetch that instead of
            # silently stalling. Generalizes to any tool/any missing
            # argument the judge identifies -- not a hardcoded case.
            sig = (call["name"], json.dumps(args, sort_keys=True))
            evidence_this_turn = {
                (tr["tool"], json.dumps(tr.get("args", {}), sort_keys=True)): tr
                for tr in (state.get("turn_tool_results") or [])
            }
            known_tool_names = {t.name for t in tools}
            if sig in evidence_this_turn:
                next_gap = next(
                    (mc for mc in (state.get("missing_calls") or [])
                     if mc["tool"] in known_tool_names
                     and (mc["tool"], json.dumps(mc.get("args", {}), sort_keys=True)) not in evidence_this_turn),
                    None,
                )
                if next_gap is not None:
                    logger.warning("dedup: model repeated %s; deterministically "
                                   "fetching judge-identified gap %s instead", sig, next_gap)
                    call = {**call, "name": next_gap["tool"]}
                    args = dict(next_gap["args"])
                else:
                    logger.warning("dedup: model repeated %s; reusing prior result", sig)
                    new_messages.append(ToolMessage(
                        content=json.dumps(evidence_this_turn[sig]["output"]),
                        tool_call_id=call["id"], name=call["name"]))
                    call_thinking.append(f"♻️ Skipped duplicate call to `{call['name']}` — reused this turn's own result")
                    continue

            matching_tool = next(t for t in tools if t.name == call["name"])
            logger.info("calling tool %s(%s)", call["name"], args)
            try:
                raw_result = await matching_tool.ainvoke(args)
                output = _parse_mcp_tool_result(raw_result)
                # Log the raw tool output so you can verify the DIP API data in your terminal
                logger.info("RAW TOOL OUTPUT (%s): %s", call["name"], json.dumps(output, default=str)[:2000])
            except Exception as e:
                logger.exception("tool %s failed", call["name"])
                output = {"error": f"{type(e).__name__}: {e}"}
                tool_results.append({"tool": call["name"], "args": args, "output": output})
                new_messages.append(ToolMessage(content=json.dumps(output), tool_call_id=call["id"], name=call["name"]))
                call_thinking.append(f"❌ Called `{call['name']}({args})` → failed: {type(e).__name__}")
                continue


            tool_results.append({"tool": call["name"], "args": args, "output": output})
            new_messages.append(ToolMessage(content=json.dumps(output), tool_call_id=call["id"], name=call["name"]))
            source_note = output.get("_source", "live DIP API") if isinstance(output, dict) else "live DIP API"
            call_thinking.append(f"🔧 Called `{call['name']}({args})` → served from **{source_note}**")

            # Generic, domain-agnostic post-call step: if the tool returned a




            # clarification hint (data_notes) but NO other substantial data,
            # ask the user. If it returned substantial data alongside the note,
            # treat the note as a caveat and proceed to synthesis.
            if isinstance(output, dict) and output.get("matches"):
                names = ", ".join(_display_name(m) for m in output["matches"])
                needs_clarification = needs_clarification or \
                    f"I found multiple matches — did you mean: {names}?"

        # Deterministic gap-sweep: if the completeness judge already told
        # us EXACTLY which (tool, args) pairs are still missing from a
        # PRIOR reflection round, fetch ALL of them right now instead of
        # relying on one-per-retry dedup substitution to trickle through
        # them one reflection loop at a time. A 4-entity comparison (e.g.
        # "compare WP18, 19, 20, 21") can need several additional lookups
        # beyond the model's first pass -- without this sweep, each one
        # costs a full extra reflection round, and MAX_REFLECTION_ITERATIONS
        # can exhaust before every entity is ever fetched. Generalizes to
        # any number of missing entities, any tool -- not hardcoded to
        # Wahlperioden or party_distribution.
        known_tool_names = {t.name for t in tools}
        evidence_this_turn_after_call = {
            (tr["tool"], json.dumps(tr.get("args", {}), sort_keys=True))
            for tr in tool_results[prior_count:]
        } | {
            (tr["tool"], json.dumps(tr.get("args", {}), sort_keys=True))
            for tr in (state.get("turn_tool_results") or [])
        }
        for mc in (state.get("missing_calls") or []):
            if mc["tool"] not in known_tool_names:
                continue
            mc_sig = (mc["tool"], json.dumps(mc.get("args", {}), sort_keys=True))
            if mc_sig in evidence_this_turn_after_call:
                continue
            gap_tool = next(t for t in tools if t.name == mc["tool"])
            logger.info("deterministic gap-sweep: calling tool %s(%s)", mc["tool"], mc["args"])
            try:
                raw_result = await gap_tool.ainvoke(mc["args"])
                output = _parse_mcp_tool_result(raw_result)
                logger.info("RAW TOOL OUTPUT (%s): %s", mc["tool"], json.dumps(output, default=str)[:2000])
                source_note = output.get("_source", "live DIP API") if isinstance(output, dict) else "live DIP API"
                call_thinking.append(f"🔧 Called `{mc['tool']}({mc['args']})` → served from **{source_note}** (gap-sweep)")
            except Exception as e:
                logger.exception("gap-sweep tool %s failed", mc["tool"])
                output = {"error": f"{type(e).__name__}: {e}"}
                call_thinking.append(f"❌ Gap-sweep call to `{mc['tool']}` failed: {type(e).__name__}")
            tool_results.append({"tool": mc["tool"], "args": mc["args"], "output": output})
            evidence_this_turn_after_call.add(mc_sig)

        # Evidence fetched during THIS invocation only (may be called more than
        # once per user turn via the reflection loop). Kept separate from the
        # full cross-turn `tool_results`, which persists for the life of the
        # thread so unrelated follow-ups can still reference older evidence.
        # Combine newly fetched tools with any relevant_prior evidence captured
        # during the cache check so multi-turn comparative queries render correctly.
        newly_fetched = tool_results[prior_count:]
        # Accumulate across reflection retries within THIS turn. This node
        # runs once per retry, and turn_tool_results has no reducer, so
        # returning only this invocation's local fetch would silently drop
        # whatever an EARLIER retry this same turn already found (e.g. WP20
        # found on retry 1, WP21 found on retry 2 -- synthesis needs both,
        # not just the most recent). Carry forward what's already
        # accumulated this turn and append only genuinely new entries,
        # deduped by (tool, args) so nothing gets counted twice.
        carried_forward = list(state.get("turn_tool_results") or [])
        already_in_turn = {
            (tr["tool"], json.dumps(tr.get("args", {}), sort_keys=True))
            for tr in carried_forward
        }
        for tr in relevant_prior + newly_fetched:
            sig = (tr["tool"], json.dumps(tr.get("args", {}), sort_keys=True))
            if sig not in already_in_turn:
                carried_forward.append(tr)
                already_in_turn.add(sig)
        turn_tool_results = carried_forward

        return {"messages": new_messages, "tool_results": tool_results,
                "needs_clarification": needs_clarification, "direct_answer": None,
                "turn_tool_results": turn_tool_results,
                "thinking_log": (state.get("thinking_log") or []) + call_thinking}

    @observe()
    async def reflection_node(state: AgentState) -> dict:
        iterations = state.get("reflection_iterations", 0) + 1
        if (state.get("direct_answer")
                or state.get("intent") in ("conversation_meta", "out_of_scope")
                or not state["tool_results"]):
            return {"completeness_score": 1.0, "reflection_iterations": iterations}

        # REVERTED: a structural "exactly one tool call = complete" check
        # was tried here to save a judge call. It broke every comparison
        # query ("compare WP 20 and 21"), because the model's first pass
        # always extracts only ONE argument -- it relies on THIS judge to
        # notice the gap and loop back to tool_selector for the second one.
        # Skipping the judge after the first call permanently disabled
        # that loop. Token savings are not worth silently wrong answers;
        # always judge for these intents.
        conversation = _human_readable_transcript(_current_turn_messages(state["messages"]))
        try:
            current_wp = resolve_current_wahlperiode()
            reference = ("As of today, the current/most recently constituted "
                         f"Wahlperiode is {current_wp}.")
        except WahlperiodeResolutionError:
            reference = None
        payload = json.dumps({
            "conversation": conversation,
            "tool_results": state["tool_results"],
            "reference": reference,
        })
        try:
            verdict = await _judge(agent_system.judge_llm, COMPLETENESS_RUBRIC, payload,
                                    schema=CompletenessVerdict)
            logger.info("completeness=%.2f (%s) missing_calls=%s iteration=%d",
                        verdict.score, verdict.rationale, verdict.missing_calls, iterations)
            incomplete = verdict.score < cfg["completeness_threshold"]
            thinking = [f"🔍 Completeness check: {verdict.score:.0%} — {verdict.rationale}"]
            if incomplete and verdict.missing_calls:
                thinking.append(f"➕ Still need: {[mc.tool for mc in verdict.missing_calls]}")
            return {"completeness_score": verdict.score,
                    "reflection_iterations": iterations,
                    "retrieval_feedback": verdict.rationale if incomplete else None,
                    "missing_calls": [mc.model_dump() for mc in verdict.missing_calls] if incomplete else None,
                    "thinking_log": (state.get("thinking_log") or []) + thinking}
        except Exception:
            logger.exception("completeness judge failed; treating existing evidence as sufficient")
            return {"completeness_score": 1.0, "reflection_iterations": iterations, "missing_calls": None}

    def route_after_reflection(state: AgentState) -> Literal["incomplete", "complete"]:
        if state.get("needs_clarification"):
            return "complete"
        incomplete = (state.get("completeness_score", 1.0) < cfg["completeness_threshold"]
                      and state["reflection_iterations"] < cfg["max_reflection_iterations"])
        return "incomplete" if incomplete else "complete"

    @observe()
    async def synthesis_node(state: AgentState) -> dict:
        if state.get("direct_answer"):
            return {"messages": [AIMessage(content=state["direct_answer"])]}

        if state["needs_clarification"]:
            answer = state["needs_clarification"]
        elif state["intent"] == "out_of_scope":
            answer = ("I can help with questions about German parliamentary data "
                       "(politicians, parties, legislative sessions) via the DIP API — "
                       "that question is outside what I can answer.")
        elif state["intent"] == "conversation_meta":
            # Meta questions (capabilities, "show as chart", summaries) need a
            # different prompt than data-retrieval synthesis. Using SYNTHESIS_PROMPT
            # would force answering "what plots can you create?" from irrelevant
            # tool JSON. CONVERSATION_META_PROMPT handles both capabilities and
            # follow-ups dynamically, whether or not prior data exists.
            transcript = _human_readable_transcript(state["messages"])
            context = json.dumps(state.get("turn_tool_results") or state["tool_results"]) if state.get("tool_results") else "None"
            response = await agent_system.llm.ainvoke(
                [("system", CONVERSATION_META_PROMPT),
                 ("user", f"Conversation so far:\n{transcript}\n\nPrior tool output (for reference):\n{context}")])
            answer = response.content

        else:
            conversation = _human_readable_transcript(state["messages"], limit=6)
            scoped_results = state.get("turn_tool_results") or state["tool_results"]
            context = json.dumps(scoped_results)
            prompt = SYNTHESIS_PROMPT_STRICT if state.get("regenerated") else SYNTHESIS_PROMPT
            try:
                response = await agent_system.llm.ainvoke(
                    [("system", prompt), ("user", f"Conversation so far:\n{conversation}\n\nTool output: {context}")])
                answer = response.content
            except Exception:
                logger.exception("synthesis LLM call failed; falling back to deterministic answer")
                answer = deterministic_fallback_answer(scoped_results)

        return {"messages": [AIMessage(content=answer)]}

    @observe()
    async def hallucination_guard_node(state: AgentState) -> dict:
        if state.get("direct_answer"):
            prior = state.get("tool_results") or []
            if prior:
                score = await grounding.check_grounded(state["direct_answer"], prior, llm=agent_system.llm)
                logger.info("[grounding] direct-answer faithfulness=%.3f vs prior-turn evidence", score)
                if score < cfg["faithfulness_threshold"]:
                    logger.warning("direct answer weakly grounded in prior tool evidence "
                                   "(score=%.3f); accepting anyway — model declined to "
                                   "re-call tools", score)
            else:
                score = 1.0
            return {"faithfulness_score": score, "needs_regenerate_this_pass": False}

        if state["needs_clarification"] or state["intent"] in ("out_of_scope", "conversation_meta"):
            return {"faithfulness_score": 1.0, "needs_regenerate_this_pass": False}

        answer = state["messages"][-1].content
        # Ground against evidence actually fetched/reused THIS turn. If 
        # turn_tool_results is empty, it means this is a meta/clarification 
        # turn (which is already handled above) or a direct_answer. We must 
        # NOT fallback to the full cross-turn history, as unrelated older 
        # tool calls will artificially deflate the grounding score and cause
        # infinite regeneration loops.
        tool_results = state.get("turn_tool_results") or []
        if not tool_results:
            score = 1.0
        else:
            score = await grounding.check_grounded(answer, tool_results, llm=agent_system.llm)
        logger.info("[grounding] faithfulness=%.3f (deterministic structured diff, %d evidence entr%s)",
                    score, len(tool_results), "y" if len(tool_results) == 1 else "ies")

        already_regenerated = state.get("regenerated", False)
        needs_regenerate = score < cfg["faithfulness_threshold"] and not already_regenerated
        thinking = [f"✅ Faithfulness check: {score:.0%} grounded in retrieved data"]
        if needs_regenerate:
            thinking.append("⚠️ Answer wasn't well-grounded — regenerating with stricter instructions")
        return {
            "faithfulness_score": score,
            "regenerated": needs_regenerate or already_regenerated,
            "needs_regenerate_this_pass": needs_regenerate,
            "thinking_log": (state.get("thinking_log") or []) + thinking,
        }

    def route_after_guard(state: AgentState) -> Literal["regenerate", "proceed"]:
        return "regenerate" if state.get("needs_regenerate_this_pass") else "proceed"

    graph = StateGraph(AgentState)
    graph.add_node("supervisor", supervisor_node)
    graph.add_node("tool_selector", tool_selector_node)
    graph.add_node("synthesis", synthesis_node)
    graph.add_edge(START, "supervisor")
    graph.add_edge("supervisor", "tool_selector")

    if cfg["enable_reflection"]:
        graph.add_node("reflection", reflection_node)
        graph.add_edge("tool_selector", "reflection")
        graph.add_conditional_edges("reflection", route_after_reflection,
            {"incomplete": "tool_selector", "complete": "synthesis"})
    else:
        graph.add_edge("tool_selector", "synthesis")

    if cfg["enable_hallucination_guard"]:
        graph.add_node("hallucination_guard", hallucination_guard_node)
        graph.add_edge("synthesis", "hallucination_guard")
        graph.add_conditional_edges("hallucination_guard", route_after_guard,
            {"regenerate": "synthesis", "proceed": END})
    else:
        graph.add_edge("synthesis", END)

    if pool is None:
        pool = AsyncConnectionPool(
            conninfo=os.getenv("DATABASE_URL"), min_size=2, max_size=20,
            kwargs={"autocommit": True}, open=False)
        await pool.open()
    checkpointer = AsyncPostgresSaver(pool)
    await checkpointer.setup()

    logger.info("graph compiled: reflection=%s guard=%s",
                cfg["enable_reflection"], cfg["enable_hallucination_guard"])
    return graph.compile(checkpointer=checkpointer)


@observe()
async def run_agent_query(agent_graph, query: str, thread_id: str) -> dict:
    update_current_trace(session_id=thread_id, tags=["dip-agent"])
    config = {"configurable": {"thread_id": thread_id}}

    # Preserve prior-turn tool evidence: without a reducer, passing
    # tool_results=[] here would overwrite the checkpointed channel, leaving
    # follow-ups ("what is her party affiliation?") with nothing to ground
    # a direct answer against. This previously surfaced as a 503.
    snapshot = await agent_graph.aget_state(config)
    prior_tool_results = (
        snapshot.values.get("tool_results", []) if snapshot and snapshot.values else []
    )

    initial_state: AgentState = {
        "messages": [HumanMessage(content=query)],
        "intent": None, "raw_date_expression": None, "needs_clarification": None,
        "tool_results": prior_tool_results, "reflection_iterations": 0,
        "completeness_score": None, "retrieval_feedback": None,
        "missing_calls": None,
        "faithfulness_score": None,
        "regenerated": False, "needs_regenerate_this_pass": False,
        "direct_answer": None,
        "turn_tool_results": [],
        "thinking_log": [],
    }
    return await agent_graph.ainvoke(initial_state, config=config)


