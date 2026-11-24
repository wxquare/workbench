from uuid import uuid4

from workbench_agent.application.errors import DomainError
from workbench_agent.application.model_profiles import ModelProfileService
from workbench_agent.settings import ModelProfile
import pytest


async def test_list_pages_only_models_visible_in_the_agent(client, platform):
    service = ModelProfileService(platform.backend)
    other = await platform.profiles.create_agent(platform.auth, 'other', key='other-agent')
    await service.create(
        platform.auth, platform.agent_id, ModelProfile(name='local'), 'local-model'
    )
    await service.create(
        platform.auth, other['agent_id'], ModelProfile(name='hidden'), 'hidden-model'
    )
    url = f'/v1/agents/{platform.agent_id}/model-profiles'
    names = []
    cursor = None
    for _ in range(5):
        response = await client.get(
            url, params={'limit': 1, **({'after': cursor} if cursor else {})}
        )
        assert response.status_code == 200
        page = response.json()
        names.extend(item['name'] for item in page['items'])
        cursor = page['next_cursor']
        if not cursor:
            break
    assert sorted(names) == ['deepseek-dod-default', 'local']
    assert cursor is None
    assert (await client.get(url, params={'limit': 0})).status_code == 422
    assert (await client.get(url, params={'after': 'invalid'})).status_code == 422


async def test_list_requires_admin_and_agent_scope(platform):
    service = ModelProfileService(platform.backend)
    await platform.teams.set_member(
        platform.auth, 'reader', 'viewer', 'active', expected_revision=0, key='reader'
    )
    viewer = platform.auth.model_copy(update={'subject': 'reader', 'roles': ('viewer',)})
    with pytest.raises(DomainError):
        await service.list(viewer, platform.agent_id)
    restricted = platform.auth.model_copy(update={'agent_scope': (str(uuid4()),)})
    with pytest.raises(DomainError):
        await service.list(restricted, platform.agent_id)
    with pytest.raises(DomainError):
        await service.list(platform.other_auth, platform.agent_id)


async def test_published_models_expose_pinned_reference(client, platform):
    url = f'/v1/agents/{platform.agent_id}/model-profiles'
    response = await client.get(url)
    model = next(
        item for item in response.json()['items'] if item['name'] == 'deepseek-dod-default'
    )
    context = await platform.contexts.resolve(platform.auth, platform.agent_id)
    assert model['active_ref'] == context.default_model.model_dump(mode='json')
    detail = await client.get(url + '/deepseek-dod-default')
    assert detail.json()['active_ref'] == model['active_ref']
