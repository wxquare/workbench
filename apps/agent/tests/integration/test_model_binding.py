from uuid import uuid4
import pytest
from pydantic import ValidationError
from workbench_agent.domain.profiles import ProfileDraft, RoleModelBinding
from workbench_agent.domain.identity import AuthContext
from workbench_agent.application.errors import DomainError

async def test_snapshot_pins_old_versions_and_role_binding_wins(platform):
    old = await platform.contexts.resolve(platform.auth, platform.agent_id)
    model = await platform.publish_model('approved-new')
    row = await platform.profiles.get_agent(platform.auth, platform.agent_id)
    changed = await platform.profiles.save_draft(platform.auth, platform.agent_id,
        ProfileDraft(default_model=model.ref, role_models=(RoleModelBinding(role='code', ref=old.default_model),)),
        expected_revision=row['revision'], key=str(uuid4()))
    await platform.profiles.publish(platform.auth, platform.agent_id,
        changed['revision'], key=str(uuid4()))
    new = await platform.contexts.resolve(platform.auth, platform.agent_id)
    assert old.default_model.version == '1'
    assert new.model_for('code') == old.default_model
    assert new.model_for('summary') == model.ref
    with pytest.raises(ValidationError):
        new.default_model.version = '99'
    with pytest.raises((TypeError, AttributeError)):
        new.role_models[0] = RoleModelBinding(role='summary', ref=model.ref)
    with pytest.raises(ValidationError):
        new.role_models[0].ref.version = '99'

async def test_cross_tenant_model_reference_cannot_be_published(platform):
    other = await platform.profiles.get_active_model(platform.other_auth)
    row = await platform.profiles.get_agent(platform.auth, platform.agent_id)
    with pytest.raises(DomainError) as error:
        await platform.profiles.save_draft(platform.auth, platform.agent_id,
            ProfileDraft(default_model=other.ref), expected_revision=row['revision'], key='foreign')
    assert error.value.status == 404

async def test_model_configuration_return_is_copy_isolated(platform):
    version = await platform.profiles.get_active_model(platform.auth)
    version.configuration.provider_options['thinking']['type'] = 'enabled'
    assert version.configuration.provider_options == {'thinking': {'type': 'disabled'}}

async def test_runtime_context_roundtrip_keeps_pinned_refs(platform):
    from workbench_agent.domain.context import RunContext
    old = await platform.contexts.resolve(platform.auth, platform.agent_id)
    encoded = old.model_dump_json()
    await platform.publish_model('future-model')
    restored = RunContext.model_validate_json(encoded)
    await platform.contexts.revalidate(restored)
    assert restored.default_model.version == '1'
    assert restored.agent_profile == old.agent_profile

async def test_model_version_publication_requires_admin_even_with_stale_auth_roles(platform):
    from workbench_agent.settings import ModelProfile
    await platform.teams.set_member(platform.auth, platform.auth.subject, 'maintainer', 'active', expected_revision=0, key='demote')
    with pytest.raises(DomainError) as error:
        await platform.profiles.publish_model(platform.auth, platform.agent_id, ModelProfile(model_id='unauthorized'),
            expected_revision=0, key='unauthorized-model')
    assert error.value.status == 403

async def test_model_ref_invalid_version_is_a_validation_error(platform, client):
    model = await platform.profiles.get_active_model(platform.auth)
    ref = model.ref.model_dump()
    ref['version'] = 'not-an-integer'
    response = await client.patch(f'/v1/agents/{platform.agent_id}',
        json={'default_model': ref}, headers={'If-Match': str((await platform.profiles.get_agent(platform.auth,platform.agent_id))['revision']), 'Idempotency-Key': 'malformed'})
    assert response.status_code == 422

async def test_context_pins_published_graph_id_across_later_publication(platform):
    old = await platform.contexts.resolve(platform.auth, platform.agent_id)
    current = await platform.profiles.get_agent(platform.auth, platform.agent_id)
    changed = await platform.profiles.save_draft(platform.auth, platform.agent_id,
        ProfileDraft(default_model=old.default_model, graph_id='conversation.single_turn'), current['revision'], key='graph-draft')
    await platform.profiles.publish(platform.auth, platform.agent_id, changed['revision'], key='graph-publish')
    new = await platform.contexts.resolve(platform.auth, platform.agent_id)
    assert old.graph_id == 'conversation.multi_turn'
    assert new.graph_id == 'conversation.single_turn'

