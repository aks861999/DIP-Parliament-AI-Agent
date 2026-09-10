from datetime import date

WAHLPERIODE_START_DATES = {
    1: date(1949, 9, 7), 2: date(1953, 10, 6), 3: date(1957, 10, 15),
    4: date(1961, 10, 17), 5: date(1965, 10, 19), 6: date(1969, 10, 20),
    7: date(1972, 12, 13), 8: date(1976, 12, 14), 9: date(1980, 11, 4),
    10: date(1983, 3, 29), 11: date(1987, 2, 18), 12: date(1990, 12, 20),
    13: date(1994, 11, 10), 14: date(1998, 10, 26), 15: date(2002, 10, 17),
    16: date(2005, 10, 18), 17: date(2009, 10, 27), 18: date(2013, 10, 22),
    19: date(2017, 10, 24), 20: date(2021, 10, 26), 21: date(2025, 3, 25),
}


def _wahlperiode_for_date(d: date) -> int | None:
    cands = [wp for wp, s in WAHLPERIODE_START_DATES.items() if s <= d]
    return max(cands) if cands else None