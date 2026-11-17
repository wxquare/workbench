import json
import pytest
from workbench_agent.api.app import create_app
from support.events import EventPlatform,StreamingClient,token

@pytest.mark.asyncio
async def test_incremental_stream_replay_and_cross_run_cursor(platform):
    p=EventPlatform(platform);run,guard,outcome=await p.finalizable_run()
    app=create_app(platform.settings,backend=platform.backend)
    async with StreamingClient(app,f'/v1/runs/{run.run_id}/events',token(platform)) as stream:
        assert stream.start['status']==200
        first=await stream.chunk()
        assert b'event: run.accepted' in first['body'] and first['more_body']
        # First event arrived while Run is still executing, before finalization.
        assert (await platform.get_run(run.run_id)).status=='running'
    assert (await platform.get_run(run.run_id)).status=='running'
    await p.results.finalize(guard,outcome,outcome.digest())
    async with StreamingClient(app,f'/v1/runs/{run.run_id}/events?after_seq=1',token(platform)) as stream:
        bodies=[]
        while True:
            item=await stream.chunk();bodies.append(item.get('body',b''))
            if not item.get('more_body'):break
        wire=b''.join(bodies)
        assert b'event: run.completed' in wire and b'event: run.accepted' not in wire
        data=json.loads(next(line[6:] for line in wire.decode().splitlines() if line.startswith('data: ')))
        assert set(data)=={'schema_version','run_id','seq','type','occurred_at','attempt','message_id','payload'}
    async with StreamingClient(app,f'/v1/runs/{run.run_id}/events',token(platform),[(b'last-event-id',b'wrong-run:1')]) as stream:
        assert stream.start['status']==422

@pytest.mark.asyncio
async def test_cursor_retention_validation_and_revoke(platform,client):
    from sqlalchemy import update
    from workbench_agent.persistence import schema as s
    from workbench_agent.application.errors import DomainError
    from workbench_agent.persistence.events import RETENTION_MS
    p=EventPlatform(platform);run,guard,outcome=await p.finalizable_run()
    path=f'/v1/runs/{run.run_id}/events'
    for params,headers in [({'after_seq':-1},{}),({'after_seq':999},{}),({'after_seq':0},{'Last-Event-ID':run.run_id+':1'})]:
        assert (await client.get(path,params=params,headers=headers)).status_code==422
    async with platform.backend.create_uow().begin_write() as uow:
        now=await platform.backend.clock.now_ms(uow)
        await uow.execute(update(s.run_events).where(s.run_events.c.run_id==run.run_id,s.run_events.c.seq==1).values(occurred_at=now-RETENTION_MS-1))
    response=await client.get(path)
    assert response.status_code==410 and response.json()['links']['result']==f'/v1/runs/{run.run_id}'
    await p.events.prune(platform.auth,run.run_id)
    assert (await client.get(path)).status_code==410
    with pytest.raises(DomainError) as error: await p.events.read_batch(platform.other_auth,run.run_id,0,10)
    assert error.value.status==404
    async with platform.backend.create_uow().begin_write() as uow:
        await uow.execute(update(s.team_members).where(s.team_members.c.team_id==platform.auth.team_id).values(status='revoked'))
    with pytest.raises(DomainError) as error: await p.events.read_batch(platform.auth,run.run_id,0,10)
    assert error.value.status==404

@pytest.mark.asyncio
async def test_heartbeat_no_sequence_and_db_poll_survives_lost_hint(platform):
    import asyncio
    from workbench_agent.runtime.streaming import stream_events
    p=EventPlatform(platform);run,guard,outcome=await p.finalizable_run()
    before=await p.read_events(run.run_id);last=before[-1].seq
    stream=stream_events(p.events,platform.auth,run.run_id,last,heartbeat_seconds=.03)
    assert await asyncio.wait_for(anext(stream),.5)==b': heartbeat\n\n'
    assert (await p.read_events(run.run_id))[-1].seq==last
    await stream.aclose()
    async with StreamingClient(create_app(platform.settings,backend=platform.backend),f'/v1/runs/{run.run_id}/events?after_seq={last}',token(platform)) as transport:
        waiting=asyncio.create_task(transport.chunk())
        await p.events.append_execution(guard,'phase.changed',{'phase':'committed without hint'})
        chunk=await asyncio.wait_for(waiting,1.5)
        assert b'committed without hint' in chunk['body']

