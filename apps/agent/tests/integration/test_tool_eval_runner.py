import json
from pathlib import Path

import pytest

from workbench_agent.evaluation.journal import Journal
from workbench_agent.evaluation.tool_conversation_eval import (
    load_tool_dataset,
    tool_dataset_fingerprint,
)
from workbench_agent.evaluation.tool_runner import run_tool_experiment


class API:
    def __init__(self):
        self.creates = 0
        self.runs = 0

    async def create_run(self, body, *, key):
        self.creates += 1
        return {'run_id': 'tool-run'}

    async def provenance(self, run_id):
        ref = {'resource_id': 'r', 'version': '1', 'content_hash': 'a' * 64}
        return {
            'graph_id': 'conversation.tool_use',
            'agent_profile': ref,
            'default_model': ref,
            'role_models': [],
        }

    async def usage(self, run_id):
        return {'amount_micros': 0, 'currency': 'USD', 'cost_pending': False}

    async def get_run(self, run_id):
        self.runs += 1
        return {
            'status': 'partial',
            'outcome': {
                'completed': False,
                'summary': (
                    '以下回答基于所列工具返回的结果，未经独立校验\n\n'
                    '工具不可用。\n\n工具依据：\n'
                    '- knowledge.search / call-1 / timeout'
                ),
                'evidence_refs': [],
                'unresolved_items': ['timeout'],
            },
        }


def dataset():
    path = Path(__file__).parents[4] / 'evaluation/tool-conversation/dataset.example.json'
    value = load_tool_dataset(path)
    value['cases'] = [value['cases'][4]]
    value['cases'][0]['review_status'] = 'reviewed'
    value['cases'][0]['reviewer'] = 'reviewer'
    return value


def config():
    return {
        'agent_id': '11111111-1111-1111-1111-111111111111',
        'expected_graph_id': 'conversation.tool_use',
        'expected_profile_ref': None,
        'expected_model_refs': [],
        'limits': {
            'model_calls': 8,
            'tool_calls': 20,
            'tokens': 80000,
            'wall_seconds': 180,
        },
        'pricing': {
            'currency': 'USD',
            'pricing_version': 'v1',
            'input_micros_per_million': 1,
            'output_micros_per_million': 1,
        },
        'request_timeout_seconds': 10,
        'case_timeout_seconds': 10,
        'experiment_budget_micros': 1000,
    }


def manifest(data, cfg):
    from workbench_agent.evaluation.runner import _config_hash

    return {
        'experiment_id': 'tool-e1',
        'dataset_hash': tool_dataset_fingerprint(data),
        'config_hash': _config_hash(cfg),
        'graph_id': 'conversation.tool_use',
        'split': 'dev',
        'repeats': 1,
        'scoring_version': 'tool-conversation-rules-v1',
        'limits': cfg['limits'],
    }


@pytest.mark.asyncio
async def test_tool_runner_collects_resumable_rows(tmp_path):
    data, cfg = dataset(), config()
    api = API()
    journal = Journal(tmp_path / 'exp/journal.jsonl')
    result = await run_tool_experiment(
        data,
        cfg,
        manifest=manifest(data, cfg),
        journal=journal,
        api=api,
        split='dev',
        repeats=1,
        deadline_seconds=2,
    )
    assert result['complete'] is True
    assert api.creates == 1
    assert result['rows'][0]['outcome']['completed'] is False
    second = await run_tool_experiment(
        data,
        cfg,
        manifest=manifest(data, cfg),
        journal=journal,
        api=api,
        split='dev',
        repeats=1,
        deadline_seconds=2,
    )
    assert api.creates == 1
    assert second['complete'] is True


@pytest.mark.asyncio
async def test_tool_runner_rejects_unreviewed_and_wrong_graph(tmp_path):
    data, cfg = dataset(), config()
    data['cases'][0]['review_status'] = 'pending'
    with pytest.raises(ValueError, match='reviewed'):
        await run_tool_experiment(
            data,
            cfg,
            manifest=manifest(data, cfg),
            journal=Journal(tmp_path / 'exp/journal.jsonl'),
            api=API(),
            split='dev',
            repeats=1,
            deadline_seconds=1,
        )
    cfg['expected_graph_id'] = 'knowledge.citation_review'
    with pytest.raises(ValueError, match='conversation.tool_use'):
        await run_tool_experiment(
            dataset(),
            cfg,
            manifest=manifest(dataset(), cfg),
            journal=Journal(tmp_path / 'exp2/journal.jsonl'),
            api=API(),
            split='dev',
            repeats=1,
            deadline_seconds=1,
        )
