# DIP Parliamentary Agent

A conversational agent over the German Bundestag's DIP parliamentary data API. Users ask questions in natural language about politicians' biographical information, party affiliations, and legislative composition, and the agent retrieves structured data from DIP and generates natural-language answers.

## Architecture

The system consists of three services communicating over well-defined boundaries:

```
┌─────────────────┐     ┌─────────────────┐     ┌─────────────────┐
│   agent-app     │────▶│ agent-service   │────▶│   mcp-server    │
│  (Streamlit)    │     │  (FastAPI)      │     │    (FastMCP)    │
│   Port 8501     │     │   Port 8000     │     │   Port 8000     │
└─────────────────┘     └─────────────────┘     └─────────────────┘
                               │                        │
                               ▼                        ▼
                        ┌──────────┐            ┌──────────┐
                        │ Postgres │            │  Redis   │
                        │  :5432   │            │  :6379   │
                        └──────────┘            └──────────┘
```

### Why these service boundaries?

1. **mcp-server** is the data-access boundary. It's the only service that talks HTTP to DIP, holds all caching logic, and computes party percentages deterministically in Python. The LLM never calculates percentages, it only narrates what Python computed.

2. **agent-service** is the orchestration layer. It runs the LangGraph agent, manages checkpointing to Postgres, handles reflection/completeness loops, and enforces hallucination grounding. Separating this from the data layer means the MCP tools can be swapped or stubbed without touching agent logic.

3. **agent-app** is the presentation layer. It's a Streamlit chat UI with no authentication (intentional, it's a public demo). The frontend is deliberately thin; all intelligence lives in agent-service.

### Transport switching

The MCP server supports two transports:
- **stdio**: Used when agent-service spawns mcp-server as a subprocess (native development)
- **SSE**: Used in Docker when services communicate over HTTP

This enables the same codebase to run natively via shell scripts or in containers via Docker Compose, without changing agent logic.

---

## Setup

### Prerequisites

- Python 3.11+
- PostgreSQL instance (or `docker compose up postgres`)
- Redis instance (or `docker compose up redis`)
- Groq API key (or any OpenAI-compatible LLM)
- DIP API key (required, DIP returns 401 without it)
- Langfuse key pair (required at startup)



### Docker Compose (production deliverable)

```bash
docker compose up --build
```

Open http://localhost:8501.



# mcp-server, Design Decisions & Notes

## Overview

MCP (Model Context Protocol) server providing tools for the DIP Parliamentary Agent:
- `get_person_info`: Biographical and party information for a named individual
- `get_party_distribution`: Aggregate party composition for a Wahlperiode
- `get_persons_by_role`: Who holds a specific role/title in a Wahlperiode

## What this service is responsible for

- **Data access layer**: The only service that talks HTTP to the DIP API
- **Caching**: Redis-backed cache-aside layer for DIP responses
- **Name directory**: Cached crawl of all person names for fast lookup
- **Deterministic computation**: Party percentages are computed in Python, not by the LLM

## Key components

| File | Responsibility |
|------|----------------|
| `mcp_server.py` | FastMCP server, tool implementations |
| `dip_client.py` | DIP API client with throttling, retry, and single-flight dedup |
| `cache.py` | Generic Redis-backed cache-aside abstraction |
| `tool_contracts.py` | Pydantic models for tool inputs/outputs |
| `wahlperiode_utils.py` | Wahlperiode-to-date mapping |
| `diagnose_person_list_vs_id.py` | Diagnostic script to verify list vs. single-item data completeness |

## Architecture at a Glance

Three MCP tools, each wrapping paginated/cached calls into `DipClient`. Caching happens at three tiers with independently-tunable TTLs: raw HTTP responses inside `DipClient`, tool-output results inside `mcp_server.py`, and a full crawled name-directory cache. The service runs as either a `stdio` subprocess (native dev) or a networked `sse` service (Docker Compose).

## Design Decisions

### 1. Empirically verifying an assumption about DIP's API, via a purpose-built diagnostic script

**The problem:** The original design assumed DIP's bulk list endpoint (`GET /person`) returns a truncated `person_roles` history compared to the single-item endpoint (`GET /person/{id}`), an assumption never actually tested.

```python
list_response = await dip._get("/person", {"f.id": person_id})
list_person = list_response.get("documents", [])[0]
single_person = await dip.get_person(person_id)

comparisons = [_diff_field(field, list_person.get(field), single_person.get(field))
               for field in FIELDS_TO_COMPARE]   # funktion, fraktion, wahlperiode, person_roles, titel
```

