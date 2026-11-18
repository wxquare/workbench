async def test_mcp_chain_returns_exact_kb_version(kb_chain):
    result = await kb_chain.search('扣减失败')
    assert result['status'] == 'ok'
    match = result['data']['matches'][0]
    assert match['source_id'].startswith(f'kb:{kb_chain.entry_id}@1#')
    assert '检查库存扣减失败' in match['content']
    assert match['source_locator']
    assert match['source_uri'] is None
    assert result['evidence_refs']

async def test_mcp_chain_empty_is_not_an_error(kb_chain):
    result = await kb_chain.search('不存在的知识')
    assert result['status'] == 'empty'
    assert result['data']['matches'] == []


async def test_mcp_chain_denies_internal_and_principal_scope(kb_chain):
    internal_denied = await kb_chain.search('扣减失败', region='id')
    principal_denied = await kb_chain.search('扣减失败', region='xx')
    assert internal_denied['status'] == 'unavailable'
    assert principal_denied['status'] == 'denied'


import pytest
from sqlalchemy import text


@pytest.mark.parametrize('kb_chain', ['wrong-token', 'wrong-endpoint'], indirect=True)
async def test_mcp_upstream_auth_and_http_errors_are_not_empty(kb_chain):
    result = await kb_chain.search('扣减失败')
    assert result['status'] == 'unavailable'
    assert result['status'] != 'empty'


async def test_mcp_index_failure_is_not_empty(kb_chain, platform):
    async with platform.backend.create_uow().begin_write() as uow:
        await uow.execute(text('DROP TABLE kb_fts'))
    result = await kb_chain.search('扣减失败')
    assert result['status'] == 'unavailable'


@pytest.mark.parametrize('kb_chain', ['public-url'], indirect=True)
async def test_mcp_chain_preserves_allowed_public_url(kb_chain):
    result = await kb_chain.search('扣减失败')
    assert result['status'] == 'ok'
    assert result['data']['matches'][0]['source_uri'].startswith(
        'https://kb.example.test/app/knowledge/')


async def test_mcp_chain_survives_agent_restart(kb_chain):
    assert (await kb_chain.search('扣减失败'))['status'] == 'ok'
    await kb_chain.restart_agent()
    result = await kb_chain.search('扣减失败')
    assert result['status'] == 'ok'
    assert result['data']['matches'][0]['source_id'].startswith(f'kb:{kb_chain.entry_id}@1#')
