import pytest
from workbench_agent.cli.worker import run_worker
from support.runs import make_run_payload
from support.jobs import row
from workbench_agent.persistence import schema as s

async def publish_default(platform,client,version=None):
    model=await platform.profiles.get_active_model(platform.auth)
    agent=(await client.get('/v1/agents/'+platform.agent_id)).json()
    body={'default_model':model.ref.model_dump()}
    if version: body['graph_version']=version
    changed=await client.patch('/v1/agents/'+platform.agent_id,json=body,
        headers={'If-Match':str(agent['revision']),'Idempotency-Key':'draft-'+str(version)})
    assert changed.status_code==200,changed.text
    published=await client.post('/v1/agents/'+platform.agent_id+'/profiles/publish',
        headers={'If-Match':str(changed.json()['revision']),'Idempotency-Key':'publish-'+str(version)})
    assert published.status_code==201,published.text
    return published.json()

@pytest.mark.asyncio
@pytest.mark.parametrize('kind',['region','approve','reject'])
async def test_public_create_normal_worker_wait_resume_complete(platform,client,kind):
    published=await publish_default(platform,client)
    context={'region_candidates':['ID','PH']} if kind=='region' else {'region':'ID','confirm_recording':True}
    payload=make_run_payload(platform.agent_id);payload['business_context']=context
    created=await client.post('/v1/runs',json=payload,headers={'Idempotency-Key':'public-run'})
    assert created.status_code==202,created.text
    run_id=created.json()['run_id'];url='/v1/runs/'+run_id
    assert (await run_worker(platform.settings,once=True))['status']=='projected'
    waiting=(await client.get(url)).json()
    assert waiting['status']==('waiting_input' if kind=='region' else 'waiting_approval')
    w=waiting['waiting']
    assert w['name']==('foundation.input' if kind=='region' else 'foundation.approval')
    answer={'kind':'user_input','values':{'region':'ID'}} if kind=='region' else {
        'kind':'approval','approval_id':w['approval_id'],'decision':kind,'expected_args_hash':w['args_hash']}
    if kind!='region':
        import hashlib,json
        bound={'contract':'foundation-recording-v1','input':payload['input'],'business_context':context}
        expected=hashlib.sha256(json.dumps(bound,sort_keys=True,separators=(',',':'),ensure_ascii=False).encode()).hexdigest()
        assert w['args_hash']==expected
        invalid=await client.post(url+'/resume',json={'interrupt_id':w['interrupt_id'],
            'expected_revision':waiting['revision'],'response':dict(answer,expected_args_hash='0'*64)},
            headers={'Idempotency-Key':'bad-hash'})
        assert invalid.status_code==409
    resumed=await client.post(url+'/resume',json={'interrupt_id':w['interrupt_id'],
        'expected_revision':waiting['revision'],'response':answer},headers={'Idempotency-Key':'public-answer'})
    assert resumed.status_code==202,resumed.text
    assert (await run_worker(platform.settings,once=True))['status']=='projected'
    final=(await client.get(url)).json()
    assert final['status']=='succeeded' and final['outcome']['kind']=='foundation.recorded.v1'
    assert final['outcome']['proposed_actions']==[]
    messages=(await client.get('/v1/threads/'+created.json()['thread_id']+'/messages')).json()['items']
    assert [message['outcome'] for message in messages if message['role']=='assistant']==[final['outcome']]
    if kind!='region':
        assert ('approved' if kind=='approve' else 'rejected') in final['outcome']['summary']
        assert 'No external action' in final['outcome']['summary']
        if kind=='reject': assert 'skipped' in final['outcome']['summary']
    assert (await run_worker(platform.settings,once=True))['status']=='idle'
    request=await row(platform,s.resume_requests,run_id=run_id)
    assert request['status']=='applied'
    assert (await row(platform,s.interrupts,run_id=run_id))['status']=='consumed'
    async with platform.backend.create_uow().begin_read() as uow:
        saved=await platform.backend.runs.checkpoint(uow,platform.auth.team_id,run_id)
    assert saved['values']['last_applied_resume_id']==request['request_id']
    if kind!='region': assert saved['values']['approval_decision']==kind
    assert await platform.count_rows('actions')==0 and await platform.count_rows('outbox')==0
    assert published['graph_version']=='dod.v3'

@pytest.mark.asyncio
@pytest.mark.parametrize('context',[
    {'region_candidates':['ID','PH'],'confirm_recording':True},
    {'region_candidates':'ID,PH'}, {'region_candidates':['ID',1]},
    {'region':17},{'confirm_recording':'yes'}, {'region_candidates':['']}])
