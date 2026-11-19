"""Real Worker → MCP protocol → normalized gateway → SQLite knowledge journey."""
import json
from uuid import uuid4

import httpx
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from contract.test_dod_mcp_mapping import GRANT, SCOPE
from support.mcp import mcp_server_source
from support.runs import make_run_payload
from workbench_agent.api.app import create_app
from workbench_agent.domain.profiles import ProfileDraft
from workbench_agent.domain.tools import ToolBinding
from workbench_agent.jobs.worker import Worker
from workbench_agent.knowledge.config import KnowledgeSettings
from workbench_agent.persistence.backends.sqlite import SQLiteBackend
from workbench_agent.runtime.graphs.registry import foundation_worker_registry
from workbench_agent.runtime.models.invoker import ModelInvoker
from workbench_agent.tools.mcp import ConnectionPolicy, MCPAdapter, StreamableSessionFactory, TrustedConnection
from workbench_agent.tools.permissions import DeploymentToolPermissions
from workbench_agent.tools.policy import PolicyToolExecutor
from workbench_agent.tools.registry import ToolRegistry


async def test_missing_knowledge_to_published_replay_and_human_confirmation(platform, client, monkeypatch):
    monkeypatch.syspath_prepend(str(mcp_server_source()))
    from workbench_mcp_server.bootstrap import Runtime
    from workbench_mcp_server.infrastructure.config.settings import Settings
    from workbench_mcp_server.infrastructure.connectors.normalized_http import NormalizedHttpGateway
    from workbench_mcp_server.interfaces.mcp.server import create_http_app

    monkeypatch.setenv('JOURNEY_AGENT', 'fictional-mcp-agent')
    monkeypatch.setenv('JOURNEY_UPSTREAM', 'fictional-internal-reader')
    scope = {'team': 'ExampleTeam', 'environment': 'test', 'region': 'id', 'service': 'checkout', 'source_id': 'kb'}
    kb_settings = KnowledgeSettings.model_validate({'enabled': True, 'grants': [{
        'name': 'journey', 'team_id': platform.auth.team_id, 'business_team': 'ExampleTeam', 'source_id': 'kb',
        'token_secret_ref': 'env://JOURNEY_UPSTREAM',
        'allowed_scopes': [{'service': 'checkout', 'environment': 'test', 'region': 'id'}]}]})
    agent_app = create_app(platform.settings.model_copy(update={'knowledge': kb_settings}), backend=platform.backend)
    settings = Settings.model_validate({'server': {'allowed_hosts': ['localhost:*']},
        'principals': [{'id': 'agent', 'token_secret_ref': 'env://JOURNEY_AGENT', 'tools': ['knowledge_search'], 'scopes': [scope]}],
        'sources': [{'id': 'kb', 'kind': 'knowledge', 'adapter': 'normalized-http-v1',
            'endpoint': 'https://example.invalid/internal/kb/query', 'auth_secret_ref': 'env://JOURNEY_UPSTREAM',
            'binding_id': 'fixture-kb', 'scopes': [scope], 'scope_enforced': True}]})
    runtime = Runtime(settings)
    async with agent_app.router.lifespan_context(agent_app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=agent_app)) as upstream:
            runtime.executor.gateways['kb'] = NormalizedHttpGateway(settings.sources[0], upstream, 'fictional-internal-reader')
            mcp_app = create_http_app(runtime)
            try:
                async with mcp_app.app.router.lifespan_context(mcp_app.app):
                    sessions = StreamableSessionFactory({('local', 'read'): 'https://localhost:8000/mcp'},
                        lambda *_: {'Authorization': 'Bearer fictional-mcp-agent'}, transport=httpx.ASGITransport(app=mcp_app))
                    adapter = MCPAdapter(sessions, ConnectionPolicy([TrustedConnection(platform.auth.team_id,
                        platform.agent_id, 'local', 'read', frozenset({'knowledge_search'}), (GRANT,))]))
                    catalog = await adapter.discover('local', 'read', team_id=platform.auth.team_id, agent_id=platform.agent_id)
                    tool_schema = catalog.tools[catalog.names.index('knowledge_search')].inputSchema
                    binding = ToolBinding(alias='kb.search', provider='mcp', tool_name='knowledge_search',
                        server='local', credential_scope='read', permission='knowledge.read', input_schema=tool_schema)
                    tool = await ToolRegistry(platform.backend).publish(platform.auth, platform.agent_id, binding, key='journey-tool')
                    context = await platform.contexts.resolve(platform.auth, platform.agent_id)
                    agent = await platform.profiles.get_agent(platform.auth, platform.agent_id)
                    changed = await platform.profiles.save_draft(platform.auth, platform.agent_id,
                        ProfileDraft(default_model=context.default_model, graph_version='dod.v19', tools=(tool,), knowledge_max_age_seconds=31536000),
                        agent['revision'], key='journey-profile')
                    await platform.profiles.publish(platform.auth, platform.agent_id, changed['revision'], key='journey-profile-publish')
                    rules = {'users': {platform.auth.subject: ['knowledge.read']}, 'teams': {platform.auth.team_id: ['knowledge.read']},
                        'agents': {platform.agent_id: ['knowledge.read']}, 'skills': {'__none__': ['knowledge.read']},
                        'experts': {'knowledge': ['knowledge.read']}}
                    permissions = DeploymentToolPermissions(lambda: rules)
                    platform.backend._tool_permissions = permissions
                    platform.backend._mcp_connection_policy = adapter.policy

                    class Model:
                        def bind_tools(self, tools): return self
                        async def ainvoke(self, messages, **kwargs):
                            usage = {'input_tokens': 8, 'output_tokens': 3, 'total_tokens': 11}
                            if kwargs.get('response_format'):
                                data = json.loads(next(m.content for m in messages if isinstance(m, HumanMessage)))
                                if 'sources' in data:
                                    source = data['sources'][0]
                                    answer = {'answerable': True, 'claims': [{'text': '按来源核对退款状态。', 'citations': [{
                                        'evidence_ref': source['evidence_ref'], 'source_id': source['source_id'],
                                        'quote': source['content'].strip()}]}]}
                                else:
                                    answer = {'checks': [{'index': 0, 'supported': True, 'reason': 'reviewed'}]}
                                return AIMessage(content=json.dumps(answer, ensure_ascii=False), usage_metadata=usage)
                            if any(isinstance(m, ToolMessage) for m in messages):
                                return AIMessage(content='已检索知识。', usage_metadata=usage)
                            return AIMessage(content='', tool_calls=[{'name': 'kb_search',
                                'args': {'query': '退款重试前检查什么', 'scene': 'qa'}, 'id': 'call-1', 'type': 'tool_call'}], usage_metadata=usage)
                    class Factory:
                        def build(self, *args, **kwargs): return Model()
                    worker_backend = SQLiteBackend(platform.settings.storage.database_url.get_secret_value(), tool_permissions=permissions)
                    try:
                        worker_backend._mcp_connection_policy = adapter.policy
                        worker_backend._diagnosis_factory = lambda execution: (
                            ModelInvoker(worker_backend, Factory(), object(), execution=execution),
                            PolicyToolExecutor(worker_backend, adapter, execution=execution, delays=(0, 0)))
                        worker = Worker(worker_backend, 'journey-worker', foundation_worker_registry(worker_backend))
                        payload = make_run_payload(platform.agent_id)
                        payload['input']['text'] = '退款重试前检查什么？'
                        payload['business_context'] = {'query_scope': SCOPE}
                        original = await client.post('/v1/runs', json=payload, headers={'Idempotency-Key': 'journey-original'})
                        assert original.status_code == 202, original.text
                        original_id = original.json()['run_id']
                        assert await worker.run_once() == 'projected'
                        assert (await client.get(f'/v1/runs/{original_id}')).json()['status'] == 'partial'
                        gaps = (await client.get('/v1/kb/gaps')).json()['items']
                        gap = next(item for item in gaps if item['run_id'] == original_id)
                        draft = await client.post(f"/v1/kb/gaps/{gap['gap_id']}/draft",
                            headers={'If-Match': str(gap['gap_revision']), 'Idempotency-Key': 'journey-draft'})
                        assert draft.status_code == 201, draft.text
                        item = draft.json()
                        saved = await client.patch(f"/v1/kb/entries/{item['entry_id']}", json={**item['draft'],
                            'body_md': '# 退款检查\n先检查退款状态，确认没有完成后再重试。'},
                            headers={'If-Match': '0', 'Idempotency-Key': 'journey-save'})
                        assert saved.status_code == 200, saved.text
                        gap = (await client.get(f"/v1/kb/gaps/{gap['gap_id']}")).json()
                        first = await client.post(f"/v1/kb/gaps/{gap['gap_id']}/replay",
                            headers={'If-Match': str(gap['gap_revision']), 'Idempotency-Key': 'journey-before'})
                        assert first.status_code == 202, first.text
                        assert await worker.run_once() == 'projected'
                        assert (await client.get(f"/v1/runs/{first.json()['run_id']}")).json()['status'] == 'partial'
                        published = await client.post(f"/v1/kb/entries/{item['entry_id']}/publish",
                            headers={'If-Match': str(saved.json()['revision']), 'Idempotency-Key': 'journey-publish'})
                        assert published.status_code == 201, published.text
                        gap = (await client.get(f"/v1/kb/gaps/{gap['gap_id']}")).json()
                        replay = await client.post(f"/v1/kb/gaps/{gap['gap_id']}/replay",
                            headers={'If-Match': str(gap['gap_revision']), 'Idempotency-Key': 'journey-after'})
                        assert replay.status_code == 202, replay.text
                        assert await worker.run_once() == 'projected'
                        result = (await client.get(f"/v1/runs/{replay.json()['run_id']}")).json()
                        assert result['status'] == 'succeeded', result
                        citation = result['outcome']['knowledge_answer']['claims'][0]['citations'][0]
                        assert citation['source_id'].startswith(f"kb:{item['entry_id']}@{published.json()['version']}#")
                        gap = (await client.get(f"/v1/kb/gaps/{gap['gap_id']}")).json()
                        newer = await client.patch(f"/v1/kb/entries/{item['entry_id']}", json={**item['draft'],
                            'body_md': '# 退款检查\n更新后的其他内容。'},
                            headers={'If-Match': '2', 'Idempotency-Key': 'journey-update'})
                        assert newer.status_code == 200, newer.text
                        newer_published = await client.post(f"/v1/kb/entries/{item['entry_id']}/publish",
                            headers={'If-Match': '3', 'Idempotency-Key': 'journey-publish-2'})
                        assert newer_published.status_code == 201 and newer_published.json()['version'] == 2
                        wrong_version = await client.patch(f"/v1/kb/gaps/{gap['gap_id']}", json={'status': 'resolved',
                            'note': '尝试使用并未被重放引用的版本。', 'entry_id': item['entry_id'], 'version': 2,
                            'verification_run_id': replay.json()['run_id']},
                            headers={'If-Match': str(gap['gap_revision']), 'Idempotency-Key': 'journey-wrong-version'})
                        assert wrong_version.status_code == 409
                        resolved = await client.patch(f"/v1/kb/gaps/{gap['gap_id']}", json={'status': 'resolved',
                            'note': '已核对回答及引用。', 'entry_id': item['entry_id'], 'version': published.json()['version'],
                            'verification_run_id': replay.json()['run_id']},
                            headers={'If-Match': str(gap['gap_revision']), 'Idempotency-Key': 'journey-resolve'})
                        assert resolved.status_code == 200, resolved.text
                        assert resolved.json()['verification_status'] == 'human_confirmed'
                        assert (await client.get(f'/v1/runs/{original_id}')).json()['status'] == 'partial'
                    finally:
                        await worker_backend.close()
            finally:
                await runtime.close()
