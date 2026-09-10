# agent-service, Design Decisions & Notes

## Overview

`agent-service` is the orchestration brain of `dip-agent`, a system that answers questions about the German Bundestag using the real DIP open-data API. It's a LangGraph state machine (supervisor → tool_selector → reflection → synthesis → hallucination_guard) sitting on FastAPI, with PostgreSQL holding both LangGraph's internal checkpointed state and a separate user-facing chat transcript. The one rule everything below exists to enforce: **the agent must never answer from its own knowledge, every factual claim traces back to a real tool call against real DIP data, and every answer is checked for hallucination before it reaches the user.**

## What this service is responsible for

- **Agent orchestration**: 5-node LangGraph graph (`supervisor` → `tool_selector` → `reflection` → `synthesis` → `hallucination_guard`)
- **Intent classification**: Classifies user queries into `person_lookup`, `party_distribution`, `role_lookup`, `mixed`, `conversation_meta`, or `out_of_scope`
- **Completeness & grounding**: Uses LLM judges to verify evidence completeness and answer faithfulness
- **Checkpointing**: Persists agent state to Postgres for thread continuation
- **Observability**: Langfuse tracing (hard requirement at startup, fails open at runtime)

## Key components

| File | Responsibility |
|------|----------------|
| `agent_graph.py` | LangGraph definition, all five nodes, reflection loop, tool-calling logic |
| `main.py` | FastAPI app, `/query` endpoint, timeout handling with deterministic fallback |
| `mcp_client.py` | MCP client that connects to the mcp-server (supports stdio and SSE transport) |
| `grounding.py` | Deterministic regex/diff-based hallucination guard |
| `date_utils.py` | Wahlperiode reference detection, date expression extraction |
| `tracing.py` | Langfuse observability wrapper |
| `chat_store.py` | Postgres-based chat transcript persistence (separate from checkpointing) |


## Architecture at a Glance

```
START → supervisor → tool_selector → [reflection loop] → synthesis → [hallucination_guard loop] → END
```

`supervisor` classifies intent. `tool_selector` forces a real MCP tool call rather than letting the model answer from memory. `reflection` (toggleable via `ENABLE_REFLECTION`) judges whether the evidence gathered is actually sufficient, looping back if not. `synthesis` writes the answer strictly from tool JSON. `hallucination_guard` (toggleable via `ENABLE_HALLUCINATION_GUARD`) re-checks that answer against its evidence and can force one regeneration pass.

## Design Decisions

### 1. Only accepting a "declined" tool-choice answer if it's independently grounded

**The problem:** Setting `tool_choice="required"` doesn't always work, the model can still return plain text, which the provider rejects as a 400 error while embedding the model's intended answer inside it.

```python
decline_text = _extract_failed_generation(e)
if decline_text and tool_results:
    score = await grounding.check_grounded(decline_text, tool_results, llm=agent_system.llm)
    if score >= cfg["faithfulness_threshold"]:
        direct_answer = decline_text
        break
    # else: forced back into a real tool call, never surfaced ungrounded
```

This extracts the model's "wanted to say" text from the error, then only accepts it as an answer if it independently passes the same faithfulness check a normal synthesized answer would. This closes the obvious hole, accepting declined text unconditionally would let the model bypass tool-forcing entirely, while still giving credit for legitimate follow-ups whose answer already exists in prior evidence.

### 2 & 3. Two reflection-loop shortcuts, tried and reverted

**The problem:** Every reflection cycle costs a real LLM judge call, so a structural shortcut to skip it looked like free savings, twice.

**Attempt 1, skip the judge whenever exactly one tool call happened this turn.** This is the actual `reflection_node` code running today, with the reverted attempt left in as a comment directly above the line that replaced it:

```python
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
    # Token savings are not worth silently wrong answers; always judge
    # for these intents.
    conversation = _human_readable_transcript(_current_turn_messages(state["messages"]))
    payload = json.dumps({"conversation": conversation, "tool_results": state["tool_results"], ...})
    verdict = await _judge(agent_system.judge_llm, COMPLETENESS_RUBRIC, payload,
                            schema=CompletenessVerdict)
```

