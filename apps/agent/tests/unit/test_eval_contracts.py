import json

import pytest

from workbench_agent.evaluation.contracts import (
    make_run_key,
    validate_dataset,
    validate_experiment_config,
    validate_seed_manifest,
)


def test_run_key_separates_experiments_and_reuses_resume():
    first = make_run_key('e1', 'dataset', 'config', 'case-1', 0)
    assert first == make_run_key('e1', 'dataset', 'config', 'case-1', 0)
    assert first != make_run_key('e2', 'dataset', 'config', 'case-1', 0)
    assert first != make_run_key('e1', 'dataset', 'config', 'case-1', 1)


def test_seed_manifest_rejects_invalid_content_and_wildcards():
    body = {
        'schema_version': 'knowledge-seed-v1',
        'seeds': [
            {
                'seed_id': 'stock',
                'provenance': {'source': 'internal wiki'},
                'draft': {'title': 'Stock', 'kind': 'faq', 'body_md': 'Check stock.'},
            }
        ],
    }
    assert validate_seed_manifest(body) == body['seeds']
    body['seeds'][0]['draft']['body_md'] = 'x' * (16 * 1024 + 1)
    with pytest.raises(ValueError, match='16 KiB'):
        validate_seed_manifest(body)
    body['seeds'][0]['draft']['body_md'] = 'small'
    body['seeds'][0]['draft']['kind'] = 'unknown'
    with pytest.raises(ValueError, match='kind'):
        validate_seed_manifest(body)
    body['seeds'][0]['draft']['kind'] = 'faq'
    body['seeds'][0]['draft']['service'] = '*'
    with pytest.raises(ValueError, match='wildcard'):
        validate_seed_manifest(body)


def test_seed_manifest_allows_empty_template_only_without_seeds():
    with pytest.raises(ValueError):
        validate_seed_manifest(
            {'schema_version': 'knowledge-seed-v1', 'seeds': []}
        )


def test_dataset_template_is_empty_but_labels_are_validated():
    template = {
        'schema_version': 'knowledge-eval-v1',
        'dataset_kind': 'business',
        'cases': [],
    }
    assert validate_dataset(template) is template
    template['cases'] = [
        {
            'id': 'case-1',
            'scenario': 'no_answer',
            'answerable': False,
            'query_scope': {
                'service': 'checkout',
                'environment': 'test',
                'region': 'id',
                'start_at': '2026-11-20T00:00:00Z',
                'end_at': '2026-11-20T00:10:00Z',
            },
            'expected_source_ids': [],
            'must_include': [],
        }
    ]
    assert validate_dataset(template) is template
    template['cases'][0]['scenario'] = 'made_up'
    with pytest.raises(ValueError):
        validate_dataset(template)


def test_local_session_config_rejects_remote_and_secret_values():
    config = {
        'api_base_url': 'http://127.0.0.1:8000',
        'token_env': 'WORKBENCH_BASELINE_TOKEN',
        'agent_id': '11111111-1111-1111-1111-111111111111',
        'agent_config_path': 'config/local/agent.yaml',
        'mcp_config_path': 'config/local/mcp-server.yaml',
        'dataset_path': 'evaluation/knowledge/dataset.local.json',
        'seed_manifest_path': 'evaluation/knowledge/seed-manifest.local.json',
        'seed_namespace': 'baseline',
        'expected_profile_ref': None,
        'expected_model_refs': [],
        'expected_graph_id': 'knowledge.citation_review',
        'output_root': 'artifacts/evaluation',
        'request_timeout_seconds': 30,
        'case_timeout_seconds': 300,
        'limits': {'model_calls': 8, 'tool_calls': 20, 'tokens': 80000, 'wall_seconds': 180},
        'pricing': {
            'currency': None,
            'pricing_version': None,
            'input_micros_per_million': None,
            'output_micros_per_million': None,
        },
        'experiment_budget_micros': None,
    }
    assert validate_experiment_config(config) is config
    config['api_base_url'] = 'https://example.invalid'
    with pytest.raises(ValueError, match='loopback'):
        validate_experiment_config(config)
