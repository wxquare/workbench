import hashlib
import json
from uuid import uuid4
import pytest
from sqlalchemy import insert
from workbench_agent.domain.profiles import ProfileDraft, VersionRef
from workbench_agent.application.errors import DomainError
from workbench_agent.persistence import schema as s


async def snapshot(platform, *, status='ready', sha='a' * 40, scope='current'):
    owner = platform.auth
    agent = platform.agent_id
    if scope == 'team':
        owner = platform.other_auth
        agent = (await platform.profiles.get_agent(owner))['agent_id']
    elif scope == 'agent':
        agent = str(uuid4())
        async with platform.backend.create_uow().begin_write() as uow:
            await uow.execute(
                insert(s.agents).values(team_id=owner.team_id, agent_id=agent, name=agent)
            )
    values = dict(
        snapshot_id=str(uuid4()),
        team_id=owner.team_id,
        agent_id=agent,
        repo_id=str(uuid4()),
        commit_sha=sha,
        format_version='git-archive-v1',
        storage_ref='s3://snapshots/immutable-object',
        status=status,
    )
    async with platform.backend.create_uow().begin_write() as uow:
        await uow.execute(insert(s.repo_snapshots).values(**values))
    descriptor = {k: v for k, v in values.items() if k != 'status'}
    descriptor['contract'] = 'repo-snapshot-identity-v1'
    hashed = hashlib.sha256(
        json.dumps(descriptor, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode()
    ).hexdigest()
    return VersionRef(resource_id=values['snapshot_id'], version=sha, content_hash=hashed)


async def save(platform, ref):
    context = await platform.contexts.resolve(platform.auth, platform.agent_id)
    current = await platform.profiles.get_agent(platform.auth, platform.agent_id)
    return await platform.profiles.save_draft(
        platform.auth,
        platform.agent_id,
        ProfileDraft(default_model=context.default_model, repositories=(ref,)),
        current['revision'],
        key=str(uuid4()),
    )


@pytest.mark.asyncio
@pytest.mark.parametrize('sha', ['a' * 40, 'b' * 64])
async def test_ready_snapshot_can_be_published_pinned_and_admitted(platform, sha):
    ref = await snapshot(platform, sha=sha)
    draft = await save(platform, ref)
    published = await platform.profiles.publish(
        platform.auth, platform.agent_id, draft['revision'], key='publish-repository'
    )
    assert published.repositories == (ref,)
    context = await platform.contexts.resolve(platform.auth, platform.agent_id)
    assert context.repositories == (ref,)
    run = await platform.create_run()
    assert (await platform.get_run(run.run_id)).context.repositories == (ref,)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    'invalid', ['hash', 'version', 'agent', 'team', 'unready', 'commit_short', 'commit_upper']
)
async def test_invalid_snapshot_reference_is_scoped_not_found(platform, invalid):
    ref = await snapshot(
        platform,
        status='pending' if invalid == 'unready' else 'ready',
        sha='BAD'
        if invalid == 'commit_short'
        else 'A' * 40
        if invalid == 'commit_upper'
        else 'a' * 40,
        scope=invalid if invalid in ('agent', 'team') else 'current',
    )
    if invalid == 'hash':
        ref = ref.model_copy(update={'content_hash': '0' * 64})
    if invalid == 'version':
        ref = ref.model_copy(update={'version': 'b' * 40})
    with pytest.raises(DomainError) as error:
        await save(platform, ref)
    assert error.value.status == 404