**Attempt 2, a deterministic pattern-match instead of an LLM judge.** This one was removed outright rather than replaced in place; the comment sits directly above the function that now handles completeness the safe way:

```python
# NOTE: The deterministic completeness short-circuit was removed.
# It incorrectly assumed a single tool call satisfied multi-entity
# queries (e.g. "compare WP 20 and 21"), preventing the LLM judge
# from triggering necessary reflection retries. The LLM judge
# handles completeness safely.

def load_agent_config(overrides: dict | None = None) -> dict:
    cfg = {
        "enable_reflection": os.getenv("ENABLE_REFLECTION", "true").lower() == "true",
        "enable_hallucination_guard": os.getenv("ENABLE_HALLUCINATION_GUARD", "true").lower() == "true",
        "max_reflection_iterations": int(os.getenv("MAX_REFLECTION_ITERATIONS", "3")),
        "faithfulness_threshold": float(os.getenv("FAITHFULNESS_THRESHOLD", "0.8")),
        "completeness_threshold": float(os.getenv("COMPLETENESS_THRESHOLD", "0.7")),
    }
    if overrides:
        cfg.update(overrides)
    return cfg
```

Both shortcuts assumed "one tool call happened" meant "the question is answered," which silently broke every multi-entity comparison query, the model's first pass reliably extracts only the first entity mentioned, and only a real judge call ever notices the second one is missing. Both were removed; the reflection judge now always runs unconditionally whenever there's tool evidence and the intent isn't `conversation_meta`/`out_of_scope` (the `verdict = await _judge(...)` line above, with no shortcut branch in front of it).

### 4. A deterministic gap-sweep so multi-entity comparisons don't run out of reflection budget

**The problem:** A 4-entity comparison ("compare WP 18, 19, 20, 21") could need four separate tool calls, discovering and fixing one gap per reflection round risks exhausting `MAX_REFLECTION_ITERATIONS` before every entity is fetched.

```python
for mc in (state.get("missing_calls") or []):
    mc_sig = (mc["tool"], json.dumps(mc.get("args", {}), sort_keys=True))
    if mc_sig in evidence_this_turn_after_call:
        continue
    gap_tool = next(t for t in tools if t.name == mc["tool"])
    raw_result = await gap_tool.ainvoke(mc["args"])
    tool_results.append({"tool": mc["tool"], "args": mc["args"], "output": output})
```

Once the completeness judge names every missing `(tool, args)` pair, this fetches all of them in one pass instead of one per reflection retry. Paired with a prompt-level nudge (`MULTI_ENTITY_TOOL_USE_HINT`) to batch calls in the first place, this is the actual fix for the "runs out of iterations" failure mode.

### 5. A dedup backstop for when the model just repeats an already-made call

**The problem:** Even after being told exactly what's missing, the model sometimes repeats the exact same tool call, a generic compliance failure.

```python
if sig in evidence_this_turn:
    next_gap = next((mc for mc in (state.get("missing_calls") or [])
                      if (mc["tool"], json.dumps(mc.get("args", {}), sort_keys=True)) not in evidence_this_turn), None)
    if next_gap is not None:
        call = {**call, "name": next_gap["tool"]}
        args = dict(next_gap["args"])
    else:
        # reuse prior result, don't stall waiting for compliance
```

If the model repeats a signature already fetched this turn, and the judge already identified a specific unfulfilled gap, the code substitutes that gap deterministically instead of waiting for another reflection round the model may never comply with.

### 6. Two-stage grounding: deterministic checks first, an LLM judge only where lexical checking can't work

**The problem:** Grounding needs to catch hallucinated numbers/roles (lexically checkable) and whether a caveat's *substance* was conveyed regardless of paraphrase or language (not lexically checkable).

