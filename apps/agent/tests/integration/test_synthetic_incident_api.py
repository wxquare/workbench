import pytest
from httpx import ASGITransport,AsyncClient
from sqlalchemy import update,insert
from workbench_agent.api.app import create_app
from workbench_agent.jobs.worker import Worker,DeploymentConnectorPolicy
from workbench_agent.runtime.graphs.registry import foundation_worker_registry
from workbench_agent.settings import SyntheticIngressBinding
from workbench_agent.persistence import schema
from test_synthetic_alert_v2 import publish_v13,event,signed


@pytest.mark.asyncio
async def test_signed_alert_http_to_incident_with_current_access(platform,monkeypatch):
    await publish_v13(platform)
    monkeypatch.setenv('SYNTHETIC_V2_KEY','test-only-secret')
    binding=SyntheticIngressBinding(protocol='synthetic-alert-v2',team_id=platform.auth.team_id,
        agent_id=platform.agent_id,actor_subject=platform.auth.subject,secret_ref='env://SYNTHETIC_V2_KEY',
        identity_id='v2-test',allowed_scopes=[('checkout','test','id','error-rate')])
    identities={'v2-test':{'subject':platform.auth.subject,'team_id':platform.auth.team_id,'agent_ids':[platform.agent_id]}}
    settings=platform.settings.model_copy(update={'environment':'test','service':platform.settings.service.model_copy(update={
        'synthetic_ingress':{'v2-test':binding},'connector_identities':identities})})
    app=create_app(settings,backend=platform.backend)
    raw,headers=signed({**event(),'text':'sensitive alert text'})
    from uuid import uuid4
    async with platform.backend.create_uow().begin_write() as uow:
        now=await platform.backend.clock.now_ms(uow)
        await uow.execute(insert(schema.team_members).values(team_id=platform.auth.team_id,member_id=str(uuid4()),subject='incident-reader',role='viewer',status='active',created_at=now,updated_at=now))
    auth={'Authorization':f'Bearer development:{platform.auth.team_id}:incident-reader'}
    other={'Authorization':f'Bearer development:{platform.other_auth.team_id}:{platform.other_auth.subject}'}
    async with app.router.lifespan_context(app):
        async with AsyncClient(transport=ASGITransport(app=app),base_url='http://test') as client:
            response=await client.post('/v1/ingress/v2-test',content=raw,headers=headers)
            assert response.status_code==202
            duplicate=await client.post('/v1/ingress/v2-test',content=raw,headers=headers)
            run_id=response.json()['run_id'];assert duplicate.json()['run_id']==run_id
            policy=DeploymentConnectorPolicy(lambda:identities)
            assert await Worker(platform.backend,'incident-api',foundation_worker_registry(platform.backend),connector_policy=policy).run_once()=='projected'
            second_raw,second_headers=signed({**event(),'event_id':'alert-3','text':'second sensitive text'})
            assert (await client.post('/v1/ingress/v2-test',content=second_raw,headers=second_headers)).status_code==202
            assert await Worker(platform.backend,'incident-api-2',foundation_worker_registry(platform.backend),connector_policy=policy).run_once()=='projected'
            listing=await client.get('/v1/incidents',params={'agent_id':platform.agent_id,'limit':1},headers=auth)
            assert listing.status_code==200 and len(listing.json()['items'])==1
            item=listing.json()['items'][0];assert item['source']=='synthetic-alert-v2'
            page2=await client.get('/v1/incidents',params={'agent_id':platform.agent_id,'limit':1,'after':listing.json()['next_cursor']},headers=auth)
            assert page2.status_code==200 and len(page2.json()['items'])==1 and page2.json()['next_cursor'] is None
            items=[item,page2.json()['items'][0]]
            item=next(x for x in items if x['run_id']==run_id)
            assert 'sensitive alert text' not in listing.text
            incident_id=item['incident_id']
            assert (await client.get('/v1/incidents/'+incident_id,headers=auth)).json()==item
            assert (await client.get('/v1/incidents/'+incident_id,headers=other)).status_code==404
            saved_identities=dict(settings.service.connector_identities)
            from workbench_agent.application.synthetic_source_policy import SyntheticSourcePolicy
            from workbench_agent.persistence.backends.sqlite import SQLiteBackend
            other_backend=SQLiteBackend(platform.settings.storage.database_url.get_secret_value())
            try: await SyntheticSourcePolicy(other_backend).sync([],expected_revision=1)
            finally: await other_backend.close()
            assert (await client.get('/v1/incidents/'+incident_id,headers=auth)).status_code==404
            assert (await client.get('/v1/incidents',params={'agent_id':platform.agent_id},headers=auth)).json()['items']==[]
            from workbench_agent.cli.synthetic_source_policy import sync_source_policy
            await sync_source_policy(settings,2)
            settings.service.connector_identities.clear()
            assert (await client.get('/v1/incidents/'+incident_id,headers=auth)).status_code==404
            settings.service.connector_identities.update(saved_identities)
            settings.service.synthetic_ingress.pop('v2-test')
            assert (await client.get('/v1/incidents/'+incident_id,headers=auth)).status_code==404
            settings.service.synthetic_ingress['v2-test']=binding
            async with platform.backend.create_uow().begin_write() as uow:
                await uow.execute(update(schema.synthetic_alert_sources).where(schema.synthetic_alert_sources.c.run_id==run_id).values(payload_hash='0'*64))
            assert (await client.get('/v1/incidents/'+incident_id,headers=auth)).status_code==404
            from workbench_agent.application.idempotency import digest
            import json
            async with platform.backend.create_uow().begin_write() as uow:
                await uow.execute(update(schema.synthetic_alert_sources).where(schema.synthetic_alert_sources.c.run_id==run_id).values(payload_hash=digest(json.loads(raw))))
            async with platform.backend.create_uow().begin_write() as uow:
                scope=await platform.backend.runs.current_scope(uow,platform.auth.team_id,run_id)
                await uow.execute(update(schema.query_scope_revisions).where(schema.query_scope_revisions.c.scope_id==scope['scope_id']).values(scope_json='{}'))
            assert (await client.get('/v1/incidents/'+incident_id,headers=auth)).status_code==404
            remaining=(await client.get('/v1/incidents',params={'agent_id':platform.agent_id},headers=auth)).json()['items']
            assert all(x['incident_id']!=incident_id for x in remaining)
            async with platform.backend.create_uow().begin_write() as uow:
                await uow.execute(update(schema.team_members).where(schema.team_members.c.team_id==platform.auth.team_id,schema.team_members.c.subject=='incident-reader').values(status='revoked'))
            assert (await client.get('/v1/incidents/'+incident_id,headers=auth)).status_code in (401,404)

