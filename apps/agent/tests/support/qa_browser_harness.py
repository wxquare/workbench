"""Isolated, synthetic QA fixture. Never connects to deployment services."""

import argparse
import asyncio
import json
import multiprocessing
import os
from pathlib import Path
import secrets
import sys
import tempfile

# Running this file directly must not shadow stdlib platform with support/platform.py.
sys.path = [
    path for path in sys.path if Path(path or '.').resolve() != Path(__file__).resolve().parent
]

from uuid import uuid4

import httpx
import uvicorn
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / 'mcp-server' / 'src'))

from workbench_mcp_server.bootstrap import Runtime
from workbench_mcp_server.infrastructure.config.settings import Settings as MCPSettings
from workbench_mcp_server.interfaces.mcp.server import create_http_app
from workbench_agent.api.app import create_app
from workbench_agent.application.bootstrap import BootstrapService
from workbench_agent.application.context import ContextResolver
from workbench_agent.application.profiles import ProfileService
from workbench_agent.cli.db import migrate
from workbench_agent.domain.identity import AuthContext
from workbench_agent.domain.profiles import ProfileDraft
from workbench_agent.domain.tools import ToolBinding
from workbench_agent.jobs.worker import Worker
from workbench_agent.persistence.backends.sqlite import SQLiteBackend
from workbench_agent.runtime.graphs.registry import foundation_worker_registry
from workbench_agent.runtime.models.invoker import ModelInvoker
from workbench_agent.settings import Settings
from workbench_agent.tools.mcp import (
    ConnectionPolicy,
    MCPAdapter,
    StreamableSessionFactory,
    TrustedConnection,
)
from workbench_agent.tools.permissions import DeploymentToolPermissions
from workbench_agent.tools.policy import PolicyToolExecutor
from workbench_agent.tools.registry import ToolRegistry

QUERY_SCOPE = {
    'service': 'checkout',
    'environment': 'test',
    'region': 'id',
    'start_at': '2026-11-13 08:00:00',
    'end_at': '2026-11-13 08:10:00',
}
GRANT = {
    'kind': 'dod-mcp-v1',
    'team': 'Fixture',
    'allowed_scope': {'service': 'checkout', 'environment': 'test', 'region': 'id'},
    'earliest': '2020-01-01 00:00:00',
    'latest': '2100-01-01 00:00:00',
    'max_span_seconds': 3600,
    'timezone': 'Asia/Shanghai',
    'sources': {'knowledge_search': 'fixture-kb'},
}


def rules(team, agent):
    return {
        'users': {'qa-fixture-admin': ['knowledge.read']},
        'teams': {team: ['knowledge.read']},
        'agents': {agent: ['knowledge.read']},
        'skills': {'__none__': ['knowledge.read']},
        'experts': {'knowledge': ['knowledge.read']},
    }


def settings_for(root, team, agent, frontend):
    return Settings.model_validate(
        {
            'environment': 'test',
            'asset_root': root,
            'frontend_dist_root': frontend,
            'bootstrap': {'team_id': team, 'agent_id': agent, 'admin_subject': 'qa-fixture-admin'},
            'storage': {
                'database_url': f'sqlite+aiosqlite:///{root}/fixture.sqlite3',
                'persistent_root': root,
            },
        }
    )


