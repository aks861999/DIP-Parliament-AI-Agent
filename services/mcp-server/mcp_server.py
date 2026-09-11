import asyncio
import json
import logging
import os
import sys
from contextlib import asynccontextmanager
from pathlib import Path

import redis.asyncio as redis
from cache import Cache  # NEW
from dip_client import (
    DipClient,
    aggregate_party_distribution,
    resolve_current_person_fraktion,
    suggest_person_names,
)
from dotenv import load_dotenv
from mcp.server.fastmcp import FastMCP
from tool_contracts import DateRange, PartyDistribution, PersonInfoResult
from wahlperiode_utils import _wahlperiode_for_date

load_dotenv()


def _get_required_env(name: str) -> str:
    value = os.getenv(name)
    if not value:
        raise ValueError(f"required environment variable {name} is not set")
    return value


_file_parents = Path(__file__).resolve().parents
_PROJECT_ROOT = _file_parents[2] if len(_file_parents) > 2 else _file_parents[-1]
_LOG_FILE = _PROJECT_ROOT / "dip_agent.log"
logging.basicConfig(
level=_get_required_env("LOG_LEVEL"),
format="%(asctime)s [mcp-server] %(levelname)s:%(name)s: %(message)s",
handlers=[
logging.StreamHandler(sys.stderr),
logging.FileHandler(_LOG_FILE, mode="a", encoding="utf-8"),
],
)

logger = logging.getLogger(__name__)



PREWARM_ON_STARTUP = _get_required_env("PREWARM_ON_STARTUP").lower() == "true"

NAME_DIRECTORY_TTL_SECONDS = int(_get_required_env("NAME_DIRECTORY_TTL_SECONDS"))
_NAME_DIRECTORY_CACHE_KEY = "name_directory:v1"

DIP_API_BASE_URL = _get_required_env("DIP_API_BASE_URL")
DIP_API_KEY = os.getenv("DIP_API_KEY") or None
CACHE_TTL_SECONDS = int(_get_required_env("MCP_CACHE_TTL_SECONDS"))
REDIS_URL = _get_required_env("REDIS_URL")



@asynccontextmanager
async def app_lifespan(server: FastMCP):
    if PREWARM_ON_STARTUP:
        asyncio.create_task(_prewarm_name_directory())
    yield



mcp = FastMCP("dip-parliamentary-tools", host="0.0.0.0", port=8000, lifespan=app_lifespan)
redis_client = redis.from_url(REDIS_URL, decode_responses=True)
dip_cache = Cache(redis_client, default_ttl_seconds=CACHE_TTL_SECONDS)  # NEW
dip = DipClient(DIP_API_BASE_URL, DIP_API_KEY, cache=dip_cache)  # NEW: cache= added





# --- REPLACE the _cache_get function with: ---
async def _cache_get(key: str) -> dict | None:
    raw = await redis_client.get(key)
    if raw is None:
        logger.info("tool-output cache MISS: %s", key)
        return None
    logger.info("tool-output cache HIT: %s", key)
    return json.loads(raw)


async def _cache_set(key: str, value: "PersonInfoResult | PartyDistribution | dict") -> None:
    """Cache helper that supports both Pydantic models and standard dicts."""
    if hasattr(value, "model_dump_json"):
        await redis_client.set(key, value.model_dump_json(), ex=CACHE_TTL_SECONDS)
    else:
        await redis_client.set(key, json.dumps(value), ex=CACHE_TTL_SECONDS)





async def _get_name_directory() -> dict[str, dict]:
    raw = await redis_client.get(_NAME_DIRECTORY_CACHE_KEY)
    if raw is not None:
        logger.info("name directory cache HIT — reusing cached directory, no crawl")
        return json.loads(raw)
    logger.warning("name directory cache MISS — starting full /person crawl to rebuild it")
    directory = await dip.build_name_directory()
    await redis_client.set(_NAME_DIRECTORY_CACHE_KEY, json.dumps(directory), ex=NAME_DIRECTORY_TTL_SECONDS)
    logger.info("name directory rebuilt and cached (%d entries)", len(directory))
    return directory


