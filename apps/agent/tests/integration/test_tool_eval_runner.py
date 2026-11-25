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
        return {
            'amount_micros': 0,
            'currency': 'USD',
            'cost_pending': False,
            'usage_pending': False,
            'known_costs': {'USD': 0},
        }

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
            'tool_call_micros': 0,
            'scope': 'all_run_models_and_tools',
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


@pytest.mark.asyncio
async def test_tool_runner_stops_before_second_case_when_budget_is_spent(tmp_path):
    path = Path(__file__).parents[4] / 'evaluation/tool-conversation/dataset.example.json'
    data = load_tool_dataset(path)
    data['cases'] = data['cases'][:2]
    for case in data['cases']:
        case['review_status'] = 'reviewed'
        case['reviewer'] = 'reviewer'
    cfg = config()
    cfg['limits'].update(model_calls=1, tool_calls=1, tokens=100)
    cfg['pricing'].update(input_micros_per_million=40000, output_micros_per_million=40000)
    cfg['experiment_budget_micros'] = 10

    class ExpensiveAPI(API):
        async def usage(self, run_id):
            return {
                'amount_micros': 6,
                'currency': 'USD',
                'cost_pending': False,
                'usage_pending': False,
                'known_costs': {'USD': 6},
            }

    api = ExpensiveAPI()
    result = await run_tool_experiment(
        data,
        cfg,
        manifest=manifest(data, cfg),
        journal=Journal(tmp_path / 'exp/journal.jsonl'),
        api=api,
        split='dev',
        repeats=1,
        deadline_seconds=2,
    )
    assert api.creates == 1
    assert result['complete'] is False


@pytest.mark.asyncio
async def test_tool_runner_blocks_previously_reserved_case_after_late_overrun(tmp_path):
    from workbench_agent.evaluation.budget import ExperimentBudget
    from workbench_agent.evaluation.contracts import make_run_key

    path = Path(__file__).parents[4] / 'evaluation/tool-conversation/dataset.example.json'
    data = load_tool_dataset(path)
    data['cases'] = data['cases'][:2]
    for case in data['cases']:
        case['review_status'] = 'reviewed'
        case['reviewer'] = 'reviewer'
    cfg = config()
    cfg['limits'].update(model_calls=1, tool_calls=1, tokens=100)
    cfg['pricing'].update(input_micros_per_million=40000, output_micros_per_million=40000)
    cfg['experiment_budget_micros'] = 10
    pinned = manifest(data, cfg)
    journal = Journal(tmp_path / 'exp/journal.jsonl')
    budget = ExperimentBudget(cfg, journal)
    first, second = data['cases']
    key_a = make_run_key(
        pinned['experiment_id'], pinned['dataset_hash'], pinned['config_hash'], first['id'], 0
    )
    key_b = make_run_key(
        pinned['experiment_id'], pinned['dataset_hash'], pinned['config_hash'], second['id'], 0
    )
    assert budget.reserve(first['id'], 0, key_a)
    assert budget.reserve(second['id'], 0, key_b)
    journal.append(
        {
            'event': 'case_result',
            'case_id': first['id'],
            'repeat': 0,
            'request_key': key_a,
            'row': {
                'case_id': first['id'],
                'repeat': 0,
                'request_key': key_a,
                'run_id': 'run-a',
                'run_status': 'succeeded',
                'transport_status': 'completed',
                'usage': {
                    'amount_micros': None,
                    'currency': 'USD',
                    'cost_pending': True,
                    'usage_pending': True,
                },
            },
        }
    )

    class LateCostAPI(API):
        async def usage(self, run_id):
            return {
                'amount_micros': 8,
                'currency': 'USD',
                'cost_pending': False,
                'usage_pending': False,
                'known_costs': {'USD': 8},
            }

    api = LateCostAPI()
    result = await run_tool_experiment(
        data,
        cfg,
        manifest=pinned,
        journal=journal,
        api=api,
        split='dev',
        repeats=1,
        deadline_seconds=2,
    )
    assert api.creates == 0
    assert result['complete'] is False
    third = await run_tool_experiment(
        data,
        cfg,
        manifest=pinned,
        journal=journal,
        api=api,
        split='dev',
        repeats=1,
        deadline_seconds=2,
    )
    assert third['rows'][0]['usage']['amount_micros'] == 8