@pytest.mark.asyncio
async def test_same_subject_connectors_require_the_exact_source_identity(platform,monkeypatch):
    import hashlib,hmac,json,time
    from uuid import uuid4
    from workbench_agent.ingress.alerts import mac_message
    await publish_v13(platform);monkeypatch.setenv('SYNTHETIC_V2_KEY','test-only-secret')
    common={'protocol':'synthetic-alert-v2','team_id':platform.auth.team_id,'agent_id':platform.agent_id,
        'actor_subject':platform.auth.subject,'secret_ref':'env://SYNTHETIC_V2_KEY',
        'allowed_scopes':[('checkout','test','id','error-rate')]}
    from workbench_agent.application.synthetic_source_policy import SyntheticSourcePolicy
    grants=[dict(team_id=platform.auth.team_id,agent_id=platform.agent_id,connector_id='connector-'+suffix,
        identity_id='identity-'+suffix,subject=platform.auth.subject,service='checkout',environment='test',region='id',rule='error-rate') for suffix in ('a','b')]
    await SyntheticSourcePolicy(platform.backend).sync(grants,connector_identities={key:{'subject':platform.auth.subject,'team_id':platform.auth.team_id,'agent_ids':[platform.agent_id]} for key in ('identity-a','identity-b')},expected_revision=1)
    binding_a=SyntheticIngressBinding(**common,identity_id='identity-a')
    binding_b=SyntheticIngressBinding(**common,identity_id='identity-b')
    identities={key:{'subject':platform.auth.subject,'team_id':platform.auth.team_id,'agent_ids':[platform.agent_id]} for key in ('identity-a','identity-b')}
    settings=platform.settings.model_copy(update={'environment':'test','service':platform.settings.service.model_copy(update={
        'synthetic_ingress':{'connector-a':binding_a,'connector-b':binding_b},'connector_identities':identities})})
    app=create_app(settings,backend=platform.backend)
    async with platform.backend.create_uow().begin_write() as uow:
        now=await platform.backend.clock.now_ms(uow)
        await uow.execute(insert(schema.team_members).values(team_id=platform.auth.team_id,member_id=str(uuid4()),subject='separate-reader',role='viewer',status='active',created_at=now,updated_at=now))
    auth={'Authorization':f'Bearer development:{platform.auth.team_id}:separate-reader'}
    async with app.router.lifespan_context(app):
        async with AsyncClient(transport=ASGITransport(app=app),base_url='http://test') as client:
            for connector in ('connector-a','connector-b'):
                payload={**event(),'event_id':connector};raw=json.dumps(payload,sort_keys=True).encode();timestamp=str(int(time.time()))
                signature=hmac.new(b'test-only-secret',mac_message('synthetic-alert-v2',connector,timestamp,raw),hashlib.sha256).hexdigest()
                response=await client.post('/v1/ingress/'+connector,content=raw,headers={'x-dod-timestamp':timestamp,'x-dod-signature':signature})
                assert response.status_code==202
                policy=DeploymentConnectorPolicy(lambda:identities)
                assert await Worker(platform.backend,'worker-'+connector,foundation_worker_registry(platform.backend),connector_policy=policy).run_once()=='projected'
            listing=(await client.get('/v1/incidents',params={'agent_id':platform.agent_id},headers=auth)).json()['items']
            assert len(listing)==2
            by_run={item['run_id']:item for item in listing}
            async with platform.backend.create_uow().begin_read() as uow:
                from sqlalchemy import select
                sources=(await uow.execute(select(schema.synthetic_alert_sources))).mappings().all()
            a=next(by_run[row['run_id']] for row in sources if row['connector_id']=='connector-a')
            b=next(by_run[row['run_id']] for row in sources if row['connector_id']=='connector-b')
            async with platform.backend.create_uow().begin_write() as uow:
                await uow.execute(update(schema.synthetic_alert_sources).where(schema.synthetic_alert_sources.c.run_id==a['run_id']).values(identity_id=None))
            assert (await client.get('/v1/incidents/'+a['incident_id'],headers=auth)).status_code==404
            async with platform.backend.create_uow().begin_write() as uow:
                await uow.execute(update(schema.synthetic_alert_sources).where(schema.synthetic_alert_sources.c.run_id==a['run_id']).values(identity_id='identity-a'))
            identities.pop('identity-a')
            assert (await client.get('/v1/incidents/'+a['incident_id'],headers=auth)).status_code==404
            assert (await client.get('/v1/incidents/'+b['incident_id'],headers=auth)).status_code==200
            visible=(await client.get('/v1/incidents',params={'agent_id':platform.agent_id},headers=auth)).json()['items']
            assert [x['incident_id'] for x in visible]==[b['incident_id']]


