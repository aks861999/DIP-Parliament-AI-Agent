
# 2026-09-13 - Multi-Wahlperiode retrieval, date resolution & caching fixes

## 1. Couldn't retrieve full-history data ("which year was CDU highest?")

**Problem:** `get_party_distribution` only accepted one `wahlperiode: int` at a time. A full-history question needed 21 separate calls; the reflection loop capped out after 2-3, so answers were based on 3 of 21 Wahlperioden.

**Fix:** Unified the tool to accept a list, using the DIP API's repeatable `f.wahlperiode` OR-filter (confirmed in the OpenAPI spec) to fetch multiple Wahlperioden in **one** crawl:
```python
async def get_party_distribution(wahlperioden: list[int] | None = None) -> PartyDistributionHistory:
    wps = wahlperioden or list(range(1, 22))
    ...
```

## 2. Date questions resolved to the wrong Wahlperiode (WP21 instead of WP20)

**Problem:** Asking "as of 12th Dec 2024, which party..." was answered using WP21 (wall-clock "current") instead of WP20 (the date's actual period). The log's tell was `♻️ Reused ... no new API call needed` - the request never reached a tool call at all. Root cause: three separate places (cache-reuse judge, tool-argument builder, completeness judge) trusted an LLM's "current Wahlperiode" framing instead of the deterministic `resolve_wahlperiode_from_date()` resolver already in the codebase. One of the three (the tool-argument builder) had an additional nesting bug from an earlier edit that silently disabled the resolver for `get_party_distribution` specifically.

**Fix - a generic two-layer pattern, not a one-off date patch:**
- **Layer 1 (proactive):** compute any deterministically-knowable fact for the turn and hand it to the judge as an authoritative constraint, not a hint.
- **Layer 2 (defensive veto):** after the judge responds, discard any reused evidence that contradicts the known fact, regardless of what the judge said.
```python
if known_wp_for_turn is not None:
    filtered = [tr for tr in relevant_prior if _matches_known_wp(tr)]
    if not filtered:
        cache_verdict.score = 0.0
```
This generalizes to any future deterministic resolver (name→ID, role→funktion, etc.) - compute the fact, veto anything that disagrees, instead of trusting an LLM to honor a hint buried in prose. Applied identically in all three locations that previously relied on the judge's own "current" framing.

**Note:** this never touched the Redis data cache in `mcp-server` - only the agent's own conversation-level "is prior evidence still relevant" judgment, which behaves identically for the 99% of cases where dates aren't ambiguous.

## 3. Classifier randomly said "out of scope" for valid questions

**Problem:** The judge LLM (`judge_llm`) had no `temperature` set, so identical questions occasionally misclassified as `out_of_scope`. Persisted even after pinning `temperature=0.0` (residual MoE-batching nondeterminism).

**Fix:** Added a deterministic keyword veto in `supervisor_node` - an `out_of_scope` verdict gets overridden to `party_distribution` if the message contains unambiguous domain terms (party names, "Bundestag," "Wahlperiode," etc.), regardless of what the classifier said.

## 4. Synthesis refused to answer, calling "population" an invalid field

**Problem:** Under stricter regeneration instructions, the model treated the user's colloquial word "population" as needing a literal JSON field match, and refused to answer instead of mapping it to `counts`.

**Fix:** Added a clarifying line to `SYNTHESIS_PROMPT` telling it to interpret colloquial terms naturally and only ground the *numbers*, not the vocabulary.

## 5. Charts rendered as a blank Plotly skeleton

**Problem:** `_build_chart_data()` still read the old flat `{"wahlperiode": ..., "counts": ...}` shape; the new tool returns a nested `{"distributions": [...]}`, so every chart entry collapsed to `None`.

**Fix:** Flattened `output["distributions"]` per Wahlperiode before building the chart payload.

## 6. Asking for "20 and 21" refetched WP20 live even though it was cached

**Problem:** Redis cached by the *whole* request signature (`party_dist:[20]` vs `party_dist:[20, 21]`) - a combined request was always a cache miss, even if part of it was already cached (in this thread, another thread, or minutes/hours earlier).

**Fix:** Cache per individual Wahlperiode (`party_dist:20`, `party_dist:21`); only the genuinely missing ones hit the live API.

## 7. Reusing one Wahlperiode's evidence dragged in an unrelated one on the chart

**Problem:** A combined `[20, 21]` call was stored as **one** `tool_results` entry. Later reusing "the WP21 part" of that entry for a WP21-only question still carried WP20 along with it into the chart.

**Fix:** Decompose multi-Wahlperiode tool results into one `tool_results` entry per Wahlperiode at storage time:
```python
def _decompose_tool_result(call_name, args, output) -> list[tuple[dict, object]]:
    if call_name != "get_party_distribution" or not isinstance(output, dict):
        return [(args, output)]
    ...
```

## 8. Tool-call dedup missed partial overlaps after the item-7 fix

**Problem:** The duplicate-call detector compared a proposed call's *combined* signature (e.g. `wahlperioden: [20, 21]`) against `turn_tool_results`, which item 7 now stores as *decomposed* single-Wahlperiode entries. An exact repeat of a combined call no longer matched anything, so it fell through to a real (if now cheap, thanks to item 6) re-fetch instead of being caught as a duplicate - and a *partial* overlap (e.g. WP20 already fetched, WP21 not) wasn't narrowed at all.

**Fix:** Decompose the *candidate* call's args the same way before comparing, then narrow the live call to only the genuinely-missing sub-parts and merge in the reused ones afterward:
```python
sub_sigs = [(call_name, json.dumps(a, sort_keys=True)) for a in _decompose_call_args(call_name, args)]
missing = [a for a, s in zip(sub_args_list, sub_sigs) if s not in evidence_this_turn]
# fully covered -> reuse everything; partially covered -> fetch only `missing`, merge the rest back in
```

## Result

- Full-history and multi-Wahlperiode comparison questions now resolve in one tool call instead of exhausting the reflection loop.
- Date-based questions consistently resolve to the correct Wahlperiode across cache-reuse, tool-calling, and completeness checks.
- Classifier misfires no longer silently refuse valid questions.
- Charts and cache reuse are scoped per Wahlperiode, not per whole request.
- Duplicate and partially-overlapping tool calls are caught and narrowed at the same per-Wahlperiode granularity as everything else.
