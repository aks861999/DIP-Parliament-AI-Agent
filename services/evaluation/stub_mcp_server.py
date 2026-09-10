import os

from mcp.server.fastmcp import FastMCP
from pydantic import BaseModel


class PersonInfoResult(BaseModel):
    person: dict | None = None
    matches: list[dict] = []
    suggestions: list[dict] = []
    resolved_fraktion: str | None = None
    data_notes: str | None = None


class PartyDistribution(BaseModel):
    wahlperiode: int
    date_range: dict | None = None
    counts: dict[str, int]
    percentages: dict[str, float]
    total_persons: int
    unclassified_count: int
    data_notes: str | None = None


mcp = FastMCP("dip-parliamentary-tools-stub", host="0.0.0.0", port=8000)

_FIXTURE_PARTY_DISTRIBUTION = {
    19: {
        "wahlperiode": 19, "date_range": None,
        "counts": {"CDU/CSU": 246, "SPD": 152, "AfD": 92, "FDP": 80, "DIE LINKE": 69, "GRÜNE": 67},
        "percentages": {"CDU/CSU": 34.85, "SPD": 21.53, "AfD": 13.03, "FDP": 11.33,
                        "DIE LINKE": 9.77, "GRÜNE": 9.49},
        "total_persons": 709, "unclassified_count": 3,
        "data_notes": "3 of 709 persons had no resolvable party affiliation for this Wahlperiode and are excluded from percentages.",
    },
    20: {
        "wahlperiode": 20, "date_range": None,
        "counts": {"SPD": 206, "CDU/CSU": 197, "GRÜNE": 118, "FDP": 92, "AfD": 78, "DIE LINKE": 39},
        "percentages": {"SPD": 28.22, "CDU/CSU": 26.99, "GRÜNE": 16.16, "FDP": 12.6,
                        "AfD": 10.68, "DIE LINKE": 5.34},
        "total_persons": 736, "unclassified_count": 6,
        "data_notes": "6 of 736 persons had no resolvable party affiliation for this Wahlperiode and are excluded from percentages.",
    },
    21: {
        "wahlperiode": 21, "date_range": None,
        "counts": {"CDU/CSU": 208, "AfD": 152, "SPD": 120, "GRÜNE": 85, "DIE LINKE": 64, "FDP": 0},
        "percentages": {"CDU/CSU": 33.07, "AfD": 24.17, "SPD": 19.08, "GRÜNE": 13.51,
                        "DIE LINKE": 10.17, "FDP": 0.0},
        "total_persons": 634, "unclassified_count": 5,
        "data_notes": "5 of 634 persons had no resolvable party affiliation for this Wahlperiode and are excluded from percentages.",
    },
}

_FIXTURE_PERSON = {
    "friedrich merz": {
        "person": {"id": "999", "vorname": "Friedrich", "nachname": "Merz", "fraktion": "CDU/CSU"},
        "matches": [],
    },
    "olaf scholz": {
        "person": {"id": "998", "vorname": "Olaf", "nachname": "Scholz", "fraktion": "SPD"},
        "matches": [],
    },
    "angela merkel": {
        "person": {"id": "997", "vorname": "Angela", "nachname": "Merkel", "fraktion": "CDU/CSU"},
        "matches": [],
    },
}


@mcp.tool(description=(
    "USE WHEN the user asks about a specific NAMED individual. DO NOT USE for "
    "aggregate/statistical party-composition questions. On a miss, returns "
    "close-match suggestions and a clarification hint in data_notes."))
async def get_person_info(name: str) -> PersonInfoResult:
    key = name.strip().lower()
    if key in _FIXTURE_PERSON:
        return PersonInfoResult.model_validate(_FIXTURE_PERSON[key])
    return PersonInfoResult(
        person=None, matches=[],
        suggestions=[],
        data_notes="I couldn't find that person in the parliamentary data. "
                   "Check the spelling, or reply with the correct name.")


@mcp.tool()
async def get_party_distribution(wahlperiode: int, date_range: dict | None = None) -> PartyDistribution:
    if wahlperiode not in _FIXTURE_PARTY_DISTRIBUTION:
        raise ValueError(f"no fixture data for wahlperiode={wahlperiode}")
    return PartyDistribution.model_validate(_FIXTURE_PARTY_DISTRIBUTION[wahlperiode])

import re


def _normalize_role(s: str) -> str:
    return re.sub(r'[^a-z0-9]', '', s.lower())

@mcp.tool()
async def get_persons_by_role(funktion: str, wahlperiode: int) -> dict:
    # This is a test stub. It returns hardcoded fixture data so the evaluation 
    # suite can verify the LLM's synthesis logic without hitting the real API.
    if _normalize_role(funktion) in ["bundeskanzler", "bundeskanzl"] and wahlperiode == 21:
        return {"persons": [{"id": "8167", "vorname": "Friedrich", "nachname": "Merz", "funktion": "Bundeskanzl.", "fraktion": "CDU/CSU", "wahlperiode": [21]}], "data_notes": None}
    if _normalize_role(funktion) in ["bundeskanzler", "bundeskanzl"] and wahlperiode == 20:
        return {"persons": [{"id": "998", "vorname": "Olaf", "nachname": "Scholz", "funktion": "Bundeskanzl.", "fraktion": "SPD", "wahlperiode": [20]}], "data_notes": None}
    return {"persons": [], "data_notes": None}


if __name__ == "__main__":
    mcp.run(transport=os.getenv("MCP_TRANSPORT", "stdio"))
