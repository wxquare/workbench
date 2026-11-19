"""Durable schedule tests with a database-controlled clock; no external calls."""
import pytest
from workbench_agent.application.runs import RunService
from workbench_agent.domain.runs import CreateRun
from support.runs import make_run_payload

@pytest.mark.asyncio
async def test_run_admission_can_join_existing_transaction_and_rolls_back(platform):
    request=CreateRun.model_validate(make_run_payload(platform.agent_id))
    with pytest.raises(RuntimeError,match='fault'):
        async with platform.backend.create_uow().begin_write() as uow:
            await RunService(platform.backend).create(platform.auth,request,'atomic-run',_uow=uow)
            raise RuntimeError('fault')
    assert await platform.count_rows('runs') == 0
    assert await platform.count_rows('threads') == 0
    accepted=await RunService(platform.backend).create(platform.auth,request,'atomic-run')
    assert accepted.run_id

def test_schedule_dst_skips_nonexistent_and_distinguishes_repeated_instants():
    from datetime import datetime,timezone
    from workbench_agent.domain.schedules import next_due
    utc=timezone.utc
    # New York spring-forward: 02:30 does not exist on March 8.
    assert next_due('30 2 * * *','America/New_York',datetime(2026,3,8,5,0,tzinfo=utc)) == datetime(2026,3,9,6,30,tzinfo=utc)
    # Two physical instants share 01:30 on fall-back day.
    first=next_due('30 1 * * *','America/New_York',datetime(2026,11,1,4,0,tzinfo=utc))
    second=next_due('30 1 * * *','America/New_York',first)
    assert first == datetime(2026,11,1,5,30,tzinfo=utc)
    assert second == datetime(2026,11,1,6,30,tzinfo=utc)

@pytest.mark.asyncio
async def test_concurrent_schedule_scans_admit_one_run(platform):
    import asyncio
    from datetime import datetime,timezone
    from workbench_agent.application.schedules import ScheduleService
    from workbench_agent.jobs.schedules import ScheduleScanner
    from workbench_agent.application.workflows import WorkflowService
    from workbench_agent.domain.workflows import WorkflowDefinition
    from workbench_agent.domain.profiles import ProfileDraft
    from workbench_agent.domain.schedules import ScheduleSpec
    current=[int(datetime(2026,1,1,0,0,tzinfo=timezone.utc).timestamp()*1000)]
    async def now(_): return current[0]
    platform.backend.clock.now_ms=now
    ctx=await platform.contexts.resolve(platform.auth,platform.agent_id)
    ws=await platform.profiles.get_agent(platform.auth,platform.agent_id)
    d=await platform.profiles.save_draft(platform.auth,platform.agent_id,ProfileDraft(default_model=ctx.default_model,graph_id='sop.conditional'),ws['revision'],key='schedule-profile-draft')
    await platform.profiles.publish(platform.auth,platform.agent_id,d['revision'],key='schedule-profile')
    wf=await WorkflowService(platform.backend).publish(platform.auth,platform.agent_id,
        WorkflowDefinition.model_validate({'name':'check','steps':[{'step_id':'logs','tool':'logs.read'}]}),key='schedule-workflow')
    service=ScheduleService(platform.backend)
    schedule=await service.create(platform.auth,platform.agent_id,ScheduleSpec(cron='*/5 * * * *',timezone='UTC',workflow_version_id=wf.workflow_version_id,input_text='inspect'),key='create-schedule')
    assert schedule['next_due_at']==current[0]+5*60000
    current[0]+=5*60000
    await asyncio.gather(ScheduleScanner(platform.backend).tick(),ScheduleScanner(platform.backend).tick())
    assert await platform.count_rows('runs')==1
    assert await platform.count_rows('threads')==1
    assert await platform.count_rows('schedule_fires')==1
    await ScheduleScanner(platform.backend).tick()
    assert await platform.count_rows('runs')==1

