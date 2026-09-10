import json
import re
from decimal import Decimal
from typing import Any

from pydantic import BaseModel

_PERCENT_RE = re.compile(r"(\d+(?:[.,]\d+)?)\s*%")
_NUMBER_RE = re.compile(r"\d+(?:[.,]\d+)?")

# Office/role nouns (German + English). Checked by 7-char stem so that
# "bundeskanzler" matches DIP's abbreviated "bundeskanzl." This guards the
# claim category that numeric checks can't see: attributing an office to
# a person when no evidence entry contains that office.
_ROLE_RE = re.compile(
    r"\b(?:bundes)?(kanzler\w*|präsident\w*|minister\w*|"
    r"bürgermeister\w*|chancellor|president|mayor)\b")


# --- ADD this new function ---
def _strip_note_keys(node):
    """Recursively remove caveat/note fields (data_notes, note, caveat)
    from a JSON-like structure before it's used as ROLE-matching evidence.

    These fields never positively confirm a role -- they only ever
    describe caveats or negative results. A failed get_persons_by_role
    call's own "No person matching role 'X' was found" message otherwise
    leaks the literal role name into the evidence text, letting a
    hallucinated role claim falsely "ground" against its own negative
    result. Generalizes to any role and any Wahlperiode -- not specific
    to Adenauer or Bundeskanzler."""
    if isinstance(node, dict):
        return {k: _strip_note_keys(v) for k, v in node.items() if k not in _NOTE_KEYS}
    if isinstance(node, list):
        return [_strip_note_keys(item) for item in node]
    return node



def _role_stems(text: str) -> set[str]:
    return {m.group(1)[:7] for m in _ROLE_RE.finditer(text.lower())}

# Keys whose string values are notes/caveats, checked separately from numbers
_NOTE_KEYS = {"data_notes", "note", "caveat"}
_SKIP_KEYS = _NOTE_KEYS | {"id", "pdf_hash", "url"}


def _extract_percentages(text: str) -> set[Decimal]:
    return {Decimal(m.replace(",", ".")) for m in _PERCENT_RE.findall(text)}


def _iter_leaf_values(node: Any, key: str = ""):
    """Yield (key, value) for every scalar leaf in the output JSON tree."""
    if isinstance(node, dict):
        for k, v in node.items():
            yield from _iter_leaf_values(v, k)
    elif isinstance(node, list):
        for item in node:
            yield from _iter_leaf_values(item, key)
    elif node is not None:
        yield key, node


def _numeric_tokens(output: dict) -> set[Decimal]:
    tokens: set[Decimal] = set()
    for key, value in _iter_leaf_values(output):
        if key in _SKIP_KEYS or isinstance(value, bool):
            continue
        if isinstance(value, (int, float)):
            tokens.add(Decimal(str(value)))
        elif isinstance(value, str) and key not in _NOTE_KEYS:
            for m in _NUMBER_RE.findall(value):
                tokens.add(Decimal(m.replace(",", ".")))
    return tokens


class CaveatVerdict(BaseModel):
    addressed: bool
    rationale: str

# Mirrors CompletenessVerdict/CacheSufficiencyVerdict in agent_graph.py --
# same judge_llm(rubric, payload, schema) pattern. Whether a caveat's
# SUBSTANCE came through is a language-understanding task, not a lexical
# one: an answer in German/French/etc. will translate every content word
# of an English data_notes string, so no stopword list can verify this
# safely across languages. An LLM judge is the one component here that
# actually understands paraphrase and translation.
CAVEAT_RUBRIC = """You are checking whether an assistant's answer conveyed \
the SUBSTANCE of a data caveat/limitation to the user. The answer may be in \
any language, and may paraphrase, translate, summarize, or restructure the \
caveat entirely -- exact wording, word choice, or language match is NOT \
required and must NOT be penalized. Judge only whether the underlying \
limitation or scope issue described in the caveat was communicated to the \
user in some form, however briefly. If the caveat's numbers are checked \
separately elsewhere, focus only on whether its qualitative point came \
through. Return addressed=true or false, plus a one-sentence rationale."""

def _numbers_covered(answer_lower: str, data_notes: str) -> bool:
    """Language-invariant half of caveat coverage: digits are written
    identically across scripts/languages, so this stays a cheap,
    synchronous, purely lexical check."""
    numbers = [Decimal(m.replace(",", ".")) for m in _NUMBER_RE.findall(data_notes)]
    if not numbers:
        return True
    answer_nums = {Decimal(m.replace(",", ".")) for m in _NUMBER_RE.findall(answer_lower)}
    return all(n in answer_nums for n in numbers)

