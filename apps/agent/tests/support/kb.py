"""Real Agent HTTP to MCP subprocess test harness."""
import asyncio
import json
import os
import socket
import sys
from dataclasses import dataclass
from uuid import uuid4

import httpx
import pytest
import uvicorn
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

from support.mcp import mcp_server_source
from workbench_agent.api.app import create_app
from workbench_agent.knowledge.config import KnowledgeSettings


@dataclass
class KBChain:
    client: object
    entry_id: str
    url: str
    restart_agent: object

    async def search(self, query, *, region='sg'):
        args = {'scope': {'team': 'ExampleTeam', 'source_id': 'workbench-kb', 'service': 'stock',
                          'environment': 'test', 'region': region},
                'context': {'run_id': 'test-run', 'invocation_id': str(uuid4()), 'query_scope_ref': 'test-scope'},
                'query': query, 'scene': 'qa', 'limit': 3}
        async with httpx.AsyncClient(headers={'Authorization': 'Bearer fictional-mcp-token'}, trust_env=False) as http:
            async with streamable_http_client(self.url, http_client=http) as (read, write, _):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    result = await session.call_tool('knowledge_search', args)
                    assert result.structuredContent is not None, result
                    return result.structuredContent


@pytest.fixture
async def kb_chain(client, platform, tmp_path, monkeypatch, request):
    created = await client.post('/v1/kb/entries', headers={'Idempotency-Key': str(uuid4())}, json={
        'title': '库存诊断', 'kind': 'runbook', 'body_md': '# 库存\n检查库存扣减失败。',
        'service': 'stock', 'environment': 'test', 'region': 'sg'})
    assert created.status_code == 201, created.text
    entry_id = created.json()['entry_id']
    published = await client.post(f'/v1/kb/entries/{entry_id}/publish',
        headers={'If-Match': '0', 'Idempotency-Key': str(uuid4())})
    assert published.status_code == 201, published.text
    monkeypatch.setenv('TEST_KB_INTERNAL_TOKEN', 'fictional-internal-token')
    knowledge = KnowledgeSettings.model_validate({'enabled': True,
        'public_base_url': 'https://kb.example.test' if getattr(request, 'param', None) == 'public-url' else None, 'grants': [{
        'name': 'reader', 'team_id': platform.auth.team_id, 'business_team': 'ExampleTeam',
        'source_id': 'workbench-kb', 'token_secret_ref': 'env://TEST_KB_INTERNAL_TOKEN',
        'allowed_scopes': [{'service': 'stock', 'environment': 'test', 'region': 'sg'}]}]})
    app = create_app(platform.settings.model_copy(update={'knowledge': knowledge}), backend=platform.backend)
    sock = socket.socket()
    sock.bind(('127.0.0.1', 0))
    agent_port = sock.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(app, log_level='error', lifespan='on'))
    task = asyncio.create_task(server.serve(sockets=[sock]))
    process = None
    try:
        for _ in range(500):
            if server.started:
                break
            if task.done():
                await task
                raise RuntimeError('Agent server exited before starting')
            await asyncio.sleep(.02)
        assert server.started, 'Agent server startup timed out'
        with socket.socket() as probe:
            probe.bind(('127.0.0.1', 0))
            mcp_port = probe.getsockname()[1]
        scopes = [{'team': 'ExampleTeam', 'source_id': 'workbench-kb', 'service': 'stock',
                   'environment': 'test', 'region': region} for region in ('sg', 'id')]
        config = {'server': {'host': '127.0.0.1', 'port': mcp_port, 'local_development': True,
                             'allowed_hosts': ['127.0.0.1:*', 'localhost:*']},
                  'principals': [{'id': 'agent', 'token_secret_ref': 'env://TEST_MCP_TOKEN',
                                  'tools': ['knowledge_search'], 'scopes': scopes}],
                  'sources': [{'id': 'workbench-kb', 'kind': 'knowledge', 'adapter': 'normalized-http-v1',
                               'endpoint': f'http://127.0.0.1:{agent_port}/internal/kb/query',
                               'auth_secret_ref': 'env://TEST_KB_INTERNAL_TOKEN', 'binding_id': 'workbench-kb@1',
                               'scopes': scopes, 'scope_enforced': True,
                               'allowed_source_hosts': ['kb.example.test']}]}
        config_path = tmp_path / 'mcp.json'
        config_path.write_text(json.dumps(config))
        env = os.environ.copy()
        env.update(TEST_MCP_TOKEN='fictional-mcp-token', PYTHONPATH=str(mcp_server_source()))
        if getattr(request, 'param', None) == 'wrong-token':
            env['TEST_KB_INTERNAL_TOKEN'] = 'fictional-wrong-token'
        elif getattr(request, 'param', None) == 'wrong-endpoint':
            config['sources'][0]['endpoint'] += '/missing'
            config_path.write_text(json.dumps(config))
        process = await asyncio.create_subprocess_exec(sys.executable, '-m', 'workbench_mcp_server.interfaces.cli',
            'serve', '--config', str(config_path), '--transport', 'streamable-http', env=env,
            stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE)
        url = f'http://127.0.0.1:{mcp_port}/mcp'
        async with httpx.AsyncClient(trust_env=False) as http:
            for _ in range(500):
                if process.returncode is not None:
                    raise RuntimeError(f'MCP process exited: {(await process.stderr.read()).decode()}')
                try:
                    response = await http.post(url, json={})
                    if response.status_code == 401:
                        break
                except httpx.ConnectError:
                    pass
                await asyncio.sleep(.02)
            else:
                raise RuntimeError('MCP process startup timed out')
        async def restart_agent():
            nonlocal server, task, sock
            server.should_exit = True
            await asyncio.wait_for(task, 5)
            sock.close()
            sock = socket.socket()
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            sock.bind(('127.0.0.1', agent_port))
            server = uvicorn.Server(uvicorn.Config(app, log_level='error', lifespan='on'))
            task = asyncio.create_task(server.serve(sockets=[sock]))
            for _ in range(500):
                if server.started:
                    return
                if task.done():
                    await task
                    raise RuntimeError('Agent server exited before restarting')
                await asyncio.sleep(.02)
            raise RuntimeError('Agent server restart timed out')
        yield KBChain(client, entry_id, url, restart_agent)
    finally:
        if process is not None:
            if process.returncode is None:
                process.terminate()
            try:
                await asyncio.wait_for(process.communicate(), 5)
            except asyncio.TimeoutError:
                process.kill()
                await process.communicate()
        server.should_exit = True
        await asyncio.wait_for(task, 5)
        sock.close()
