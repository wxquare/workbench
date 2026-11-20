"""Pure contracts shared by knowledge collection and evaluation tooling."""

from __future__ import annotations

import hashlib
import json
from typing import Any
from dataclasses import dataclass

SEED_KINDS = {'faq', 'runbook', 'glossary', 'case'}


def canonical(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(',', ':'),
        allow_nan=False,
    )


def hash_json(value: Any) -> str:
    return hashlib.sha256(canonical(value).encode('utf-8')).hexdigest()


def make_run_key(
    experiment_id: str,
    dataset_hash: str,
    config_hash: str,
    case_id: str,
    repeat: int,
) -> str:
    """Separate experiments while allowing an exact experiment to resume."""
    if not all(
        isinstance(value, str) and value and value == value.strip()
        for value in (experiment_id, dataset_hash, config_hash, case_id)
    ):
        raise ValueError('run identity fields must be nonempty strings without surrounding whitespace')
    if type(repeat) is not int or repeat < 0:
        raise ValueError('repeat must be a nonnegative integer')
    return hash_json(
        {
            'experiment_id': experiment_id,
            'dataset_hash': dataset_hash,
            'config_hash': config_hash,
            'case_id': case_id,
            'repeat': repeat,
        }
    )


def validate_seed_manifest(value: dict) -> list[dict]:
    """Validate the import manifest without copying private provenance into KB."""
    if not isinstance(value, dict) or value.get('schema_version') != 'knowledge-seed-v1':
        raise ValueError('invalid seed manifest schema')
    seeds = value.get('seeds')
    if not isinstance(seeds, list) or not seeds:
        raise ValueError('seed manifest requires at least one seed')
    identifiers: set[str] = set()
    normalized = []
    for seed in seeds:
        if not isinstance(seed, dict):
            raise ValueError('each seed must be an object')
        if set(seed) != {'seed_id', 'provenance', 'draft'}:
            raise ValueError('unexpected seed fields')
        seed_id = seed['seed_id']
        if (
            not isinstance(seed_id, str)
            or not seed_id
            or seed_id != seed_id.strip()
            or len(seed_id) > 128
        ):
            raise ValueError('invalid seed_id')
        if seed_id in identifiers:
            raise ValueError('duplicate seed_id')
        identifiers.add(seed_id)
        provenance = seed['provenance']
        if (
            not isinstance(provenance, dict)
            or not isinstance(provenance.get('source'), str)
            or not provenance['source'].strip()
        ):
            raise ValueError('seed provenance is required')
        draft = seed['draft']
        if not isinstance(draft, dict):
            raise ValueError('seed draft must be an object')
        body = draft.get('body_md')
        if not isinstance(body, str) or len(body.encode('utf-8')) > 16 * 1024:
            raise ValueError('knowledge body exceeds the 16 KiB limit')
        if draft.get('kind') not in SEED_KINDS:
            raise ValueError('unsupported knowledge kind')
        for field in ('service', 'environment', 'region'):
            scope = draft.get(field)
            if scope is not None and (
                not isinstance(scope, str)
                or not scope.strip()
                or scope != scope.strip()
                or scope.casefold() == 'all'
                or ',' in scope
                or '*' in scope
            ):
                raise ValueError('wildcard or invalid knowledge scope')
        from pydantic import ValidationError
        from workbench_agent.knowledge.models import KBDraft

        try:
            KBDraft.model_validate(draft)
        except ValidationError:
            raise ValueError('invalid knowledge draft') from None
        normalized.append(seed)
    return normalized


