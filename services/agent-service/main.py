import asyncio
import contextlib
import json
import logging
import os
from pathlib import Path
from types import SimpleNamespace

import chat_store
from agent_graph import (
    ModelDegenerateResponseError,
    create_agent_graph,
    deterministic_fallback_answer,
    run_agent_query,
)
from dotenv import load_dotenv
from fastapi import Depends, FastAPI, Header, HTTPException
from langchain_core.rate_limiters import InMemoryRateLimiter
from langchain_openai import ChatOpenAI
from mcp_client import start_mcp_session, stop_mcp_session
from psycopg_pool import AsyncConnectionPool
from pydantic import BaseModel
from tracing import flush

_file_parents = Path(__file__).resolve().parents
PROJECT_ROOT = _file_parents[2] if len(_file_parents) > 2 else _file_parents[-1]
ENV_PATH = PROJECT_ROOT / ".env"
load_dotenv(ENV_PATH)  # no-op inside Docker where this file doesn't exist —
                        # docker-compose already injects env vars directly

_env_last_mtime = 0
try:
    _env_last_mtime = os.path.getmtime(ENV_PATH)
except Exception:  # noqa: S110 -- optional at startup; _maybe_reload_env()
    # re-stats and logs a warning on every subsequent poll, so a missed
    # stat here (e.g. no .env file present at all, as in Docker) just
    # means the first reload check runs once with mtime=0. Can't log
    # here: `logger` isn't configured yet at this point in the module.
    pass




def _build_judge_llm(api_key: str) -> ChatOpenAI:
    return ChatOpenAI(
        model=LLM_MODEL,              
        api_key=api_key,
        base_url=LLM_BASE_URL,          
        rate_limiter=_LLM_RATE_LIMITER,
        max_retries=int(_get_required_env("LLM_MAX_RETRIES")),
        timeout=60.0,
        temperature=0.0,
    )



def _maybe_reload_env():
    """Check if .env has been modified and dynamically swap the LLM client."""
    global _env_last_mtime
    try:
        mtime = os.path.getmtime(ENV_PATH)
        if mtime != _env_last_mtime:
            _env_last_mtime = mtime
            load_dotenv(ENV_PATH, override=True)
            new_key = os.getenv("LLM_API_KEY")
            agent_system = state.get("agent_system")
            if new_key and agent_system and new_key != state.get("current_llm_api_key"):
                agent_system.llm = _build_llm(new_key)
                agent_system.judge_llm = _build_judge_llm(new_key)
                state["current_llm_api_key"] = new_key
                logger.info("Detected change in .env. Successfully reloaded LLM_API_KEY.")
    except Exception as e:
        logger.warning("Failed to check/reload .env file: %s", e)



_LOG_FILE = PROJECT_ROOT / "dip_agent.log"
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [agent-service] %(levelname)s:%(name)s: %(message)s",
    handlers=[logging.StreamHandler(), logging.FileHandler(_LOG_FILE, mode="a", encoding="utf-8")],
)
logger = logging.getLogger(__name__)


def _get_required_env(name: str) -> str:
    value = os.getenv(name)
    if not value:
        raise ValueError(f"required environment variable {name} is not set")
    return value



APP_SHARED_SECRET = _get_required_env("APP_SHARED_SECRET")

def _verify_secret(x_app_secret: str = Header(...)):
    if x_app_secret != APP_SHARED_SECRET:
        raise HTTPException(status_code=401, detail="unauthorized")





LLM_API_KEY = _get_required_env("LLM_API_KEY")
LLM_BASE_URL = _get_required_env("LLM_BASE_URL")
LLM_MODEL = _get_required_env("LLM_MODEL")
_get_required_env("DATABASE_URL")
_get_required_env("LANGFUSE_PUBLIC_KEY")
_get_required_env("LANGFUSE_SECRET_KEY")

# A single turn fans out into several sequential Groq calls (intent,
# cache-sufficiency, tool-invocation, completeness, synthesis, ...).
# Without a proactive limiter, that fan-out (plus any second concurrent
# request) blows past Groq's per-key RPM ceiling, and the only defense
# becomes the SDK's blind retry-with-backoff -- which is what burns the
# 45s request budget and surfaces as timeouts. Set GROQ_REQUESTS_PER_MINUTE
# comfortably under your actual Groq tier's RPM limit.
_LLM_RPM = float(_get_required_env("LLM_REQUESTS_PER_MINUTE"))
_LLM_RATE_LIMITER = InMemoryRateLimiter(
    requests_per_second=_LLM_RPM / 60,
    check_every_n_seconds=0.1,
    max_bucket_size=4,
)


