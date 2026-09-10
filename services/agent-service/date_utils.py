from datetime import date, datetime, time, timezone

import dateparser
from pydantic import BaseModel


class DateResolutionAmbiguousError(Exception):

    def __init__(self, expression: str):
        super().__init__(f"cannot resolve date expression: {expression}")
        self.expression = expression


class DateRangeExtraction(BaseModel):
    start: str | None = None  # ISO-8601 YYYY-MM-DD or None
    end: str | None = None





_WAHLPERIODE_START_DATES: dict[int, date] ={
    1: date(1949, 9, 7), 2: date(1953, 10, 6), 3: date(1957, 10, 15),
    4: date(1961, 10, 17), 5: date(1965, 10, 19), 6: date(1969, 10, 20),
    7: date(1972, 12, 13), 8: date(1976, 12, 14), 9: date(1980, 11, 4),
    10: date(1983, 3, 29), 11: date(1987, 2, 18), 12: date(1990, 12, 20),
    13: date(1994, 11, 10), 14: date(1998, 10, 26), 15: date(2002, 10, 17),
    16: date(2005, 10, 18), 17: date(2009, 10, 27), 18: date(2013, 10, 22),
    19: date(2017, 10, 24), 20: date(2021, 10, 26), 21: date(2025, 3, 25),
}


class WahlperiodeResolutionError(Exception):
    pass


def resolve_current_wahlperiode(today: date | None = None) -> int:
    today = today or datetime.now(timezone.utc).date()
    candidates = [wp for wp, start in _WAHLPERIODE_START_DATES.items() if start <= today]
    if not candidates:
        raise WahlperiodeResolutionError(
            "no known Wahlperiode start date has passed — is _WAHLPERIODE_START_DATES stale?")
    return max(candidates)


def resolve_wahlperiode_from_date(target_date: date) -> int | None:
    candidates = [wp for wp, start in _WAHLPERIODE_START_DATES.items()
                  if start <= target_date]
    if not candidates:
        return None
    return max(candidates)


_DATE_EXTRACTION_PROMPT = """\
Extract the start and end date from this date expression (German or English,
e.g. "bis März 2023", "between 2021 and 2023", "seit 2017", "in den letzten
zwei Jahren", "zwischen Januar 2021 und Ende 2022").

Return ISO-8601 dates (YYYY-MM-DD). Use null for an open/unbounded end.
If the expression is a single point in time, return it as both start and end.
If you cannot extract any date, return null for both."""


import re

_WAHLPERIODE_REF_RE = re.compile(
    r"\b(wahlperiode|legislative period|election period|parliamentary"
    r" period|parliamentary term|wp\.?)\b", re.IGNORECASE)



async def resolve_date_expression(llm, expression: str,
                                  relative_base: date | None = None) -> dict | None:
    
    if _WAHLPERIODE_REF_RE.search(expression):
        return None  # not a date — e.g. "election period 20"; never ask the user
    
    base = relative_base or datetime.now(timezone.utc).date()
    anchor = f"Reference: today is {base.isoformat()}.\nExpression: {expression!r}"
    extracted: DateRangeExtraction = await llm.with_structured_output(
        DateRangeExtraction).ainvoke(
        [("system", _DATE_EXTRACTION_PROMPT), ("user", anchor)])

    if extracted.start is None and extracted.end is None:
        # Cheap deterministic fallback for simple single dates
        settings = {
            "RELATIVE_BASE": datetime.combine(base, time.min),
            "PREFER_DATES_FROM": "past",
            "DATE_ORDER": "DMY",
        }
        parsed = dateparser.parse(expression, settings=settings)
        if parsed is None or expression.lower().strip() in {"recently", "lately"}:
            raise DateResolutionAmbiguousError(expression)
        iso = parsed.date().isoformat()
        return {"start": iso, "end": iso}

    return {"start": extracted.start, "end": extracted.end}