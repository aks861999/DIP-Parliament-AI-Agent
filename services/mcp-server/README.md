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
