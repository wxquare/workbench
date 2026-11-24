"""Offline knowledge QA gates based on explicitly reviewed labels and results.

Citation judgments and semantic usability are supplied by reviewers; this module
never treats substring matching as proof of semantic correctness.
"""

from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import Any

from workbench_agent.domain.query_scope import validate_qa_scope


@dataclass(frozen=True)
class KnowledgeQualityReport:
    total_cases: int
    reviewed_cases: int
    missing_reviews: int
    cited_claims: int
    correct_citations: int
    citation_accuracy: float | None
    answerable_cases: int
    usable_answers: int
    usable_answer_ratio: float | None
    unanswerable_cases: int
    incorrect_conclusions: int
    unauthorized_accesses: int
    degradation_reasons: dict[str, int]
    thresholds: dict[str, float | int]
    failure_reasons: list[str]
    release_passed: bool
    status: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _text(value: object) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _text_list(value: object) -> bool:
    return isinstance(value, list) and all(_text(item) for item in value)


def _valid_scope(value: object) -> bool:
    try:
        validate_qa_scope(value)
        return True
    except ValueError:
        return False


def _case_key(case: dict[str, Any]) -> tuple | None:
    scope = case.get('query_scope')
    if not _text(case.get('question')) or not _valid_scope(scope):
        return None

    def time_key(value: str) -> str:
        parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
        if parsed.tzinfo is not None:
            parsed = parsed.astimezone(timezone.utc)
        return parsed.isoformat()

    return (
        ' '.join(case['question'].split()).casefold(),
        scope['service'],
        scope['environment'],
        scope['region'],
        time_key(scope['start_at']),
        time_key(scope['end_at']),
    )


def _valid_claim(claim: object) -> bool:
    if not isinstance(claim, dict) or 'citation_correct' not in claim:
        return False
    cited = claim.get('cited')
    if 'cited' not in claim:
        return type(claim['citation_correct']) is bool
    if type(cited) is not bool:
        return False
    return type(claim['citation_correct']) is bool if cited else claim['citation_correct'] is None