@pytest.mark.asyncio
@pytest.mark.parametrize(
    'bad_usage',
    [
        {
            'amount_micros': 20,
            'currency': 'EUR',
            'cost_pending': False,
            'usage_pending': False,
            'known_costs': {'EUR': 20},
        },
        {
            'amount_micros': 80,
            'currency': 'USD',
            'cost_pending': False,
            'usage_pending': True,
            'known_costs': {'USD': 1},
        },
    ],
)
async def test_tool_runner_blocks_other_currency_until_reconciled(tmp_path, bad_usage):
    path = Path(__file__).parents[4] / 'evaluation/tool-conversation/dataset.example.json'
    data = load_tool_dataset(path)
    data['cases'] = data['cases'][:2]
    for case in data['cases']:
        case['review_status'] = 'reviewed'
        case['reviewer'] = 'reviewer'
    cfg = config()
    cfg['limits'].update(model_calls=1, tool_calls=1, tokens=100)
    cfg['pricing'].update(input_micros_per_million=40000, output_micros_per_million=40000)
    cfg['experiment_budget_micros'] = 100

    class MixedCurrencyAPI(API):
        mixed = True

        async def usage(self, run_id):
            if self.mixed:
                return bad_usage
            return await super().usage(run_id)

    api = MixedCurrencyAPI()
    pinned = manifest(data, cfg)
    journal = Journal(tmp_path / 'exp/journal.jsonl')
    for _ in range(2):
        result = await run_tool_experiment(
            data,
            cfg,
            manifest=pinned,
            journal=journal,
            api=api,
            split='dev',
            repeats=1,
            deadline_seconds=2,
        )
        assert result['complete'] is False
        assert api.creates == 1
    api.mixed = False
    result = await run_tool_experiment(
        data,
        cfg,
        manifest=pinned,
        journal=journal,
        api=api,
        split='dev',
        repeats=1,
        deadline_seconds=2,
    )
    assert result['complete'] is True
    assert api.creates == 2


def test_cli_tool_experiment_uses_tool_fingerprint_for_new_and_resumed_run(tmp_path, monkeypatch):
    from argparse import Namespace
    from workbench_agent.cli import eval as eval_cli

    data = dataset()
    data_path = tmp_path / 'tool-dataset.json'
    data_path.write_text(json.dumps(data))
    seed_path = tmp_path / 'seed.json'
    seed_path.write_text('{}')
    cfg = config()
    cfg.update(
        seed_manifest_path=str(seed_path),
        token_env='TOOL_TEST_TOKEN',
        api_base_url='http://127.0.0.1:8000',
        output_root=str(tmp_path / 'output'),
    )
    config_path = tmp_path / 'config.json'
    config_path.write_text(json.dumps(cfg))
    monkeypatch.setenv('TOOL_TEST_TOKEN', 'fixture-token')
    observed = []

    async def fake_runner(value, config, **kwargs):
        observed.append(kwargs['manifest']['dataset_hash'])
        return {'rows': [], 'expected': 0, 'complete': True}

    monkeypatch.setattr(eval_cli, 'run_tool_experiment', fake_runner)
    args = Namespace(
        eval_command='run',
        purpose='business',
        resume=None,
        dataset=str(data_path),
        config=str(config_path),
        experiment_id='exp',
        split='dev',
        repeats=1,
    )
    assert eval_cli.main(args) == 0
    experiment = tmp_path / 'output/exp'
    (experiment / 'manifest.json').rename(experiment / 'experiment.json')
    args.resume = str(tmp_path / 'output/exp')
    args.dataset = None
    args.experiment_id = None
    args.split = None
    assert eval_cli.main(args) == 0
    assert observed == [tool_dataset_fingerprint(data)] * 2
