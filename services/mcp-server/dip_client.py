import asyncio
import difflib
import json
import logging
import random
import time
from collections import defaultdict
from decimal import Decimal

import httpx
from cache import Cache  # NEW
from tool_contracts import PartyDistribution, PartyDistributionHistory

logger = logging.getLogger(__name__)


class DipClientError(Exception):
    pass


def normalize_str_field(value) -> str | None:
    if value is None:
        return None
    if isinstance(value, list):
        return ", ".join(str(v) for v in value) if value else None
    return str(value)


_KNOWN_FRAKTIONEN = {
    "SPD": "SPD",
    "CDU": "CDU/CSU",
    "CSU": "CDU/CSU",
    "CDU/CSU": "CDU/CSU",
    "GRÜNE": "GRÜNE",
    "BÜNDNIS 90/DIE GRÜNEN": "GRÜNE",
    "DIE LINKE": "DIE LINKE",
    "DIE LINKE.": "DIE LINKE",
    "FDP": "FDP",
    "AfD": "AfD",
}


def normalize_party(value) -> str | None:
    if not value:
        return None
    return _KNOWN_FRAKTIONEN.get(str(value).strip())



def suggest_person_names(query: str, directory: dict[str, dict],
                          limit: int = 3, cutoff: float = 0.72) -> list[dict]:
    close_keys = difflib.get_close_matches(query.strip().lower(), directory.keys(),
                                            n=limit, cutoff=cutoff)
    return [directory[k] for k in close_keys]


