from datetime import datetime, timezone

from pydantic import AwareDatetime, BaseModel, ConfigDict, model_validator


def normalize_utc(value: AwareDatetime) -> AwareDatetime:
    return value.astimezone(timezone.utc)


def parse_utc_iso(name: str, value: object) -> datetime:
    if not isinstance(value, str):
        raise ValueError(f"{name} must be a valid ISO 8601 timestamp")
    normalized = f"{value[:-1]}+00:00" if value.endswith("Z") else value
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError as exc:
        raise ValueError(f"{name} must be a valid ISO 8601 timestamp") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(f"{name} must include a timezone offset")
    return parsed.astimezone(timezone.utc)


class TemporalEnvelope(BaseModel):
    model_config = ConfigDict(frozen=True)

    event_time: AwareDatetime
    observed_at: AwareDatetime
    ingested_at: AwareDatetime
    effective_from: AwareDatetime | None = None
    effective_to: AwareDatetime | None = None
    source_revision: str | None = None
    raw_payload_hash: str | None = None

    @model_validator(mode="after")
    def validate_ordering(self) -> "TemporalEnvelope":
        for name in ("event_time", "observed_at", "ingested_at", "effective_from", "effective_to"):
            value = getattr(self, name)
            if value is not None:
                object.__setattr__(self, name, normalize_utc(value))
        if self.observed_at > self.ingested_at:
            raise ValueError("observed_at cannot be after ingested_at")
        if self.effective_from and self.effective_to and self.effective_from >= self.effective_to:
            raise ValueError("effective_from must be before effective_to")
        return self