async def test_default_agent_lookup_is_not_limited_to_first_inventory_page(platform):
    from uuid import UUID
    async with platform.backend.create_uow().begin_write() as uow:
        for number in range(1, 102):
            await platform.backend.configuration.insert(uow, 'agents', {'team_id': platform.auth.team_id,
                'agent_id': str(UUID(int=number)), 'name': f'agent-{number}'})
    assert (await platform.profiles.get_agent(platform.auth))['agent_id'] == platform.agent_id
    scoped = platform.auth.model_copy(update={'identity_kind': 'connector', 'agent_scope': (platform.agent_id,)})
    assert (await platform.profiles.get_agent(scoped))['agent_id'] == platform.agent_id
    allowed = (str(UUID(int=100)), str(UUID(int=101)))
    fallback = platform.auth.model_copy(update={'identity_kind': 'connector', 'agent_scope': allowed})
    assert (await platform.profiles.get_agent(fallback))['agent_id'] == allowed[0]


# M2 model invocation coverage
import pytest
from langchain_core.messages import AIMessage, HumanMessage
from pydantic import BaseModel, StrictStr
from support.jobs import claim

class Output(BaseModel):
    summary: StrictStr

class Scripted:
    def __init__(self, replies): self.replies=list(replies); self.count=0
    async def ainvoke(self, messages, **kwargs):
        self.count+=1
        value=self.replies.pop(0)
        if isinstance(value, Exception): raise value
        return AIMessage(content=value, response_metadata={'model_name':'actual','finish_reason':'stop'},
            usage_metadata={'input_tokens': 10,'output_tokens': 5,'total_tokens': 15})

class Factory:
    def __init__(self, model): self.model=model; self.model_ids=[]
    def build(self, profile, secrets, **scope): self.model_ids.append(profile.model_id); return self.model

@pytest.mark.asyncio
async def test_json_repair_is_one_new_budgeted_attempt(platform):
    from workbench_agent.runtime.models.invoker import ModelInvoker
    run=await platform.create_run(summary_token_reserve=8000)
    guard=(await claim(platform)).guard
    model=Scripted(['   ', '{"summary":7}'])
    result=await ModelInvoker(platform.backend, Factory(model), object()).invoke(guard,'summary',[HumanMessage(content='Answer')],output_schema=Output)
    assert result.status=='invalid_output'
    assert model.count==2
    usage=await platform.budgets.snapshot(platform.auth,run.run_id)
    assert usage.model_attempts==2 and usage.known_tokens==30

@pytest.mark.asyncio
async def test_run_uses_frozen_model_after_rebinding(platform):
    from workbench_agent.runtime.models.invoker import ModelInvoker
    await platform.create_run(summary_token_reserve=8000)
    await platform.publish_model('new-model')
    guard=(await claim(platform)).guard
    factory=Factory(Scripted(['ok']))
    result=await ModelInvoker(platform.backend,factory,object()).invoke(guard,'diagnosis',[HumanMessage(content='hello')])
    assert factory.model_ids==['deepseek-flash']
    assert result.requested_model=='deepseek-flash' and result.response_model=='actual'

@pytest.mark.asyncio
async def test_missing_usage_remains_pending(platform):
    from workbench_agent.runtime.models.invoker import ModelInvoker
    run=await platform.create_run(summary_token_reserve=8000)
    guard=(await claim(platform)).guard
    class Model:
        async def ainvoke(self,*args,**kwargs): return AIMessage(content='ok')
    await ModelInvoker(platform.backend,Factory(Model()),object()).invoke(guard,'diagnosis',[HumanMessage(content='x')])
    usage=await platform.budgets.snapshot(platform.auth,run.run_id)
    assert usage.usage_pending and usage.reserved_tokens>0 and usage.cost_pending

@pytest.mark.asyncio
async def test_retry_attempts_are_committed_before_transport(platform):
    from workbench_agent.runtime.models.invoker import ModelInvoker
    run=await platform.create_run(summary_token_reserve=8000)
    guard=(await claim(platform)).guard
    class Failure(Exception): status_code=503
    class Model:
        def __init__(self): self.count=0
        async def ainvoke(self,*args,**kwargs):
            self.count+=1
            snapshot=await platform.budgets.snapshot(platform.auth,run.run_id)
            assert snapshot.model_attempts==self.count
            if self.count==1: raise Failure('fictional-provider-secret')
            return AIMessage(content='ok',usage_metadata={'input_tokens':1,'output_tokens':1,'total_tokens':2})
    model=Model()
    result=await ModelInvoker(platform.backend,Factory(model),object()).invoke(guard,'diagnosis',[HumanMessage(content='x')])
    assert result.status=='ok' and model.count==2
    snapshot=await platform.budgets.snapshot(platform.auth,run.run_id)
    assert snapshot.model_attempts==2 and snapshot.usage_pending