def _build_llm(api_key: str) -> ChatOpenAI:
    return ChatOpenAI(
        model=LLM_MODEL,               
        api_key=api_key,
        base_url=LLM_BASE_URL,
        rate_limiter=_LLM_RATE_LIMITER,
        max_retries=int(_get_required_env("LLM_MAX_RETRIES")),
        timeout=60.0,
        temperature=0.3,
    )

REFLECTION_CONFIG = {
    "enable_reflection": _get_required_env("ENABLE_REFLECTION").lower() == "true",
    "enable_hallucination_guard": _get_required_env("ENABLE_HALLUCINATION_GUARD").lower() == "true",
    "max_reflection_iterations": int(_get_required_env("MAX_REFLECTION_ITERATIONS")),
    "faithfulness_threshold": float(_get_required_env("FAITHFULNESS_THRESHOLD")),
    "completeness_threshold": float(_get_required_env("COMPLETENESS_THRESHOLD")),
}

# Hard ceiling on a single /query turn. Now set generously high because
# waiting out a TPM-limited retry chain is preferred over failing fast --
# must stay comfortably below the frontend's own HTTP client timeout in
# app.py (bumped alongside this) so the backend always gives up and
# answers gracefully before the caller's HTTP client has stopped listening.
AGENT_QUERY_TIMEOUT_SECONDS = float(_get_required_env("AGENT_QUERY_TIMEOUT_SECONDS"))

state: dict = {"graph": None, "pool": None, "agent_system": None, "current_llm_api_key": None}

@contextlib.asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("starting MCP session...")
    await start_mcp_session()

    # Single shared pool for the whole service: LangGraph's checkpointer
    # (agent-internal reasoning state) and chat_store (the user-facing
    # transcript) are two different tables on the same database, not two
    # different concerns needing separate connections.
    pool = AsyncConnectionPool(
        conninfo=os.getenv("DATABASE_URL"), min_size=2, max_size=20,
        kwargs={"autocommit": True}, open=False)
    await pool.open()
    await chat_store.init_chat_store(pool)
    state["pool"] = pool

    llm = _build_llm(LLM_API_KEY)
    logger.info("compiling LangGraph agent pipeline...")
    # Pass a mutable agent_system object so we can dynamically swap the LLM 
    # client if the .env API key changes at runtime.
    agent_system = SimpleNamespace(llm=llm, judge_llm=_build_judge_llm(LLM_API_KEY))

    state["agent_system"] = agent_system
    state["current_llm_api_key"] = LLM_API_KEY
    state["graph"] = await create_agent_graph(agent_system, config=REFLECTION_CONFIG, pool=pool)
    logger.info("agent service ready")

    yield

    logger.info("shutting down: closing MCP session")
    await stop_mcp_session()
    await pool.close()
    flush()


app = FastAPI(lifespan=lifespan)


class QueryRequest(BaseModel):
    query: str
    thread_id: str


class QueryResponse(BaseModel):
    answer: str
    faithfulness_score: float | None = None
    completeness_score: float | None = None
    party_distribution: dict | None = None
    thinking_log: list[str] = []



def _build_chart_data(scoped_results: list[dict], chart_type: str) -> dict | None:
    party_dist_by_key: dict[int, dict] = {}
    for tr in scoped_results:
        if tr.get("tool") != "get_party_distribution" or not isinstance(tr.get("output"), dict):
            continue
        output = tr["output"]
        for dist in output.get("distributions", []):
            if not isinstance(dist, dict):
                continue
            key = dist.get("wahlperiode")
            entry = dict(dist)
            entry["chart_type"] = chart_type
            party_dist_by_key[key] = entry

    distributions = list(party_dist_by_key.values())
    if not distributions:
        return None
    return {"type": "party_distribution", "distributions": distributions, "chart_type": chart_type}