@pytest.mark.asyncio
async def test_delta_merger_char_timer_flush_and_stale_cancel(platform):
    import asyncio
    from workbench_agent.runtime.streaming import DeltaMerger
    from support.jobs import expire_lease,claim
    p=EventPlatform(platform);run,guard,outcome=await p.finalizable_run()
    revision=(await platform.get_run(run.run_id)).revision
    async with DeltaMerger(p.events,guard) as merger:
        await merger.add('a'*255)
        assert not any(e.type=='assistant.delta' for e in await p.read_events(run.run_id))
        await merger.add('b')
        assert len([e for e in await p.read_events(run.run_id) if e.type=='assistant.delta'])==1
        await merger.add('timer')
        await asyncio.sleep(.3)
        assert [e.payload['text'] for e in await p.read_events(run.run_id) if e.type=='assistant.delta']==['a'*255+'b','timer']
    assert (await platform.get_run(run.run_id)).revision==revision
    merger=DeltaMerger(p.events,guard)
    await merger.__aenter__();await merger.add('must not persist')
    await expire_lease(platform,guard);await claim(platform,'replacement')
    await merger.__aexit__(asyncio.CancelledError,asyncio.CancelledError(),None)
    assert not any(e.payload.get('text')=='must not persist' for e in await p.read_events(run.run_id))

@pytest.mark.asyncio
async def test_slow_client_buffer_overflow_closes_transport_without_cancelling_run(platform):
    import asyncio
    p=EventPlatform(platform);run,guard,outcome=await p.finalizable_run()
    app=create_app(platform.settings,backend=platform.backend)
    for i in range(5): await p.events.append_execution(guard,'phase.changed',{'phase':str(i)})
    async with StreamingClient(app,f'/v1/runs/{run.run_id}/events',token(platform)) as stream:
        await asyncio.wait_for(stream.send_blocked.wait(),2)
        # Commit >100 behind a blocked sender in one real guarded transaction.
        async with platform.backend.create_uow().begin_write() as uow:
            for i in range(110):
                await platform.backend.execution_events.append_execution(uow,guard,'phase.changed',{'phase':str(i)})
        await asyncio.wait_for(stream.task,2.5)
        assert (await platform.get_run(run.run_id)).status=='running'

@pytest.mark.asyncio
async def test_configured_redis_connection_failure_falls_back_within_one_second(platform,monkeypatch):
    import asyncio,time,socket
    from workbench_agent.runtime.streaming import RedisHints
    p=EventPlatform(platform);run,guard,outcome=await p.finalizable_run()
    with socket.socket() as port:
        port.bind(('127.0.0.1',0));unused=port.getsockname()[1]
    monkeypatch.setenv('WORKBENCH_REDIS_URL',f'redis://127.0.0.1:{unused}/0')
    app=create_app(platform.settings,backend=platform.backend)
    assert isinstance(app.state.events.hints,RedisHints)
    start=time.monotonic()
    await app.state.events.wait_for_hint_or_timeout(run.run_id,.1)
    assert time.monotonic()-start<.5
    last=(await p.read_events(run.run_id))[-1].seq
    async with StreamingClient(app,f'/v1/runs/{run.run_id}/events?after_seq={last}',token(platform)) as stream:
        pending=asyncio.create_task(stream.chunk())
        await p.events.append_execution(guard,'phase.changed',{'phase':'redis down'})
        assert b'redis down' in (await asyncio.wait_for(pending,1.5))['body']
    await app.state.events.hints.close()