async def prepare_schedule(platform, *, cron='*/5 * * * *', graph_id='sop.conditional'):
    from datetime import datetime,timezone
    from workbench_agent.application.schedules import ScheduleService
    from workbench_agent.application.workflows import WorkflowService
    from workbench_agent.domain.workflows import WorkflowDefinition
    from workbench_agent.domain.profiles import ProfileDraft
    from workbench_agent.domain.schedules import ScheduleSpec
    current=[int(datetime(2026,1,1,0,0,tzinfo=timezone.utc).timestamp()*1000)]
    async def now(_): return current[0]
    platform.backend.clock.now_ms=now
    ctx=await platform.contexts.resolve(platform.auth,platform.agent_id); ws=await platform.profiles.get_agent(platform.auth,platform.agent_id)
    d=await platform.profiles.save_draft(platform.auth,platform.agent_id,ProfileDraft(default_model=ctx.default_model,graph_id=graph_id),ws['revision'],key='setup-draft')
    await platform.profiles.publish(platform.auth,platform.agent_id,d['revision'],key='setup-profile')
    wf=await WorkflowService(platform.backend).publish(platform.auth,platform.agent_id,
        WorkflowDefinition.model_validate({'name':'check','steps':[{'step_id':'logs','tool':'logs.read'}]}),key='setup-workflow')
    service=ScheduleService(platform.backend)
    item=await service.create(platform.auth,platform.agent_id,ScheduleSpec(cron=cron,timezone='UTC',workflow_version_id=wf.workflow_version_id,input_text='inspect'),key='setup-schedule')
    return service,item,current

@pytest.mark.asyncio
async def test_schedule_pause_manual_trigger_and_overlap(platform):
    from workbench_agent.jobs.schedules import ScheduleScanner
    service,item,clock=await prepare_schedule(platform)
    paused=await service.set_status(platform.auth,platform.agent_id,item['schedule_id'],'paused',0,key='pause')
    clock[0]+=5*60000
    assert await ScheduleScanner(platform.backend).tick()==[]
    manual=await service.trigger(platform.auth,platform.agent_id,item['schedule_id'],key='manual')
    assert manual == await service.trigger(platform.auth,platform.agent_id,item['schedule_id'],key='manual')
    assert await platform.count_rows('runs')==1
    resumed=await service.set_status(platform.auth,platform.agent_id,item['schedule_id'],'active',paused['revision']+1,key='resume')
    assert resumed['status']=='active'
    fired=await ScheduleScanner(platform.backend).tick()
    assert fired[0]['status']=='overlap_skipped'
    assert await platform.count_rows('runs')==1

@pytest.mark.asyncio
async def test_schedule_revoked_owner_does_not_start_run(platform):
    from workbench_agent.jobs.schedules import ScheduleScanner
    service,item,clock=await prepare_schedule(platform)
    async with platform.backend.create_uow().begin_write() as uow:
        await platform.backend.configuration.change(uow,'team_members',{'team_id':platform.auth.team_id,'subject':platform.auth.subject}, {'status':'inactive'})
    clock[0]+=5*60000
    fired=await ScheduleScanner(platform.backend).tick()
    assert fired[0]['status']=='denied'
    assert await platform.count_rows('runs')==0

@pytest.mark.asyncio
async def test_schedule_old_fires_record_missed_and_coalesce_latest(platform):
    from workbench_agent.jobs.schedules import ScheduleScanner
    service,item,clock=await prepare_schedule(platform,cron='0 * * * *')
    clock[0]+=10*3600000
    fired=await ScheduleScanner(platform.backend).tick()
    assert fired[0]['status']=='started'
    assert fired[0]['due_at']==clock[0]
    assert await platform.count_rows('runs')==1
    assert await platform.count_rows('schedule_fires')==2

@pytest.mark.asyncio
async def test_schedule_rolls_back_all_when_admission_fails_after_writes(platform, monkeypatch):
    from workbench_agent.jobs.schedules import ScheduleScanner
    from workbench_agent.application.errors import DomainError
    service,item,clock=await prepare_schedule(platform)
    clock[0]+=5*60000
    original=RunService.create
    async def fail_after(self,*args,**kwargs):
        await original(self,*args,**kwargs)
        raise DomainError('INJECTED_FAILURE',409)
    monkeypatch.setattr(RunService,'create',fail_after)
    result=await ScheduleScanner(platform.backend).tick()
    assert result[0]['status']=='unavailable'
    assert await platform.count_rows('runs')==0
    assert await platform.count_rows('threads')==0
    assert await platform.count_rows('schedule_fires')==0
    async with platform.backend.create_uow().begin_read() as uow:
        row=await platform.backend.schedules.one(uow,platform.auth.team_id,item['schedule_id'])
    assert row['next_due_at']==clock[0]

