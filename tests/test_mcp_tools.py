import os
import sys

sys.path.append(os.path.join(os.path.dirname(__file__), "..", "services", "mcp-server"))

import pytest


class _FakeRedis:
    def __init__(self):
        self._store: dict[str, str] = {}

    async def get(self, key):
        return self._store.get(key)

    async def set(self, key, value, ex=None):
        self._store[key] = value


class _FakeDip:
    """get_person_info no longer calls dip.get_person() after a directory
    hit -- it uses the directory entry itself (build_name_directory now
    stores the FULL raw person record, not a sparse subset). This fake
    only needs to serve build_name_directory(); search_result/get_person
    are removed since nothing in mcp_server.py calls them anymore."""
    def __init__(self, name_directory: dict | None = None):
        self._name_directory = name_directory or {}

    async def build_name_directory(self, max_pages: int = 5000):
        return self._name_directory


@pytest.mark.unit
@pytest.mark.asyncio
async def test_get_person_info_exact_directory_hit(monkeypatch):
    import mcp_server

    fake_person = {"id": "123", "vorname": "Friedrich", "nachname": "Merz"}
    monkeypatch.setattr(mcp_server, "dip",
                         _FakeDip(name_directory={"friedrich merz": fake_person}))
    monkeypatch.setattr(mcp_server, "redis_client", _FakeRedis())

    result = await mcp_server.get_person_info(name="Friedrich Merz")
    assert result.person == fake_person
    assert result.matches == []
    assert result.suggestions == []


@pytest.mark.unit
@pytest.mark.asyncio
async def test_get_person_info_near_miss_returns_suggestions(monkeypatch):
    """Disambiguation is now via close-match `suggestions` (difflib
    fuzzy matching against the name directory), not `matches` -- there
    is no longer a live "multiple exact search hits" concept, since
    lookup is a single deterministic directory key, not a search."""
    import mcp_server

    close_match = {"id": "1", "vorname": "Anna", "nachname": "Mueller"}
    monkeypatch.setattr(mcp_server, "dip",
                         _FakeDip(name_directory={"anna mueller": close_match}))
    monkeypatch.setattr(mcp_server, "redis_client", _FakeRedis())

    result = await mcp_server.get_person_info(name="Ana Mueler")
    assert result.person is None
    assert result.matches == []
    assert result.suggestions == [close_match]
    assert "Anna Mueller" in result.data_notes


@pytest.mark.unit
@pytest.mark.asyncio
async def test_get_person_info_no_matches(monkeypatch):
    import mcp_server

    monkeypatch.setattr(mcp_server, "dip", _FakeDip(name_directory={}))
    monkeypatch.setattr(mcp_server, "redis_client", _FakeRedis())

    result = await mcp_server.get_person_info(name="Nobody Real")
    assert result.person is None
    assert result.matches == []
    assert result.suggestions == []


@pytest.mark.unit
@pytest.mark.asyncio
async def test_get_party_distribution_rejects_implausible_wahlperiode(monkeypatch):
    import mcp_server

    monkeypatch.setattr(mcp_server, "redis_client", _FakeRedis())
    with pytest.raises(ValueError):
        await mcp_server.get_party_distribution(wahlperiode=999)




@pytest.mark.unit
def test_suggest_person_names_returns_close_matches():
    # suggest_similar_person_names is not an mcp_server-level function --
    # it's dip_client.suggest_person_names, invoked inline inside
    # get_person_info. Test it directly at its actual source.
    from dip_client import suggest_person_names

    directory = {"merkel angela": {"vorname": "Angela", "nachname": "Merkel"}}
    assert suggest_person_names("Markela Angela", directory) == [
        {"vorname": "Angela", "nachname": "Merkel"}]

@pytest.mark.unit
def test_suggest_person_names_no_close_match():
    from dip_client import suggest_person_names

    directory = {"merkel angela": {"vorname": "Angela", "nachname": "Merkel"}}
    assert suggest_person_names("Zzzznobody Qqqreal", directory) == []



@pytest.mark.unit
@pytest.mark.asyncio
async def test_name_directory_builds_once_then_hits_redis_cache(monkeypatch):
    import mcp_server

    directory = {"merz friedrich": {"vorname": "Friedrich", "nachname": "Merz"}}
    fake_dip = _FakeDip(name_directory=directory)
    build_calls = []
    original_build = fake_dip.build_name_directory

    async def _counting_build(max_pages=5000):
        build_calls.append(1)
        return await original_build(max_pages)

    fake_dip.build_name_directory = _counting_build
    monkeypatch.setattr(mcp_server, "dip", fake_dip)
    monkeypatch.setattr(mcp_server, "redis_client", _FakeRedis())

    await mcp_server.get_person_info(name="Merz Friedrich")
    await mcp_server.get_person_info(name="Merz Friedrich")

    assert len(build_calls) == 1


@pytest.mark.unit
@pytest.mark.asyncio
async def test_prewarm_name_directory_survives_dip_failure(monkeypatch):
    import mcp_server

    class _FailingDip:
        async def build_name_directory(self, max_pages: int = 5000):
            raise RuntimeError("DIP unreachable")

    monkeypatch.setattr(mcp_server, "dip", _FailingDip())
    monkeypatch.setattr(mcp_server, "redis_client", _FakeRedis())

    await mcp_server._prewarm_name_directory()


@pytest.mark.unit
@pytest.mark.asyncio
async def test_get_person_info_resolves_fraktion_from_person_roles_when_top_level_missing(monkeypatch):
    import mcp_server

    fake_person = {
        "id": "997", "vorname": "Angela", "nachname": "Merkel", "fraktion": None,
        "person_roles": [{"wahlperiode_nummer": [18, 19], "fraktion": "CDU/CSU"}],
    }
    monkeypatch.setattr(mcp_server, "dip",
                         _FakeDip(name_directory={"angela merkel": fake_person}))
    monkeypatch.setattr(mcp_server, "redis_client", _FakeRedis())

    result = await mcp_server.get_person_info(name="Angela Merkel")
    assert result.person == fake_person
    assert result.resolved_fraktion == "CDU/CSU"
