"""Portable saver contract exercised against durable SQLite, not memory saver."""

import pytest
from langgraph.checkpoint.base import empty_checkpoint
from langchain_core.messages import HumanMessage
from workbench_agent.runtime.checkpoints.portable import PortableCheckpointSaver, InvocationGuard
from workbench_agent.persistence.checkpoints.codec import CheckpointCodec
from support.jobs import claim, expire_lease


@pytest.mark.asyncio
async def test_parent_pending_special_dedup_and_reopen(platform):
    run = await platform.create_run()
    owned = await claim(platform)
    provider = InvocationGuard()
    saver = PortableCheckpointSaver(platform.backend, provider, CheckpointCodec())
    config = {'configurable': {'thread_id': run.run_id, 'checkpoint_ns': 'diagnosis:stable'}}
    with provider.bind(owned.guard):
        first = empty_checkpoint()
        first['channel_values'] = {
            'run_id': run.run_id,
            'messages': [HumanMessage('hello', id='stable')],
        }
        parent = await saver.aput(config, first, {'source': 'input', 'step': 0, 'parents': {}}, {})
        second = empty_checkpoint()
        second['channel_values'] = first['channel_values']
        saved = await saver.aput(parent, second, {'source': 'loop', 'step': 1, 'parents': {}}, {})
        await saver.aput_writes(
            saved, [('answer', 'first'), ('__error__', 'old')], 'task', 'branch/path'
        )
        await saver.aput_writes(
            saved, [('answer', 'replacement'), ('__error__', 'new')], 'task', 'branch/path'
        )
        await platform.backend.close()
        from workbench_agent.persistence.backends.sqlite import SQLiteBackend

        backend = SQLiteBackend(platform.settings.storage.database_url.get_secret_value())
        try:
            reopened = PortableCheckpointSaver(backend, provider, CheckpointCodec())
            restored = await reopened.aget_tuple(saved)
            assert restored.parent_config == parent
            assert restored.checkpoint['channel_values']['messages'][0].id == 'stable'
            assert ('task', 'answer', 'first') in restored.pending_writes
            assert ('task', '__error__', 'new') in restored.pending_writes
            listed = [
                item
                async for item in reopened.alist(config, filter={'step': 0}, before=saved, limit=1)
            ]
            assert [item.config for item in listed] == [parent]
        finally:
            await backend.close()


@pytest.mark.asyncio
async def test_stale_guard_cannot_write_and_scope_is_required(platform):
    run = await platform.create_run()
    owned = await claim(platform)
    provider = InvocationGuard()
    saver = PortableCheckpointSaver(platform.backend, provider, CheckpointCodec())
    config = {'configurable': {'thread_id': run.run_id}}
    with pytest.raises(PermissionError):
        await saver.aget_tuple(config)
    with provider.bind(owned.guard):
        saved = await saver.aput(config, empty_checkpoint(), {}, {})
        with pytest.raises(PermissionError):
            await saver.adelete_thread(run.run_id)
        await expire_lease(platform, owned.guard)
        replacement = await claim(platform, 'new')
        assert replacement.guard.run_epoch > owned.guard.run_epoch
        from workbench_agent.persistence.errors import StaleExecution

        with pytest.raises(StaleExecution):
            await saver.aput(saved, empty_checkpoint(), {}, {})
        with pytest.raises(StaleExecution):
            await saver.aput_writes(saved, [('answer', 'late')], 'task')


def test_codec_rejects_unsupported_and_oversized_values():
    codec = CheckpointCodec()
    with pytest.raises(ValueError):
        codec.dumps_typed(object())
    with pytest.raises(ValueError):
        codec.dumps_typed({'text': 'x' * (2 * 1024 * 1024)})
    with pytest.raises(ValueError):
        codec.loads_typed(('pickle', b'bad'))


@pytest.mark.asyncio
async def test_delete_requires_current_maintainer_and_terminal_scope(platform):
    from workbench_agent.jobs.leases import LeaseService
    from workbench_agent.application.errors import DomainError
    from workbench_agent.persistence import schema as s
    from sqlalchemy import update

    run = await platform.create_run()
    owned = await claim(platform)
    provider = InvocationGuard()
    saver = PortableCheckpointSaver(platform.backend, provider, CheckpointCodec())
    config = {'configurable': {'thread_id': run.run_id}}
    with provider.bind(owned.guard):
        await saver.aput(config, empty_checkpoint(), {}, {})
    with provider.bind(owned.guard, delete_auth=platform.auth):
        with pytest.raises(PermissionError):
            await saver.adelete_thread(run.run_id)
    await LeaseService(platform.backend).release(owned.guard, status='succeeded')
    async with platform.backend.create_uow().begin_write() as uow:
        await uow.execute(
            update(s.team_members)
            .where(
                s.team_members.c.team_id == platform.auth.team_id,
                s.team_members.c.subject == platform.auth.subject,
            )
            .values(role='viewer')
        )
    with provider.bind(owned.guard, delete_auth=platform.auth):
        with pytest.raises(DomainError):
            await saver.adelete_thread(run.run_id)
    async with platform.backend.create_uow().begin_write() as uow:
        await uow.execute(
            update(s.team_members)
            .where(
                s.team_members.c.team_id == platform.auth.team_id,
                s.team_members.c.subject == platform.auth.subject,
            )
            .values(role='admin')
        )
    with provider.bind(owned.guard, delete_auth=platform.other_auth):
        with pytest.raises(PermissionError):
            await saver.adelete_thread(run.run_id)
    with provider.bind(owned.guard, delete_auth=platform.auth):
        await saver.adelete_thread(run.run_id)
        assert await saver.aget_tuple(config) is None


