import pytest
from workbench_agent.application.errors import DomainError


def rule(platform,rule_id='jira'):
    source=dict(team_id=platform.auth.team_id,agent_id=platform.agent_id,connector_id='alert',identity_id='source-id',subject=platform.auth.subject,
        service='checkout',environment='test',region='id',rule='error-rate')
    scope={'service':'checkout','environment':'test','region':'id','start_at':'start','end_at':'end'}
    return {'rule_id':rule_id,'source':source,'scope':scope,'binding':{'resource_id':'8ad892ba-bda0-4ebc-a603-c208af125985','version':'1','content_hash':'a'*64},
        'target':{'server':'local.mock.jira','credential_scope':'local-mock','tool_name':'CreateTicket'},'arguments':scope,'approver':'operator'}


@pytest.mark.asyncio
async def test_batch_rules_are_bounded_unique_versioned_and_exact(platform):
    from workbench_agent.application.synthetic_batch_policy import SyntheticBatchPolicy,matching_rules
    service=SyntheticBatchPolicy(platform.backend)
    one=rule(platform);two={**rule(platform,'notification'),'target':{'server':'local.mock.seatalk','credential_scope':'local-mock','tool_name':'SendNotification'}}
    assert await service.sync([two,one],expected_revision=0)==1
    async with platform.backend.create_uow().begin_read() as uow:
        assert [r['rule_id'] for r in await matching_rules(uow,one['source'],one['scope'])]==['jira','notification']
        assert await matching_rules(uow,{**one['source'],'region':'ph'},one['scope'])==[]
    with pytest.raises(DomainError): await service.sync([one,{**two,'rule_id':'jira'}],expected_revision=1)
    with pytest.raises(DomainError): await service.sync([rule(platform,str(i)) for i in range(5)],expected_revision=1)
    with pytest.raises(DomainError): await service.sync([],expected_revision=0)

@pytest.mark.asyncio
async def test_batch_policy_maintenance_is_explicit_and_stale_safe(platform):
    from workbench_agent.cli.synthetic_batch_policy import batch_policy_status,sync_batch_policy
    assert (await batch_policy_status(platform.settings))['revision']==0
    assert (await sync_batch_policy(platform.settings,[rule(platform)],0))['revision']==1
    with pytest.raises(DomainError): await sync_batch_policy(platform.settings,[],0)

@pytest.mark.asyncio
async def test_duplicate_external_action_with_different_approver_is_rejected(platform):
    from workbench_agent.application.synthetic_batch_policy import SyntheticBatchPolicy
    original=rule(platform)
    with pytest.raises(DomainError):
        await SyntheticBatchPolicy(platform.backend).sync([original,{**original,'rule_id':'copy','approver':'another'}],expected_revision=0)

@pytest.mark.asyncio
async def test_limit_is_per_source_scope_and_multiple_teams_can_each_have_three(platform):
    from workbench_agent.application.synthetic_batch_policy import SyntheticBatchPolicy,matching_rules
    from uuid import uuid4
    first=rule(platform)
    second_source={**first['source'],'team_id':str(uuid4()),'agent_id':str(uuid4())}
    def variants(source,count,prefix):
        return [{**first,'rule_id':prefix+str(i),'source':source,'arguments':{**first['arguments'],'case':str(i)}} for i in range(count)]
    left=variants(first['source'],3,'a');right=variants(second_source,3,'b')
    service=SyntheticBatchPolicy(platform.backend)
    assert await service.sync(left+right,expected_revision=0)==1
    async with platform.backend.create_uow().begin_read() as uow:
        assert len(await matching_rules(uow,first['source'],first['scope']))==3
        assert len(await matching_rules(uow,second_source,first['scope']))==3
    with pytest.raises(DomainError): await service.sync(variants(first['source'],5,'c'),expected_revision=1)

@pytest.mark.asyncio
async def test_equivalent_external_targets_deduplicate_across_binding_versions(platform):
    from workbench_agent.application.synthetic_batch_policy import SyntheticBatchPolicy
    first=rule(platform)
    second={**first,'rule_id':'same-effect','binding':{**first['binding'],'version':'2','content_hash':'b'*64}}
    with pytest.raises(DomainError): await SyntheticBatchPolicy(platform.backend).sync([first,second],expected_revision=0)
    distinct={**second,'target':{'server':'local.mock.seatalk','credential_scope':'local-mock','tool_name':'SendNotification'}}
    assert await SyntheticBatchPolicy(platform.backend).sync([first,distinct],expected_revision=0)==1