@pytest.mark.asyncio
async def test_schedule_http_create_list_pause_and_manual_trigger(platform,client):
    from workbench_agent.domain.workflows import WorkflowDefinition
    from workbench_agent.application.workflows import WorkflowService
    from workbench_agent.domain.profiles import ProfileDraft
    ctx=await platform.contexts.resolve(platform.auth,platform.agent_id); ws=await platform.profiles.get_agent(platform.auth,platform.agent_id)
    d=await platform.profiles.save_draft(platform.auth,platform.agent_id,ProfileDraft(default_model=ctx.default_model,graph_id='sop.conditional'),ws['revision'],key='api-draft')
    await platform.profiles.publish(platform.auth,platform.agent_id,d['revision'],key='api-profile')
    wf=await WorkflowService(platform.backend).publish(platform.auth,platform.agent_id,WorkflowDefinition.model_validate({'name':'check','steps':[{'step_id':'logs','tool':'logs.read'}]}),key='api-wf')
    path=f'/v1/agents/{platform.agent_id}/schedules'
    request={'cron':'0 * * * *','timezone':'UTC','workflow_version_id':wf.workflow_version_id,'input_text':'inspect'}
    created=await client.post(path,json=request,headers={'Idempotency-Key':'create-http'})
    assert created.status_code==201
    schedule_id=created.json()['schedule_id']
    replay=await client.post(path,json=request,headers={'Idempotency-Key':'create-http'})
    assert replay.json()==created.json() and replay.headers['Idempotency-Replayed']=='true'
    listed=await client.get(path)
    assert listed.status_code==200 and [v['schedule_id'] for v in listed.json()['items']]==[schedule_id]
    paused=await client.patch(path+'/'+schedule_id,json={'status':'paused'},headers={'Idempotency-Key':'pause-http','If-Match':'0'})
    assert paused.status_code==200 and paused.json()['status']=='paused'
    trigger=await client.post(path+'/'+schedule_id+'/trigger',headers={'Idempotency-Key':'trigger-http'})
    assert trigger.status_code==202 and trigger.json()['run_id']

@pytest.mark.asyncio
async def test_scheduler_tick_scans_persisted_schedule(platform):
    from workbench_agent.jobs.scheduler import Scheduler
    from workbench_agent.runtime.graphs.registry import foundation_worker_registry
    _,_,clock=await prepare_schedule(platform)
    clock[0]+=5*60000
    await Scheduler(platform.backend,foundation_worker_registry(platform.backend)).tick()
    assert await platform.count_rows('runs')==1
    assert await platform.count_rows('schedule_fires')==1

@pytest.mark.asyncio
async def test_scheduled_run_scope_uses_due_time_and_is_atomic(platform):
    from workbench_agent.application.schedules import ScheduleService
    from workbench_agent.domain.schedules import ScheduleSpec,ScheduleScope
    from workbench_agent.jobs.schedules import ScheduleScanner
    service,item,clock=await prepare_schedule(platform)
    async with platform.backend.create_uow().begin_read() as uow:
        row=await platform.backend.schedules.one(uow,platform.auth.team_id,item['schedule_id'])
    import json
    spec=ScheduleSpec.model_validate(json.loads(row['payload'])['spec']).model_copy(update={'query_scope':ScheduleScope(region='id',service='checkout',environment='test',window_seconds=600)})
    second=await service.create(platform.auth,platform.agent_id,spec,key='scope-schedule')
    # Disable the first schedule so only the configured one fires.
    await service.set_status(platform.auth,platform.agent_id,item['schedule_id'],'paused',0,key='scope-pause')
    clock[0]+=5*60000
    fired=await ScheduleScanner(platform.backend).tick()
    assert len(fired)==1 and fired[0]['schedule_id']==second['schedule_id']
    async with platform.backend.create_uow().begin_read() as uow:
        scope=await platform.backend.runs.current_scope(uow,platform.auth.team_id,fired[0]['run_id'])
    assert json.loads(scope['scope_json']) == {'region':'id','service':'checkout','environment':'test',
        'start_at':'2025-12-31T23:55:00+00:00','end_at':'2026-01-01T00:05:00+00:00'}

