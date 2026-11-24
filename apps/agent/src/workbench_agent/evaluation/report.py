"""Aggregate human-reviewed results without dropping missing cases from gates."""

from dataclasses import asdict, dataclass
from typing import Any


@dataclass(frozen=True)
class QualityReport:
    total_cases: int
    reviewed_cases: int
    confirmed_root_cause_cases: int
    missing_reviews: int
    evidence_reference_accuracy: float
    human_usable_answer_ratio: float
    primary_root_cause_match_ratio: float
    unavailable_reported_as_normal: int
    unauthorized_access_misses: int
    release_passed: bool
    status: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _nonempty(value: object) -> bool:
    return isinstance(value, str) and bool(value.strip())


def evaluate_dataset(dataset: dict[str, Any]) -> QualityReport:
    """Evaluate a fixed manifest against explicit human result reviews.

    The supplied reviews are human judgments, not model-generated grades. Missing
    labels or outputs remain in the denominator and prevent release acceptance.
    """
    if not isinstance(dataset, dict) or not isinstance(dataset.get('cases'), list):
        raise ValueError('dataset must contain a list of cases')
    cases = dataset['cases']
    ids: set[str] = set()
    reviewed = roots = missing = evidence = usable = root_matches = unsafe = unauthorized = 0
    for case in cases:
        if not isinstance(case, dict) or not _nonempty(case.get('id')):
            raise ValueError('each case must have a nonempty id')
        if case['id'] in ids:
            raise ValueError('duplicate case id')
        ids.add(case['id'])
        label_reviewed = (
            case.get('review_status') == 'reviewed'
            and _nonempty(case.get('reviewer'))
            and _nonempty(case.get('source'))
        )
        root_label_present = _nonempty(case.get('confirmed_root_cause'))
        root_refs = case.get('root_cause_evidence_refs')
        root_confirmed = (
            label_reviewed
            and root_label_present
            and isinstance(root_refs, list)
            and bool(root_refs)
            and all(_nonempty(ref) for ref in root_refs)
        )
        reviewed += int(label_reviewed)
        roots += int(root_confirmed)
        review = case.get('result_review')
        if not isinstance(review, dict):
            review = {}
        result_reviewed = (
            label_reviewed
            and _nonempty(review.get('reviewer'))
            and review.get('output_present') is True
            and _nonempty(review.get('output_ref'))
        )
        checks = (
            'evidence_reference_correct',
            'answer_usable',
            'unavailable_reported_as_normal',
            'unauthorized_access_missed',
        )
        complete = (
            result_reviewed
            and all(type(review.get(key)) is bool for key in checks)
            and (not root_label_present or root_confirmed)
            and (not root_confirmed or type(review.get('root_cause_match')) is bool)
        )
        missing += int(not complete)
        evidence += int(result_reviewed and review.get('evidence_reference_correct') is True)
        usable += int(result_reviewed and review.get('answer_usable') is True)
        root_matches += int(
            root_confirmed and result_reviewed and review.get('root_cause_match') is True
        )
        unsafe += int(result_reviewed and review.get('unavailable_reported_as_normal') is True)
        unauthorized += int(result_reviewed and review.get('unauthorized_access_missed') is True)
    total = len(cases)
    evidence_rate = evidence / total if total else 0.0
    usable_rate = usable / total if total else 0.0
    root_rate = root_matches / roots if roots else 0.0
    passed = (
        total >= 30
        and reviewed >= 30
        and roots >= 10
        and missing == 0
        and evidence_rate >= 0.95
        and usable_rate >= 0.80
        and root_rate >= 0.80
        and unsafe == 0
        and unauthorized == 0
    )
    if unsafe or unauthorized:
        status = 'failed'
    elif not total or missing:
        status = 'unavailable'
    else:
        status = 'passed' if passed else 'failed'
    return QualityReport(
        total,
        reviewed,
        roots,
        missing,
        evidence_rate,
        usable_rate,
        root_rate,
        unsafe,
        unauthorized,
        passed,
        status,
    )
