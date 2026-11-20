from pathlib import Path

import yaml

WORKFLOW = (
    Path(__file__).resolve().parents[4]
    / ".github"
    / "workflows"
    / "knowledge-evaluation.yml"
)


def workflow():
    return yaml.safe_load(WORKFLOW.read_text())


def test_evaluation_workflow_is_manual_and_scheduled_read_only():
    value = workflow()

    # PyYAML follows YAML 1.1 and parses the unquoted key "on" as True.
    triggers = value.get("on", value.get(True))
    assert set(triggers) == {"workflow_dispatch", "schedule"}
    assert value["permissions"] == {"contents": "read"}


def test_evaluation_workflow_is_disabled_without_explicit_inputs():
    value = workflow()
    job = value["jobs"]["evaluate"]
    assert job["if"] == "${{ vars.KNOWLEDGE_EVAL_ENABLED == 'true' }}"
    env = job["env"]
    assert {
        "WORKBENCH_BASELINE_TOKEN",
        "KNOWLEDGE_EVAL_BUDGET_MICROS",
        "KNOWLEDGE_EVAL_CURRENCY",
    } <= set(env)
    require = next(
        step["run"]
        for step in job["steps"]
        if step.get("name") == "Require explicit inputs"
    )
    for name in (
        "WORKBENCH_BASELINE_TOKEN",
        "KNOWLEDGE_EVAL_BUDGET_MICROS",
        "KNOWLEDGE_EVAL_CURRENCY",
    ):
        assert f'test -n "${name}"' in require


def test_evaluation_workflow_creates_artifact_directory_for_no_baseline():
    steps = workflow()["jobs"]["evaluate"]["steps"]
    run = next(
        step["run"]
        for step in steps
        if step.get("name") == "Run configured evaluation"
    )

    assert "mkdir -p artifacts/evaluation" in run
    assert run.index("mkdir -p artifacts/evaluation") < run.index(
        '> artifacts/evaluation/comparison.json'
    )
    assert '"status":"no_baseline"' in run
    assert '"quality_status":"unavailable"' in run


def test_evaluation_workflow_artifact_is_sanitized_summary_only():
    job = workflow()["jobs"]["evaluate"]
    upload = next(
        step
        for step in job["steps"]
        if step.get("name") == "Upload sanitized result"
    )

    assert job["timeout-minutes"] == 45
    assert upload["with"]["path"] == "artifacts/evaluation/comparison.json"
    assert upload["with"]["retention-days"] == 14