@pytest.mark.asyncio
async def test_unavailable_schedule_does_not_starve_other_due_schedules(platform,monkeypatch):
    from workbench_agent.jobs.schedules import ScheduleScanner
    from workbench_agent.application.errors import DomainError
    from workbench_agent.domain.schedules import ScheduleSpec
    service,item,clock=await prepare_schedule(platform)
    async with platform.backend.create_uow().begin_read() as uow:
        row=await platform.backend.schedules.one(uow,platform.auth.team_id,item['schedule_id'])
    import json
    spec=ScheduleSpec.model_validate(json.loads(row['payload'])['spec']).model_copy(update={'input_text':'broken'})
    broken=await service.create(platform.auth,platform.agent_id,spec,key='broken-schedule')
    original=RunService.create
    async def fail_one(self,auth,request,*args,**kwargs):
        if request.input.text=='broken': raise DomainError('WORKFLOW_UNAVAILABLE',409)
        return await original(self,auth,request,*args,**kwargs)
    monkeypatch.setattr(RunService,'create',fail_one)
    clock[0]+=5*60000
    fired=await ScheduleScanner(platform.backend).tick()
    assert {x['status'] for x in fired}=={'started','unavailable'}
    assert next(x for x in fired if x['schedule_id']==broken['schedule_id'])['status']=='unavailable'
    assert await platform.count_rows('runs')==1

@pytest.mark.asyncio
async def test_repeated_unavailable_schedules_do_not_starve_beyond_page(platform,monkeypatch):
    from workbench_agent.jobs.schedules import ScheduleScanner
    from workbench_agent.application.errors import DomainError
    from workbench_agent.domain.schedules import ScheduleSpec
    import json
    service,item,clock=await prepare_schedule(platform)
    async with platform.backend.create_uow().begin_read() as uow:
        row=await platform.backend.schedules.one(uow,platform.auth.team_id,item['schedule_id'])
    spec=ScheduleSpec.model_validate(json.loads(row['payload'])['spec'])
    for i in range(100): await service.create(platform.auth,platform.agent_id,spec,key=f'bulk-{i}')
    visited=set()
    async def fail_all(self,auth,request,key,**kwargs):
        visited.add(key)
        raise DomainError('WORKFLOW_UNAVAILABLE',409)
    monkeypatch.setattr(RunService,'create',fail_all)
    clock[0]+=5*60000
    scanner=ScheduleScanner(platform.backend)
    await scanner.tick(); await scanner.tick()
    assert len(visited)==101

def test_schedule_rejects_bad_cron_and_timezone():
    from datetime import datetime,timezone
    from workbench_agent.domain.schedules import next_due
    now=datetime(2026,1,1,tzinfo=timezone.utc)
    for cron,zone in [('not-a-cron','UTC'),('* * * * *','No/Such_Zone'),('* * * * * *','UTC')]:
        with pytest.raises(ValueError): next_due(cron,zone,now)

@pytest.mark.asyncio
async def test_schedule_http_revision_conflict_and_other_team_hidden(platform,client):
    service,item,_=await prepare_schedule(platform)
    path=f'/v1/agents/{platform.agent_id}/schedules/{item["schedule_id"]}'
    stale=await client.patch(path,json={'status':'paused'},headers={'Idempotency-Key':'stale','If-Match':'42'})
    assert stale.status_code==409
    from workbench_agent.application.errors import DomainError
    with pytest.raises(DomainError):
        await service.set_status(platform.other_auth,platform.agent_id,item['schedule_id'],'paused',0,key='other-team')
    async with platform.backend.create_uow().begin_read() as uow:
        schedule=await platform.backend.schedules.one(uow,platform.auth.team_id,item['schedule_id'])
    assert schedule['status']=='active' and schedule['revision']==0

@pytest.mark.asyncio
async def test_schedule_manual_trigger_failure_rolls_back_run_and_receipt(platform,monkeypatch):
    from workbench_agent.application.errors import DomainError
    service,item,_=await prepare_schedule(platform)
    original=RunService.create
    async def fail_after(self,*args,**kwargs):
        await original(self,*args,**kwargs)
        raise DomainError('INJECTED_FAILURE',409)
    monkeypatch.setattr(RunService,'create',fail_after)
    with pytest.raises(DomainError):
        await service.trigger(platform.auth,platform.agent_id,item['schedule_id'],key='manual-fault')
    assert await platform.count_rows('runs')==0 and await platform.count_rows('threads')==0
    assert await platform.count_rows('schedule_manual_fires')==0
    monkeypatch.setattr(RunService,'create',original)
    result=await service.trigger(platform.auth,platform.agent_id,item['schedule_id'],key='manual-fault')
    assert result['run_id'] and await platform.count_rows('runs')==1

