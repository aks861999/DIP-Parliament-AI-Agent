import os
import sys

sys.path.append(os.path.join(os.path.dirname(__file__), "..", "services", "mcp-server"))

import pytest

from dip_client import (
    DipClient,
    DipClientError,
    aggregate_party_distribution,
    normalize_party,
    resolve_current_person_fraktion
)



def test_normalize_party_known_variants():
    assert normalize_party("CDU") == "CDU/CSU"
    assert normalize_party("CSU") == "CDU/CSU"
    assert normalize_party("BÜNDNIS 90/DIE GRÜNEN") == "GRÜNE"
    assert normalize_party("DIE LINKE.") == "DIE LINKE"


def test_normalize_party_unknown_returns_none():
    assert normalize_party("Some Unknown Fraktion") is None
    assert normalize_party("") is None


def _person(fraktion: str, wahlperiode: int = 20) -> dict:
    return {"person_roles": [{"wahlperiode_nummer": [wahlperiode], "fraktion": fraktion}]}


class _FakeDipClient:
    def __init__(self, pages: list[dict]):
        self._pages = pages
        self._calls = 0

    async def search_persons_by_wahlperiode(self, wahlperiode: int, cursor: str | None = None):
        page = self._pages[self._calls]
        self._calls += 1
        return page

    async def get_all_persons_for_wahlperiode(self, wahlperiode: int) -> list[dict]:
        # Mirrors the real DipClient method aggregate_party_distribution
        # now calls -- paginate search_persons_by_wahlperiode until a
        # page returns no cursor, exactly like the production code does.
        documents = []
        cursor = None
        while True:
            page = await self.search_persons_by_wahlperiode(wahlperiode, cursor)
            docs = page.get("documents", [])
            documents.extend(docs)
            cursor = page.get("cursor")
            if not docs or cursor is None:
                break
        return documents


@pytest.mark.unit
@pytest.mark.asyncio
async def test_aggregate_party_distribution_paginates_and_sums_correctly():
    pages = [
        {"documents": [_person("SPD")] * 5, "cursor": "c1"},
        {"documents": [_person("CDU")] * 3 + [_person("Unknown Party")] * 2, "cursor": "c2"},
        {"documents": [_person("GRÜNE")] * 4, "cursor": None},
    ]
    client = _FakeDipClient(pages)
    result = await aggregate_party_distribution(client, wahlperiode=20)

    assert sum(result.counts.values()) + result.unclassified_count == result.total_persons
    assert result.total_persons == 14
    assert result.unclassified_count == 2
    assert result.counts["SPD"] == 5
    assert result.counts["CDU/CSU"] == 3
    assert result.counts["GRÜNE"] == 4


@pytest.mark.unit
@pytest.mark.asyncio
async def test_aggregate_party_distribution_raises_on_no_records():
    client = _FakeDipClient([{"documents": [], "cursor": None}])
    with pytest.raises(DipClientError):
        await aggregate_party_distribution(client, wahlperiode=20)




from dip_client import resolve_current_person_fraktion


def test_resolve_current_person_fraktion_prefers_most_recent_role():
    person = {"fraktion": None, "person_roles": [
        {"wahlperiode_nummer": [17, 18], "fraktion": "CDU"},
        {"wahlperiode_nummer": [19], "fraktion": "CDU/CSU"},
    ]}
    assert resolve_current_person_fraktion(person) == "CDU/CSU"


def test_resolve_current_person_fraktion_falls_back_to_top_level_wahlperiode():
    person = {"fraktion": "SPD", "wahlperiode": [20], "person_roles": []}
    assert resolve_current_person_fraktion(person) == "SPD"


def test_resolve_current_person_fraktion_none_when_no_period_resolvable():
    assert resolve_current_person_fraktion({"fraktion": None, "person_roles": []}) is None


@pytest.mark.unit
@pytest.mark.asyncio
async def test_build_name_directory_indexes_namenszusatz_both_forms():
    async def _fake_search_all_persons(cursor=None):
        return {"documents": [
            {"vorname": "Ursula", "namenszusatz": "von der", "nachname": "Leyen"},
            {"vorname": "Friedrich", "nachname": "Merz"},
        ], "cursor": None}

    client = DipClient(base_url="http://test", api_key="test-key")
    client.search_all_persons = _fake_search_all_persons

    directory = await client.build_name_directory()

    assert directory["ursula von der leyen"] == {"vorname": "Ursula", "namenszusatz": "von der", "nachname": "Leyen"}
    assert directory["ursula leyen"] == {"vorname": "Ursula", "namenszusatz": "von der", "nachname": "Leyen"}
    assert directory["friedrich merz"] == {"vorname": "Friedrich", "nachname": "Merz"}
    await client.aclose()


@pytest.mark.unit
@pytest.mark.asyncio
async def test_build_name_directory_paginates_until_cursor_stops_changing():
    pages = [
        {"documents": [{"vorname": "A", "nachname": "One"}], "cursor": "c1"},
        {"documents": [{"vorname": "B", "nachname": "Two"}], "cursor": None},
    ]
    calls = {"n": 0}

    async def _fake_search_all_persons(cursor=None):
        page = pages[calls["n"]]
        calls["n"] += 1
        return page

    client = DipClient(base_url="http://test", api_key="test-key")
    client.search_all_persons = _fake_search_all_persons

    directory = await client.build_name_directory()
    assert set(directory.keys()) == {"a one", "b two"}
    await client.aclose()
