import argparse
import asyncio
import csv
import os
import sys
import uuid
from datetime import datetime, timezone

from scenario_models import MockedScenario

sys.path.append(os.path.join(os.path.dirname(__file__), "..", "agent-app"))
sys.path.append(os.path.join(os.path.dirname(__file__), "..", "agent-service"))

os.environ.setdefault("MCP_TRANSPORT", "stdio")
os.environ.setdefault("MCP_SERVER_PYTHON", sys.executable)
os.environ.setdefault(
    "MCP_SERVER_SCRIPT", os.path.join(os.path.dirname(__file__), "stub_mcp_server.py"))

import grounding
from agent_graph import (
    create_agent_graph,
    load_agent_config,
    run_agent_query,
)
from dotenv import load_dotenv
from langchain_openai import ChatOpenAI
from mcp_client import start_mcp_session, stop_mcp_session

load_dotenv()


class _AgentSystemShim:
    def __init__(self, llm, judge_llm=None):
        self.llm = llm
        self.judge_llm = judge_llm if judge_llm is not None else llm


def _tool_call_matches(tool_results, expected_tool, expected_args_contains):
    if expected_tool is None:
        return len(tool_results) == 0
    for entry in tool_results:
        if entry.get("tool") != expected_tool:
            continue
        if not expected_args_contains:
            return True
        call_args = entry.get("args", {})
        if all(call_args.get(k) == v for k, v in expected_args_contains.items()):
            return True
    return False


async def run_scenarios(records, cfg, llm):
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
            tool_ok = _tool_call_matches(
                tool_results, record.expected_tool, record.expected_args_contains)
            grounded_score = await grounding.check_grounded(answer, tool_results, llm=llm)
            rows.append({
                "question": question, "answer": answer,
                "expected_tool": record.expected_tool,
                "tool_selection_correct": tool_ok, "grounding_score": grounded_score, "error": "",
            })
        except Exception as e:
            rows.append({
                "question": question, "answer": "", "expected_tool": record.expected_tool,
                "tool_selection_correct": False, "grounding_score": 0.0, "error": str(e),
            })
    return rows


def print_summary(rows, grounding_threshold=0.8):
    n = len(rows)
    tool_accuracy = sum(1 for r in rows if r["tool_selection_correct"]) / n
    avg_grounding = sum(r["grounding_score"] for r in rows) / n
    print(f"n={n} tool_selection_accuracy={tool_accuracy:.3f} avg_grounding={avg_grounding:.3f}")
    for r in rows:
        if not r["tool_selection_correct"] or r["grounding_score"] < grounding_threshold:
            print(f"  FLAG: {r['question']!r} expected_tool={r['expected_tool']} "
                  f"tool_ok={r['tool_selection_correct']} grounding={r['grounding_score']:.2f} "
                  f"error={r['error']!r}")
    return tool_accuracy, avg_grounding


async def main(args):
    with open(args.testset) as f:
        records = [MockedScenario.model_validate_json(line) for line in f if line.strip()]
    llm = ChatOpenAI(
    model=os.environ["LLM_MODEL"],
    api_key=os.environ["LLM_API_KEY"],
    base_url=os.environ["LLM_BASE_URL"],
)

    await start_mcp_session()
    try:
        cfg = load_agent_config(overrides={
            "enable_reflection": True, "enable_hallucination_guard": True,
            "faithfulness_threshold": args.grounding_threshold,
        })
        print(f"Running {len(records)} mocked scenarios against the fixture MCP stub...")
        rows = await run_scenarios(records, cfg, llm)
    finally:
        await stop_mcp_session()



    if not rows:
        print("No scenarios in testset — nothing to evaluate.")
        sys.exit(1)
    os.makedirs("results", exist_ok=True)
    out_path = f"results/eval_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}.csv"
    with open(out_path, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=list(rows[0].keys()))

        writer.writeheader()
        writer.writerows(rows)
    print(f"Results written to {out_path}")

    tool_accuracy, avg_grounding = print_summary(rows, args.grounding_threshold)
    if tool_accuracy < args.tool_selection_threshold or avg_grounding < args.grounding_threshold:
        print(f"GATE FAILED: tool_selection_accuracy={tool_accuracy:.2f} "
              f"(need {args.tool_selection_threshold}), avg_grounding={avg_grounding:.2f} "
              f"(need {args.grounding_threshold})")
        sys.exit(1)
    print(f"GATE PASSED: tool_selection_accuracy={tool_accuracy:.2f}, avg_grounding={avg_grounding:.2f}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--testset", default="scenarios_mocked.jsonl")
    parser.add_argument("--tool-selection-threshold", type=float, default=0.9)
    parser.add_argument("--grounding-threshold", type=float, default=0.8)
    parser.add_argument("--faithfulness-threshold", dest="grounding_threshold", type=float)
    parser.add_argument("--completeness-threshold", type=float, default=0.7,
                        help="accepted for CLI compatibility; completeness is "
                             "scored per-turn by the reflection node, not gated here")
    args = parser.parse_args()
    asyncio.run(main(args))
