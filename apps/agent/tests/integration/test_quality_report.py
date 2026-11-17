"""Quality gates use human review and retain missing cases in their denominator."""
import json
from pathlib import Path

import pytest


def reviewed_case(index: int) -> dict:
    return {
        "id": f"case-{index}", "source": "reviewed-test-source", "review_status": "reviewed", "reviewer": "human-reviewer",
        "confirmed_root_cause": "database timeout" if index < 10 else None,
        "root_cause_evidence_refs": ["evidence-1"] if index < 10 else [],
        "result_review": {
            "reviewer": "human-reviewer", "output_present": True, "output_ref": "run-output-1",
            "evidence_reference_correct": True, "answer_usable": True,
            "root_cause_match": True if index < 10 else None,
            "unavailable_reported_as_normal": False, "unauthorized_access_missed": False,
        },
    }


def test_pending_candidates_and_absent_outputs_cannot_pass_release_gate():
    from workbench_agent.evaluation.report import evaluate_dataset

    dataset_path = Path(__file__).parents[1] / "fixtures/business/manifest.json"
    report = evaluate_dataset(json.loads(dataset_path.read_text()))
    assert report.total_cases == 30
    assert report.reviewed_cases == 0
    assert report.missing_reviews == 30
    assert not report.release_passed


def test_complete_human_review_can_pass_and_missing_output_remains_in_denominator():
    from workbench_agent.evaluation.report import evaluate_dataset

    cases = [reviewed_case(i) for i in range(30)]
    assert evaluate_dataset({"cases": cases}).release_passed
    cases[0]["result_review"] = None
    report = evaluate_dataset({"cases": cases})
    assert report.reviewed_cases == 30
    assert report.missing_reviews == 1
    assert report.evidence_reference_accuracy == pytest.approx(29 / 30)
    assert report.primary_root_cause_match_ratio == pytest.approx(9 / 10)
    assert not report.release_passed


def test_unsafe_or_unreviewed_results_never_pass():
    from workbench_agent.evaluation.report import evaluate_dataset

    cases = [reviewed_case(i) for i in range(30)]
    cases[0]["result_review"]["unauthorized_access_missed"] = True
    assert not evaluate_dataset({"cases": cases}).release_passed
    cases[0]["result_review"]["unauthorized_access_missed"] = False
    cases[0]["result_review"]["reviewer"] = ""
    assert not evaluate_dataset({"cases": cases}).release_passed


def test_duplicate_ids_cannot_inflate_the_dataset():
    from workbench_agent.evaluation.report import evaluate_dataset

    with pytest.raises(ValueError, match="duplicate"):
        evaluate_dataset({"cases": [reviewed_case(0), reviewed_case(0)]})


def test_evaluate_cli_writes_unavailable_report_without_configuration(tmp_path):
    import subprocess
    import sys

    dataset = Path(__file__).parents[1] / "fixtures/business/manifest.json"
    output = tmp_path / "report.json"
    result = subprocess.run(
        [sys.executable, "-m", "workbench_agent.cli.main", "evaluate", "--dataset", str(dataset), "--output", str(output)],
        capture_output=True, text=True, check=False,
    )
    assert result.returncode == 1
    report = json.loads(output.read_text())
    assert report["total_cases"] == 30
    assert report["release_passed"] is False
    assert report["missing_reviews"] == 30


def test_missing_human_provenance_or_output_reference_blocks_release():
    from workbench_agent.evaluation.report import evaluate_dataset

    cases = [reviewed_case(i) for i in range(30)]
    cases[0]["source"] = ""
    assert not evaluate_dataset({"cases": cases}).release_passed
    cases[0]["source"] = "reviewed-source"
    cases[0]["result_review"]["output_ref"] = ""
    assert not evaluate_dataset({"cases": cases}).release_passed
    cases[0]["result_review"]["output_ref"] = "output"
    cases[0]["root_cause_evidence_refs"] = []
    assert not evaluate_dataset({"cases": cases}).release_passed


def test_report_status_distinguishes_unavailable_failed_and_passed():
    from workbench_agent.evaluation.report import evaluate_dataset

    cases = [reviewed_case(i) for i in range(30)]
    assert evaluate_dataset({"cases": cases}).status == "passed"
    cases[0]["result_review"]["unauthorized_access_missed"] = True
    assert evaluate_dataset({"cases": cases}).status == "failed"
    cases[0]["result_review"] = None
    assert evaluate_dataset({"cases": cases}).status == "unavailable"
    assert evaluate_dataset({"cases": []}).status == "unavailable"