`diagnose_person_list_vs_id.py` fetches both endpoints live for real people with rich, multi-Wahlperiode histories (Merkel, Merz, von der Leyen) and diffs every field that matters. The result: identical data in both modes. The original truncation assumption was wrong, directly enabling Decision 2.

### 2. Eliminating a redundant second API call per person lookup

**The problem:** The two-call design (cheap directory hit + an always-executed "authoritative" second call) was doing unnecessary work on every lookup, once Decision 1 proved the second call added nothing.

```python
# Store the FULL raw person record, not a sparse subset.
# Confirmed via diagnose_person_list_vs_id.py: GET /person (list)
# already returns the exact same funktion/fraktion/person_roles/
# wahlperiode data as GET /person/{id} -- discarding it down to
# 4 fields was throwing away data unnecessarily.
entry = dict(person)
```
```python
# entry IS the full person record now -- confirmed via
# diagnose_person_list_vs_id.py that list-mode data is complete,
# so the second live GET /person/{id} call is redundant and removed.
person = entry
result = PersonInfoResult(person=person, matches=[],
                          resolved_fraktion=resolve_current_person_fraktion(person))
```

Every lookup that hits the directory now costs zero additional live DIP requests, down from one, a real, measurable saving against a rate-limited government API.

### 3. A full-directory crawl-and-cache design instead of a live search-per-query

**The problem:** Resolving a user-typed name against DIP's records needs tolerance for typos and alternate spellings, calling DIP's own search per query puts match quality and latency entirely in DIP's hands.

```python
async def build_name_directory(self, max_pages: int = 5000) -> dict[str, dict]:
    directory = {}
    cursor = None
    while True:
        page = await self.search_all_persons(cursor)
        documents = page.get("documents", [])
        for person in documents:
            entry = dict(person)
            full_key = " ".join(part for part in (vorname, namenszusatz, nachname) if part).strip().lower()
            if full_key:
                directory[full_key] = entry
        next_cursor = page.get("cursor")
        if not documents or next_cursor is None or next_cursor == cursor:
            break
        cursor = next_cursor
    return directory
```

The whole directory is crawled once and cached in Redis, indexed under both a full name key and a shorter key without `namenszusatz` (so "Ursula von der Leyen" resolves via either "ursula von der leyen" or "ursula leyen"). Every lookup after that is instant and fully under local control.

### 4. Deterministic local lookup with fuzzy-match suggestions, replacing a previous "multiple exact matches" design

**The problem:** Once lookup became a deterministic dictionary key (Decision 3), the old concept of "DIP's search returned several ambiguous hits" no longer applied, a key either exists or it doesn't.

```python
def suggest_person_names(query: str, directory: dict[str, dict],
                          limit: int = 3, cutoff: float = 0.72) -> list[dict]:
    close_keys = difflib.get_close_matches(query.strip().lower(), directory.keys(),
                                            n=limit, cutoff=cutoff)
    return [directory[k] for k in close_keys]
```

On a directory miss, this replaces the old `matches` disambiguation concept with local, typo-tolerant `suggestions`, "Ana Mueler" successfully suggests "Anna Mueller", entirely under this codebase's control rather than dependent on DIP's own search behavior.

### 5. Background prewarming with a fail-safe fallback to lazy building

**The problem:** Directory crawling (Decision 3) is expensive; without prewarming, whichever real request happens first after a cache miss absorbs the full crawl latency.

```python
async def _prewarm_name_directory():
    try:
        await _get_name_directory()
        logger.info("name directory pre-warmed")
    except Exception:
        logger.exception("name directory pre-warm failed, will lazy-build on first miss instead")

if PREWARM_ON_STARTUP:
    asyncio.get_event_loop().create_task(_prewarm_name_directory())
```

Prewarming runs as a fire-and-forget background task at startup, and its own failure is swallowed rather than blocking service startup, a DIP outage at boot shouldn't prevent the service from coming up at all. `PREWARM_ON_STARTUP=false` in CI's live-api-check context skips the eager crawl where it isn't worth the time/rate-limit cost.

### 7. A cheap pre-filter before an expensive per-person backfill

**The problem:** Role lookups need period-scoped role data often missing from sparse list rows, but a full per-person backfill is one DIP API call each, unaffordable across 500+ people in a Wahlperiode.

```python
if not _has_period_scoped_roles(candidate, wahlperiode):
    if not any(_role_matches(target_norm, c) for c in _funktion_candidates(candidate)):
        continue          # skip the backfill entirely -- no plausible signal at all
    try:
        candidate = await dip.get_person(p["id"])
        backfill_count += 1
    except Exception:
        logger.exception("failed to backfill person_roles for person %s", p.get("id"))
        continue
```

`_funktion_candidates()` cheaply pre-filters using whatever funktion signal is already visible in the sparse row before paying for the expensive backfill; period-correctness is only verified afterward, against the full record.



