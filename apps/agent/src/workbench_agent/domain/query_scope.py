"""Pure alert-scope resolution; callers must authenticate authoritative details.

These values are not themselves persistence or authorization. A caller must
persist the result through ContextResolver in a guarded transaction.
"""

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone


def validate_qa_scope(values):
    """Validate the public QA scope; deployment grants remain authoritative."""
    keys = {'service', 'environment', 'region', 'start_at', 'end_at'}
    if not isinstance(values, dict) or set(values) != keys:
        raise ValueError('invalid scope')
    if any(
        not isinstance(value, str)
        or not value.strip()
        or value != value.strip()
        or len(value) > 128
        for value in values.values()
    ):
        raise ValueError('invalid scope')

    def parse(raw):
        try:
            return datetime.strptime(raw, '%Y-%m-%d %H:%M:%S')
        except ValueError:
            result = datetime.fromisoformat(raw.replace('Z', '+00:00'))
            if result.tzinfo is None or result.utcoffset() is None:
                raise ValueError('timezone required')
            return result

    try:
        start, end = parse(values['start_at']), parse(values['end_at'])
        seconds = (end - start).total_seconds()
        if not 0 < seconds <= 3600:
            raise ValueError('invalid window')
    except (ValueError, TypeError, OverflowError):
        raise ValueError('invalid scope window') from None
    return dict(values)


@dataclass(frozen=True)
class AlertScopeInput:
    team: str
    service: str
    environment: str
    body_region: str | None
    link_region: str | None
    anchor: str
    window_seconds: int


@dataclass(frozen=True)
class AlertScope:
    team: str
    service: str
    environment: str
    region: str
    anchor: datetime
    start: datetime
    end: datetime


@dataclass(frozen=True)
class ScopeResolution:
    scope: AlertScope | None
    required_fields: tuple[str, ...] = ()


def _field(value):
    if not isinstance(value, str) or not value.strip() or value != value.strip():
        raise ValueError('invalid scope field')
    return value


def _validated(alert):
    for value in (alert.team, alert.service, alert.environment):
        _field(value)
    for value in (alert.body_region, alert.link_region):
        if value is not None:
            _field(value)
    if type(alert.window_seconds) is not int or not 1 <= alert.window_seconds <= 3600:
        raise ValueError('invalid scope window')
    try:
        anchor = datetime.fromisoformat(alert.anchor.replace('Z', '+00:00'))
        if anchor.tzinfo is None or anchor.utcoffset() is None:
            raise ValueError('timezone required')
        return anchor.astimezone(timezone.utc)
    except (ValueError, TypeError, AttributeError, OverflowError):
        raise ValueError('invalid scope anchor') from None


def alert_window(alert: AlertScopeInput):
    """Return the fixed UTC window even when the alert region is unresolved."""
    anchor = _validated(alert)
    try:
        return anchor - timedelta(seconds=alert.window_seconds), anchor
    except OverflowError:
        raise ValueError('invalid scope window') from None


def resolve_alert_scope(alert: AlertScopeInput, *, authoritative_region: str | None = None):
    anchor = _validated(alert)
    if authoritative_region is not None:
        region = _field(authoritative_region)
    else:
        candidates = {
            value for value in (alert.body_region, alert.link_region) if value is not None
        }
        if len(candidates) != 1:
            return ScopeResolution(None, ('region',))
        region = candidates.pop()
    try:
        start = anchor - timedelta(seconds=alert.window_seconds)
    except OverflowError:
        raise ValueError('invalid scope window') from None
    return ScopeResolution(
        AlertScope(alert.team, alert.service, alert.environment, region, anchor, start, anchor)
    )


def revise_alert_scope(alert: AlertScopeInput, values: dict):
    if not isinstance(values, dict) or set(values) != {'region'}:
        raise ValueError('only the requested region may be changed')
    return resolve_alert_scope(alert, authoritative_region=_field(values['region'])).scope