@mcp.tool(description=(
    "USE WHEN the user asks about a specific NAMED individual (biography, party, "
    "parliamentary roles, e.g. 'Who is Friedrich Merz?'). "
    "DO NOT USE for aggregate/statistical party-composition questions — those go "
    "to get_party_distribution. If no exact match is found, the result includes "
    "close-match suggestions in `suggestions` and a clarification hint in "
    "`data_notes`; surface those to the user instead of answering."))
async def get_person_info(name: str) -> PersonInfoResult:
    cache_key = f"person:{name.strip().lower()}"
    cached = await _cache_get(cache_key)
    if cached is not None:
        logger.info("cache hit: %s", cache_key)
        return PersonInfoResult.model_validate(cached)

    directory = await _get_name_directory()
    entry = directory.get(name.strip().lower())
    logger.info("get_person_info(name=%r) -> directory %s", name, "hit" if entry else "miss")

    if entry is None:
        suggestions = suggest_person_names(name, directory)
        if suggestions:
            names = ", ".join(
                " ".join(p for p in (s.get("vorname", ""), s.get("namenszusatz", ""),
                                     s.get("nachname", "")) if p)
                for s in suggestions)
            note = (f"I couldn't find an exact match for '{name}'. "
                    f"Did you mean: {names}? Reply with the correct name to confirm.")
        else:
            note = (f"I couldn't find '{name}' in the parliamentary data. "
                    "Check the spelling, or reply with the correct name.")
        result = PersonInfoResult(person=None, matches=[], suggestions=suggestions,
                                  data_notes=note)
    else:
        # entry IS the full person record now (build_name_directory stores
        # the raw crawled document, not a sparse subset) -- confirmed via
        # diagnose_person_list_vs_id.py that list-mode data is complete,
        # so the second live GET /person/{id} call is redundant and removed.
        person = entry
        result = PersonInfoResult(person=person, matches=[],
                                  resolved_fraktion=resolve_current_person_fraktion(person))

    await _cache_set(cache_key, result)
    return result




async def _prewarm_name_directory():
    try:
        await _get_name_directory()
        logger.info("name directory pre-warmed")
    except Exception:
        logger.exception("name directory pre-warm failed — will lazy-build on first miss instead")





@mcp.tool(description=(
    "USE WHEN the user asks about aggregate party composition / distribution for "
    "a Wahlperiode (e.g. 'party distribution in the 20th legislative session'). "
    "DO NOT USE for questions about one named person — that is get_person_info. "
    "wahlperiode must be 1-21 (see schema constraints). `date_range` is accepted "
    "but does NOT filter the aggregation — the DIP API has no role-tenure-scoped "
    "date filter, so the result always covers the full Wahlperiode; a caveat is "
    "returned in data_notes when date_range is supplied."
    "DO NOT USE for role/title questions  — that is get_persons_by_role."))
