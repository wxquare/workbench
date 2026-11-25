from types import SimpleNamespace

from workbench_agent.evaluation.preflight import preflight
from workbench_agent.evaluation.contracts import validate_experiment_config
import pytest


def config(tmp_path):
    agent = tmp_path / 'agent.yaml'
    mcp = tmp_path / 'mcp.yaml'
    agent.write_text('agent')
    mcp.write_text('mcp')
    return {
        'api_base_url': 'http://127.0.0.1:8000',
        'token_env': 'BASELINE_TOKEN',
        'agent_id': '11111111-1111-1111-1111-111111111111',
        'agent_config_path': str(agent),
        'mcp_config_path': str(mcp),
        'dataset_path': 'dataset.json',
        'seed_manifest_path': 'seed.json',
        'seed_namespace': 'baseline',
        'expected_profile_ref': None,
        'expected_model_refs': [],
        'expected_graph_id': 'knowledge.citation_review',
        'output_root': 'artifacts',
        'request_timeout_seconds': 30,
        'case_timeout_seconds': 300,
        'limits': {'model_calls': 8, 'tool_calls': 20, 'tokens': 1000, 'wall_seconds': 180},
        'pricing': {
            'currency': None,
            'pricing_version': None,
            'input_micros_per_million': None,
            'output_micros_per_million': None,
        },
        'experiment_budget_micros': None,
    }


def settings(*, graph='knowledge.citation_review'):
    grant = SimpleNamespace(team_id='team-1', token_secret_ref='env://KNOWLEDGE_TOKEN')
    return SimpleNamespace(
        service=SimpleNamespace(
            mcp_connections=[
                SimpleNamespace(
                    endpoint='http://127.0.0.1:8001/mcp',
                    allow_loopback_http=True,
                    read_tools=('knowledge_search',),
                    team_id='team-1',
                )
            ]
        ),
        knowledge=SimpleNamespace(
            enabled=True, grants=[grant], public_base_url='http://127.0.0.1:8000'
        ),
        worker=SimpleNamespace(supported_graph_ids=[graph]),
    )


def test_amount_budget_requires_explicit_model_and_tool_cost_ceiling(tmp_path):
    value = config(tmp_path)
    value['experiment_budget_micros'] = 10
    value['pricing'] = {
        'currency': 'USD',
        'pricing_version': 'v1',
        'input_micros_per_million': 1,
        'output_micros_per_million': 2,
    }
    with pytest.raises(ValueError, match='cost ceilings'):
        validate_experiment_config(value)
    value['pricing'].update(tool_call_micros=0, scope='all_run_models_and_tools')
    assert validate_experiment_config(value) is value


def test_port_collision_and_missing_secrets_fail_closed(tmp_path):
    value = config(tmp_path)
    value['api_base_url'] = 'http://127.0.0.1:8001'
    result = preflight(value, environ={}, settings=settings())
    assert not result['ready']
    assert not any(check['ok'] and check['code'] == 'AGENT_ENDPOINT' for check in result['checks'])
    assert not any(check['ok'] and check['code'] == 'API_TOKEN' for check in result['checks'])


def test_ready_preflight_does_not_return_secret(tmp_path):
    result = preflight(
        config(tmp_path),
        environ={'BASELINE_TOKEN': 'fake-secret', 'KNOWLEDGE_TOKEN': 'kb-secret'},
        settings=settings(),
    )
    assert result['ready'] is True
    assert 'fake-secret' not in str(result)
    assert 'kb-secret' not in str(result)


def test_missing_worker_capability_is_structured_failure(tmp_path):
    result = preflight(
        config(tmp_path),
        environ={'BASELINE_TOKEN': 'fake-secret', 'KNOWLEDGE_TOKEN': 'kb-secret'},
        settings=settings(graph='conversation.multi_turn'),
    )
    assert not result['ready']
    assert any(check['code'] == 'WORKER_GRAPH' and not check['ok'] for check in result['checks'])
