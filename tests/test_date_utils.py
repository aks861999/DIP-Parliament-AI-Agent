import os
import sys
from datetime import date

sys.path.append(os.path.join(os.path.dirname(__file__), "..", "services", "agent-service"))

import pytest

from date_utils import resolve_current_wahlperiode, WahlperiodeResolutionError


@pytest.mark.unit
def test_resolve_current_wahlperiode_on_wp21_start_date():
    assert resolve_current_wahlperiode(today=date(2025, 3, 25)) == 21


@pytest.mark.unit
def test_resolve_current_wahlperiode_day_before_wp21_still_wp20():
    assert resolve_current_wahlperiode(today=date(2025, 3, 24)) == 20


@pytest.mark.unit
def test_resolve_current_wahlperiode_raises_before_any_known_start():
    with pytest.raises(WahlperiodeResolutionError):
        resolve_current_wahlperiode(today=date(1900, 1, 1))
