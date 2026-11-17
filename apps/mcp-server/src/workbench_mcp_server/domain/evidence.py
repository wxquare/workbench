from dataclasses import dataclass
from datetime import datetime

from .scope import QueryScope, TimeWindow


@dataclass(frozen=True)
class Evidence:
    evidence_id: str
    source_type: str
    tool_binding_id: str
    invocation_id: str
    query_scope_ref: str
    scope: QueryScope
    time_window: TimeWindow | None
    observed_at: datetime
    summary: str
    source_uri: str | None
    quality_status: str
    source_locator: str

    def __post_init__(self):
        if self.quality_status not in {"valid", "empty", "unavailable", "conflicting", "stale"}:
            raise ValueError("Invalid evidence quality")
        if not all(
            (
                self.evidence_id,
                self.tool_binding_id,
                self.invocation_id,
                self.query_scope_ref,
                self.source_locator,
            )
        ):
            raise ValueError("Evidence provenance is required")
        if self.observed_at.utcoffset() is None:
            raise ValueError("Evidence observation requires timezone")
