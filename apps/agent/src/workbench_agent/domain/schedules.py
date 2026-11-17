"""Cron schedules evaluated as physical instants in an IANA time zone."""
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
from croniter import croniter


def next_due(expression: str, zone_name: str, after: datetime) -> datetime:
    """Return the next matching UTC minute, including both fall-back folds.

    Nonexistent local times are skipped. Ambiguous wall-clock times represent
    two distinct UTC due_at values. Only standard five-field cron is supported.
    """
    if not isinstance(expression,str) or len(expression.split()) != 5 or not croniter.is_valid(expression):
        raise ValueError('invalid five-field cron')
    try: zone = ZoneInfo(zone_name)
    except (ZoneInfoNotFoundError,ValueError,TypeError): raise ValueError('invalid IANA timezone') from None
    if after.tzinfo is None or after.utcoffset() is None: raise ValueError('aware timestamp required')
    after = after.astimezone(timezone.utc)
    local = after.astimezone(zone)
    naive = local.replace(tzinfo=None)
    offset0=naive.replace(tzinfo=zone,fold=0).utcoffset()
    offset1=naive.replace(tzinfo=zone,fold=1).utcoffset()
    lookback = abs(offset0-offset1) if local.fold == 0 and offset0 != offset1 else timedelta(0)
    iterator=croniter(expression,naive-lookback-timedelta(microseconds=1))
    best=None
    for _ in range(10000):
        candidate=iterator.get_next(datetime)
        for fold in (0,1):
            aware=candidate.replace(tzinfo=zone,fold=fold)
            instant=aware.astimezone(timezone.utc)
            roundtrip=instant.astimezone(zone)
            if roundtrip.replace(tzinfo=None) != candidate or roundtrip.fold != fold or instant <= after:
                continue
            if best is None or instant < best: best=instant
        if best is not None and candidate > naive:
            return best
    raise ValueError('cron search limit exceeded')

from uuid import UUID
from pydantic import BaseModel, ConfigDict, Field, model_validator

class ScheduleScope(BaseModel):
    model_config = ConfigDict(frozen=True, extra='forbid')
    region: str = Field(min_length=1,max_length=128)
    service: str = Field(min_length=1,max_length=128)
    environment: str = Field(min_length=1,max_length=128)
    window_seconds: int = Field(gt=0,le=3600,strict=True)

    @model_validator(mode='after')
    def validate_values(self):
        if any(value != value.strip() or not value for value in (self.region,self.service,self.environment)):
            raise ValueError('invalid schedule scope')
        return self

    def values(self,anchor_ms):
        end=datetime.fromtimestamp(anchor_ms/1000,timezone.utc)
        start=end-timedelta(seconds=self.window_seconds)
        return {'region':self.region,'service':self.service,'environment':self.environment,
                'start_at':start.isoformat(),'end_at':end.isoformat()}

class ScheduleSpec(BaseModel):
    model_config = ConfigDict(frozen=True, extra='forbid')
    cron: str = Field(min_length=1,max_length=128)
    timezone: str = Field(min_length=1,max_length=128)
    workflow_version_id: UUID
    input_text: str = Field(min_length=1,max_length=4096)
    overlap_policy: str = 'forbid'
    query_scope: ScheduleScope | None = None

    @model_validator(mode='after')
    def validate_schedule(self):
        if self.overlap_policy != 'forbid': raise ValueError('unsupported overlap policy')
        next_due(self.cron,self.timezone,datetime(2026,1,1,tzinfo=timezone.utc))
        return self