async def get_party_distribution(wahlperiode: int, date_range: DateRange | None = None) -> PartyDistribution:
    if not (1 <= wahlperiode <= 21):
        raise ValueError(f"implausible wahlperiode: {wahlperiode}")

    cache_key = f"party_dist:{wahlperiode}:{date_range.model_dump_json() if date_range else 'None'}"
    cached = await _cache_get(cache_key)
    if cached is not None:
        logger.info("cache hit: %s", cache_key)
        return PartyDistribution.model_validate(cached)

    distribution = await aggregate_party_distribution(dip, wahlperiode, date_range=date_range)
    if distribution.unclassified_count > 0:
        distribution.data_notes = (
            f"{distribution.unclassified_count} of {distribution.total_persons} "
            f"persons had no resolvable party affiliation for this Wahlperiode and are "
            f"excluded from percentages.")
    if date_range is not None:
        note = ("Note: date_range was supplied but the underlying DIP API has no "
                "role-tenure-scoped date filter, so this result reflects the full "
                "Wahlperiode, not the specific date range.")
        distribution.data_notes = ((distribution.data_notes or "") + " " + note).strip()

        # NEW: warn (not silently correct) when wahlperiode contradicts the date
        from datetime import date as _date
        try:
            if date_range.start:
                implied = _wahlperiode_for_date(_date.fromisoformat(date_range.start))
                if implied is not None and implied != wahlperiode:
                    warn = (f"Warning: the supplied date_range starts in Wahlperiode "
                            f"{implied}, but wahlperiode={wahlperiode} was requested — "
                            f"results are for Wahlperiode {wahlperiode}.")
                    distribution.data_notes = ((distribution.data_notes or "") + " " + warn).strip()
        except (ValueError, TypeError):
            pass

    await _cache_set(cache_key, distribution)
    return distribution

import difflib
import re


def _normalize_role(s: str) -> str:
    return re.sub(r'[^a-z0-9]', '', s.lower())


def _role_matches(target_norm: str, candidate_norm: str, cutoff: float = 0.78) -> bool:
    """Similarity ratio, not raw substring containment. Containment
    matched 'Bundeskanzleramtes' (Chancellery office staff) against a
    'Bundeskanzler' (Chancellor) query because one string is literally a
    prefix of the other once punctuation is stripped -- a false positive
    that has nothing to do with query wording, so it generalizes to any
    role pair with this kind of compound-word overlap, not just this one."""
    if not target_norm or not candidate_norm:
        return False
    if target_norm == candidate_norm:
        return True
    return difflib.SequenceMatcher(None, target_norm, candidate_norm).ratio() >= cutoff





def _period_list(value) -> list[int]:
    if value is None:
        return []
    if isinstance(value, int):
        return [value]
    return list(value)


def _has_period_scoped_roles(person: dict, wahlperiode: int) -> bool:
    """Whether this person's record has ANY person_roles entry tagged with
    this Wahlperiode -- i.e. whether period-scoped detail exists to judge
    from, vs. a sparse search row with no role detail for this period."""
    return any(wahlperiode in _period_list(role.get("wahlperiode_nummer"))
               for role in person.get("person_roles", []) or [])


def _funktion_candidates(person: dict) -> set[str]:
    """Every distinct (normalized) funktion string visible in a person's
    SPARSE search-row data -- top-level field plus any embedded
    person_roles -- regardless of which period they're tagged with. This
    is only a cheap pre-filter for whether a full-record fetch is worth
    paying for; period-correctness is verified afterward against the full
    record, never assumed from this."""
    candidates = {_normalize_role(str(person.get("funktion") or ""))}
    for role in person.get("person_roles", []) or []:
        candidates.add(_normalize_role(str(role.get("funktion") or "")))
    candidates.discard("")
    return candidates


def _role_active_in_wahlperiode(target_norm: str, person: dict, wahlperiode: int) -> bool:
    """Whether this person held a role matching target_norm SPECIFICALLY
    during the given Wahlperiode. Checked only against person_roles
    entries tagged with that Wahlperiode -- never the top-level `funktion`
    field, which reflects the person's current/most-recent function, not
    the function they held during any specific earlier period."""
    for role in person.get("person_roles", []) or []:
        if wahlperiode not in _period_list(role.get("wahlperiode_nummer")):
            continue
        role_funktion = _normalize_role(str(role.get("funktion") or ""))
        if _role_matches(target_norm, role_funktion):
            return True

    # FALLBACK: DIP never tags top-office titles (Bundeskanzler,
    # Bundespräsident, ...) with a period-scoped `funktion` inside
    # person_roles -- only fraktion/ressort_titel history is tagged per
    # period there. The actual title lives solely in the top-level
    # `funktion` (representing the most senior office ever held, not
    # merely "current"). Refusing to use it produces a guaranteed-wrong
    # "not found" for every top-office query, for every person, in every
    # Wahlperiode -- there is no more precise signal available for this
    # category of role. Use the top-level funktion ONLY when this
    # person's period-scoped role entries carry no funktion data AT ALL
    # (i.e. this signal genuinely doesn't exist elsewhere for them), and
    # the target Wahlperiode is one they actually have records for.
    has_any_period_scoped_funktion = any(
        role.get("funktion") for role in person.get("person_roles", []) or []
    )
    if not has_any_period_scoped_funktion:
        top_funktion = _normalize_role(str(person.get("funktion") or ""))
        if _role_matches(target_norm, top_funktion) and wahlperiode in _period_list(person.get("wahlperiode")):
            return True

    return False


