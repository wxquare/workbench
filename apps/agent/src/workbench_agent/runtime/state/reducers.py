"""Deterministic structured reducers; expert input never replaces evidence silently."""

from copy import deepcopy


def merge_evidence(existing: dict, updates: dict) -> dict:
    merged = deepcopy(existing)
    for evidence_id, evidence in updates.items():
        actual = evidence.get('evidence_id') if isinstance(evidence, dict) else evidence.evidence_id
        if actual != evidence_id:
            raise ValueError('evidence identity mismatch')
        if evidence_id in merged and merged[evidence_id] != evidence:
            raise ValueError('conflicting evidence identity')
        merged[evidence_id] = deepcopy(evidence)
    return merged


def merge_hypotheses(existing: dict, updates: dict) -> dict:
    merged = deepcopy(existing)
    for hypothesis_id, value in updates.items():
        if value.get('hypothesis_id') != hypothesis_id:
            raise ValueError('hypothesis identity mismatch')
        previous = merged.get(hypothesis_id)
        if previous is None:
            merged[hypothesis_id] = deepcopy(value)
            continue
        if previous.get('summary') != value.get('summary'):
            raise ValueError('conflicting hypothesis identity')
        for key in ('support', 'contradict'):
            previous[key] = list(dict.fromkeys((*previous.get(key, ()), *value.get(key, ()))))
    return merged