@pytest.mark.asyncio
async def test_delivery_requires_actual_lease_and_can_follow_terminal_stream(platform):
    from uuid import uuid4
    from dataclasses import replace
    from sqlalchemy import insert
    from workbench_agent.persistence import schema as s
    from workbench_agent.domain.events import ActionGuard
    from workbench_agent.persistence.errors import StaleExecution
    p=EventPlatform(platform);run,guard,outcome=await p.finalizable_run()
    await p.results.finalize(guard,outcome,outcome.digest())
    tail=(await p.read_events(run.run_id))[-1].seq
    action_id,outbox_id=str(uuid4()),str(uuid4())
    async with platform.backend.create_uow().begin_write() as uow:
        now=await platform.backend.clock.now_ms(uow)
        await uow.execute(insert(s.actions).values(team_id=platform.auth.team_id,action_id=action_id,run_id=run.run_id,
            action_key='fixture-action',tool_version='fixture@1',payload_hash='a'*64,payload_ref='{}',scope='{}',state='pending'))
        await uow.execute(insert(s.outbox).values(team_id=platform.auth.team_id,outbox_id=outbox_id,action_id=action_id,
            destination='fixture',payload_ref='{}',status='leased',available_at=now,lease_owner='dispatcher',lease_until=now+45000,revision=2))
    delivery=ActionGuard(platform.auth.team_id,run.run_id,action_id,outbox_id,'dispatcher',2)
    with pytest.raises(StaleExecution): await p.events.append_delivery(replace(delivery,revision=1),'delivery.updated',{'state':'sent'})
    event=await p.events.append_delivery(delivery,'delivery.updated',{'state':'sent','receipt':'fixture-receipt'})
    assert event.seq==tail+1 and event.attempt==1
    assert not await p.events.is_terminal_and_drained(platform.auth,run.run_id,tail)
    replay=await p.events.read_batch(platform.auth,run.run_id,tail,100)
    assert [e.type for e in replay]==['delivery.updated']
    assert await p.events.is_terminal_and_drained(platform.auth,run.run_id,event.seq)

@pytest.mark.asyncio
async def test_waiting_events_use_canonical_envelope_and_interrupt_identifier(platform):
    from support.interrupts import InterruptPlatform
    p=InterruptPlatform(platform);run,waiting=await p.waiting_run()
    events=await EventPlatform(platform).read_events(run.run_id)
    required=next(e for e in events if e.type=='input.required')
    assert required.payload['interrupt_id']==waiting['interrupt_id']

@pytest.mark.asyncio
async def test_revocation_closes_live_stream_and_last_event_id_replays_exact_tail(platform):
    import asyncio
    from sqlalchemy import update
    from workbench_agent.persistence import schema as s
    p=EventPlatform(platform);run,guard,outcome=await p.finalizable_run()
    last=(await p.read_events(run.run_id))[-1].seq
    app=create_app(platform.settings,backend=platform.backend)
    async with StreamingClient(app,f'/v1/runs/{run.run_id}/events',token(platform),[(b'last-event-id',f'{run.run_id}:{last}'.encode())]) as stream:
        assert stream.start['status']==200
        async with platform.backend.create_uow().begin_write() as uow:
            await uow.execute(update(s.team_members).where(s.team_members.c.team_id==platform.auth.team_id).values(status='revoked'))
        await asyncio.wait_for(stream.task,1.5)
    async with StreamingClient(app,f'/v1/runs/{run.run_id}/events',token(platform)) as stream:
        assert stream.start['status']==404

