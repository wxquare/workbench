"""Offline human-reviewed release quality report."""
import json
from pathlib import Path

from workbench_agent.evaluation.report import evaluate_dataset


def run_evaluate(dataset_path: str, output_path: str, dataset_type: str = "rca") -> bool:
    dataset = json.loads(Path(dataset_path).read_text(encoding='utf-8'))
    if dataset_type == "knowledge":
        from workbench_agent.evaluation.knowledge_report import evaluate_knowledge_dataset
        report = evaluate_knowledge_dataset(dataset)
    elif dataset_type == "rca":
        report = evaluate_dataset(dataset)
    else:
        raise ValueError("unknown dataset type")
    output = Path(output_path)
    output.write_text(json.dumps(report.to_dict(), ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    return report.release_passed
