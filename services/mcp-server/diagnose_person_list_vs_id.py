"""
services/mcp-server/diagnose_person_list_vs_id.py   (NEW, standalone script)

Diagnoses the open question from our caching work: does DIP's list
endpoint (GET /person) already return the SAME populated fields
(funktion, fraktion, person_roles, wahlperiode) as the single-item
endpoint (GET /person/{id}), or does the bulk/list response truncate
person_roles history the way build_name_directory()'s existing code
comments assumed?

Method: GET /person?f.id=<id> (list, filtered to ONE person) side-by-side
with GET /person/<id> (single item) for the SAME person. If DIP's list
rows are genuinely just as complete, these two JSON blobs should be
identical (or near-identical) for every field that matters.

Place this file in services/mcp-server/ (same directory as dip_client.py,
cache.py, wahlperiode_utils.py, tool_contracts.py) so the imports below
resolve against your real, already-fixed production code -- this is NOT
a reimplementation, it's the actual DipClient making real calls.

Run:
    cd services/mcp-server
    python diagnose_person_list_vs_id.py

Requires DIP_API_BASE_URL / DIP_API_KEY in your .env (same as mcp_server.py).
"""

import asyncio
import json
import logging
import os
from pathlib import Path

from dip_client import DipClient
from dotenv import load_dotenv

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("diagnose")


_file_parents = Path(__file__).resolve().parents
PROJECT_ROOT = _file_parents[2] if len(_file_parents) > 2 else _file_parents[-1]


load_dotenv(PROJECT_ROOT / ".env")

DIP_API_BASE_URL = os.getenv("DIP_API_BASE_URL", "https://search.dip.bundestag.de/api/v1")
DIP_API_KEY = os.getenv("DIP_API_KEY") or None

# Known persons with rich, multi-Wahlperiode role histories -- good stress
# cases for whether person_roles gets truncated in list mode. IDs taken
# directly from your own session logs (Merkel=14, Merz=8167) plus the
# example person from DIP's own OpenAPI schema (von der Leyen=1728).
TEST_PERSON_IDS = ["14", "8167", "1728"]

# Fields that matter for get_person_info's answer quality -- if these
# match between list and single-item responses, the second HTTP call
# is provably redundant for these fields.
FIELDS_TO_COMPARE = ["funktion", "fraktion", "wahlperiode", "person_roles", "titel"]


def _diff_field(name: str, list_val, single_val) -> dict:
    equal = list_val == single_val
    result = {"field": name, "equal": equal}
    if not equal:
        result["list_value"] = list_val
        result["single_value"] = single_val
        # For person_roles specifically, also report the count difference --
        # the most likely form of truncation (fewer historical entries in
        # list mode) rather than a total absence of the field.
        if name == "person_roles" and isinstance(list_val, list) and isinstance(single_val, list):
            result["list_role_count"] = len(list_val)
            result["single_role_count"] = len(single_val)
    return result


async def diagnose_one(dip: DipClient, person_id: str) -> dict:
    logger.info("=== Diagnosing person id=%s ===", person_id)

    list_response = await dip._get("/person", {"f.id": person_id})
    list_docs = list_response.get("documents", [])
    if not list_docs:
        return {"person_id": person_id, "error": "no document returned by list endpoint for this f.id"}
    list_person = list_docs[0]

    single_person = await dip.get_person(person_id)

    comparisons = [
        _diff_field(field, list_person.get(field), single_person.get(field))
        for field in FIELDS_TO_COMPARE
    ]
    all_equal = all(c["equal"] for c in comparisons)

    return {
        "person_id": person_id,
        "name": f"{single_person.get('vorname', '')} {single_person.get('nachname', '')}".strip(),
        "list_and_single_identical": all_equal,
        "field_comparisons": comparisons,
        "raw_list_person": list_person,
        "raw_single_person": single_person,
    }


async def main():
    # cache=None deliberately: we want two genuinely live fetches per
    # person for this diagnostic, not a cache hit masking one of them.
    dip = DipClient(DIP_API_BASE_URL, DIP_API_KEY, cache=None)
    try:
        results = [await diagnose_one(dip, pid) for pid in TEST_PERSON_IDS]
    finally:
        await dip.aclose()

    report_path = Path(__file__).parent / "person_list_vs_id_report.json"
    report_path.write_text(json.dumps(results, indent=2, default=str, ensure_ascii=False))

    print("\n" + "=" * 70)
    print("SUMMARY")
    print("=" * 70)
    for r in results:
        if "error" in r:
            print(f"  id={r['person_id']}: ERROR — {r['error']}")
            continue
        verdict = "IDENTICAL" if r["list_and_single_identical"] else "DIFFERENT"
        print(f"  id={r['person_id']} ({r['name']}): list vs single-item = {verdict}")
        if not r["list_and_single_identical"]:
            for c in r["field_comparisons"]:
                if not c["equal"]:
                    extra = ""
                    if "list_role_count" in c:
                        extra = f" (list had {c['list_role_count']} roles, single had {c['single_role_count']})"
                    print(f"      -> field '{c['field']}' differs{extra}")
    print("=" * 70)
    print(f"\nFull raw JSON for both responses per person written to: {report_path}")
    print(
        "\nIf ALL fields show IDENTICAL: the second /person/{id} call in "
        "get_person_info is provably redundant -- build_name_directory() "
        "could store the full record from the crawl and skip the second "
        "fetch entirely.\n"
        "If 'person_roles' differs (fewer entries in list mode): the "
        "existing two-call design is correct and necessary -- the list "
        "endpoint truncates role history the way the code comments assumed."
    )


if __name__ == "__main__":
    asyncio.run(main())