@mcp.tool(description=(
    "USE WHEN the user asks about who holds a specific ROLE or TITLE in a Wahlperiode "
    "(e.g. 'Who is the Bundeskanzler?', 'Who are the ministers?', 'Who is the President?'). "
    "DO NOT USE if the user provides a specific name. This tool fetches all persons for the "
    "given Wahlperiode and filters them locally by their role/funktion."))
async def get_persons_by_role(funktion: str, wahlperiode: int) -> dict:
    cache_key = f"persons_by_role:{funktion}:{wahlperiode}"
    cached = await _cache_get(cache_key)
    if cached is not None:
        if isinstance(cached, dict):
            cached["_source"] = "cache"
        return cached

    all_persons = await dip.get_all_persons_for_wahlperiode(wahlperiode)
    target_norm = _normalize_role(funktion)
    matched_persons = []
    backfill_count = 0

    for p in all_persons:
        candidate = p

        if not _has_period_scoped_roles(candidate, wahlperiode):
            # Sparse row: no period-scoped role data to check locally. A
            # full backfill (dip.get_person) resolves that, but is one DIP
            # API call PER PERSON -- unaffordable for every one of the
            # (often 500+) people in a Wahlperiode. Cheaply pre-filter
            # first: only pay for the backfill when some funktion string
            # already visible in the sparse row could plausibly match this
            # role at all, in any period.
            if not any(_role_matches(target_norm, c) for c in _funktion_candidates(candidate)):
                continue
            try:
                candidate = await dip.get_person(p["id"])
                backfill_count += 1
            except Exception:
                logger.exception("failed to backfill person_roles for person %s", p.get("id"))
                continue

        if not _role_active_in_wahlperiode(target_norm, candidate, wahlperiode):
            continue

        entry = {
            "id": candidate.get("id"),
            "vorname": candidate.get("vorname"),
            "nachname": candidate.get("nachname"),
            "funktion": candidate.get("funktion"),
            "fraktion": candidate.get("fraktion"),
            "resolved_fraktion": resolve_current_person_fraktion(candidate),
            "wahlperiode": candidate.get("wahlperiode"),
        }
        matched_persons.append(entry)

    data_notes = None
    if not matched_persons:
        data_notes = (f"No person matching role '{funktion}' was found in the "
                       f"data for Wahlperiode {wahlperiode}.")

    logger.info("backfilled %d/%d candidate persons via individual API calls for role=%r wahlperiode=%d",
                backfill_count, len(all_persons), funktion, wahlperiode)

    result = {"persons": matched_persons, "data_notes": data_notes, "_source": "live DIP API"}
    await _cache_set(cache_key, result)
    return result




@mcp.custom_route("/health", methods=["GET"])
async def health(_request):
    from starlette.responses import PlainTextResponse
    return PlainTextResponse("ok")


if __name__ == "__main__":
    transport = _get_required_env("MCP_TRANSPORT")
    logger.info("starting mcp-server (transport=%s, redis=%s)", transport, REDIS_URL)
    mcp.run(transport=transport)