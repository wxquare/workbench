from typing import Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field


class Payload(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True, allow_inf_nan=False)


class KnowledgeMatch(Payload):
    source_id: str = Field(min_length=1, max_length=512)
    title: str = Field(max_length=4000)
    content: str = Field(max_length=100000)
    source_uri: str | None = None
    source_locator: str | None = None
    updated_at: AwareDatetime | None = None


class Guidance(Payload):
    content: str
    steps: list[str] = Field(default_factory=list)
    required_evidence: list[str] = Field(default_factory=list)
    source_ids: list[str] = Field(default_factory=list)
    applicability: str | None = None
    matched_conditions: list[str] = Field(default_factory=list)
    missing_conditions: list[str] = Field(default_factory=list)
    limitations: list[str] = Field(default_factory=list)
    source_rule: str | None = None
    relevance_score: float | None = None


class Hypothesis(Payload):
    content: str
    required_evidence: list[str] = Field(default_factory=list)
    source_ids: list[str] = Field(default_factory=list)
    matched_conditions: list[str] = Field(default_factory=list)
    missing_conditions: list[str] = Field(default_factory=list)


class ConfirmedKnowledgeScope(Payload):
    product: list[str] = Field(default_factory=list)
    domain: list[str] = Field(default_factory=list)
    provider: list[str] = Field(default_factory=list)
    market: list[str] = Field(default_factory=list)


class KnowledgeScopeFilter(Payload):
    confirmed: ConfirmedKnowledgeScope
    rejected_candidates: int = Field(default=0, ge=0)
    missing_metadata_policy: str | None = None


class KnowledgeData(Payload):
    matches: list[KnowledgeMatch]
    guidance: list[Guidance] = Field(default_factory=list)
    hypotheses: list[Hypothesis] = Field(default_factory=list)
    release_id: str | None = None
    scope_filter: KnowledgeScopeFilter | None = None
    truncated: bool = False


class MetricSeries(Payload):
    labels: dict[str, str]
    points: list[tuple[AwareDatetime, float]]
    unit: str
    source_id: str = Field(min_length=1)


class MetricsData(Payload):
    series: list[MetricSeries]
    missing: list[str] = Field(default_factory=list)
    truncated: bool = False


class LogEntry(Payload):
    timestamp: AwareDatetime
    service: str
    level: Literal["DEBUG", "INFO", "WARN", "ERROR", "FATAL"]
    message: str
    record_id: str = Field(min_length=1)


class LogsData(Payload):
    entries: list[LogEntry]
    next_cursor: str | None = Field(default=None, max_length=4096)
    truncated: bool = False


class Span(Payload):
    span_id: str = Field(min_length=1)
    parent_id: str | None = None
    service: str
    name: str
    error: bool
    start: AwareDatetime
    end: AwareDatetime


class TraceData(Payload):
    trace_id: str = Field(min_length=1, max_length=128)
    spans: list[Span]
    omitted_spans: int = Field(default=0, ge=0)
    truncated: bool = False


DATA_MODELS = {
    "knowledge": KnowledgeData,
    "metrics": MetricsData,
    "logs": LogsData,
    "trace": TraceData,
}