def evaluate_knowledge_dataset(dataset: dict[str, Any]) -> KnowledgeQualityReport:
    if not isinstance(dataset, dict) or not isinstance(dataset.get('cases'), list):
        raise ValueError('dataset must contain a list of cases')
    ids: set[str] = set()
    output_refs: set[str] = set()
    case_keys: set[tuple] = set()
    duplicate_outputs = duplicate_cases = uncited = 0
    scenarios: set[str] = set()
    multi_source_usable = False
    reviewed = missing = cited = correct = answerable = usable = unanswerable = false_answers = (
        unauthorized
    ) = 0
    reasons: dict[str, int] = {}
    for case in dataset['cases']:
        if not isinstance(case, dict) or not _text(case.get('id')):
            raise ValueError('each case must have a nonempty id')
        if case['id'] in ids:
            raise ValueError('duplicate case id')
        ids.add(case['id'])
        scope = case.get('query_scope')
        case_key = _case_key(case)
        if case_key is not None:
            duplicate_cases += int(case_key in case_keys)
            case_keys.add(case_key)
        label_complete = (
            case.get('review_status') == 'reviewed'
            and _text(case.get('reviewer'))
            and _text(case.get('question'))
            and _text(case.get('scenario'))
            and case.get('scenario')
            in {'answerable', 'no_answer', 'conflict', 'stale', 'multiple_sources', 'unauthorized'}
            and (case.get('scenario') != 'answerable' or case.get('answerable') is True)
            and (case.get('scenario') != 'no_answer' or case.get('answerable') is False)
            and _valid_scope(scope)
            and _text_list(case.get('expected_source_ids'))
            and _text_list(case.get('must_include'))
            and type(case.get('answerable')) is bool
            and (
                case.get('answerable') is not True
                or bool(case.get('expected_source_ids'))
                and bool(case.get('must_include'))
            )
        )
        reviewed += int(label_complete)
        if label_complete:
            scenarios.add(case['scenario'])
        # Include every declared category even if its review or output is absent.
        answerable += int(case.get('answerable') is True)
        unanswerable += int(case.get('answerable') is False)
        review = case.get('result_review')
        review = review if isinstance(review, dict) else {}
        output_ref = review.get('output_ref')
        if _text(output_ref):
            duplicate_outputs += int(output_ref.strip() in output_refs)
            output_refs.add(output_ref.strip())
        claims = review.get('claims')
        claims_valid = isinstance(claims, list) and all(_valid_claim(c) for c in claims)
        result_complete = (
            _text(review.get('reviewer'))
            and review.get('output_present') is True
            and _text(review.get('output_ref'))
            and claims_valid
            and type(review.get('answer_usable')) is bool
            and (
                review.get('answer_usable') is not True
                or isinstance(claims, list)
                and any(_valid_claim(c) and c.get('cited') is not False for c in claims)
            )
            and type(review.get('unauthorized_access')) is bool
            and (
                review.get('degradation_reason') is None or _text(review.get('degradation_reason'))
            )
        )
        if (
            label_complete
            and result_complete
            and case.get('answerable') is True
            and review.get('answer_usable') is True
            and len(set(case['expected_source_ids'])) >= 2
        ):
            multi_source_usable = True
        missing += int(not (label_complete and result_complete))
        if isinstance(claims, list):
            uncited += sum(isinstance(c, dict) and c.get('cited') is False for c in claims)
            cited += sum(
                not isinstance(c, dict)
                or c.get('cited') is not False
                or c.get('citation_correct') is not None
                for c in claims
            )
            correct += sum(
                isinstance(c, dict) and c.get('citation_correct') is True for c in claims
            )
            if case.get('answerable') is False:
                false_answers += len(claims)
        usable += int(
            case.get('answerable') is True
            and result_complete
            and review.get('answer_usable') is True
        )
        unauthorized += int(review.get('unauthorized_access') is True)
        reason = review.get('degradation_reason')
        if _text(reason):
            reasons[reason] = reasons.get(reason, 0) + 1
    total = len(dataset['cases'])
    citation_rate = correct / cited if cited else None
    usable_rate = usable / answerable if answerable else None
    failures = []
    if dataset.get('dataset_kind') != 'business':
        failures.append('business_dataset_required')
    if total < 30:
        failures.append('fewer_than_30_cases')
    if missing:
        failures.append('missing_reviews')
    if duplicate_outputs:
        failures.append('duplicate_output_refs')
    if duplicate_cases:
        failures.append('duplicate_question_scope')
    if not {'answerable', 'no_answer', 'conflict', 'stale'} <= scenarios:
        failures.append('missing_required_scenarios')
    if not multi_source_usable:
        failures.append('missing_usable_multi_source_case')
    if uncited:
        failures.append('uncited_conclusions')
    if not cited:
        failures.append('no_cited_claims')
    elif citation_rate < 0.95:
        failures.append('citation_accuracy_below_threshold')
    if not answerable:
        failures.append('no_answerable_cases')
    elif usable_rate < 0.80:
        failures.append('usable_answer_ratio_below_threshold')
    if not unanswerable:
        failures.append('no_unanswerable_cases')
    if false_answers:
        failures.append('unanswerable_conclusions')
    if unauthorized:
        failures.append('unauthorized_access')
    passed = not failures
    status = (
        'passed'
        if passed
        else (
            'unavailable'
            if missing or not cited or not answerable or not unanswerable
            else 'failed'
        )
    )
    return KnowledgeQualityReport(
        total,
        reviewed,
        missing,
        cited,
        correct,
        citation_rate,
        answerable,
        usable,
        usable_rate,
        unanswerable,
        false_answers,
        unauthorized,
        reasons,
        {
            'minimum_cases': 30,
            'citation_accuracy': 0.95,
            'usable_answer_ratio': 0.80,
            'incorrect_conclusions': 0,
            'unauthorized_accesses': 0,
        },
        failures,
        passed,
        status,
    )