def validate_experiment_config(value: dict) -> dict:
    """Validate non-secret session settings and reject unsupported endpoints."""
    required = {
        'api_base_url',
        'token_env',
        'agent_id',
        'agent_config_path',
        'mcp_config_path',
        'dataset_path',
        'seed_manifest_path',
        'seed_namespace',
        'expected_graph_id',
        'output_root',
        'request_timeout_seconds',
        'case_timeout_seconds',
        'limits',
        'pricing',
        'experiment_budget_micros',
    }
    if not isinstance(value, dict):
        raise ValueError('config must be a mapping')
    missing = sorted(required - set(value))
    if missing:
        raise ValueError('missing config fields: ' + ', '.join(missing))
    from urllib.parse import urlsplit
    from ipaddress import ip_address
    from uuid import UUID

    parsed = urlsplit(value['api_base_url'])
    try:
        loopback = ip_address(parsed.hostname).is_loopback
    except (ValueError, TypeError):
        loopback = False
    if (
        parsed.scheme != 'http'
        or not loopback
        or parsed.username
        or parsed.password
        or parsed.path not in ('', '/')
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError('local baseline API must use credential-free numeric loopback HTTP')
    if str(UUID(value['agent_id'])) != value['agent_id']:
        raise ValueError('agent_id must be a canonical UUID')
    for field in ('token_env', 'seed_namespace', 'expected_graph_id'):
        if not isinstance(value[field], str) or not value[field].strip():
            raise ValueError(f'{field} is required')
    import re
    if not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]*', value['token_env']):
        raise ValueError('token_env must name an environment variable')
    for field in ('agent_config_path', 'mcp_config_path', 'dataset_path', 'seed_manifest_path'):
        if not isinstance(value[field], str):
            raise ValueError(f'{field} must be a path')
    if not isinstance(value['output_root'], str):
        raise ValueError('output_root must be a path')
    for field, minimum, maximum in (
        ('request_timeout_seconds', 0, 60),
        ('case_timeout_seconds', 0, 3600),
    ):
        number = value[field]
        if type(number) not in (int, float) or not minimum < number <= maximum:
            raise ValueError(f'invalid {field}')
    budget = value['experiment_budget_micros']
    if budget is not None and (
        type(budget) is not int or not 0 < budget <= 10**18
    ):
        raise ValueError('invalid experiment_budget_micros')
    limits = value['limits']
    if not isinstance(limits, dict) or set(limits) != {
        'model_calls',
        'tool_calls',
        'tokens',
        'wall_seconds',
    }:
        raise ValueError('limits must contain the four run budgets')
    if any(type(item) is not int or item <= 0 for item in limits.values()):
        raise ValueError('run limits must be positive integers')
    pricing = value['pricing']
    if not isinstance(pricing, dict) or set(pricing) != {
        'currency',
        'pricing_version',
        'input_micros_per_million',
        'output_micros_per_million',
    }:
        raise ValueError('invalid pricing fields')
    known = [
        pricing['currency'],
        pricing['pricing_version'],
        pricing['input_micros_per_million'],
        pricing['output_micros_per_million'],
    ]
    if any(item is not None for item in known):
        if (
            not all(item is not None and item != '' for item in known)
            or len(str(pricing['currency'])) != 3
            or str(pricing['currency']) != str(pricing['currency']).upper()
            or any(
                type(pricing[field]) is not int or pricing[field] < 0
                for field in (
                    'input_micros_per_million',
                    'output_micros_per_million',
                )
            )
        ):
            raise ValueError('known pricing requires currency and complete prices')
    return value


@dataclass(frozen=True)
class AttemptOutcome:
    value: dict
    replayed: bool = False


async def request_with_retry(operation, *, attempts: int = 3, delays=(0, 1, 2)):
    """Retry only errors explicitly marked safe after the server rejects them."""
    from workbench_agent.evaluation.api_client import APIError

    for index in range(attempts):
        try:
            return AttemptOutcome(value=await operation(), replayed=index > 0)
        except APIError as error:
            if not error.retryable or index + 1 == attempts:
                raise
            import asyncio
            await asyncio.sleep(delays[min(index, len(delays) - 1)])
    raise RuntimeError('retry loop unexpectedly exhausted')
