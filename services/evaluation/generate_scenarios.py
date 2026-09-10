"""Generate paraphrase/adversarial variants of base scenarios via one LLM call
per scenario. Output: scenarios_mocked_gen.jsonl (same MockedScenario format)."""
import argparse
import asyncio
import os
import sys

sys.path.append(os.path.join(os.path.dirname(__file__), "..", "agent-app"))

from dotenv import load_dotenv
from langchain_openai import ChatOpenAI
from pydantic import BaseModel
from scenario_models import MockedScenario

load_dotenv()


class Variants(BaseModel):
    questions: list[str]


VARIANT_PROMPT = """\
Rewrite the question below in {n} different phrasings a real user might type
(mix German and English, include one deliberately ambiguous/underspecified
phrasing). Keep the SAME underlying information need — do not change what is
being asked. Output only the rewritten questions.

Question: {question}"""


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="scenarios_mocked.jsonl")
    ap.add_argument("--out", default="scenarios_mocked_gen.jsonl")
    ap.add_argument("--variants", type=int, default=5)
    args = ap.parse_args()

    llm = ChatOpenAI(
    model=os.environ["LLM_MODEL"],
    api_key=os.environ["LLM_API_KEY"],
    base_url=os.environ["LLM_BASE_URL"],
)
    gen = llm.with_structured_output(Variants)

    with open(args.base) as f:
        base = [MockedScenario.model_validate_json(line) for line in f if line.strip()]

    out = open(args.out, "w")  # closed explicitly via out.close() further down
    for rec in base:
        out.write(rec.model_dump_json() + "\n")  # always keep the original
        if rec.expected_tool is None:
            continue  # out-of-scope scenario: variants are less meaningful
        v: Variants = await gen.ainvoke(
            [("user", VARIANT_PROMPT.format(n=args.variants, question=rec.question))])
        out.writelines(MockedScenario(
                question=q, expected_tool=rec.expected_tool,
                expected_args_contains=rec.expected_args_contains).model_dump_json() + "\n" for q in v.questions)
    out.close()
    print(f"wrote generated scenarios to {args.out}")


if __name__ == "__main__":
    asyncio.run(main())