@pytest.mark.asyncio
async def test_historical_waiting_event_is_read_as_canonical_input_event(platform):
    from support.interrupts import InterruptPlatform
    from sqlalchemy import update
    from workbench_agent.persistence import schema as s
    p=InterruptPlatform(platform);run,waiting=await p.waiting_run()
    async with platform.backend.create_uow().begin_write() as uow:
        await uow.execute(update(s.run_events).where(s.run_events.c.run_id==run.run_id,s.run_events.c.type=='input.required').values(type='run.waiting'))
    events=await EventPlatform(platform).read_events(run.run_id)
    assert next(e for e in events if e.type=='input.required').payload['interrupt_id']==waiting['interrupt_id']

@pytest.mark.asyncio
async def test_invalid_optional_redis_url_cannot_block_admission(platform,monkeypatch):
    monkeypatch.setenv('WORKBENCH_REDIS_URL','unavailable-invalid-url')
    app=create_app(platform.settings,backend=platform.backend)
    assert app.state.events.hints is None
    assert await platform.backend.health()

@pytest.mark.asyncio
async def test_delivery_guard_rejects_a_finished_outbox_with_leftover_lease_fields(platform):
    from uuid import uuid4
    from sqlalchemy import insert
    from workbench_agent.persistence import schema as s
    from workbench_agent.domain.events import ActionGuard
    from workbench_agent.persistence.errors import StaleExecution
    p=EventPlatform(platform);run,guard,outcome=await p.finalizable_run()
    action_id,outbox_id=str(uuid4()),str(uuid4())
    async with platform.backend.create_uow().begin_write() as uow:
        now=await platform.backend.clock.now_ms(uow)
        await uow.execute(insert(s.actions).values(team_id=guard.team_id,action_id=action_id,run_id=run.run_id,
            action_key='finished',tool_version='test@1',payload_hash='c'*64,payload_ref='{}',scope='{}',state='succeeded'))
        await uow.execute(insert(s.outbox).values(team_id=guard.team_id,outbox_id=outbox_id,action_id=action_id,
            destination='test',payload_ref='{}',status='done',available_at=now,lease_owner='old',lease_until=now+10000,revision=1))
    with pytest.raises(StaleExecution):
        await p.events.append_delivery(ActionGuard(guard.team_id,run.run_id,action_id,outbox_id,'old',1),'delivery.updated',{})

@pytest.mark.asyncio
async def test_invalid_durable_envelope_closes_live_transport_instead_of_hanging(platform):
    import asyncio
    p=EventPlatform(platform);run,guard,outcome=await p.finalizable_run()
    app=create_app(platform.settings,backend=platform.backend)
    last=(await p.read_events(run.run_id))[-1].seq
    async with StreamingClient(app,f'/v1/runs/{run.run_id}/events?after_seq={last}',token(platform)) as stream:
        async with platform.backend.create_uow().begin_write() as uow:
            from sqlalchemy import update
            from workbench_agent.persistence import schema as s
            seq=await platform.backend.execution_events.append_execution(uow,guard,'phase.changed',{})
            # Simulate corrupt persisted data, bypassing the validated producer.
            await uow.execute(update(s.run_events).where(s.run_events.c.run_id==run.run_id,s.run_events.c.seq==seq).values(type='unregistered.event'))
        await asyncio.wait_for(stream.task,1.5)

@pytest.mark.asyncio
async def test_resume_emits_canonical_event_from_real_named_interrupt(platform):
    from support.interrupts import InterruptPlatform
    p=InterruptPlatform(platform);run,waiting=await p.waiting_run()
    await p.resume(run,waiting,key='event-resume')
    events=await EventPlatform(platform).read_events(run.run_id)
    resumed=next(e for e in events if e.type=='run.resumed')
    assert resumed.payload['generation']==2 and resumed.payload['resume_request_id']