class DipClient:
    def __init__(
        self,
        base_url: str,
        api_key: str | None = None,
        timeout: float = 10.0,
        cache: "Cache | None" = None,
        min_request_interval_seconds: float = 0.35,
        
    ):  
        self._inflight: dict[str, asyncio.Lock] = {}
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.http = httpx.AsyncClient(
            timeout=timeout,
            limits=httpx.Limits(max_connections=4, max_keepalive_connections=4),
        )
        self.cache = cache
        self.min_request_interval_seconds = min_request_interval_seconds
        self._last_request_at = 0.0
        self._request_lock = asyncio.Lock()
        if not api_key:
            logger.warning(
                "DipClient initialized without an API key; DIP requires a valid "
                "key for EVERY request (see the API's 401 response) -- expect "
                "errors or empty results until DIP_API_KEY is set."
            )

    @staticmethod
    def _cache_key(path: str, params: dict) -> str:
        clean = {k: v for k, v in sorted(params.items()) if v is not None}
        return f"dip_raw:{path}:{json.dumps(clean, sort_keys=True)}"

    async def _throttle(self) -> None:
        """Minimum spacing between actual outbound network calls. Cache
        hits never reach this -- it only protects the network, so it
        generalizes to ANY caller (pagination loops, concurrent tool
        calls, retries) without knowing about any of them specifically."""
        async with self._request_lock:
            elapsed = time.monotonic() - self._last_request_at
            wait = self.min_request_interval_seconds - elapsed
            if wait > 0:
                logger.info("throttled outbound DIP request by %.2fs to respect rate limit", wait)
                await asyncio.sleep(wait)
            self._last_request_at = time.monotonic()


    async def search_all_persons(self, cursor: str | None = None) -> dict:
        return await self._get("/person", {"cursor": cursor}, ttl_seconds=86400)



    # --- REPLACE the entire build_name_directory method with: ---
    async def build_name_directory(self, max_pages: int = 5000) -> dict[str, dict]:
        directory: dict[str, dict] = {}
        cursor = None
        pages_seen = 0
        while True:
            page = await self.search_all_persons(cursor)
            documents = page.get("documents", [])
            for person in documents:
                vorname = person.get("vorname", "")
                nachname = person.get("nachname", "")
                namenszusatz = person.get("namenszusatz", "") or ""
                # Store the FULL raw person record, not a sparse subset.
                # Confirmed via diagnose_person_list_vs_id.py: GET /person
                # (list) already returns the exact same funktion/fraktion/
                # person_roles/wahlperiode data as GET /person/{id} -- the
                # crawl already carries everything get_person_info needs,
                # so discarding it down to 4 fields was throwing away data
                # unnecessarily.
                entry = dict(person)

                full_key = " ".join(part for part in (vorname, namenszusatz, nachname) if part).strip().lower()
                if full_key:
                    directory[full_key] = entry

                if namenszusatz:
                    short_key = " ".join(part for part in (vorname, nachname) if part).strip().lower()
                    if short_key:
                        directory.setdefault(short_key, entry)

            pages_seen += 1
            if pages_seen > max_pages:
                raise DipClientError(f"exceeded {max_pages} pages building the name directory — possible infinite cursor loop")
            next_cursor = page.get("cursor")
            if not documents or next_cursor is None or next_cursor == cursor:
                break
            cursor = next_cursor
        return directory

    async def _get(self, path: str, params: dict, ttl_seconds: int | None = None) -> dict:
        cache_key = self._cache_key(path, params)
        if self.cache is not None:
            cached = await self.cache.get(cache_key)
            if cached is not None:
                return cached

        lock = self._inflight.setdefault(cache_key, asyncio.Lock())
        if lock.locked():
            logger.info("piggybacking on in-flight request for %s (single-flight dedup)", cache_key)
        try:
            async with lock:
                if self.cache is not None:
                    cached = await self.cache.get(cache_key)
                    if cached is not None:
                        return cached

                headers = {
                    "Authorization": f"ApiKey {self.api_key}" if self.api_key else "",
                    "User-Agent": "dip-agent/1.0 (+https://github.com/aks861999/dip-agent)",
                }
                clean_params = {k: v for k, v in params.items() if v is not None}

                last_exc: httpx.HTTPStatusError | None = None
                for attempt in range(5):
                    await self._throttle()
                    try:
                        resp = await self.http.get(
                            f"{self.base_url}{path}", params=clean_params, headers=headers
                        )
                        resp.raise_for_status()
                        data = resp.json()
                        if self.cache is not None:
                            await self.cache.set(cache_key, data, ttl_seconds=ttl_seconds)
                        return data

                    except httpx.HTTPStatusError as e:
                        last_exc = e
                        if resp.status_code == 429 or resp.is_redirect:
                            delay = min(2 ** attempt + random.uniform(0, 1), 30)
                            retry_after = resp.headers.get("Retry-After")
                            if retry_after:
                                try:
                                    delay = max(delay, float(retry_after))
                                except ValueError:
                                    pass
                            logger.warning(
                                "DIP request throttled/challenged (status=%s), retrying "
                                "attempt %d/5 in %.1fs: %s",
                                resp.status_code, attempt + 1, delay, path,
                            )
                            await asyncio.sleep(delay)
                            continue
                        raise

                    except (httpx.ConnectError, httpx.ReadTimeout, httpx.ConnectTimeout) as e:
                        last_exc = e
                        delay = min(2 ** attempt + random.uniform(0, 1), 30)
                        logger.warning(
                            "DIP network error (%s), retrying attempt %d/5 in %.1fs: %s",
                            type(e).__name__, attempt + 1, delay, path,
                        )
                        await asyncio.sleep(delay)
                        continue

                if isinstance(last_exc, httpx.HTTPStatusError) and last_exc.response.is_redirect:
                    logger.error(
                        "DIP request still being challenge-redirected after 5 retries "
                        "for %s -- verify with dip_deep_diagnostic.py from a different "
                        "network; if confirmed, contact parlamentsdokumentation@bundestag.de.",
                        path,
                    )
                raise last_exc
        finally:
            # Prevent _inflight from growing forever: only remove the entry
            # if no OTHER concurrent caller is currently waiting on it --
            # checking .locked() right after our own `async with` released
            # it tells us whether a waiter grabbed it before we could clean up.
            if cache_key in self._inflight and not self._inflight[cache_key].locked():
                del self._inflight[cache_key]


    async def get_person(self, person_id: str) -> dict:
        return await self._get(f"/person/{person_id}", {}, ttl_seconds=86400) 

    async def search_persons_by_wahlperioden(self, wahlperioden: list[int], cursor: str | None = None) -> dict:
        return await self._get("/person", {"f.wahlperiode": wahlperioden, "cursor": cursor})

    async def get_all_persons_for_wahlperioden(self, wahlperioden: list[int]) -> list[dict]:
        """Fetches all persons across one or more Wahlperioden in ONE paginated
        crawl, using the DIP API's repeatable f.wahlperiode array (OR semantics,
        per the OpenAPI spec). A single-element list behaves exactly like the
        old single-Wahlperiode call; multiple elements cover a comparison or
        full-history scan in one crawl instead of one crawl PER Wahlperiode."""
        documents = []
        cursor = None
        while True:
            page = await self.search_persons_by_wahlperioden(wahlperioden, cursor)
            docs = page.get("documents", [])
            documents.extend(docs)
            cursor = page.get("cursor")
            if not docs or cursor is None:
                break
        return documents

    async def aclose(self) -> None:
        await self.http.aclose()


