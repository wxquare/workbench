"""Explicit maintenance for bounded synthetic multi-action rules."""
import argparse
import asyncio
import json
from pathlib import Path
from sqlalchemy import select
from workbench_agent.application.synthetic_batch_policy import SyntheticBatchPolicy
from workbench_agent.persistence import schema
from workbench_agent.persistence.backends.sqlite import SQLiteBackend
from workbench_agent.persistence.readiness import storage_ready
from workbench_agent.settings import load_settings


async def batch_policy_status(settings):
    backend=SQLiteBackend(settings.storage.database_url.get_secret_value())
    try:
        if not await storage_ready(settings,backend): return {'ready':False,'status':'storage_unavailable'}
        async with backend.create_uow().begin_read() as uow:
            row=(await uow.execute(select(schema.synthetic_batch_policy).where(schema.synthetic_batch_policy.c.policy_id=='deployment'))).mappings().first()
        return {'ready':True,'revision':row['revision'] if row else 0,'policy_hash':row['policy_hash'] if row else None}
    finally: await backend.close()


async def sync_batch_policy(settings,rules,expected_revision):
    if settings.environment not in ('development','test'): raise ValueError('synthetic batch rules require a non-production environment')
    backend=SQLiteBackend(settings.storage.database_url.get_secret_value())
    try:
        if not await storage_ready(settings,backend): return {'ready':False,'status':'storage_unavailable'}
        return {'ready':True,'revision':await SyntheticBatchPolicy(backend).sync(rules,expected_revision=expected_revision)}
    finally: await backend.close()


def main():
    parser=argparse.ArgumentParser(description='Explicit synthetic batch policy maintenance')
    parser.add_argument('--config',required=True)
    commands=parser.add_subparsers(dest='command',required=True)
    commands.add_parser('status')
    sync=commands.add_parser('sync');sync.add_argument('--expected-revision',required=True,type=int);sync.add_argument('--rules-file',required=True)
    args=parser.parse_args();settings=load_settings(args.config)
    if args.command=='status': result=asyncio.run(batch_policy_status(settings))
    else:
        rules=json.loads(Path(args.rules_file).read_text(encoding='utf-8'))
        result=asyncio.run(sync_batch_policy(settings,rules,args.expected_revision))
    print(json.dumps(result,sort_keys=True))
