from dataclasses import dataclass
from datetime import datetime, timezone


@dataclass(frozen=True)
class QueryScope:
    team: str
    environment: str
    region: str
    service: str
    source_id: str

    def __post_init__(self):
        for value in (self.team, self.environment, self.region, self.service, self.source_id):
            if not isinstance(value, str) or not value.strip() or value != value.strip():
                raise ValueError("Scope dimensions must be explicit")
            if len(value) > 128 or value.lower() in {"*", "all"} or "," in value:
                raise ValueError("Scope dimensions must identify one resource")


@dataclass(frozen=True)
class TimeWindow:
    start: datetime
    end: datetime

    def __post_init__(self):
        if self.start.utcoffset() is None or self.end.utcoffset() is None:
            raise ValueError("Timezone is required")
        if not 0 < (self.end - self.start).total_seconds() <= 3600:
            raise ValueError("Time window must be positive and at most one hour")
        object.__setattr__(self, "start", self.start.astimezone(timezone.utc))
        object.__setattr__(self, "end", self.end.astimezone(timezone.utc))
