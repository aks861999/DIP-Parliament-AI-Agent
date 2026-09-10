# agent-app, Design Decisions & Engineering Notes

## Overview

`agent-app` is the Streamlit chat frontend for `dip-agent`. It's deliberately thin: it renders the conversation, posts queries to `agent-service`'s `/query` endpoint, renders whatever chart payload comes back, and owns exactly one piece of its own state, a lightweight thread-metadata table, while the actual message content and agent reasoning state live entirely in `agent-service` and its own Postgres tables. Most of the genuinely hard engineering in this system (grounding, reflection, caching) happens upstream; what's below is what this specific layer had to get right.

## Architecture at a Glance

```
Streamlit session ──▶ POST /query ──▶ agent-service ──▶ (returns answer + optional chart_data)
                  ──▶ GET /threads/{id}/messages ──▶ agent-service (page-reload history)
Local: chat_threads table in Postgres (title, timestamps), separate from agent-service's chat_turns table on the same database.
```

## Engineering Decisions

### 1. Frontend HTTP client timeout deliberately exceeds the backend's own SLA

```python
@st.cache_resource(show_spinner=False)
def _get_http_client() -> httpx.Client:
    # Must exceed the backend's AGENT_QUERY_TIMEOUT_SECONDS (180s) --
    # otherwise the frontend gives up and shows a false "backend timed
    # out" error while the backend is still legitimately retrying a
    # TPM-limited LLM call in the background.
    return httpx.Client(base_url=AGENT_SERVICE_URL, timeout=200.0)
```

**The problem:** `agent-service` enforces its own 180-second timeout and, on hitting it, tries to salvage a best-effort answer from whatever evidence it already fetched rather than failing outright. If the frontend's own timeout were set equal to (or shorter than) that, it would abandon the request and show the user a raw connection error *before* the backend even reaches its own graceful-degradation path, turning a recoverable situation into a hard failure purely due to a client-side timing mismatch.

### 2. No authentication, no per-user thread scoping

```python
with st.sidebar:
    st.subheader("Conversations")
    st.caption("Public demo, every visitor can see every thread here.")
```

**The problem:** Every thread in `chat_threads` is visible to every visitor; there's no user identity anywhere in the system to scope by. Stated explicitly in the UI copy rather than silently, this is a demo-scope decision.

### 3. Chart rendering normalizes both the current structured payload and an older single-distribution shape

```python
if isinstance(chart_data, dict) and chart_data.get("type") == "party_distribution":
    distributions = chart_data.get("distributions") or []
    chart_type = chart_data.get("chart_type", "bar")
elif isinstance(chart_data, dict) and "counts" in chart_data:
    # Legacy single-distribution row from the DB
    distributions = [chart_data]
    chart_type = chart_data.get("chart_type", "bar")
else:
    distributions = [chart_data] if chart_data else []
    chart_type = "bar"
```

**The problem:** `chart_data` is persisted in Postgres and reloaded on every page visit. If the backend's payload shape ever changes (as it did, a single-distribution dict became a `{"type": ..., "distributions": [...]}` wrapper supporting N>1 comparisons), older rows already saved under the previous shape would break rendering on reload. This branch accepts both shapes rather than requiring a data migration for every historical chart.

### 4. Loading chat history exactly once per thread, with the thread ID carried in the URL

```python
query_params = st.query_params
if "thread_id" not in st.session_state:
    if "thread" in query_params:
        st.session_state.thread_id = query_params["thread"]
        st.session_state.thread_persisted = True
        st.session_state.chat_messages = []
        st.session_state.loaded_from_db = False
    else:
        st.session_state.thread_id = str(uuid.uuid4())
        st.session_state.loaded_from_db = True

if not st.session_state.get("loaded_from_db", False):
    try:
        db_msgs = get_thread_messages(st.session_state.thread_id)
        st.session_state.chat_messages.extend(db_msgs)
    except httpx.HTTPError:
        st.warning("Couldn't load this conversation's history, starting fresh from here.")
    st.session_state.loaded_from_db = True
```

**The problem:** Streamlit re-executes the whole script on every interaction. Without the `loaded_from_db` guard, every rerun would re-fetch and re-append the same history, duplicating every message in the displayed chat. Storing the thread ID in `st.query_params` also means a reload or a shared link resumes the same conversation instead of silently starting a new one.

### 5. Backend failures degrade to a plain-language message.

```python
try:
    result = run_query(query, st.session_state.thread_id)
    answer = result["answer"]
    chart_data = result.get("party_distribution")
    thinking_log = result.get("thinking_log") or []
except httpx.HTTPError:
    logger.exception("agent-service request failed")
    answer = "Sorry, the backend took too long to respond. Please try again."
    chart_data = None
    thinking_log = []
```

**The problem:** Any `agent-service` failure (timeout, 5xx, connection drop) would otherwise surface as an unhandled exception in the Streamlit UI. This catches it at the one call site that matters and shows a plain, actionable message instead, while still logging the real exception server-side for debugging.

### 6. Thread metadata and the full chat transcript both live in Postgres, as separate tables

```python
DATABASE_URL = os.getenv("DATABASE_URL")

def _get_connection() -> psycopg.Connection:
    return psycopg.connect(DATABASE_URL, autocommit=True, row_factory=psycopg.rows.dict_row)

def init_db() -> None:
    with _get_connection() as conn:
        conn.execute("""CREATE TABLE IF NOT EXISTS chat_threads (
            thread_id UUID PRIMARY KEY,
            title TEXT,
            created_at TIMESTAMPTZ NOT NULL,
            updated_at TIMESTAMPTZ NOT NULL
        )""")
```

**The problem:** The sidebar needs a fast, lightweight list of threads (ID, title, last-updated); the full message transcript with roles, chart data, and grounding scores is a heavier, differently-shaped record. `chat_threads` here and `chat_turns` in `agent-service` are two tables on the *same* Postgres instance (same `DATABASE_URL`), keeping the schemas independent avoided coupling this thin frontend to the transcript table's shape.

## Configuration

| Variable | What it's for |
|---|---|
| `AGENT_SERVICE_URL` | Base URL for the backend (`http://agent-service:8000` in Docker, overridable natively). |
| `DATABASE_URL` | Same Postgres instance `agent-service` uses, required for `chat_threads`. |

## What I'd Do Differently

- The 200s/180s timeout margin (Decision 1) is a manually-chosen constant tied to `agent-service`'s own `AGENT_QUERY_TIMEOUT_SECONDS`, if that backend value ever changes, this frontend constant has to be remembered and updated in lockstep; there's no single shared source of truth for it today.