def _fraktion_for_wahlperiode(person: dict, wahlperiode: int) -> str | None:
    def _includes(periods, n: int) -> bool:
        if periods is None:
            return False
        if isinstance(periods, int):
            return periods == n
        return n in periods

    for role in person.get("person_roles", []) or []:
        if _includes(role.get("wahlperiode_nummer"), wahlperiode) and role.get("fraktion"):
            return normalize_party(normalize_str_field(role["fraktion"]))

    if _includes(person.get("wahlperiode"), wahlperiode) and person.get("fraktion"):
        return normalize_party(normalize_str_field(person["fraktion"]))

    return None



def resolve_current_person_fraktion(person: dict) -> str | None:
    def _periods(value) -> list[int]:
        if value is None:
            return []
        if isinstance(value, int):
            return [value]
        return list(value)

    all_periods: list[int] = []
    for role in person.get("person_roles", []) or []:
        all_periods.extend(_periods(role.get("wahlperiode_nummer")))
    all_periods.extend(_periods(person.get("wahlperiode")))

    if not all_periods:
        return None

    # A person's role entry for their most recent Wahlperiode doesn't
    # always carry a `fraktion` (e.g. a government post like Bundeskanzler
    # has no fraktion of its own even though the party is unchanged and
    # recorded on an earlier-period role). Fall back through every
    # period, most recent first, and return the first that has one.
    for period in sorted(set(all_periods), reverse=True):
        fraktion = _fraktion_for_wahlperiode(person, period)
        if fraktion is not None:
            return fraktion
    return None








async def aggregate_party_distribution_history(
    client: "DipClient", wahlperioden: list[int]
) -> PartyDistributionHistory:
    all_persons = await client.get_all_persons_for_wahlperioden(wahlperioden)

    per_wp_counts: dict[int, dict[str, int]] = {wp: defaultdict(int) for wp in wahlperioden}
    per_wp_total: dict[int, int] = {wp: 0 for wp in wahlperioden}
    per_wp_unclassified: dict[int, int] = {wp: 0 for wp in wahlperioden}

    for wp in wahlperioden:
        for person in all_persons:
            periods = person.get("wahlperiode")
            periods_list = [periods] if isinstance(periods, int) else (periods or [])
            if wp not in periods_list:
                continue
            per_wp_total[wp] += 1
            fraktion = _fraktion_for_wahlperiode(person, wp)
            if fraktion is None:
                per_wp_unclassified[wp] += 1
            else:
                per_wp_counts[wp][fraktion] += 1

    distributions = []
    for wp in wahlperioden:
        total = per_wp_total[wp]
        if total == 0:
            continue
        classified_total = total - per_wp_unclassified[wp]
        percentages = {}
        if classified_total > 0:
            percentages = {
                p: float((Decimal(c) / Decimal(classified_total) * 100).quantize(Decimal("0.01")))
                for p, c in per_wp_counts[wp].items()
            }
        distributions.append(PartyDistribution(
            wahlperiode=wp, date_range=None, counts=dict(per_wp_counts[wp]),
            percentages=percentages, total_persons=total,
            unclassified_count=per_wp_unclassified[wp],
        ))

    logger.info("aggregated party distribution history for wahlperioden=%s: %d entries",
                wahlperioden, len(distributions))
    return PartyDistributionHistory(distributions=distributions)