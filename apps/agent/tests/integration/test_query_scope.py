import pytest
from support.interrupts import InterruptPlatform

@pytest.mark.asyncio
async def test_scope_revision_is_trusted_and_old_evidence_is_not_current(platform):
    p=InterruptPlatform(platform); run,w=await p.waiting_run()
    await p.resume(run,w,key='scope',values={'region':'ID'})
    first=await p.current_scope(run)
    assert first['version']==1 and first['scope_json']=='{"region":"ID"}'
    assert first['created_by']==platform.auth.subject
    newer=await p.change_scope(run,{'region':'PH'})
    assert newer['version']==2 and newer['scope_id']!=first['scope_id']

@pytest.mark.asyncio
async def test_scope_storage_filters_old_rows_and_disclosure_rejects_unproven_rows(platform):
    from uuid import uuid4
    from sqlalchemy import insert
    from workbench_agent.persistence import schema as s
    p=InterruptPlatform(platform);run,w=await p.waiting_run()
    await p.resume(run,w,key='scope',values={'region':'ID'})
    first=await p.current_scope(run)
    async with platform.backend.create_uow().begin_write() as uow:
        await uow.execute(insert(s.evidence).values(team_id=platform.auth.team_id,evidence_id=str(uuid4()),run_id=run.run_id,
            scope_id=first['scope_id'],source='logs',quality='normal',time_window='{}',summary='old scope'))
    second=await p.change_scope(run,{'region':'PH'})
    assert second['scope_id']!=first['scope_id']
    async with platform.backend.create_uow().begin_read() as uow:
        assert await platform.contexts.current_evidence(uow,platform.auth,run.run_id)==[]
    async with platform.backend.create_uow().begin_write() as uow:
        await uow.execute(insert(s.evidence).values(team_id=platform.auth.team_id,evidence_id=str(uuid4()),run_id=run.run_id,
            scope_id=second['scope_id'],source='logs',quality='normal',time_window='{}',summary='current scope'))
    async with platform.backend.create_uow().begin_read() as uow:
        rows = await platform.backend.runs.current_evidence(uow, platform.auth.team_id, run.run_id)
        assert [item['summary'] for item in rows] == ['current scope']
        # A matching scope is necessary but not sufficient: no trusted invocation
        # receipt exists for this manually inserted row, so disclosure fails closed.
        assert await platform.contexts.current_evidence(uow,platform.auth,run.run_id) == []