### 8. Documenting a real DIP API limitation instead of silently ignoring or rejecting a parameter

**The problem:** `get_party_distribution` accepts a `date_range`, but DIP's API has no role-tenure-scoped date filter at all, the parameter can't actually narrow the aggregation.

```python
if date_range is not None:
    note = ("Note: date_range was supplied but the underlying DIP API has no "
            "role-tenure-scoped date filter, so this result reflects the full "
            "Wahlperiode, not the specific date range.")
    distribution.data_notes = ((distribution.data_notes or "") + " " + note).strip()
```

The tool's own MCP description states this limitation explicitly to the calling LLM, and every response that supplies `date_range` carries the caveat forward, honest at both the point of tool selection and the point of presentation, rather than a parameter that silently does nothing.

### 9. Raising a hard error instead of returning an empty distribution

**The problem:** A Wahlperiode with zero returned person records almost certainly indicates a fetch problem, not a genuine "no parliamentarians" fact.

```python
if total == 0:
    raise DipClientError(f"no person records found for wahlperiode={wahlperiode}")
```

A silently-returned all-zero distribution risks being confidently presented as a real finding. A distinct exception forces the calling code to handle this as the data problem it actually is.

### 10. A cache-aside abstraction, decoupled from Redis specifics and fail-open on any backend error

**The problem:** Every cache call site needs a policy for what happens if Redis itself is unavailable.

```python
async def get(self, key: str) -> Any | None:
    try:
        raw = await self._redis.get(key)
    except Exception:
        logger.exception("cache backend unavailable on GET %r; treating as miss", key)
        return None
```

A cache outage degrades to "always call the live API," never a crashed request. `Cache` is also domain-agnostic, a plain key/value store with TTLs, so `DipClient` depends on this interface, not Redis directly.

### 11. Jittered cache TTLs to avoid a synchronized cache-stampede

**The problem:** A batch of related entries written together (e.g., during a directory crawl) would all expire at nearly the same instant with a fixed TTL, causing a burst of simultaneous live-API calls.

```python
async def set(self, key: str, value: Any, ttl_seconds: int | None = None) -> None:
    base_ttl = ttl_seconds or self.default_ttl_seconds
    jittered_ttl = int(base_ttl * random.uniform(0.9, 1.1))  # ±10%
    try:
        await self._redis.set(key, json.dumps(value), ex=jittered_ttl)
    except Exception:
        logger.exception("cache backend unavailable on SET %r; continuing without caching", key)
```

A ±10% randomization spreads expirations out over time instead of clustering, cheap, and strictly beneficial given this service's own batch-write patterns.

### 12. Single-flight request deduplication for concurrent identical cache misses

**The problem:** Concurrent tool calls fanning out for the same uncached key would otherwise each independently hit the network for identical data.

```python
lock = self._inflight.setdefault(cache_key, asyncio.Lock())
if lock.locked():
    logger.info("piggybacking on in-flight request for %s (single-flight dedup)", cache_key)
async with lock:
    ...
finally:
    # only remove the entry if no OTHER concurrent caller is currently waiting on it
    if cache_key in self._inflight and not self._inflight[cache_key].locked():
        del self._inflight[cache_key]
```

A per-cache-key lock means only the first concurrent caller reaches the network; the rest wait and share its result. The cleanup ordering is deliberate, checking `.locked()` right after release avoids both a lock leak and a race where a waiting caller loses its lock.

### 13. Client-side network throttling decoupled from any specific caller

**The problem:** Multiple code paths (pagination, retries, concurrent tool calls) can each originate a live DIP request, and every one needs to respect a minimum spacing to avoid tripping rate limits.

```python
async def _throttle(self) -> None:
    async with self._request_lock:
        elapsed = time.monotonic() - self._last_request_at
        wait = self.min_request_interval_seconds - elapsed
        if wait > 0:
            await asyncio.sleep(wait)
        self._last_request_at = time.monotonic()
```

Placed inside `_get()`, the one function every real DIP request must pass through, so every current and future call path automatically respects the throttle without its own copy of the logic.

### 14. Distinguishing rate-limit/challenge responses from generic network failures

**The problem:** A 429/challenge response and a generic connection timeout need different handling, the former may carry a server-provided wait time.

```python
except httpx.HTTPStatusError as e:
    if resp.status_code == 429 or resp.is_redirect:
        delay = min(2 ** attempt + random.uniform(0, 1), 30)
        retry_after = resp.headers.get("Retry-After")
        if retry_after:
            delay = max(delay, float(retry_after))
        await asyncio.sleep(delay)
        continue
    raise
except (httpx.ConnectError, httpx.ReadTimeout, httpx.ConnectTimeout) as e:
    delay = min(2 ** attempt + random.uniform(0, 1), 30)
    await asyncio.sleep(delay)
    continue
```

