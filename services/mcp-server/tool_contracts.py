from datetime import date

from pydantic import BaseModel, Field, field_validator


class DateRange(BaseModel):
    start: str | None = None
    end: str | None = None

    @field_validator("start", "end")
    @classmethod
    def _validate_iso_date(cls, value: str | None) -> str | None:
        if value is None:
            return value
        try:
            date.fromisoformat(value)
        except ValueError:
            raise ValueError(f"expected an ISO-8601 date (YYYY-MM-DD), got {value!r}")
        return value


class PartyDistribution(BaseModel):
    wahlperiode: int = Field(ge=1, le=21)
    date_range: DateRange | None = None
    counts: dict[str, int]
    percentages: dict[str, float]
    total_persons: int
    unclassified_count: int
    data_notes: str | None = None


class PersonInfoResult(BaseModel):
    person: dict | None = None
    matches: list[dict] = []
    suggestions: list[dict] = []       # NEW: close matches on exact miss
    resolved_fraktion: str | None = None
    data_notes: str | None = None      # NEW: clarification hint for the graph