def test_v2_binding_requires_explicit_identity(platform):
    from pydantic import ValidationError
    with pytest.raises(ValidationError):
        SyntheticIngressBinding(protocol='synthetic-alert-v2',team_id=platform.auth.team_id,agent_id=platform.agent_id,
            actor_subject=platform.auth.subject,secret_ref='env://TEST',allowed_scopes=[('checkout','test','id','error-rate')])


def test_v2_startup_rejects_missing_or_mismatched_identity(platform):
    common={'protocol':'synthetic-alert-v2','team_id':platform.auth.team_id,'agent_id':platform.agent_id,
        'actor_subject':platform.auth.subject,'secret_ref':'env://TEST',
        'identity_id':'identity-required','allowed_scopes':[('checkout','test','id','error-rate')]}
    binding=SyntheticIngressBinding(**common)
    for identities in ({},{'identity-required':{'subject':'wrong','team_id':platform.auth.team_id,'agent_ids':[platform.agent_id]}},
        {'other-identity':{'subject':platform.auth.subject,'team_id':platform.auth.team_id,'agent_ids':[platform.agent_id]}}):
        settings=platform.settings.model_copy(update={'environment':'test','service':platform.settings.service.model_copy(update={
            'synthetic_ingress':{'connector':binding},'connector_identities':identities})})
        with pytest.raises(ValueError,match='matching connector identity'): create_app(settings,backend=platform.backend)
