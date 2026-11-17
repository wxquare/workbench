"""Conservative deterministic baseline assessment; no model confidence involved."""
from dataclasses import dataclass
from typing import Literal


@dataclass(frozen=True)
class BaselineObservation:
    step: str
    status: Literal['valid', 'empty', 'unavailable', 'conflicting', 'stale']
    evidence_ref: str | None
    scope_ref: str | None


@dataclass(frozen=True)
class BaselineAssessment:
    complete: bool
    business_normal: bool
    statuses: dict[str, str]
    evidence_refs: tuple[str, ...]


def evaluate_baseline(scope_ref: str, observations: list[BaselineObservation], *, required: set[str]):
    if not isinstance(scope_ref, str) or not scope_ref.strip() or not required:
        raise ValueError('a scope and required checks are needed')
    if any(not isinstance(step, str) or not step.strip() for step in required):
        raise ValueError('invalid required check')
    statuses = {}
    refs = []
    for item in observations:
        if item.step in statuses or not item.step or item.status not in {'valid', 'empty', 'unavailable', 'conflicting', 'stale'}:
            raise ValueError('duplicate or invalid baseline observation')
        status = item.status if item.scope_ref == scope_ref else 'stale'
        if status == 'valid' and (not isinstance(item.evidence_ref, str) or not item.evidence_ref.strip()):
            status = 'unavailable'
        statuses[item.step] = status
        if status == 'valid':
            refs.append(item.evidence_ref)
    complete = all(statuses.get(step) == 'valid' for step in required)
    # Successful data retrieval does not prove business health. A separate,
    # explicitly defined business check is required before declaring normal.
    return BaselineAssessment(complete, False, statuses, tuple(refs))