```python
async def _note_tokens_covered(llm, answer, answer_lower, data_notes):
    if not _numbers_covered(answer_lower, data_notes):   # cheap, deterministic
        return False
    if llm is None:
        return True
    return await _note_covered_semantic(llm, answer, data_notes)   # only path that needs an LLM
```

Numbers and role-noun stems (`_ROLE_RE`, matched against DIP's abbreviated "bundeskanzl.") are checked with plain regex/set logic, free and deterministic. Only caveat-substance coverage goes to an LLM judge, and that stage is independently toggleable via `ENABLE_LLM_FALLBACK_GUARD`.

### 7. Stripping caveat text before role-matching, so a negative result can't ground a hallucination

**The problem:** A failed `get_persons_by_role` call's own message ("No person matching role 'X' was found") contains the literal role name, a naive containment check would let a hallucinated claim about role X falsely "ground" against that negative result.

```python
def _strip_note_keys(node):
    if isinstance(node, dict):
        return {k: _strip_note_keys(v) for k, v in node.items() if k not in _NOTE_KEYS}
    if isinstance(node, list):
        return [_strip_note_keys(item) for item in node]
    return node
```

This recursively removes `data_notes`/`note`/`caveat` fields before the JSON is used as role-matching evidence text, so a tool's own explanation of *why* it found nothing can never accidentally confirm the very claim it's refuting.

### 8. Reusing prior-turn evidence only when specific indices are verified.

**The problem:** Trusting a bare "is this cached evidence sufficient?" score without confirming *which* entries it applies to caused a real bug: a plain "WP 20" question once returned a chart mixing in unrelated WP 21/12/10 data from earlier in the conversation.

```python
if cache_verdict.score >= cfg["completeness_threshold"] and relevant_prior:
    # Only reuse cached evidence when the judge gave us SPECIFIC,
    # verified indices. A score/indices mismatch must fall through
    # to a real tool call -- never substitute the entire history.
    return {"tool_results": tool_results, "turn_tool_results": relevant_prior, ...}
```

A score/indices mismatch now falls through to a live tool call instead of silently reusing the full cross-turn history, the exact condition that caused the original bug.

### 9. Keeping LangGraph's internal state separate from the user-facing chat transcript

**The problem:** LangGraph's checkpointed `messages` state includes tool calls, retries, and regenerated drafts, exactly what a page reload should *not* show a user.

```sql
CREATE TABLE IF NOT EXISTS chat_turns (
    id BIGSERIAL PRIMARY KEY,
    thread_id UUID NOT NULL,
    turn_index INT NOT NULL,
    role TEXT NOT NULL CHECK (role IN ('user', 'assistant')),
    content TEXT NOT NULL,
    chart_data JSONB,
    ...
)
```

`chat_turns` is a separate, presentation-only table storing only the final answer and chart per turn, the one source of truth for what the UI shows, decoupled from the agent's internal reasoning state.

### 10. Salvaging partial evidence on timeout instead of discarding the whole turn

**The problem:** A turn that hits its own timeout would otherwise throw away any tool calls that already succeeded before the timeout fired.

```python
except asyncio.TimeoutError:
    snapshot = await state["graph"].aget_state({"configurable": {"thread_id": req.thread_id}})
    scoped_results = snapshot.values.get("turn_tool_results") or snapshot.values.get("tool_results", [])
    if scoped_results:
        answer = deterministic_fallback_answer(scoped_results)
```

LangGraph's checkpointer persists state after every node, so a successful tool call isn't lost, the timeout handler reads it back and returns a deterministic best-effort answer instead of a bare 504.

### 12. A proactive rate limiter sized below the provider's real ceiling

**The problem:** One turn fans out into several sequential LLM calls (intent, cache-sufficiency, tool invocation, completeness, synthesis), without throttling, that fan-out blows past a provider's RPM ceiling and the SDK's blind retry-with-backoff burns the request timeout budget.

```python
_LLM_RATE_LIMITER = InMemoryRateLimiter(
    requests_per_second=_LLM_RPM / 60,
    check_every_n_seconds=0.1,
    max_bucket_size=4,
)
```

Every LLM client this service builds shares this limiter, sized deliberately below the real ceiling so the provider's own 429s are avoided proactively rather than absorbed reactively.

### 15. Rotating the LLM API key without restarting the service

**The problem:** An expired or rate-limited key shouldn't require a redeploy just to swap in a new one.

```python
def _maybe_reload_env():
    mtime = os.path.getmtime(ENV_PATH)
    if mtime != _env_last_mtime:
        load_dotenv(ENV_PATH, override=True)
        new_key = os.getenv("LLM_API_KEY")
        if new_key and new_key != state.get("current_llm_api_key"):
            agent_system.llm = _build_llm(new_key)
```

Called at the top of every `/query` request, this polls `.env`'s mtime and hot-swaps the LLM client in place the moment the key actually changes, no restart needed.

### 16. Resolving "current" deterministically, never trusting the model's own sense of time

**The problem:** Models have a training cutoff and no reliable notion of today's date or who currently holds a given office, exactly the kind of thing they'll confidently answer wrong.

```python
_WAHLPERIODE_START_DATES = {..., 20: date(2021, 10, 26), 21: date(2025, 3, 25)}

def resolve_current_wahlperiode(today=None):
    today = today or datetime.now(timezone.utc).date()
    candidates = [wp for wp, start in _WAHLPERIODE_START_DATES.items() if start <= today]
    return max(candidates)
```

This resolved value is injected as a system-level fact into every tool-selection call: *"never guess a Wahlperiode number from memory... never name the person who holds a role from your own knowledge."*

### 17. Typed judge outputs instead of free-text, so the gap-sweep in Decision 4 can act on them directly

**The problem:** If the completeness judge returned prose instead of structure, acting on "what's missing" would need another parsing step, possibly another LLM call.

```python
class MissingToolCall(BaseModel):
    tool: str
    args: dict

class CompletenessVerdict(BaseModel):
    score: float
    rationale: str
    missing_calls: list[MissingToolCall] = []
```

Every judge in the graph (`JudgeVerdict`, `CompletenessVerdict`, `CacheSufficiencyVerdict`) returns a typed Pydantic schema via `.with_structured_output()`. This is what makes Decision 4's gap-sweep possible without an extra round-trip, the code just iterates `missing_calls` directly.

## Configuration Philosophy, "Options Are Fine, As Long As They're Explained"

| Flag | What it changes | Why you'd turn it off | Default and why |
|---|---|---|---|
| `ENABLE_REFLECTION` | Whether the completeness-judging loop (Decisions 2–4) runs at all. | To save judge-call latency/cost, accepting that multi-entity queries may return incomplete answers. | **On.** Two reverted shortcuts around this both caused silently wrong comparison answers. |
| `ENABLE_HALLUCINATION_GUARD` | Whether the post-synthesis grounding check (Decision 6) and its regeneration pass run. | To save grounding-check latency/cost in a lower-stakes context. | **On.** This is the last line of defense against the core failure mode the system exists to prevent. |
| `ENABLE_LLM_FALLBACK_GUARD` | Whether Stage 2 of the faithfulness cascade (Decision 6's semantic caveat check) runs on top of the always-on deterministic checks. | To reproduce pure lexical grounding, faster, fully deterministic, no LLM cost. | **On.** Caveat coverage is a paraphrase/translation problem a lexical check structurally cannot verify. |

## What I'd Do Differently

- Both reverted reflection-loop shortcuts (Decisions 2–3) cost real time to discover were wrong. I'd write the multi-entity comparison test case *before* attempting either one.
- `_WAHLPERIODE_START_DATES` (Decision 16) is a static table needing manual updates after every real election, a more robust version would resolve the current Wahlperiode from a live DIP endpoint instead of a maintained constant.