@pytest.mark.asyncio
async def test_manual_fires_are_durable_distinct_and_idempotent(platform):
    from workbench_agent.application.errors import DomainError
    service,item,_=await prepare_schedule(platform)
    first=await service.trigger(platform.auth,platform.agent_id,item['schedule_id'],key='manual-one')
    assert await service.trigger(platform.auth,platform.agent_id,item['schedule_id'],key='manual-one') == first
    with pytest.raises(DomainError):
        await service.trigger(platform.auth,platform.agent_id,item['schedule_id'],key='manual-overlap')
    async with platform.backend.create_uow().begin_write() as uow:
        await platform.backend.runs.change(uow,'runs',{'team_id':platform.auth.team_id,'run_id':first['run_id']},{'status':'partial'})
    second=await service.trigger(platform.auth,platform.agent_id,item['schedule_id'],key='manual-two')
    assert second['run_id'] != first['run_id']
    history=await service.fires(platform.auth,platform.agent_id,item['schedule_id'])
    assert {item['run_id'] for item in history['items']} == {first['run_id'],second['run_id']}
    assert all(item['kind']=='manual' for item in history['items'])
    assert await platform.count_rows('schedule_manual_fires')==2

@pytest.mark.asyncio
async def test_six_hour_cutoff_is_strict_to_millisecond(platform):
    from workbench_agent.jobs.schedules import ScheduleScanner
    service,item,clock=await prepare_schedule(platform,cron='0 0 * * *')
    # First due is Jan 2 midnight; 6 hours and 30 seconds late is outside window.
    clock[0]+=24*3600000+6*3600000+30000
    fired=await ScheduleScanner(platform.backend).tick()
    assert fired[0]['status']=='missed'
    assert await platform.count_rows('runs')==0
    assert await platform.count_rows('schedule_fires')==1

@pytest.mark.asyncio
async def test_six_hour_cutoff_is_inclusive_at_exact_boundary(platform):
    from workbench_agent.jobs.schedules import ScheduleScanner
    _,_,clock=await prepare_schedule(platform,cron='0 0 * * *')
    clock[0]+=24*3600000+6*3600000
    fired=await ScheduleScanner(platform.backend).tick()
    assert fired[0]['status']=='started'
    assert await platform.count_rows('runs')==1

@pytest.mark.asyncio
async def test_manual_fire_write_failure_rolls_back_run_and_history(platform,monkeypatch):
    from workbench_agent.application.errors import DomainError
    service,item,_=await prepare_schedule(platform)
    original=platform.backend.schedules.manual_fire
    async def fail_after(uow,values):
        await original(uow,values)
        raise DomainError('INJECTED_FAILURE',409)
    monkeypatch.setattr(platform.backend.schedules,'manual_fire',fail_after)
    with pytest.raises(DomainError):
        await service.trigger(platform.auth,platform.agent_id,item['schedule_id'],key='manual-history-fault')
    assert await platform.count_rows('runs')==0
    assert await platform.count_rows('threads')==0
    assert await platform.count_rows('schedule_manual_fires')==0
    monkeypatch.setattr(platform.backend.schedules,'manual_fire',original)
    result=await service.trigger(platform.auth,platform.agent_id,item['schedule_id'],key='manual-history-fault')
    assert result['run_id'] and await platform.count_rows('schedule_manual_fires')==1

@pytest.mark.asyncio
async def test_legacy_manual_payload_still_blocks_overlap(platform):
    from sqlalchemy import delete
    from workbench_agent.persistence import schema
    from workbench_agent.application.errors import DomainError
    service,item,_=await prepare_schedule(platform)
    await service.trigger(platform.auth,platform.agent_id,item['schedule_id'],key='old-manual')
    # Model a pre-migration schedule where only last_manual_run_id existed.
    async with platform.backend.create_uow().begin_write() as uow:
        await uow.execute(delete(schema.schedule_manual_fires).where(schema.schedule_manual_fires.c.schedule_id==item['schedule_id']))
    with pytest.raises(DomainError,match='SCHEDULE_OVERLAP'):
        await service.trigger(platform.auth,platform.agent_id,item['schedule_id'],key='new-manual')