@pytest.mark.asyncio
@pytest.mark.parametrize('legacy,canonical,payload',[('run.cancel_requested','run.status_changed',{}),('run.resume_accepted','run.resumed',{'generation':2})])
async def test_retained_legacy_control_event_aliases_have_canonical_wire_types(platform,legacy,canonical,payload):
    from sqlalchemy import update
    from workbench_agent.persistence import schema as s
    p=EventPlatform(platform);run,guard,outcome=await p.finalizable_run()
    async with platform.backend.create_uow().begin_write() as uow:
        seq=await platform.backend.execution_events.append_control(uow,guard.team_id,run.run_id,canonical,payload)
        await uow.execute(update(s.run_events).where(s.run_events.c.run_id==run.run_id,s.run_events.c.seq==seq).values(type=legacy))
    event=(await p.read_events(run.run_id))[-1]
    assert event.type==canonical
    if legacy=='run.cancel_requested': assert event.payload['status']=='cancel_requested'

@pytest.mark.asyncio
@pytest.mark.parametrize('takeover',[False,True])
async def test_delta_timer_exit_settles_committed_append_before_flushing_remainder(platform,monkeypatch,takeover):
    import asyncio
    from workbench_agent.runtime.streaming import DeltaMerger
    from support.jobs import expire_lease,claim
    p=EventPlatform(platform);run,guard,outcome=await p.finalizable_run()
    committed=asyncio.Event();release_hint=asyncio.Event();leave=asyncio.Event();closing=asyncio.Event()
    async def paused_hint(run_id):
        # Replace only the optional notification wait, after the real UoW commits.
        # A gate avoids depending on the production hint's 100ms timeout.
        committed.set()
        await release_hint.wait()
    monkeypatch.setattr(p.events,'_hint',paused_hint)
    async def produce():
        async with DeltaMerger(p.events,guard) as merger:
            await merger.add('committed timer text')
            await leave.wait()
            closing.set()
    task=asyncio.create_task(produce())
    try:
        await asyncio.wait_for(committed.wait(),2)
        deltas=[e for e in await p.read_events(run.run_id) if e.type=='assistant.delta']
        assert [e.payload['text'] for e in deltas]==['committed timer text']
        if takeover:
            await expire_lease(platform,guard)
            await claim(platform,'replacement-after-commit')
        leave.set();await asyncio.wait_for(closing.wait(),1)
        release_hint.set()
        await asyncio.wait_for(task,2)
        deltas=[e for e in await p.read_events(run.run_id) if e.type=='assistant.delta']
        assert [e.payload['text'] for e in deltas]==['committed timer text']
    finally:
        release_hint.set();task.cancel()
        await asyncio.gather(task,return_exceptions=True)

@pytest.mark.asyncio
@pytest.mark.parametrize('takeover',[False,True])
async def test_delta_size_flush_cancellation_preserves_exactly_one_committed_delta(platform,monkeypatch,takeover):
    import asyncio
    from workbench_agent.runtime.streaming import DeltaMerger
    from support.jobs import expire_lease,claim
    p=EventPlatform(platform);run,guard,outcome=await p.finalizable_run()
    committed=asyncio.Event();release_hint=asyncio.Event();closing=asyncio.Event()
    async def paused_hint(run_id):
        committed.set()
        await release_hint.wait()
    monkeypatch.setattr(p.events,'_hint',paused_hint)
    async def produce():
        async with DeltaMerger(p.events,guard) as merger:
            try: await merger.add('x'*256)
            finally: closing.set()
    task=asyncio.create_task(produce())
    try:
        await asyncio.wait_for(committed.wait(),2)
        assert [e.payload['text'] for e in await p.read_events(run.run_id) if e.type=='assistant.delta']==['x'*256]
        if takeover:
            await expire_lease(platform,guard)
            await claim(platform,'replacement-after-size-commit')
        task.cancel();await asyncio.wait_for(closing.wait(),1)
        release_hint.set()
        with pytest.raises(asyncio.CancelledError): await asyncio.wait_for(task,2)
        assert [e.payload['text'] for e in await p.read_events(run.run_id) if e.type=='assistant.delta']==['x'*256]
    finally:
        release_hint.set();task.cancel()
        await asyncio.gather(task,return_exceptions=True)