@app.post("/query", response_model=QueryResponse, dependencies=[Depends(_verify_secret)])
async def query(req: QueryRequest) -> QueryResponse:
    if state["graph"] is None:
        raise HTTPException(status_code=503, detail="agent not ready")
    
    # Check if the user updated the .env file to rotate the API key
    _maybe_reload_env()

    try:
        result = await asyncio.wait_for(
            run_agent_query(state["graph"], req.query, req.thread_id),
            timeout=AGENT_QUERY_TIMEOUT_SECONDS)
    except asyncio.TimeoutError:
        # Fail gracefully within our own SLA instead of letting an internal
        # LLM/tool/reflection chain keep running after the caller gave up.
        # LangGraph's checkpointer durably saves state after every node, so
        # a tool call that already succeeded before the timeout fired is
        # NOT lost -- reuse it instead of discarding the whole turn.
        logger.error("agent query exceeded %.0fs timeout (thread_id=%s)",
                     AGENT_QUERY_TIMEOUT_SECONDS, req.thread_id)
        try:
            snapshot = await state["graph"].aget_state({"configurable": {"thread_id": req.thread_id}})
            scoped_results = (snapshot.values.get("turn_tool_results")
                              or snapshot.values.get("tool_results", [])) if snapshot and snapshot.values else []
        except Exception:
            logger.exception("failed to read checkpoint after timeout (thread_id=%s)", req.thread_id)
            scoped_results = []

        if scoped_results:
            answer = deterministic_fallback_answer(scoped_results)
            chart_type = "scatter" if "scatter" in req.query.lower() else "bar"
            chart_data = _build_chart_data(scoped_results, chart_type)
            turn_index = await chat_store.next_turn_index(state["pool"], req.thread_id)
            await chat_store.save_turn(state["pool"], req.thread_id, turn_index, "user", req.query)
            await chat_store.save_turn(state["pool"], req.thread_id, turn_index + 1, "assistant", answer,
                                        chart_data=chart_data)
            return QueryResponse(answer=answer, party_distribution=chart_data,
                                  thinking_log=["⏱️ Request timed out — showing best-effort answer from data already retrieved before the timeout"])

        raise HTTPException(status_code=504, detail="Request took too long. Please try again.")
    except ModelDegenerateResponseError:
        logger.exception("degenerate LLM response (thread_id=%s)", req.thread_id)
        raise HTTPException(status_code=503, detail="Model temporarily unavailable, please retry")
    except Exception as e:
        error_type = type(e).__name__
        error_str = str(e)
        # Catch Groq rate limits explicitly to avoid raw 500s crashing the UI.
        if "RateLimitError" in error_type or "429" in error_str:
            logger.error("LLM Rate limit reached (thread_id=%s)", req.thread_id)
            raise HTTPException(
                status_code=429, 
                detail="The AI provider rate limit was reached. Please update the LLM_API_KEY in the .env file and try again."
            )
        # Catch Payload Too Large errors gracefully
        if "413" in error_str or "PayloadTooLarge" in error_type:
            logger.error("LLM context limit exceeded (thread_id=%s)", req.thread_id)
            raise HTTPException(
                status_code=413,
                detail="The retrieved data was too large for the AI model's context window. Please try a more specific query."
            )
        logger.exception("query failed (thread_id=%s)", req.thread_id)
        raise HTTPException(status_code=500, detail="agent query failed")
    flush()

    # Extract ALL party-distribution evidence from this turn so the
    # frontend can render comparison charts. Dedupe by (wahlperiode, date_range)
    # so a reflection retry doesn't result in duplicate chart series.
    # We wrap this in a structured payload to strictly separate data from
    # presentation hints (chart_type).
    # Never fall back to the full cross-turn tool_results here: an empty
    # turn_tool_results means nothing new/relevant was fetched or reused
    # THIS turn, so the correct chart is no chart -- not every distribution
    # ever fetched in the session mashed into one comparison.
    scoped_results = result.get("turn_tool_results") or []
    chart_type = "scatter" if "scatter" in req.query.lower() else "bar"
    chart_data = _build_chart_data(scoped_results, chart_type)

    answer = result["messages"][-1].content

    # Persist this turn to the presentation-layer transcript (chat_turns) --
    # separate from LangGraph's internal checkpointed state -- so that
    # GET /threads/{id}/messages (used on page reload) can replay exactly
    # this single, final answer and its chart, never the agent's internal
    # tool-call/tool-output/regenerated-draft messages.
    thinking_log = result.get("thinking_log") or []

    turn_index = await chat_store.next_turn_index(state["pool"], req.thread_id)
    await chat_store.save_turn(state["pool"], req.thread_id, turn_index, "user", req.query)
    await chat_store.save_turn(
        state["pool"], req.thread_id, turn_index + 1, "assistant", answer,
        chart_data=chart_data,
        faithfulness_score=result.get("faithfulness_score"),
        completeness_score=result.get("completeness_score"),
        thinking_log=thinking_log)

    return QueryResponse(
        answer=answer,
        faithfulness_score=result.get("faithfulness_score"),
        completeness_score=result.get("completeness_score"),
        party_distribution=chart_data,
        thinking_log=thinking_log
    )


@app.get("/threads/{thread_id}/messages", dependencies=[Depends(_verify_secret)])
async def get_thread_messages(thread_id: str) -> list[dict]:
    if state["pool"] is None:
        raise HTTPException(status_code=503, detail="agent not ready")
    try:
        return await chat_store.get_turns(state["pool"], thread_id)
    except Exception:
        logger.exception("failed to load chat history (thread_id=%s)", thread_id)
        raise HTTPException(status_code=503, detail="could not load chat history, please retry")


@app.get("/health")
async def health() -> dict:
    return {"status": "ok" if state["graph"] is not None else "starting"}
