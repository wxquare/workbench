"""Opt-in DeepSeek + local synthetic-source Worker acceptance; no business data."""

import os
from uuid import uuid4

import pytest

from workbench_agent.application.runs import RunService
from workbench_agent.connectors.secrets import SecretProvider
from workbench_agent.domain.profiles import ProfileDraft
from workbench_agent.domain.runs import CreateRun
from workbench_agent.domain.tools import ToolBinding
from workbench_agent.jobs.worker import Worker
from workbench_agent.runtime.graphs.registry import foundation_worker_registry
from workbench_agent.runtime.models.factory import ModelCredentialPolicy, ModelFactory
from workbench_agent.runtime.models.invoker import ModelInvoker
from workbench_agent.tools.permissions import DeploymentToolPermissions
from workbench_agent.tools.policy import PolicyToolExecutor
from workbench_agent.tools.registry import ToolRegistry

pytestmark = pytest.mark.live


def invoker(platform, execution):
    policy = ModelCredentialPolicy(
        [
            {
                'team_id': platform.auth.team_id,
                'agent_ids': [platform.agent_id],
                'base_url': 'https://api.deepseek.com',
                'secret_ref': 'env://DEEPSEEK_API_KEY',
            }
        ]
    )
    return ModelInvoker(
        platform.backend, ModelFactory(policy), SecretProvider(), execution=execution
    )


async def create_run(platform, mode, prompt, *, query_scope):
    if not os.environ.get('DEEPSEEK_API_KEY'):
        pytest.skip('DEEPSEEK_API_KEY not configured')
    request = CreateRun.model_validate(
        {
            'agent_id': platform.agent_id,
            'mode': mode,
            'input': {'type': 'text', 'text': prompt, 'artifact_ids': []},
            'business_context': {'query_scope': query_scope},
            'summary_token_reserve': 8000,
        }
    )
    return await RunService(platform.backend).create(platform.auth, request, str(uuid4()))


async def publish_profile(platform, graph_id, *, tools=(), repositories=()):
    context = await platform.contexts.resolve(platform.auth, platform.agent_id)
    agent = await platform.profiles.get_agent(platform.auth, platform.agent_id)
    draft = ProfileDraft(
        default_model=context.default_model,
        graph_id=graph_id,
        tools=tools,
        repositories=repositories,
    )
    changed = await platform.profiles.save_draft(
        platform.auth, platform.agent_id, draft, agent['revision'], key=str(uuid4())
    )
    await platform.profiles.publish(
        platform.auth, platform.agent_id, changed['revision'], key=str(uuid4())
    )


async def test_live_qa_uses_local_mcp_synthetic_source(platform, monkeypatch):
    import httpx
    from contract.test_scoped_mcp_mapping import GRANT, SCOPE
    from workbench_agent.tools.mcp import (
        MCPAdapter,
        ConnectionPolicy,
        TrustedConnection,
        StreamableSessionFactory,
    )
    from support.mcp import mcp_server_source

    server_src = mcp_server_source()
    monkeypatch.syspath_prepend(str(server_src))
    from workbench_mcp_server.bootstrap import Runtime
    from workbench_mcp_server.infrastructure.config.settings import Settings
    from workbench_mcp_server.interfaces.mcp.server import create_http_app

    monkeypatch.setenv('KNOWLEDGE_TEST_AGENT', 'fixture-agent')
    monkeypatch.setenv('KNOWLEDGE_TEST_UPSTREAM', 'fixture-upstream')
    scope = {
        'team': 'ExampleTeam',
        'environment': 'test',
        'region': 'id',
        'service': 'checkout',
        'source_id': 'kb',
    }
    settings = Settings.model_validate(
        {
            'server': {'allowed_hosts': ['localhost:*']},
            'principals': [
                {
                    'id': 'agent',
                    'token_secret_ref': 'env://KNOWLEDGE_TEST_AGENT',
                    'tools': ['knowledge_search'],
                    'scopes': [scope],
                }
            ],
            'sources': [
                {
                    'id': 'kb',
                    'kind': 'knowledge',
                    'adapter': 'normalized-http-v1',
                    'endpoint': 'https://example.invalid/query',
                    'auth_secret_ref': 'env://KNOWLEDGE_TEST_UPSTREAM',
                    'binding_id': 'fixture-kb',
                    'scopes': [scope],
                    'scope_enforced': True,
                }
            ],
        }
    )
    runtime = Runtime(settings)

    class Gateway:
        calls = 0

        async def query(self, request):
            self.calls += 1
            return {
                'matches': [
                    {
                        'source_id': 'guide-1',
                        'title': 'Guide',
                        'content': 'Check the queue',
                        'source_uri': None,
                    }
                ]
            }

    gateway = Gateway()
    runtime.executor.gateways['kb'] = gateway
    app = create_http_app(runtime)
    async with app.app.router.lifespan_context(app.app):
        try:
            factory = StreamableSessionFactory(
                {('local', 'read'): 'https://localhost:8000/mcp'},
                lambda *_: {'Authorization': 'Bearer fixture-agent'},
                transport=httpx.ASGITransport(app=app),
            )
            adapter = MCPAdapter(
                factory,
                ConnectionPolicy(
                    [
                        TrustedConnection(
                            platform.auth.team_id,
                            platform.agent_id,
                            'local',
                            'read',
                            frozenset({'knowledge_search'}),
                            (GRANT,),
                        )
                    ]
                ),
            )
            catalog = await adapter.discover(
                'local', 'read', team_id=platform.auth.team_id, agent_id=platform.agent_id
            )
            schema = catalog.tools[catalog.names.index('knowledge_search')].inputSchema
            binding = ToolBinding(
                alias='kb.search',
                provider='mcp',
                tool_name='knowledge_search',
                server='local',
                credential_scope='read',
                permission='knowledge.read',
                input_schema=schema,
            )
            ref = await ToolRegistry(platform.backend).publish(
                platform.auth, platform.agent_id, binding, key='kb'
            )
            await publish_profile(platform, 'knowledge.scoped_excerpt', tools=(ref,))
            permission = 'knowledge.read'
            rules = {
                'users': {platform.auth.subject: [permission]},
                'teams': {platform.auth.team_id: [permission]},
                'agents': {platform.agent_id: [permission]},
                'experts': {'knowledge': [permission]},
                'skills': {'__none__': [permission]},
            }
            platform.backend._tool_permissions = DeploymentToolPermissions(lambda: rules)
            platform.backend._diagnosis_factory = lambda execution: (
                invoker(platform, execution),
                PolicyToolExecutor(platform.backend, adapter, execution=execution, delays=(0, 0)),
            )
            run = await create_run(
                platform,
                'qa',
                'Use kb_search with query queue and scene qa. What does the guide recommend?',
                query_scope=SCOPE,
            )
            assert (
                await Worker(
                    platform.backend, 'qa-live', foundation_worker_registry(platform.backend)
                ).run_once()
                == 'projected'
            )
            result = await platform.get_run(run.run_id)
            assert (
                result.status == 'partial'
                and len(result.outcome['evidence_refs']) >= 1
                and gateway.calls >= 1
            )
            async with platform.backend.create_uow().begin_read() as uow:
                evidence = await platform.contexts.evidence_content(
                    uow, platform.auth, run.run_id, str(result.outcome['evidence_refs'][0])
                )
            assert 'guide-1' in str(evidence) and 'Check the queue' in str(evidence)
            usage = await platform.budgets.snapshot(platform.auth, run.run_id)
            assert (
                usage.model_attempts >= 2 and usage.tool_attempts >= 1 and not usage.usage_pending
            )
        finally:
            await runtime.close()