async def _note_covered_semantic(llm, answer: str, data_notes: str) -> bool:
    """LLM-judged half of caveat coverage: verifies the qualitative
    substance was conveyed, regardless of answer language or phrasing.
    Fails OPEN (treats as addressed) on any judge error, matching the
    fail-open philosophy already used for judge calls elsewhere in the
    pipeline (e.g. reflection_node's completeness-judge except clause) --
    an unavailable judge should not silently tank every faithfulness score."""
    try:
        verdict = await llm.with_structured_output(CaveatVerdict).ainvoke([
            {"role": "system", "content": CAVEAT_RUBRIC},
            {"role": "user", "content": f"Caveat: {data_notes}\n\nAnswer: {answer}"},
        ])
        return verdict.addressed
    except Exception:
        return True

async def _note_tokens_covered(llm, answer: str, answer_lower: str, data_notes: str) -> bool:
    if not _numbers_covered(answer_lower, data_notes):
        return False
    if llm is None:
        return True
    return await _note_covered_semantic(llm, answer, data_notes)


async def _non_numeric_grounding(llm, answer: str, answer_lower: str, output: dict) -> tuple[int, int]:
    """Person-identity and data_notes coverage, checked PER-ENTRY -- each
    entry's own person/caveat is a distinct fact the answer should
    individually surface, unlike numbers (see check_grounded)."""
    matched = checks = 0

    persons = output.get("persons")
    if isinstance(persons, list) and persons:
        for p in persons:
            for field in ("vorname", "nachname"):
                v = p.get(field)
                if v:
                    checks += 1
                    matched += 1 if str(v).lower() in answer_lower else 0
    else:
        person = output.get("person")
        if isinstance(person, dict):
            for field in ("vorname", "nachname"):
                v = person.get(field)
                if v:
                    checks += 1
                    matched += 1 if str(v).lower() in answer_lower else 0

    data_notes = output.get("data_notes")
    if data_notes:
        checks += 1
        matched += 1 if await _note_tokens_covered(llm, answer, answer_lower, data_notes) else 0

    return matched, checks


async def check_grounded(answer: str, tool_results: list[dict], llm=None) -> float:
    if not tool_results:
        return 1.0

    outputs = [entry.get("output", {}) for entry in tool_results
               if isinstance(entry.get("output"), dict)]
    if not outputs:
        return 1.0

    answer_lower = answer.lower()

    # Numeric hallucination check: every number/percentage the answer
    # states must appear SOMEWHERE across the combined evidence, not in
    # every single entry in isolation. A comparison answer legitimately
    # mixes numbers from multiple entries in one sentence/table --
    # checking each entry against the WHOLE shared answer text was
    # structurally guaranteed to fail for any multi-entry answer,
    # regardless of correctness, which is what was burning an extra
    # synthesis call on every comparison turn.
    answer_nums = _extract_percentages(answer)
    answer_nums.update({Decimal(m.replace(",", ".")) for m in _NUMBER_RE.findall(answer)})
    known_union: set = set()
    for output in outputs:
        if "error" not in output:
            known_union |= _numeric_tokens(output)

    total_matched = total_checks = 0
    if answer_nums:
        total_checks += len(answer_nums)
        total_matched += sum(1 for n in answer_nums if n in known_union)

    # Role-claim grounding: every office/title noun asserted in the answer
    # must appear (stemmed) somewhere in the combined evidence. A correct
    # comparison mixes roles from multiple entries, so -- like numbers --
    # this checks against the UNION of evidence, not per-entry.
    answer_roles = _role_stems(answer)
    if answer_roles:
        role_evidence = [_strip_note_keys(o) for o in outputs if "error" not in o]
        evidence_text = json.dumps(role_evidence, default=str).lower()
        evidence_stems = _role_stems(evidence_text)
        for stem in answer_roles:
            total_checks += 1
            if stem in evidence_stems or stem in evidence_text:
                total_matched += 1

    for output in outputs:
        if "error" in output:
            total_checks += 1
            continue
        m, c = await _non_numeric_grounding(llm, answer, answer_lower, output)
        total_matched += m
        total_checks += c

    return total_matched / total_checks if total_checks else 1.0