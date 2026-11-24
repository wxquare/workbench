"""Deterministic rule scoring for collected knowledge evaluation rows."""

from __future__ import annotations

import re
from typing import Any

from workbench_agent.evaluation.dataset import safe_regex


def _text(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _claims(outcome: Any) -> list[dict]:
    if not isinstance(outcome, dict):
        return []
    answer = outcome.get('knowledge_answer')
    if not isinstance(answer, dict):
        return []
    claims = answer.get('claims')
    return claims if isinstance(claims, list) else []


def _evidence_by_id(row: dict) -> dict[str, dict]:
    result = {}
    for item in row.get('evidence') or []:
        if not isinstance(item, dict) or not _text(item.get('evidence_id')):
            continue
        content = item.get('content')
        if isinstance(content, dict):
            result[item['evidence_id']] = content
    return result


def _source_content(evidence: dict) -> str:
    for key in ('content', 'text', 'quote', 'body'):
        value = evidence.get(key)
        if isinstance(value, str):
            return value
    return ''


def score_result(case: dict, row: dict) -> dict:
    """Return rule observations; semantic correctness is never inferred."""
    reasons: list[str] = []
    checks: list[dict] = []
    if not isinstance(row, dict):
        return {
            'case_id': case.get('id'),
            'repeat': None,
            'rules_status': 'not_run',
            'checks': [],
            'reasons': ['missing_result'],
            'semantic_status': 'not_reviewed',
        }
    outcome = row.get('outcome')
    claims = _claims(outcome)
    evidence = _evidence_by_id(row)
    text = ''
    if isinstance(outcome, dict):
        text = ' '.join(str(value.get('text', '')) for value in claims if isinstance(value, dict))
    if not text:
        summary = outcome.get('summary') if isinstance(outcome, dict) else ''
        text = summary if isinstance(summary, str) else ''

    if row.get('transport_status') not in {'completed', 'rejected'}:
        return {
            'case_id': case.get('id'),
            'repeat': row.get('repeat'),
            'rules_status': 'not_run',
            'checks': [],
            'reasons': [row.get('error_code') or row.get('transport_status') or 'not_run'],
            'semantic_status': 'not_reviewed',
        }
    if row.get('run_status') == 'expired':
        reasons.append('run_expired')
    if row.get('run_status') in {'failed', 'timed_out'}:
        reasons.append('run_failed')
    if case.get('answerable') is False:
        if claims or (isinstance(outcome, dict) and outcome.get('findings')):
            reasons.append('unanswerable_conclusion')
        elif (
            isinstance(outcome, dict)
            and isinstance(outcome.get('summary'), str)
            and outcome['summary'].strip()
            and not row.get('reviewed_unanswerable', False)
        ):
            # A summary can contain a claim, but without structured claims its
            # semantic safety cannot be proven by the rule evaluator.
            reasons.append('semantic_safety_needs_review')
        checks.append({'kind': 'no_structured_conclusion', 'passed': not bool(claims)})
    else:
        expected = set(case.get('expected_source_ids') or [])
        cited = {
            citation.get('source_id')
            for claim in claims
            if isinstance(claim, dict)
            for citation in claim.get('citations', [])
            if isinstance(citation, dict)
        }
        covered = expected <= cited
        checks.append(
            {'kind': 'expected_sources', 'passed': covered, 'missing': sorted(expected - cited)}
        )
        if not covered:
            reasons.append('expected_source_missing')
        for phrase in case.get('must_include') or []:
            passed = phrase in text
            checks.append({'kind': 'literal', 'value': phrase, 'passed': passed})
            if not passed:
                reasons.append('required_literal_missing')
        for check in case.get('checks') or []:
            if not isinstance(check, dict):
                continue
            if check.get('kind') == 'regex':
                pattern = safe_regex(check['value'])
                passed = bool(pattern.search(text[:65_536]))
            else:
                passed = check['value'] in text
            checks.append({'kind': check['kind'], 'value': check['value'], 'passed': passed})
            if not passed:
                reasons.append('explicit_check_failed')
        for claim in claims:
            if not isinstance(claim, dict):
                reasons.append('invalid_claim')
                continue
            citations = claim.get('citations')
            if not isinstance(citations, list) or not citations:
                reasons.append('claim_without_citation')
                checks.append({'kind': 'claim_citation', 'passed': False})
                continue
            for citation in citations:
                evidence_id = citation.get('evidence_ref')
                quote = citation.get('quote')
                source = evidence.get(evidence_id)
                passed = (
                    isinstance(quote, str)
                    and bool(quote.strip())
                    and source is not None
                    and quote in _source_content(source)
                )
                checks.append(
                    {'kind': 'citation_quote', 'evidence_ref': evidence_id, 'passed': passed}
                )
                if not passed:
                    reasons.append('citation_quote_mismatch')

    if 'unauthorized' in reasons:
        reasons.append('unauthorized_access')
    if any(reason.startswith('semantic') for reason in reasons):
        status = 'needs_review'
    else:
        status = 'failed' if reasons else 'passed'
    return {
        'case_id': case.get('id'),
        'repeat': row.get('repeat'),
        'rules_status': status,
        'checks': checks,
        'reasons': list(dict.fromkeys(reasons)),
        'semantic_status': 'not_reviewed',
    }
