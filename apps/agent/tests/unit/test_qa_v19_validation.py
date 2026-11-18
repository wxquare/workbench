from datetime import datetime, timedelta, timezone

import pytest
from pydantic import ValidationError
from workbench_agent.runtime.graphs.qa_v19_validation import AnswerClaim, CitedAnswer, KnowledgeSource, extract_knowledge_sources, verify_answer

NOW = datetime(2026, 11, 18, tzinfo=timezone.utc)

def source(**changes):
    values = dict(evidence_ref='e1', source_id='s1', content='CPU is 80% on `node-1`', updated_at=NOW, source_uri='https://docs.example.test/s1')
    return KnowledgeSource(**(values | changes))

def claim(**changes):
    values = dict(text='CPU is 80%', evidence_ref='e1', source_id='s1', quote='CPU is 80%')
    return AnswerClaim(**(values | changes))

def answer(claims=None, **changes):
    values = dict(answerable=True, claims=[claim()] if claims is None else claims, unanswered=[], conflicts=[])
    return CitedAnswer(**(values | changes))

def check(a, sources, **kwargs):
    return verify_answer(a, sources, now=kwargs.get('now', NOW), max_age=kwargs.get('max_age', timedelta(days=30)))

def test_valid_claim_is_bound_to_its_source():
    s = source()
    result = check(answer(), [s])
    assert result.completed and result.accepted == [(claim(), s)] and not result.rejection_reasons

@pytest.mark.parametrize('a,sources,options', [
    (answer(), [source(updated_at=NOW - timedelta(days=31)), source(source_id='s2', content='CPU is 50%')], {}),
    (answer(), [source(source_id='other')], {}), (answer(), [source(), source()], {}),
    (answer([]), [source()], {}), (answer([claim(), claim()]), [source()], {}),
    (answer([claim(quote='CPU is 50%')]), [source()], {}),
    (answer(), [source(updated_at=NOW.replace(tzinfo=None))], {}),
    (answer(), [source(updated_at=NOW + timedelta(seconds=1))], {}),
    (answer(), [source()], {'max_age': None}), (answer(unanswered=['Cause unknown']), [source()], {}),
    (answer(conflicts=['Disagreement']), [source()], {}), (answer(answerable=False), [source()], {}),
    (answer([claim(text='CPU is 90%')]), [source()], {}),
    (answer([claim(text='Use `node-2`')]), [source()], {}),
    (answer([claim(text='CPU is 80%')]), [source(content='cpu is 80%')], {}),
])
def test_incomplete_or_unverifiable_answers_are_rejected(a, sources, options):
    result = check(a, sources, **options)
    assert not result.completed and result.rejection_reasons

def test_whitespace_normalization_and_unicode():
    s = source(content='温度\n  -2.5%  version v1.2.3 /srv/数据')
    c = claim(text='温度 -2.5% version v1.2.3 /srv/数据', quote='温度 -2.5% version v1.2.3 /srv/数据')
    assert check(answer([c]), [s]).completed

@pytest.mark.parametrize('text', ['温度 2.5%', 'version v1.2.4', '日期 2026-11-19', 'path `/srv/other`', 'ALARM'])
def test_claim_identifiers_and_numbers_must_be_present_in_same_source(text):
    s = source(content='温度 -2.5% version v1.2.3 日期 2026-11-18 path `/srv/data` OK')
    assert not check(answer([claim(text=text, quote='温度 -2.5%')]), [s]).completed

def test_models_forbid_extra_and_enforce_bounds():
    with pytest.raises(ValidationError): AnswerClaim(text='', evidence_ref='e', source_id='s', quote='x')
    with pytest.raises(ValidationError): CitedAnswer(answerable=True, claims=[], unanswered=[], conflicts=[], extra='bad')
    with pytest.raises(ValidationError): answer([claim()] * 7)

