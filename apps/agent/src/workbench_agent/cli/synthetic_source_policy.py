"""Explicit offline CAS synchronization of synthetic source authority."""

import argparse
import asyncio
import json
from sqlalchemy import select
from workbench_agent.application.synthetic_source_policy import SyntheticSourcePolicy
from workbench_agent.persistence import schema
from workbench_agent.persistence.backends.sqlite import SQLiteBackend
from workbench_agent.persistence.readiness import storage_ready
from workbench_agent.settings import load_settings


def grants_from_settings(settings):
    grants = []
    for connector, binding in settings.service.synthetic_ingress.items():
        if binding.protocol != 'synthetic-alert-v2':
            continue
        for service, environment, region, rule in binding.allowed_scopes:
            grants.append(
                dict(
                    team_id=binding.team_id,
                    agent_id=binding.agent_id,
                    connector_id=connector,
                    identity_id=binding.identity_id,
                    subject=binding.actor_subject,
                    service=service,
                    environment=environment,
                    region=region,
                    rule=rule,
                )
            )
    return grants


async def source_policy_status(settings):
    backend = SQLiteBackend(settings.storage.database_url.get_secret_value())
    try:
        if not await storage_ready(settings, backend):
            return {'ready': False, 'status': 'storage_unavailable'}
        async with backend.create_uow().begin_read() as uow:
            row = (
                (
                    await uow.execute(
                        select(schema.synthetic_source_policy).where(
                            schema.synthetic_source_policy.c.policy_id == 'deployment'
                        )
                    )
                )
                .mappings()
                .first()
            )
        return {
            'ready': True,
            'revision': row['revision'] if row else 0,
            'policy_hash': row['policy_hash'] if row else None,
        }
    finally:
        await backend.close()


async def sync_source_policy(settings, expected_revision):
    grants = grants_from_settings(settings)
    backend = SQLiteBackend(settings.storage.database_url.get_secret_value())
    try:
        if not await storage_ready(settings, backend):
            return {'ready': False, 'status': 'storage_unavailable'}
        revision = await SyntheticSourcePolicy(backend).sync(
            grants,
            connector_identities=settings.service.connector_identities,
            expected_revision=expected_revision,
        )
        return {'ready': True, 'revision': revision}
    finally:
        await backend.close()


def main():
    parser = argparse.ArgumentParser(description='Explicit synthetic source policy maintenance')
    parser.add_argument('--config', required=True)
    commands = parser.add_subparsers(dest='command', required=True)
    commands.add_parser('status')
    sync = commands.add_parser('sync')
    sync.add_argument('--expected-revision', required=True, type=int)
    args = parser.parse_args()
    settings = load_settings(args.config)
    result = asyncio.run(
        source_policy_status(settings)
        if args.command == 'status'
        else sync_source_policy(settings, args.expected_revision)
    )
    print(json.dumps(result, sort_keys=True))
