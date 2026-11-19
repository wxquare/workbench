"""Bootstrap defaults and retirement of the original foundation graphs."""
from uuid import uuid4
import pytest


@pytest.mark.parametrize('old_id', [f'dod.v{number}' for number in range(1, 12)])
async def test_retired_graphs_cannot_be_published(platform, client, old_id):
    url = '/v1/agents/' + platform.agent_id
    agent = (await client.get(url)).json()
    response = await client.patch(url, json={**agent['draft'], 'graph_id': old_id},
        headers={'If-Match': str(agent['revision']), 'Idempotency-Key': str(uuid4())})
    assert response.status_code == 422
    assert response.json()['code'] == 'GRAPH_UNAVAILABLE'
    assert (await client.get(url)).json()['revision'] == agent['revision']


async def test_bootstrap_creates_qa_agent_without_replacing_existing_profile(platform):
    from workbench_agent.cli.bootstrap import BootstrapService
    previous = await platform.contexts.resolve(platform.auth, platform.agent_id)
    settings = platform.settings.model_copy(deep=True)
    settings.bootstrap.agent_id = str(uuid4())
    settings.bootstrap.agent_name = 'new-default'
    assert (await BootstrapService(platform.backend).apply(settings)).created_agent
    current = await platform.contexts.resolve(platform.auth, platform.agent_id)
    assert current.agent_profile == previous.agent_profile
    agent = await platform.profiles.get_agent(platform.auth, settings.bootstrap.agent_id)
    assert agent['draft']['graph_id'] == 'conversation.multi_turn'
    assert agent['draft']['allowed_modes'] == ['qa']