A server-provided `Retry-After` is honored when present; generic network failures fall back to exponential backoff alone. Both cap at 5 attempts / 30s max, so a tool call an agent is waiting on always fails within a bounded time.

### 15. Three independently-tunable cache TTLs for three different volatility profiles

**The problem:** Raw person records, tool-level outputs, and the full name directory have meaningfully different real-world volatility and rebuild costs.

```python
async def search_all_persons(self, cursor: str | None = None) -> dict:
    return await self._get("/person", {"cursor": cursor}, ttl_seconds=86400)  # 24h, rarely changes

async def get_person(self, person_id: str) -> dict:
    return await self._get(f"/person/{person_id}", {}, ttl_seconds=86400)
```

Raw responses get a fixed 24h TTL inside `DipClient`; tool-output caching uses the separately-configured `MCP_CACHE_TTL_SECONDS`; the full directory uses its own `NAME_DIRECTORY_TTL_SECONDS`, reflecting that it's both the least volatile and the most expensive of the three to rebuild.

## Configuration Philosophy, "Options Are Fine, As Long As They're Explained"

| Setting | What it changes | Why you'd choose differently | Default and why |
|---|---|---|---|
| `PREWARM_ON_STARTUP` | Whether the full directory crawl (Decision 3) runs eagerly at startup vs. lazily on first miss. | `false` where an eager crawl isn't worth the time/rate-limit cost before the service is needed (e.g. CI). | **`true`** in normal deployment, pays the crawl cost once, predictably, with a fail-safe to lazy-build (Decision 5). |
| `MCP_CACHE_TTL_SECONDS` / `NAME_DIRECTORY_TTL_SECONDS` | How long tool-output vs. the full directory stay cached (Decision 17). | Lower for fresher data; raise to cut DIP load further. | Independently tunable since tool-output and full-directory staleness tolerance are genuinely different operational decisions. |

## What I'd Do Differently

- Decision 2's redundant-call elimination is only as durable as DIP's list-endpoint completeness staying stable, there's no ongoing check that would catch DIP silently changing that behavior later.
- The throttling in Decision 13 is per-process, not distributed, scaling this service horizontally would need a shared (e.g. Redis-backed) rate limiter to keep the same guarantee across instances.




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

**The problem:** Even after being told exactly what's missing, the model sometimes repeats the exact same tool call, a generic compliance failure, not something specific to one prompt.

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

### 8. Reusing prior-turn evidence only when specific indices are verified, not just a score

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

- Both reverted reflection-loop shortcuts (Decisions 2–3) cost real time to discover were wrong. I'd write the multi-entity comparison test case *before* attempting either one, not after the regression surfaced.
- `_WAHLPERIODE_START_DATES` (Decision 16) is a static table needing manual updates after every real election, a more robust version would resolve the current Wahlperiode from a live DIP endpoint instead of a maintained constant.




---
### Testing

#### Two-tier CI evaluation gate

**1. `agent-eval-gate` (BLOCKING)**
- Runs against mocked stub (`stub_mcp_server.py`)
- No external API keys needed
- Tests agent behavior: tool selection + grounding
- Fails build if accuracy < threshold

**2. `live-api-check` (NON-BLOCKING, `continue-on-error`)**
- Hits real DIP API via real `mcp_server.py`
- Needs DIP_API_KEY as repo secret
- Fails only on transport errors, never on content

**Problem this solves:** "Did the agent's logic regress?" is decoupled from "Is the live upstream unreliable?" A DIP outage shouldn't block merges, but a logic regression should.

**Tradeoff:** Two separate jobs to maintain. Worth it for resilience.


---

## Testing

```bash
# Unit tests (fast, mocked)
pytest tests -v -m unit

# Integration tests (needs Postgres)
pytest tests -v -m integration
```

---

## Environment Variables

| Variable | Required | Description |
|----------|----------|-------------|
| `LLM_API_KEY` | Yes | Groq or OpenAI-compatible API key |
| `LLM_MODEL` | Yes | Model name |
| `LLM_BASE_URL` | Yes | API base URL |
| `LANGFUSE_PUBLIC_KEY` | Yes | For observability |
| `LANGFUSE_SECRET_KEY` | Yes | For observability |
| `DIP_API_KEY` | Yes | For DIP API access |
| `DATABASE_URL` | Yes | PostgreSQL connection string |
| `REDIS_URL` | Yes | Redis connection string |
| `MCP_TRANSPORT` | No | `stdio` (default) or `sse` |