@pytest.mark.asyncio
async def test_late_usage_is_quarantined_not_published(platform):
    from workbench_agent.runtime.models.invoker import ModelInvoker
    from workbench_agent.persistence.errors import StaleExecution
    from support.jobs import expire_lease
    run=await platform.create_run(summary_token_reserve=8000)
    guard=(await claim(platform)).guard
    class Model:
        async def ainvoke(self,*args,**kwargs):
            await expire_lease(platform,guard)
            return AIMessage(content='late',usage_metadata={'input_tokens':3,'output_tokens':2,'total_tokens':5})
    with pytest.raises(StaleExecution):
        await ModelInvoker(platform.backend,Factory(Model()),object()).invoke(guard,'diagnosis',[HumanMessage(content='x')])
    assert await platform.count_rows('usage_quarantine')==1
    usage=await platform.budgets.snapshot(platform.auth,run.run_id)
    assert usage.usage_pending and usage.known_tokens==0

@pytest.mark.asyncio
async def test_repair_and_retries_share_three_total_attempts(platform):
    from workbench_agent.runtime.models.invoker import ModelInvoker
    run=await platform.create_run(summary_token_reserve=8000)
    guard=(await claim(platform)).guard
    class Failure(Exception): status_code=503
    model=Scripted([Failure('unavailable'), ' ', Failure('unavailable'), '{"summary":"ok"}'])
    result=await ModelInvoker(platform.backend,Factory(model),object()).invoke(guard,'summary',[HumanMessage(content='x')],output_schema=Output)
    assert result.status=='error' and model.count==3
    assert (await platform.budgets.snapshot(platform.auth,run.run_id)).model_attempts==3

@pytest.mark.asyncio
async def test_tool_call_is_available_to_caller(platform):
    from workbench_agent.runtime.models.invoker import ModelInvoker
    await platform.create_run(summary_token_reserve=8000)
    guard=(await claim(platform)).guard
    class Model:
        async def ainvoke(self,*args,**kwargs):
            return AIMessage(content='',tool_calls=[{'name':'search','args':{'query':'x'},'id':'call1'}])
    result=await ModelInvoker(platform.backend,Factory(Model()),object()).invoke(guard,'diagnosis',[HumanMessage(content='x')])
    assert result.status=='ok' and result.message.tool_calls[0]['name']=='search'

@pytest.mark.asyncio
async def test_large_tool_schema_is_counted_before_sending(platform):
    from workbench_agent.runtime.models.invoker import ModelInvoker
    from workbench_agent.application.errors import DomainError
    await platform.create_run(summary_token_reserve=8000)
    guard=(await claim(platform)).guard
    class Model:
        def __init__(self): self.sent=False
        def bind_tools(self,tools): return self
        async def ainvoke(self,*args,**kwargs): self.sent=True; return AIMessage(content='ok')
    model=Model()
    tool={'type':'function','function':{'name':'search','description':'x'*100000,'parameters':{'type':'object','properties':{}}}}
    with pytest.raises(DomainError,match='MAX_CONTEXT_EXCEEDED'):
        await ModelInvoker(platform.backend,Factory(model),object()).invoke(guard,'diagnosis',[HumanMessage(content='x')],tools=[tool])
    assert not model.sent

@pytest.mark.asyncio
async def test_network_timeout_does_not_exceed_run_wall_budget(platform):
    import asyncio,time
    from workbench_agent.runtime.models.invoker import ModelInvoker
    from workbench_agent.application.errors import DomainError
    await platform.create_run(summary_token_reserve=8000,wall_seconds=1)
    guard=(await claim(platform)).guard
    class Model:
        async def ainvoke(self,*args,**kwargs): await asyncio.sleep(3)
    start=time.monotonic()
    try:
        await ModelInvoker(platform.backend,Factory(Model()),object()).invoke(guard,'diagnosis',[HumanMessage(content='x')])
    except DomainError as error:
        assert error.code=='BUDGET_EXCEEDED'
    assert time.monotonic()-start < 2.0

