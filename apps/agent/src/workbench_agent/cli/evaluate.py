"""Offline human-reviewed release quality report."""
import json
from pathlib import Path

from workbench_agent.evaluation.report import evaluate_dataset


def run_evaluate(dataset_path: str, output_path: str) -> bool:
    dataset = json.loads(Path(dataset_path).read_text(encoding='utf-8'))
    report = evaluate_dataset(dataset)
    output = Path(output_path)
    output.write_text(json.dumps(report.to_dict(), ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    return report.release_passed
