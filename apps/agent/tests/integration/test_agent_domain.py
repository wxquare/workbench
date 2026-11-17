"""Public Agent ownership is independent of the Agent's business scenario."""
from uuid import uuid4

from workbench_agent.persistence import schema


async def create_published_agent(client, name, prompt):
    created = await client.post('/v1/agents', json={'name': name},
        headers={'Idempotency-Key': str(uuid4())})
    assert created.status_code == 201, created.text
    agent_id = created.json()['agent_id']
    url = '/v1/agents/' + agent_id
    detail = (await client.get(url)).json()
    draft = {**detail['draft'], 'graph_version': 'dod.v18',
        'allowed_modes': ['qa'], 'system_prompt': prompt}
    saved = await client.patch(url, json=draft,
        headers={'Idempotency-Key': str(uuid4()), 'If-Match': str(detail['revision'])})
    assert saved.status_code == 200, saved.text
    published = await client.post(url + '/profiles/publish',
        headers={'Idempotency-Key': str(uuid4()), 'If-Match': str(saved.json()['revision'])})
    assert published.status_code == 201, published.text
    assert published.json()['agent_id'] == agent_id
    assert published.json()['system_prompt'] == prompt
    return agent_id


async def test_workbench_hosts_independently_configured_agents(platform, client):
    listing = await client.get('/v1/agents')
    assert listing.status_code == 200
    default = listing.json()['items'][0]
    original = (await client.get('/v1/agents/' + default['agent_id'])).json()
    agents = []
    for name, prompt in (
        ('DoD Agent', 'Assist with duty handover. Do not claim live monitoring.'),
        ('Writing Agent', 'Help edit documents and preserve the requested tone.'),
    ):
        agents.append(await create_published_agent(client, name, prompt))
    assert len(set(agents)) == 2
    assert (await client.get('/v1/agents/' + default['agent_id'])).json() == original
    assert (await client.get('/v1/workspaces')).status_code == 404


def test_current_schema_uses_agent_ownership():
    assert 'agents' in schema.metadata.tables
    assert 'workspaces' not in schema.metadata.tables
    for table_name in ('runtime_profiles', 'runs', 'threads', 'tool_bindings', 'schedules'):
        table = schema.metadata.tables[table_name]
        assert 'agent_id' in table.c
        assert 'workspace_id' not in table.c


async def test_run_rejects_legacy_workspace_request(platform, client):
    from support.runs import make_run_payload

    payload = make_run_payload(platform.agent_id)
    payload['workspace_id'] = payload.pop('agent_id')
    response = await client.post('/v1/runs', json=payload,
        headers={'Idempotency-Key': str(uuid4())})
    assert response.status_code == 422


async def test_agent_creation_does_not_require_another_active_agent(platform, client):
    from sqlalchemy import update

    async with platform.backend.create_uow().begin_write() as uow:
        await uow.execute(update(schema.agents).where(schema.agents.c.team_id == platform.auth.team_id)
            .values(status='archived'))
    response = await client.post('/v1/agents', json={'name': 'First Writing Agent'},
        headers={'Idempotency-Key': 'independent-agent'})
    assert response.status_code == 201, response.text
    assert response.json()['name'] == 'First Writing Agent'


async def test_agent_creation_replay_is_team_scoped(platform, client):
    from sqlalchemy import update

    headers = {'Idempotency-Key': 'stable-agent-creation'}
    created = await client.post('/v1/agents', json={'name': 'Editor'}, headers=headers)
    assert created.status_code == 201
    async with platform.backend.create_uow().begin_write() as uow:
        await uow.execute(update(schema.agents).where(schema.agents.c.agent_id == platform.agent_id)
            .values(status='archived'))
    replay = await client.post('/v1/agents', json={'name': 'Editor'}, headers=headers)
    assert replay.json() == created.json()
    assert replay.headers['Idempotency-Replayed'] == 'true'
    conflict = await client.post('/v1/agents', json={'name': 'Another Editor'}, headers=headers)
    assert conflict.status_code == 409


async def test_restricted_agent_identity_cannot_create_outside_its_scope(platform):
    import pytest
    from workbench_agent.application.errors import DomainError

    restricted = platform.auth.model_copy(update={'agent_scope': (platform.agent_id,)})
    with pytest.raises(DomainError) as error:
        await platform.profiles.create_agent(restricted, 'Outside Scope', key='restricted')
    assert error.value.code == 'FORBIDDEN'


async def test_agent_creation_never_inherits_agent_private_model(platform, client):
    from sqlalchemy import update

    async with platform.backend.create_uow().begin_write() as uow:
        await uow.execute(update(schema.model_profiles).where(schema.model_profiles.c.team_id == platform.auth.team_id)
            .values(agent_id=platform.agent_id, scope='agent'))
    response = await client.post('/v1/agents', json={'name': 'No Shared Model'},
        headers={'Idempotency-Key': 'private-model'})
    assert response.status_code == 409
    assert response.json()['code'] == 'MODEL_PROFILE_UNAVAILABLE'


async def test_two_agent_model_execution_and_conversations_are_isolated(platform, client):
    from langchain_core.messages import AIMessage
    from workbench_agent.jobs.worker import Worker
    from workbench_agent.runtime.graphs.registry import foundation_worker_registry
    from workbench_agent.runtime.models.invoker import ModelInvoker
    from support.runs import make_run_payload

    calls = []

    class Model:
        def build(self, *args, **kwargs):
            return self

        async def ainvoke(self, messages, **kwargs):
            calls.append([(message.type, message.content) for message in messages])
            return AIMessage(content=f'answer-{len(calls)}',
                usage_metadata={'input_tokens': 8, 'output_tokens': 5, 'total_tokens': 13})

    backend = platform.backend
    backend._code_factory = lambda execution: ModelInvoker(backend, Model(), object(), execution=execution)
    worker = Worker(backend, 'multi-agent-worker', foundation_worker_registry(backend))
    duty = await create_published_agent(client, 'DoD Agent', 'Help with duty handover only.')
    writing = await create_published_agent(client, 'Writing Agent', 'Help improve written documents.')

    async def submit(agent_id, text, thread_id=None):
        payload = make_run_payload(agent_id, thread_id=thread_id)
        payload['input']['text'] = text
        payload['business_context'] = {'conversation': True}
        response = await client.post('/v1/runs', json=payload,
            headers={'Idempotency-Key': str(uuid4())})
        assert response.status_code == 202, response.text
        return response.json()

    first = await submit(duty, 'duty-only context')
    assert await worker.run_once() == 'projected'
    second = await submit(writing, 'writing-only context')
    assert await worker.run_once() == 'projected'
    follow = await submit(duty, 'continue duty', first['thread_id'])
    assert await worker.run_once() == 'projected'
    assert 'Help with duty handover only.' in calls[0][0][1]
    assert 'Help improve written documents.' in calls[1][0][1]
    assert 'duty-only context' not in str(calls[1])
    assert calls[2][1:] == [('human', 'duty-only context'), ('ai', 'answer-1'), ('human', 'continue duty')]
    for agent_id, expected in ((duty, {first['run_id'], follow['run_id']}), (writing, {second['run_id']})):
        items = (await client.get('/v1/runs', params={'agent_id': agent_id})).json()['items']
        assert {item['run_id'] for item in items} == expected
        assert all(item['agent_id'] == agent_id for item in items)
    crossed = make_run_payload(writing, thread_id=first['thread_id'])
    crossed['business_context'] = {'conversation': True}
    denied = await client.post('/v1/runs', json=crossed,
        headers={'Idempotency-Key': str(uuid4())})
    assert denied.status_code == 404