async def test_invalid_or_ambiguous_recording_context_rejected_before_admission(platform,client,context):
    await publish_default(platform,client)
    payload=make_run_payload(platform.agent_id);payload['business_context']=context
    response=await client.post('/v1/runs',json=payload,headers={'Idempotency-Key':'bad'})
    assert response.status_code==422 and response.json()['code']=='INVALID_FOUNDATION_CONTEXT'
    assert await platform.count_rows('runs')==0

@pytest.mark.asyncio
@pytest.mark.parametrize('version',['dod.v1','dod.v2'])
async def test_historical_published_graph_is_not_remapped_or_reseeded(platform,client,version):
    published=await publish_default(platform,client,version)
    payload=make_run_payload(platform.agent_id);payload['business_context']={'region_candidates':['ID','PH']}
    created=await client.post('/v1/runs',json=payload,headers={'Idempotency-Key':'historical'})
    assert created.status_code==202
    await platform.bootstrap()
    assert (await platform.contexts.resolve(platform.auth,platform.agent_id)).agent_profile.resource_id==published['profile_id']
    await publish_default(platform,client)
    assert (await run_worker(platform.settings,once=True))['status']=='projected'
    assert (await client.get('/v1/runs/'+created.json()['run_id'])).json()['status']=='succeeded'

@pytest.mark.asyncio
@pytest.mark.parametrize('context',[{}, {'region_candidates':['ID','ID']}, {'region_candidates':['ID','PH'],'region':'PH'}])
async def test_unambiguous_context_completes_without_wait(platform,client,context):
    await publish_default(platform,client)
    payload=make_run_payload(platform.agent_id);payload['business_context']=context
    created=await client.post('/v1/runs',json=payload,headers={'Idempotency-Key':'unambiguous'})
    assert created.status_code==202
    assert (await run_worker(platform.settings,once=True))['status']=='projected'
    assert (await client.get('/v1/runs/'+created.json()['run_id'])).json()['status']=='succeeded'

@pytest.mark.asyncio
async def test_v3_unprojected_interrupt_and_applied_marker_recover_with_pinned_context(platform,client):
    from support.jobs import claim,expire_lease
    from workbench_agent.runtime.graphs.registry import foundation_worker_registry
    from workbench_agent.jobs.worker import Execution
    await publish_default(platform,client)
    payload=make_run_payload(platform.agent_id)
    payload['business_context']={'region_candidates':['ID','PH'],'region':'SG'}
    created=await client.post('/v1/runs',json=payload,headers={'Idempotency-Key':'recovery'})
    assert created.status_code==202
    run_id=created.json()['run_id'];url='/v1/runs/'+run_id
    registry=foundation_worker_registry(platform.backend);owned=await claim(platform)
    await registry._executors['dod.v3'].execute(Execution(platform.backend,owned))
    await expire_lease(platform,owned.guard)
    assert (await registry.reconciler.recover(run_id)).action=='waiting'
    waiting=(await client.get(url)).json();w=waiting['waiting']
    assert waiting['status']=='waiting_input'
    resumed=await client.post(url+'/resume',json={'interrupt_id':w['interrupt_id'],'expected_revision':waiting['revision'],
        'response':{'kind':'user_input','values':{'region':'ID'}}},headers={'Idempotency-Key':'recovery-answer'})
    assert resumed.status_code==202
    owned=await claim(platform)
    await registry._executors['dod.v3'].apply_resume(Execution(platform.backend,owned),mark=False)
    await expire_lease(platform,owned.guard)
    await publish_default(platform,client,'dod.v2')
    assert (await registry.reconciler.recover(run_id)).action=='continue'
    assert (await row(platform,s.resume_requests,run_id=run_id))['status']=='applied'
    assert (await run_worker(platform.settings,once=True))['status']=='projected'
    assert (await client.get(url)).json()['status']=='succeeded'
    assert (await row(platform,s.interrupts,run_id=run_id))['status']=='consumed'


@pytest.mark.asyncio
async def test_fresh_bootstrap_enables_public_wait_without_republishing_existing_agent(platform,client):
    from uuid import uuid4
    settings=platform.settings.model_copy(deep=True)
    settings.bootstrap.agent_id=str(uuid4());settings.bootstrap.agent_name='new-default'
    from workbench_agent.cli.bootstrap import BootstrapService
    assert (await BootstrapService(platform.backend).apply(settings)).created_agent
    assert (await platform.contexts.resolve(platform.auth,platform.agent_id)).graph_version=='dod.v1'
    payload=make_run_payload(settings.bootstrap.agent_id)
    payload['business_context']={'region_candidates':['ID','PH']}
    created=await client.post('/v1/runs',json=payload,headers={'Idempotency-Key':'new-agent'})
    assert created.status_code==202
    assert (await run_worker(settings,once=True))['status']=='projected'
    assert (await client.get('/v1/runs/'+created.json()['run_id'])).json()['status']=='waiting_input'
