import asyncio
import pytest
from support.interrupts import InterruptPlatform
from workbench_agent.application.errors import DomainError

@pytest.mark.asyncio
async def test_only_one_resume_is_accepted(platform):
    p=InterruptPlatform(platform)
    run,waiting=await p.waiting_run()
    assert waiting['kind']=='user_input'
    assert (await platform.get_run(run.run_id)).status=='waiting_input'
    results=await asyncio.gather(p.resume(run,waiting,key='a'),p.resume(run,waiting,key='b'),return_exceptions=True)
    assert sum(p.is_accepted(r) for r in results)==1
    assert await p.resume_request_count(waiting['interrupt_id'])==1

@pytest.mark.asyncio
async def test_resume_replay_and_revision_actor_schema(platform):
    p=InterruptPlatform(platform); run,w=await p.waiting_run()
    for changes,code in [({'expected_revision':0},'REVISION_CONFLICT'),({'interrupt_id':'00000000-0000-0000-0000-000000000000'},'INTERRUPT_CONFLICT'),({'values':{'region':5}},'INVALID_RESPONSE')]:
        with pytest.raises(DomainError) as error: await p.resume(run,w,key=code,**changes)
        assert error.value.code==code
    first=await p.resume(run,w,key='same')
    assert (await p.resume(run,w,key='same')).model_dump()==first.model_dump()
    assert (await p.resume(run,w,key='same')).replayed

@pytest.mark.asyncio
async def test_approval_hash_and_rejection_are_input(platform):
    p=InterruptPlatform(platform); run,w=await p.waiting_run(approval=True)
    with pytest.raises(DomainError) as error: await p.resume(run,w,key='bad',expected_args_hash='changed')
    assert error.value.code=='APPROVAL_INVALIDATED'
    await p.resume(run,w,key='reject',decision='reject')
    assert await p.worker.run_once()=='projected'
    assert (await platform.get_run(run.run_id)).status=='succeeded'
    assert await p.saved_value(run,'approval_decision')=='reject'

@pytest.mark.asyncio
async def test_current_actor_required_even_before_idempotent_replay(platform):
    from sqlalchemy import update
    from workbench_agent.persistence import schema as s
    from workbench_agent.domain.identity import AuthContext
    from workbench_agent.domain.runs import ResumeRun
    from workbench_agent.application.runs import RunService
    p=InterruptPlatform(platform);run,w=await p.waiting_run()
    async with platform.backend.create_uow().begin_write() as uow:
        await uow.execute(update(s.team_members).where(s.team_members.c.team_id==platform.auth.team_id).values(subject='replacement'))
    replacement=AuthContext(team_id=platform.auth.team_id,subject='replacement',roles=('admin',))
    body=ResumeRun.model_validate({'interrupt_id':w['interrupt_id'],'expected_revision':w['expected_revision'],
        'response':{'kind':'user_input','values':{'region':'ID'}}})
    with pytest.raises(DomainError) as error: await RunService(platform.backend).resume(replacement,run.run_id,body,'x')
    assert error.value.code=='INTERRUPT_ACTOR_MISMATCH'
    with pytest.raises(DomainError): await p.resume(run,w,key='x')

@pytest.mark.asyncio
async def test_public_resume_byte_response_and_replay_authorization(platform,client):
    from sqlalchemy import update
    from workbench_agent.persistence import schema as s
    p=InterruptPlatform(platform);run,w=await p.waiting_run()
    body={'interrupt_id':w['interrupt_id'],'expected_revision':w['expected_revision'],'response':{'kind':'user_input','values':{'region':'ID'}}}
    path='/v1/runs/'+run.run_id+'/resume'
    first=await client.post(path,json=body,headers={'Idempotency-Key':'api'})
    second=await client.post(path,json=body,headers={'Idempotency-Key':'api'})
    assert first.status_code==second.status_code==202 and first.content==second.content
    assert second.headers['Idempotency-Replayed']=='true'
    body['response']['values']['region']='PH'
    assert (await client.post(path,json=body,headers={'Idempotency-Key':'api'})).status_code==409
    async with platform.backend.create_uow().begin_write() as uow:
        await uow.execute(update(s.team_members).where(s.team_members.c.team_id==platform.auth.team_id).values(status='revoked'))
    assert (await client.post(path,json=body,headers={'Idempotency-Key':'api'})).status_code==404