@pytest.mark.asyncio
async def test_fire_history_pages_across_kinds_at_same_millisecond(platform,client):
    from uuid import uuid4
    service,item,clock=await prepare_schedule(platform)
    for i in range(60):
        run=await service.trigger(platform.auth,platform.agent_id,item['schedule_id'],key=f'manual-page-{i}')
        async with platform.backend.create_uow().begin_write() as uow:
            await platform.backend.runs.change(uow,'runs',{'team_id':platform.auth.team_id,'run_id':run['run_id']},{'status':'partial'})
    from sqlalchemy import select
    from workbench_agent.persistence import schema
    async with platform.backend.create_uow().begin_write() as uow:
        same_id=(await uow.execute(select(schema.schedule_manual_fires.c.fire_id).limit(1))).scalar_one()
        for i in range(60):
            await platform.backend.schedules.fire(uow,{'team_id':platform.auth.team_id,'fire_id':same_id if i==0 else str(uuid4()),
                'schedule_id':item['schedule_id'],'due_at':clock[0]+i,'run_id':None,'status':'missed','created_at':clock[0],'updated_at':clock[0]})
    path=f'/v1/agents/{platform.agent_id}/schedules/{item["schedule_id"]}/fires'
    cursor=None; seen=[]
    for _ in range(121):
        response=await client.get(path,params={'limit':1,**({'after':cursor} if cursor else {})})
        assert response.status_code==200
        body=response.json(); seen.extend((value['kind'],value['fire_id']) for value in body['items'])
        cursor=body['next_cursor']
        if cursor is None: break
    assert len(seen)==120 and len(set(seen))==120
    assert {kind for kind,_ in seen}=={'manual','scheduled'}

@pytest.mark.asyncio
async def test_fire_history_cursor_rejects_other_schedule_and_bad_values(platform):
    from workbench_agent.application.errors import DomainError
    from workbench_agent.domain.schedules import ScheduleSpec
    import json
    service,first,_=await prepare_schedule(platform)
    async with platform.backend.create_uow().begin_read() as uow:
        row=await platform.backend.schedules.one(uow,platform.auth.team_id,first['schedule_id'])
    second=await service.create(platform.auth,platform.agent_id,ScheduleSpec.model_validate(json.loads(row['payload'])['spec']),key='history-other')
    for i in range(2):
        run=await service.trigger(platform.auth,platform.agent_id,first['schedule_id'],key=f'history-{i}')
        async with platform.backend.create_uow().begin_write() as uow:
            await platform.backend.runs.change(uow,'runs',{'team_id':platform.auth.team_id,'run_id':run['run_id']},{'status':'partial'})
    page=await service.fires(platform.auth,platform.agent_id,first['schedule_id'],limit=1)
    assert page['next_cursor']
    with pytest.raises(DomainError):
        await service.fires(platform.auth,platform.agent_id,second['schedule_id'],limit=1,after=page['next_cursor'])
    with pytest.raises(DomainError):
        await service.fires(platform.auth,platform.agent_id,first['schedule_id'],limit=1,after='invalid')
    with pytest.raises(DomainError):
        await service.fires(platform.other_auth,platform.agent_id,first['schedule_id'],limit=1,after=page['next_cursor'])
    import base64
    malformed={'v':1,'team':platform.auth.team_id,'schedule':first['schedule_id'],'created_at':0,'fire_id':123,'kind':'manual'}
    token=base64.urlsafe_b64encode(json.dumps(malformed).encode()).decode().rstrip('=')
    with pytest.raises(DomainError):
        await service.fires(platform.auth,platform.agent_id,first['schedule_id'],limit=1,after=token)

@pytest.mark.asyncio
async def test_schedule_accepts_v12_and_pins_workflow(platform):
    from workbench_agent.jobs.schedules import ScheduleScanner
    _,item,clock=await prepare_schedule(platform,graph_id='sop.conditional')
    clock[0]+=5*60000
    fires=await ScheduleScanner(platform.backend).tick()
    assert fires[0]['status']=='started'
    run=await platform.get_run(fires[0]['run_id'])
    assert run.context.graph_id=='sop.conditional' and run.context.workflow is not None

@pytest.mark.asyncio
async def test_conditional_schedule_accepts_advanced_workflow_at_creation(platform):
    from workbench_agent.application.schedules import ScheduleService
    from workbench_agent.application.workflows import WorkflowService
    from workbench_agent.application.errors import DomainError
    from workbench_agent.domain.workflows import WorkflowDefinition
    from workbench_agent.domain.schedules import ScheduleSpec
    await prepare_schedule(platform,graph_id='sop.conditional')
    wf=await WorkflowService(platform.backend).publish(platform.auth,platform.agent_id,
        WorkflowDefinition.model_validate({'name':'advanced','steps':[{'step_id':'a','tool':'read','output_schema':{'type':'object'}}]}),key='advanced-wf')
    item = await ScheduleService(platform.backend).create(platform.auth,platform.agent_id,
        ScheduleSpec(cron='0 * * * *',timezone='UTC',workflow_version_id=wf.workflow_version_id,input_text='check'),key='advanced-schedule')
    assert item['schedule_id']