def payload(**changes):
    return {'schema_version': 'dod-tool-result-v1', 'status': 'ok', 'data': {'matches': [{'source_id': 's1', 'content': 'CPU is 80%', 'updated_at': NOW.isoformat(), 'source_uri': 'https://docs.example.test/s1'}], **changes}}

def test_extract_source_from_authorized_payload():
    result = extract_knowledge_sources('e1', payload())
    assert result.sources == [source(content='CPU is 80%')] and not result.rejection_reasons

@pytest.mark.parametrize('raw', [None, {}, {'schema_version': 'dod-tool-result-v1', 'status': 'error'}, payload(matches=[{'source_id': 's1', 'content': 1}]), payload(truncated=True), payload(matches=[{'source_id': 's1', 'content': 'x' * 4097}]), payload(matches=[{'source_id': 's1', 'content': 'x'}, {'source_id': 's1', 'content': 'y'}]), payload(matches='bad'), payload(matches=[])])
def test_extract_reports_bad_empty_or_truncated_payload(raw):
    assert extract_knowledge_sources('e1', raw).rejection_reasons


def test_oversized_direct_source_cannot_support_a_claim():
    assert not check(answer(), [source(content='CPU is 80%' + 'x' * 4096)]).completed


def test_accepted_claim_can_form_existing_outcome_contract():
    from uuid import UUID
    from workbench_agent.domain.outcomes import Finding, RunOutcome, UsageSummary
    ref = '11111111-1111-1111-1111-111111111111'
    c = claim(evidence_ref=ref)
    result = check(answer([c]), [source(evidence_ref=ref)])
    accepted, _ = result.accepted[0]
    finding = Finding(status='observed', summary=f'结论：{accepted.text}\n来源 ID：{accepted.source_id}\n引用：{accepted.quote}', evidence_refs=(UUID(ref),))
    values = dict(kind='analysis.v1', summary='Answer', findings=(finding,), root_cause_hypotheses=(), impact=None, evidence_refs=(UUID(ref),), proposed_actions=(), knowledge_gaps=(), usage_summary=UsageSummary())
    assert RunOutcome(completed=result.completed, unresolved_items=(), **values).completed
    with pytest.raises(ValidationError):
        RunOutcome(completed=False, unresolved_items=(), **values)
    with pytest.raises(ValidationError):
        RunOutcome(completed=True, unresolved_items=('unanswered',), **values)


def test_equivalent_whitespace_citations_are_duplicates():
    s = source(content='CPU is 80%')
    assert not check(answer([claim(), claim(quote='CPU  is 80%')]), [s]).completed


@pytest.mark.parametrize(('text', 'content'), [
    ('温度90%', '温度80%'),
    ('版本v1.2.4', '版本v1.2.3'),
    ('状态ALARM', '状态OK'),
    ('温度-2.5%', '温度2.5%'),
])
def test_chinese_adjacent_tokens_cannot_be_fabricated(text, content):
    s = source(content=content)
    assert not check(answer([claim(text=text, quote=content)]), [s]).completed


def test_extract_rejects_metadata_truncation():
    raw = payload()
    raw['meta'] = {'truncated': True}
    assert extract_knowledge_sources('e1', raw).rejection_reasons


@pytest.mark.parametrize(('text', 'content'), [
    ('rate .9', 'rate .8'), ('rate -.9', 'rate .9'),
    ('rate .99%', 'rate .88%'), ('rate .9', 'rate 0.9'),
])
def test_leading_decimal_numbers_must_match_exactly(text, content):
    assert not check(answer([claim(text=text, quote=content)]), [source(content=content)]).completed


@pytest.mark.parametrize('uri', [None, '', 'http://docs.example.test/s1',
    'https://user:pass@docs.example.test/s1', 'https://docs.example.test/s1?token=x',
    'https://docs.example.test/s1#anchor', 'https:///no-host', 'https://docs.example.test:bad/s1'])
def test_missing_or_unsafe_source_uri_prevents_completion(uri):
    result = check(answer(), [source(source_uri=uri)])
    assert not result.completed and result.rejection_reasons