def mcp_runtime():
    resource = {
        'team': 'Fixture',
        'environment': 'test',
        'region': 'id',
        'service': 'checkout',
        'source_id': 'fixture-kb',
    }
    settings = MCPSettings.model_validate(
        {
            'server': {'allowed_hosts': ['localhost:*']},
            'principals': [
                {
                    'id': 'fixture',
                    'token_secret_ref': 'env://WORKBENCH_QA_FIXTURE_AGENT',
                    'tools': ['knowledge_search'],
                    'scopes': [resource],
                }
            ],
            'sources': [
                {
                    'id': 'fixture-kb',
                    'kind': 'knowledge',
                    'adapter': 'normalized-http-v1',
                    'endpoint': 'https://fixture.invalid/query',
                    'auth_secret_ref': 'env://WORKBENCH_QA_FIXTURE_UPSTREAM',
                    'binding_id': 'fixture',
                    'scopes': [resource],
                    'scope_enforced': True,
                }
            ],
        }
    )
    runtime = Runtime(settings)

    class Gateway:
        async def query(self, request):
            return {
                'matches': [
                    {
                        'source_id': 'guide-1',
                        'title': 'Fixture guide',
                        'content': 'Check the queue before retrying.',
                        'source_uri': None,
                    }
                ]
            }

    runtime.executor.gateways['fixture-kb'] = Gateway()
    return runtime


def adapter_for(app, team, agent):
    factory = StreamableSessionFactory(
        {('fixture', 'read'): 'https://localhost:8000/mcp'},
        lambda *_: {'Authorization': 'Bearer ' + os.environ['WORKBENCH_QA_FIXTURE_AGENT']},
        transport=httpx.ASGITransport(app=app),
    )
    return MCPAdapter(
        factory,
        ConnectionPolicy(
            [
                TrustedConnection(
                    team, agent, 'fixture', 'read', frozenset({'knowledge_search'}), (GRANT,)
                )
            ]
        ),
    )


class SyntheticModel:
    def bind_tools(self, tools):
        return self

    async def ainvoke(self, messages, **kwargs):
        usage = {'input_tokens': 8, 'output_tokens': 3, 'total_tokens': 11}
        if kwargs.get('response_format'):
            payload = json.loads(next(m.content for m in messages if isinstance(m, HumanMessage)))
            ref = next(iter(payload['sources']))
            return AIMessage(
                content=json.dumps(
                    {'excerpts': [{'evidence_ref': ref, 'quote': payload['sources'][ref][0]}]}
                ),
                usage_metadata=usage,
            )
        if any(isinstance(m, ToolMessage) for m in messages):
            return AIMessage(content='Fixture response.', usage_metadata=usage)
        return AIMessage(
            content='',
            tool_calls=[
                {
                    'name': 'kb_search',
                    'args': {'query': 'retry', 'scene': 'qa'},
                    'id': 'fixture-call',
                    'type': 'tool_call',
                }
            ],
            usage_metadata=usage,
        )


class SyntheticFactory:
    def build(self, *args, **kwargs):
        return SyntheticModel()


class DirectSyntheticModel:
    async def ainvoke(self, messages, **kwargs):
        return AIMessage(
            content='Fixture direct answer.',
            usage_metadata={'input_tokens': 8, 'output_tokens': 3, 'total_tokens': 11},
        )


class DirectSyntheticFactory:
    def build(self, *args, **kwargs):
        return DirectSyntheticModel()


async def run_worker(root, team, agent, frontend, mode):
    settings = settings_for(root, team, agent, frontend)
    backend = SQLiteBackend(
        settings.storage.database_url.get_secret_value(),
        tool_permissions=DeploymentToolPermissions(lambda: rules(team, agent)),
    )
    if mode == 'direct':
        try:
            backend._code_factory = lambda execution: ModelInvoker(
                backend, DirectSyntheticFactory(), object(), execution=execution
            )
            await Worker(
                backend, 'fixture-worker', foundation_worker_registry(backend), scan_interval=0.1
            ).run()
        finally:
            await backend.close()
        return
    runtime = mcp_runtime()
    app = create_http_app(runtime)
    try:
        async with app.app.router.lifespan_context(app.app):
            adapter = adapter_for(app, team, agent)
            backend._diagnosis_factory = lambda execution: (
                ModelInvoker(backend, SyntheticFactory(), object(), execution=execution),
                PolicyToolExecutor(backend, adapter, execution=execution, delays=(0, 0)),
            )
            worker = Worker(
                backend, 'fixture-worker', foundation_worker_registry(backend), scan_interval=0.1
            )
            await worker.run()
    finally:
        await runtime.close()
        await backend.close()