@pytest.mark.asyncio
async def test_attempt_trace_is_structured_and_never_contains_provider_error_or_prompt(platform,caplog):
    import logging
    from workbench_agent.runtime.models.invoker import ModelInvoker
    await platform.create_run(summary_token_reserve=8000)
    guard=(await claim(platform)).guard
    class Failure(Exception): status_code=401
    model=Scripted([Failure('fictional-sensitive-error')])
    with caplog.at_level(logging.INFO,logger='workbench.models'):
        result=await ModelInvoker(platform.backend,Factory(model),object()).invoke(guard,'diagnosis',[HumanMessage(content='fictional-sensitive-prompt')])
    assert result.error_code=='MODEL_CREDENTIAL_UNAVAILABLE'
    assert any(getattr(record,'event',None)=='model.attempt' for record in caplog.records)
    assert 'fictional-sensitive-error' not in caplog.text and 'fictional-sensitive-prompt' not in caplog.text

@pytest.mark.asyncio
async def test_revoked_member_receipt_is_quarantined(platform):
    from workbench_agent.runtime.models.invoker import ModelInvoker
    from workbench_agent.application.errors import DomainError
    run=await platform.create_run(summary_token_reserve=8000)
    guard=(await claim(platform)).guard
    current=await platform.teams.get_member(platform.auth,platform.auth.subject)
    class Model:
        async def ainvoke(self,*args,**kwargs):
            await platform.teams.set_member(platform.auth,platform.auth.subject,'admin','revoked',expected_revision=current['revision'],key='revoke-during-model')
            return AIMessage(content='late',usage_metadata={'input_tokens':3,'output_tokens':2,'total_tokens':5})
    with pytest.raises(DomainError):
        await ModelInvoker(platform.backend,Factory(Model()),object()).invoke(guard,'diagnosis',[HumanMessage(content='x')])
    assert await platform.count_rows('usage_quarantine')==1

@pytest.mark.asyncio
@pytest.mark.parametrize('revoke_before_retry',[False,True])
async def test_connector_current_policy_checked_before_each_attempt(platform,revoke_before_retry):
    from workbench_agent.runtime.models.invoker import ModelInvoker
    from workbench_agent.jobs.worker import Execution,DeploymentConnectorPolicy,CapabilityBlocked
    from support.jobs import claim
    platform.auth=platform.auth.model_copy(update={'identity_kind':'connector','agent_scope':(platform.agent_id,)})
    await platform.create_run(summary_token_reserve=8000)
    claimed=await claim(platform)
    bindings={'connector':{'subject':platform.auth.subject,'team_id':platform.auth.team_id,'agent_ids':[platform.agent_id]}}
    if not revoke_before_retry: bindings.clear()
    execution=Execution(platform.backend,claimed,DeploymentConnectorPolicy(lambda:bindings))
    class Failure(Exception): status_code=503
    class Model:
        count=0
        async def ainvoke(self,*args,**kwargs):
            self.count+=1
            bindings.clear()
            raise Failure()
    model=Model()
    with pytest.raises(CapabilityBlocked):
        await ModelInvoker(platform.backend,Factory(model),object(),execution=execution).invoke(claimed.guard,'diagnosis',[HumanMessage(content='x')])
    assert model.count==(1 if revoke_before_retry else 0)

@pytest.mark.asyncio
async def test_connector_execution_must_match_persisted_context(platform):
    from workbench_agent.runtime.models.invoker import ModelInvoker
    from workbench_agent.jobs.worker import Execution,DeploymentConnectorPolicy
    from workbench_agent.application.errors import DomainError
    from dataclasses import replace
    platform.auth=platform.auth.model_copy(update={'identity_kind':'connector','agent_scope':(platform.agent_id,)})
    await platform.create_run(summary_token_reserve=8000)
    claimed=await claim(platform)
    forged=replace(claimed,context=claimed.context.model_copy(update={'identity_kind':'user'}))
    execution=Execution(platform.backend,forged,DeploymentConnectorPolicy(lambda:{}))
    model=Scripted(['must-not-send'])
    with pytest.raises(DomainError,match='STALE_EXECUTION'):
        await ModelInvoker(platform.backend,Factory(model),object(),execution=execution).invoke(claimed.guard,'diagnosis',[HumanMessage(content='x')])
    assert model.count==0
