"""Explicit one-shot local simulation; never configures real senders."""
import argparse
import asyncio
import json
from pathlib import Path
from workbench_agent.delivery.dispatcher import Dispatcher
from workbench_agent.delivery.local_simulator import LocalSimulator
from workbench_agent.persistence.backends.sqlite import SQLiteBackend
from workbench_agent.persistence.readiness import storage_ready
from workbench_agent.settings import load_settings


async def dispatch_local_once(settings,kind,mock_db):
    path=Path(mock_db)
    if settings.environment not in ('development','test') or kind not in LocalSimulator._TOOLS:
        raise ValueError('local simulation requires a development or test environment')
    if not path.is_absolute() or path.is_symlink() or path.resolve()==settings.storage.database_path.resolve():
        raise ValueError('a separate absolute simulator database is required')
    backend=SQLiteBackend(settings.storage.database_url.get_secret_value())
    try:
        if not await storage_ready(settings,backend): return {'ready':False,'status':'storage_unavailable'}
        backend._delivery_senders={'local.mock.'+kind:LocalSimulator(path,kind)}
        return {'ready':True,'status':await Dispatcher(backend,'local-simulator').dispatch_one() or 'idle'}
    finally: await backend.close()


def main():
    parser=argparse.ArgumentParser(description='Dispatch one approved action to a local simulator only')
    parser.add_argument('--config',required=True)
    parser.add_argument('--mock-kind',required=True,choices=('jira','dod','seatalk'))
    parser.add_argument('--mock-db',required=True)
    args=parser.parse_args()
    result=asyncio.run(dispatch_local_once(load_settings(args.config),args.mock_kind,args.mock_db))
    print(json.dumps(result,sort_keys=True))