@pytest.mark.asyncio
async def test_async_dependency_wait_is_bounded_and_native_order_supported(platform):
    import asyncio
    from workbench_agent.persistence.errors import StorageError

    run = await platform.create_run()
    owned = await claim(platform)
    provider = InvocationGuard()
    saver = PortableCheckpointSaver(platform.backend, provider, CheckpointCodec())
    point = empty_checkpoint()
    saved = saver.config(run.run_id, '', point['id'])
    with provider.bind(owned.guard):
        task = asyncio.create_task(saver.aput_writes(saved, [('answer', 'early')], 'task'))
        await asyncio.sleep(0.03)
        await saver.aput({'configurable': {'thread_id': run.run_id}}, point, {}, {})
        await task
        assert (await saver.aget_tuple(saved)).pending_writes == [('task', 'answer', 'early')]
        original_timeout = platform.backend.operation_timeout
        platform.backend.operation_timeout = 0.1
        try:
            with pytest.raises(StorageError, match='STORAGE_BUSY'):
                async with asyncio.timeout(1):
                    await saver.aput_writes(
                        saver.config(run.run_id, '', 'missing'), [('answer', 'lost')], 'task'
                    )
        finally:
            platform.backend.operation_timeout = original_timeout


@pytest.mark.asyncio
async def test_namespace_collision_and_invocation_scope_do_not_mix(platform, monkeypatch):
    import asyncio
    from workbench_agent.persistence.checkpoints import store

    first = await platform.create_run('first')
    first_claim = await claim(platform)
    second = await platform.create_run('second')
    second_claim = await claim(platform, 'second')
    provider = InvocationGuard()
    saver = PortableCheckpointSaver(platform.backend, provider, CheckpointCodec())

    async def save(owned):
        with provider.bind(owned.guard):
            config = await saver.aput(
                {'configurable': {'thread_id': owned.guard.run_id, 'checkpoint_ns': 'alpha'}},
                empty_checkpoint(),
                {},
                {},
            )
            await asyncio.sleep(0)
            assert [
                item.config['configurable']['thread_id'] async for item in saver.alist(None)
            ] == [owned.guard.run_id]
            other = second.run_id if owned.guard.run_id == first.run_id else first.run_id
            with pytest.raises(PermissionError):
                await saver.aget_tuple({'configurable': {'thread_id': other}})
            return config

    await asyncio.gather(save(first_claim), save(second_claim))
    with provider.bind(first_claim.guard):
        monkeypatch.setattr(
            store,
            'checkpoint_namespace_key',
            lambda namespace: store.hashlib.sha256(b'alpha').hexdigest(),
        )
        with pytest.raises(ValueError, match='collision'):
            await saver.aput(
                {'configurable': {'thread_id': first.run_id, 'checkpoint_ns': 'beta'}},
                empty_checkpoint(),
                {},
                {},
            )


@pytest.mark.asyncio
async def test_list_pagination_namespace_and_persisted_task_path(platform):
    from sqlalchemy import select
    from workbench_agent.persistence import schema as s

    run = await platform.create_run()
    owned = await claim(platform)
    provider = InvocationGuard()
    saver = PortableCheckpointSaver(platform.backend, provider, CheckpointCodec())
    root = {'configurable': {'thread_id': run.run_id, 'checkpoint_ns': ''}}
    with provider.bind(owned.guard):
        first = await saver.aput(root, empty_checkpoint(), {'label': 'root'}, {})
        second = await saver.aput(first, empty_checkpoint(), {'label': 'root'}, {})
        await saver.aput(
            {'configurable': {'thread_id': run.run_id, 'checkpoint_ns': 'child'}},
            empty_checkpoint(),
            {'label': 'child'},
            {},
        )
        await saver.aput_writes(second, [('answer', 'A')], 'z', 'a/path')
        await saver.aput_writes(second, [('answer', 'B')], 'a', 'b/path')
        assert [item.config async for item in saver.alist(root, limit=1)] == [second]
        assert [item.config async for item in saver.alist(root, before=second)] == [first]
        assert [item async for item in saver.alist(root, limit=0)] == []
        assert len([item async for item in saver.alist(None, filter={'label': 'child'})]) == 1
        assert (await saver.aget_tuple(root)).pending_writes == [
            ('z', 'answer', 'A'),
            ('a', 'answer', 'B'),
        ]
    async with platform.backend.create_uow().begin_read() as uow:
        assert (
            await uow.execute(
                select(s.graph_pending_writes.c.task_path).order_by(
                    s.graph_pending_writes.c.task_path
                )
            )
        ).scalars().all() == ['a/path', 'b/path']


def test_codec_roundtrips_only_registered_types_and_rejects_unknown_decode():
    import json
    from langgraph.types import Interrupt, Send

    codec = CheckpointCodec()
    values = {
        'raw': b'\x00\xff',
        'tuple': (1, 'x'),
        'set': {'a'},
        'interrupt': Interrupt(value={'q': 'x'}, id='stable'),
        'send': Send('node', {'x': 1}),
    }
    result = codec.loads_typed(codec.dumps_typed(values))
    assert result['raw'] == b'\x00\xff' and result['tuple'] == (1, 'x') and result['set'] == {'a'}
    assert result['interrupt'].id == 'stable' and result['send'].node == 'node'
    for data in [b'{"$":"os.system","v":"id"}', b'{"$":"dict","v":[]}', b'NaN']:
        with pytest.raises(ValueError):
            codec.loads_typed(('dod-json', data))
