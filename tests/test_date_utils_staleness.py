from datetime import date
import pytest
import sys
import os
sys.path.append(os.path.join(os.path.dirname(__file__), "..", "services", "agent-service"))

from date_utils import (
    resolve_current_wahlperiode, WahlperiodeResolutionError, _WAHLPERIODE_START_DATES)

def test_resolve_current_wahlperiode_still_raises_before_any_known_start():
    # Sanity check that the raising branch of resolve_current_wahlperiode
    # still exists and works -- kept separate from the staleness check
    # below since they test two different things.
    with pytest.raises(WahlperiodeResolutionError):
        resolve_current_wahlperiode(today=date(1900, 1, 1))

def test_wahlperiode_table_not_stale():
    """Flags if no Wahlperiode has been added in an implausibly long time --
    German elections happen roughly every 4 years, so if the newest known
    start date is more than ~6 years old, the table likely needs an entry
    for a Wahlperiode that has since started."""
    newest_known_start = max(_WAHLPERIODE_START_DATES.values())
    years_since_newest = (date.today() - newest_known_start).days / 365.25
    assert years_since_newest < 6, (
        f"Newest known Wahlperiode started {newest_known_start} "
        f"({years_since_newest:.1f} years ago) -- check dip.bundestag.de "
        "for a newer election and add it to _WAHLPERIODE_START_DATES."
    )