def worker_process(root, team, agent, frontend, mode):
    asyncio.run(run_worker(root, team, agent, frontend, mode))


async def serve(root, port, frontend, mode):
    team, agent = str(uuid4()), str(uuid4())
    os.environ['DEEPSEEK_API_KEY'] = secrets.token_urlsafe(24)
    os.environ['WORKBENCH_QA_FIXTURE_AGENT'] = secrets.token_urlsafe(24)
    os.environ['WORKBENCH_QA_FIXTURE_UPSTREAM'] = secrets.token_urlsafe(24)
    settings = settings_for(root, team, agent, frontend)
    migrate(settings.storage.database_url.get_secret_value())
    permissions = DeploymentToolPermissions(lambda: rules(team, agent))
    backend = SQLiteBackend(
        settings.storage.database_url.get_secret_value(), tool_permissions=permissions
    )
    runtime = mcp_runtime()
    mcp_app = create_http_app(runtime)
    worker = None
    try:
        await BootstrapService(backend).apply(settings)
        auth = AuthContext(team_id=team, subject='qa-fixture-admin', roles=('admin',))
        refs = ()
        if mode == 'knowledge':
            async with mcp_app.app.router.lifespan_context(mcp_app.app):
                adapter = adapter_for(mcp_app, team, agent)
                catalog = await adapter.discover('fixture', 'read', team_id=team, agent_id=agent)
                schema = catalog.tools[catalog.names.index('knowledge_search')].inputSchema
            binding = ToolBinding(
                alias='kb.search',
                provider='mcp',
                tool_name='knowledge_search',
                server='fixture',
                credential_scope='read',
                permission='knowledge.read',
                input_schema=schema,
            )
            refs = (await ToolRegistry(backend).publish(auth, agent, binding, key='fixture-tool'),)
        profiles = ProfileService(backend)
        context = await ContextResolver(backend).resolve(auth, agent)
        current = await profiles.get_agent(auth, agent)
        draft = await profiles.save_draft(
            auth,
            agent,
            ProfileDraft(
                default_model=context.default_model,
                graph_id='knowledge.scoped_excerpt'
                if mode == 'knowledge'
                else 'conversation.single_turn',
                tools=refs,
            ),
            current['revision'],
            key='fixture-draft',
        )
        await profiles.publish(auth, agent, draft['revision'], key='fixture-publish')
        app = create_app(settings, backend=backend)
        fixture = {
            'agent_id': agent,
            'token': f'development:{team}:qa-fixture-admin',
            'query_scope': QUERY_SCOPE,
        }

        @app.get('/fixture', include_in_schema=False)
        async def fixture_info():
            return fixture

        worker = multiprocessing.get_context('spawn').Process(
            target=worker_process, args=(root, team, agent, frontend, mode)
        )
        worker.start()
        print(
            json.dumps(
                {
                    'url': f'http://127.0.0.1:{port}/app',
                    'docs': f'http://127.0.0.1:{port}/docs',
                    **fixture,
                }
            ),
            flush=True,
        )
        await uvicorn.Server(
            uvicorn.Config(app, host='127.0.0.1', port=port, log_level='warning')
        ).serve()
    finally:
        if worker is not None:
            worker.terminate()
            worker.join(timeout=10)
            if worker.is_alive():
                worker.kill()
                worker.join()
        await runtime.close()
        await backend.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--port', type=int, default=8765)
    parser.add_argument('--mode', choices=('knowledge', 'direct'), default='knowledge')
    parser.add_argument(
        '--frontend-dist', default=str(Path(__file__).resolve().parents[3] / 'web' / 'dist')
    )
    args = parser.parse_args()
    with tempfile.TemporaryDirectory(prefix='dod-qa-fixture-') as root:
        asyncio.run(serve(root, args.port, str(Path(args.frontend_dist).resolve()), args.mode))


if __name__ == '__main__':
    multiprocessing.freeze_support()
    main()
