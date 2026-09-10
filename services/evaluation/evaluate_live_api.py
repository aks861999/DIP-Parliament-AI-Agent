import argparse
import asyncio
import json
import os
import sys
import uuid
from datetime import datetime, timezone

from scenario_models import LiveScenario

sys.path.append(os.path.join(os.path.dirname(__file__), "..", "agent-app"))
sys.path.append(os.path.join(os.path.dirname(__file__), "..", "agent-service"))

from agent_graph import (
    create_agent_graph,
    load_agent_config,
    run_agent_query,
)
from dotenv import load_dotenv
from langchain_openai import ChatOpenAI
from mcp_client import start_mcp_session, stop_mcp_session

load_dotenv()

_TRANSPORT_ERROR_MARKERS = (
    "401", "403", "404", "timeout", "timed out", "connection",
    "unreachable", "connect", "temporarily unavailable", "503", "502", "429",
)


class _AgentSystemShim:
    def __init__(self, llm, judge_llm=None):
        self.llm = llm
        self.judge_llm = judge_llm if judge_llm is not None else llm


def _looks_like_transport_error(exc: Exception) -> bool:
    text = str(exc).lower()
    return any(marker in text for marker in _TRANSPORT_ERROR_MARKERS)


async def run_live_scenarios(records, cfg, llm):
    agent_system = _AgentSystemShim(llm)
    graph = await create_agent_graph(agent_system, config=cfg)
    rows = []
    for record in records:
        question = record.question
        thread_id = str(uuid.uuid4())
        try:
            result = await run_agent_query(graph, question, thread_id)
            answer = result["messages"][-1].content
            tool_results = result.get("tool_results", [])
            rows.append({"question": question, "status": "OK", "answer": answer[:200],
                         "tools_called": [t["tool"] for t in tool_results], "error": ""})
        except Exception as e:
            status = "TRANSPORT_ERROR" if _looks_like_transport_error(e) else "UNEXPECTED_ERROR"
            rows.append({"question": question, "status": status, "answer": "",
                         "tools_called": [], "error": str(e)})
    return rows


async def main(args):
    with open(args.testset) as f:
        records = [LiveScenario.model_validate_json(line) for line in f if line.strip()]



    if not os.getenv("DIP_API_KEY"):
        print("DIP_API_KEY not set — skipping live-API check entirely (not a failure).")
        return


    llm = ChatOpenAI(
        model=os.environ["LLM_MODEL"],
        api_key=os.environ["LLM_API_KEY"],
        base_url=os.environ["LLM_BASE_URL"],
    )

    await start_mcp_session()
    try:
        cfg = load_agent_config(overrides={"enable_reflection": True, "enable_hallucination_guard": True})
        print(f"Running {len(records)} LIVE scenarios against the real DIP API...")
        rows = await run_live_scenarios(records, cfg, llm)
    finally:
        await stop_mcp_session()

    os.makedirs("results", exist_ok=True)
    out_path = f"results/live_api_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}.json"
    with open(out_path, "w") as f:
        json.dump(rows, f, indent=2, default=str)
    print(f"Results written to {out_path}")

    for r in rows:
        print(f"  [{r['status']}] {r['question']!r} tools={r.get('tools_called')} error={r['error'][:150]!r}")

    unexpected = [r for r in rows if r["status"] == "UNEXPECTED_ERROR"]
    transport = [r for r in rows if r["status"] == "TRANSPORT_ERROR"]
    if unexpected:
        print(f"{len(unexpected)} scenario(s) failed with a non-transport error — likely a real bug.")
        sys.exit(2)
    if transport:
        print(f"{len(transport)} scenario(s) hit a DIP transport error — non-blocking (allow_failure: true).")
        sys.exit(1)
    print("All live-API scenarios completed successfully.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--testset", default="scenarios_live.jsonl")
    args = parser.parse_args()
    asyncio.run(main(args))
