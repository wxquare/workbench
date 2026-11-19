"""Local M3 smoke journeys: fixture data, no external writes or provider calls."""
import hashlib
import hmac
import json
import time
from pathlib import Path
from datetime import datetime, timezone
from uuid import uuid4

import pytest
from httpx import ASGITransport, AsyncClient

from workbench_agent.api.app import create_app
from workbench_agent.application.synthetic_source_policy import SyntheticSourcePolicy
from workbench_agent.domain.profiles import ProfileDraft
from workbench_agent.ingress.alerts import mac_message
from workbench_agent.jobs.worker import DeploymentConnectorPolicy, Worker
from workbench_agent.runtime.graphs.registry import foundation_worker_registry
from workbench_agent.settings import SyntheticIngressBinding


@pytest.mark.asyncio
@pytest.mark.parametrize("graph_id", ["rca.proposal", "rca.batch_proposal"])
async def test_local_signed_alert_to_approved_jira_receipt(platform, monkeypatch, tmp_path, graph_id):
    import httpx
    from workbench_agent.domain.tools import ToolBinding
    from workbench_agent.tools.registry import ToolRegistry
    from workbench_agent.tools.mcp import MCPAdapter, ConnectionPolicy, TrustedConnection, StreamableSessionFactory
    from workbench_agent.tools.policy import PolicyToolExecutor
    from workbench_agent.tools.permissions import DeploymentToolPermissions
    from support.mcp import mcp_server_source
    server_src = mcp_server_source()
    monkeypatch.syspath_prepend(str(server_src))
    from workbench_mcp_server.bootstrap import Runtime
    from workbench_mcp_server.infrastructure.config.settings import Settings as MCPSettings
    from workbench_mcp_server.interfaces.mcp.server import create_http_app
    monkeypatch.setenv('JOURNEY_MCP_TOKEN','fictional-agent-token')
    monkeypatch.setenv('JOURNEY_UPSTREAM_TOKEN','fictional-upstream-token')
    monkeypatch.setenv('JOURNEY_CURSOR_KEY','c'*32)
    names=('metrics_query','logs_search')
    kinds=('metrics','logs')
    scopes=[{'team':'ExampleTeam','environment':'test','region':'id','service':'checkout','source_id':kind} for kind in kinds]
    sources=[]
    for kind,scope in zip(kinds,scopes):
        source={'id':kind,'kind':kind,'adapter':'normalized-http-v1','endpoint':'https://example.invalid/query',
            'auth_secret_ref':'env://JOURNEY_UPSTREAM_TOKEN','binding_id':'fixture-'+kind,'scopes':[scope],'scope_enforced':True}
        if kind=='metrics': source['templates']={'error_rate':{}}
        sources.append(source)
    mcp_runtime=Runtime(MCPSettings.model_validate({'server':{'allowed_hosts':['localhost:*']},
        'principals':[{'id':'agent','token_secret_ref':'env://JOURNEY_MCP_TOKEN','tools':list(names),'scopes':scopes}],
        'sources':sources,'cursor_secret_ref':'env://JOURNEY_CURSOR_KEY'}))
    class Gateway:
        def __init__(self,kind): self.kind=kind
        async def query(self,request):
            if self.kind=='metrics':
                return {'series':[{'labels':{'service':'checkout'},'points':[[request.parameters['start'], 0.1]], 'unit':'ratio','source_id':'metric-1'}]}
            return {'entries':[{'timestamp':request.parameters['start'],'service':'checkout',
                'level':'ERROR','message':'fixture log','record_id':'log-1'}]}
    for kind in kinds: mcp_runtime.executor.gateways[kind]=Gateway(kind)
    mcp_app=create_http_app(mcp_runtime)
    async with mcp_app.app.router.lifespan_context(mcp_app.app):
        factory=StreamableSessionFactory({('local','read'):'https://localhost:8000/mcp'},
            lambda *_:{'Authorization':'Bearer fictional-agent-token'},transport=httpx.ASGITransport(app=mcp_app))
        from datetime import timedelta
        today=datetime.now(timezone.utc)
        GRANT={'kind':'dod-mcp-v1','team':'ExampleTeam','allowed_scope':{'service':'checkout','environment':'test','region':'id'},
            'earliest':(today-timedelta(days=1)).strftime('%Y-%m-%d %H:%M:%S'),
            'latest':(today+timedelta(days=1)).strftime('%Y-%m-%d %H:%M:%S'),'max_span_seconds':3600,'timezone':'UTC',
            'sources':{'metrics_query':'metrics','logs_search':'logs'}}
        adapter=MCPAdapter(factory,ConnectionPolicy(lambda:[TrustedConnection(platform.auth.team_id,platform.agent_id,
            'local','read',frozenset(names),(GRANT,))]))
        catalog=await adapter.discover('local','read',team_id=platform.auth.team_id,agent_id=platform.agent_id)
        refs=[]
        for name in names:
            binding=ToolBinding(alias='local.'+name,provider='mcp',tool_name=name,server='local',credential_scope='read',
                permission='baseline.read',input_schema=catalog.tools[catalog.names.index(name)].inputSchema)
            refs.append(await ToolRegistry(platform.backend).publish(platform.auth,platform.agent_id,binding,key='journey-'+name))
        now = int(time.time())
        end=datetime.fromtimestamp(now,timezone.utc)
        action_scope={'service':'checkout','environment':'test','region':'id',
            'start_at':(end-timedelta(seconds=600)).isoformat(),'end_at':end.isoformat()}
        write_binding=ToolBinding(alias='journey.jira',provider='mcp',tool_name='CreateTicket',
            server='local.mock.jira',credential_scope='local-mock',permission='ticket.propose',read_only=False,
            input_schema={'type':'object','properties':{key:{'type':'string'} for key in action_scope},
                'required':list(action_scope),'additionalProperties':False})
        write_ref=await ToolRegistry(platform.backend).publish(platform.auth,platform.agent_id,write_binding,key='journey-jira')
        refs.append(write_ref)
        chat_ref = None
        if graph_id == 'rca.batch_proposal':
            chat_binding = write_binding.model_copy(update={'alias':'journey.seatalk','tool_name':'SendNotification','server':'local.mock.seatalk'})
            chat_ref = await ToolRegistry(platform.backend).publish(platform.auth, platform.agent_id, chat_binding, key='journey-seatalk')
            refs.append(chat_ref)
        context = await platform.contexts.resolve(platform.auth, platform.agent_id)
        agent = await platform.profiles.get_agent(platform.auth, platform.agent_id)
        draft = await platform.profiles.save_draft(platform.auth, platform.agent_id,
            ProfileDraft(default_model=context.default_model, graph_id=graph_id,tools=tuple(refs)), agent['revision'], key='journey-draft')
        await platform.profiles.publish(platform.auth, platform.agent_id, draft['revision'], key='journey-publish')
        monkeypatch.setenv('JOURNEY_ALERT_KEY', 'fictional-test-only')
        identities = {'journey-identity': {'subject': platform.auth.subject, 'team_id': platform.auth.team_id,
                                          'agent_ids': [platform.agent_id]}}
        binding = SyntheticIngressBinding(protocol='synthetic-alert-v2', team_id=platform.auth.team_id,
            agent_id=platform.agent_id, actor_subject=platform.auth.subject, identity_id='journey-identity',
            secret_ref='env://JOURNEY_ALERT_KEY', allowed_scopes=[('checkout','test','id','error-rate')])
        grant = {'team_id': platform.auth.team_id, 'agent_id': platform.agent_id,
            'connector_id':'journey', 'identity_id':'journey-identity', 'subject':platform.auth.subject,
            'service':'checkout','environment':'test','region':'id','rule':'error-rate'}
        await SyntheticSourcePolicy(platform.backend).sync([grant], connector_identities=identities, expected_revision=0)
        settings = platform.settings.model_copy(update={'environment':'test','service':platform.settings.service.model_copy(update={
            'synthetic_ingress':{'journey':binding}, 'connector_identities':identities})})
        payload = {'event_id':str(uuid4()), 'text':'synthetic alert','region':'id','service':'checkout',
            'environment':'test','rule':'error-rate','anchor':datetime.fromtimestamp(now, timezone.utc).isoformat(),
            'window_seconds':600,'occurred_at':now}
        body=json.dumps(payload,sort_keys=True).encode(); timestamp=str(now)
        signature=hmac.new(b'fictional-test-only',mac_message('synthetic-alert-v2','journey',timestamp,body),hashlib.sha256).hexdigest()
        headers={'x-dod-timestamp':timestamp,'x-dod-signature':signature}
        app=create_app(settings,backend=platform.backend)
        from sqlalchemy import insert
        from workbench_agent.persistence import schema
        async with platform.backend.create_uow().begin_write() as uow:
            now_ms = await platform.backend.clock.now_ms(uow)
            await uow.execute(insert(schema.team_members).values(team_id=platform.auth.team_id, member_id=str(uuid4()),
                subject='journey-reader',role='operator',status='active',created_at=now_ms,updated_at=now_ms))
        from workbench_agent.application.action_grants import ActionGrantService
        from workbench_agent.application.synthetic_proposal_policy import SyntheticProposalPolicy
        rules={'users':{platform.auth.subject:['baseline.read','ticket.propose'],
                        'journey-reader':['baseline.read','ticket.propose']},
            'teams':{platform.auth.team_id:['baseline.read','ticket.propose']},
            'agents':{platform.agent_id:['baseline.read','ticket.propose']},
            'skills':{'__none__':['baseline.read','ticket.propose']},
            'experts':{'diagnosis':['baseline.read','ticket.propose']}}
        action_grant={'team_id':platform.auth.team_id,'agent_id':platform.agent_id,
            'binding':write_ref.model_dump(mode='json'),'approver':'journey-reader','arguments':action_scope}
        grants = [action_grant]
        if chat_ref is not None:
            grants.append({**action_grant, 'binding':chat_ref.model_dump(mode='json')})
        await ActionGrantService(platform.backend).sync(grants,rules,identities,expected_revision=0)
        if chat_ref is None:
            await SyntheticProposalPolicy(platform.backend).sync([{'source':grant,'scope':action_scope,
                'binding':write_ref.model_dump(mode='json'),'arguments':action_scope,'approver':'journey-reader'}],expected_revision=0)
        else:
            from workbench_agent.application.synthetic_batch_policy import SyntheticBatchPolicy
            batch = []
            for name, ref, tool in [('jira', write_ref, 'CreateTicket'), ('seatalk', chat_ref, 'SendNotification')]:
                batch.append({'rule_id':name,'source':grant,'scope':action_scope,'binding':ref.model_dump(mode='json'),
                    'target':{'server':'local.mock.'+name,'credential_scope':'local-mock','tool_name':tool},
                    'arguments':action_scope,'approver':'journey-reader'})
            await SyntheticBatchPolicy(platform.backend).sync(batch,expected_revision=0)
        auth={'Authorization':f'Bearer development:{platform.auth.team_id}:journey-reader'}
        async with app.router.lifespan_context(app):
            async with AsyncClient(transport=ASGITransport(app=app),base_url='http://test') as client:
                accepted=await client.post('/v1/ingress/journey',content=body,headers=headers)
                assert accepted.status_code==202, accepted.text
                run_id=accepted.json()['run_id']
                duplicate=await client.post('/v1/ingress/journey',content=body,headers=headers)
                assert duplicate.json()['run_id']==run_id
                policy=DeploymentConnectorPolicy(lambda:identities)
                platform.backend._tool_permissions=DeploymentToolPermissions(lambda:rules)
                platform.backend._rca_metric_template='error_rate'
                platform.backend._baseline_factory=lambda execution: PolicyToolExecutor(platform.backend,adapter,execution=execution,delays=(0,0))
                assert await Worker(platform.backend,'journey-worker',foundation_worker_registry(platform.backend),connector_policy=policy).run_once()=='projected'
                assert await Worker(platform.backend,'journey-restart',foundation_worker_registry(platform.backend),connector_policy=policy).run_once()=='idle'
                assert await platform.count_rows('actions') == (2 if chat_ref else 1)
                view=await client.get('/v1/runs/'+run_id,headers=auth)
                assert view.status_code==200 and view.json()['status']=='partial'
                incidents=await client.get('/v1/incidents',params={'agent_id':platform.agent_id},headers=auth)
                assert incidents.status_code==200
                matches=[item for item in incidents.json()['items'] if item['run_id']==run_id]
                assert len(matches)==1 and matches[0]['source']=='synthetic-alert-v2'
                assert matches[0]['delivery_links']==[]
                from sqlalchemy import select
                async with platform.backend.create_uow().begin_read() as uow:
                    proposals=(await uow.execute(select(schema.actions).where(schema.actions.c.run_id==run_id))).mappings().all()
                    proposed=next(a for a in proposals if json.loads(a['payload_ref'])['target']['server']=='local.mock.jira')
                details=await client.get('/v1/actions/'+proposed['action_id'],headers=auth)
                assert details.status_code==200
                item=details.json()
                assert item['arguments']==action_scope and item['target']['server']=='local.mock.jira'
                decision=await client.post('/v1/approvals/'+item['approval_id']+'/decision',headers=auth,
                    json={'decision':'approve','expected_args_hash':item['payload_hash']})
                assert decision.status_code==200
                if chat_ref is not None:
                    chat = next(a for a in proposals if json.loads(a['payload_ref'])['target']['server']=='local.mock.seatalk')
                    chat_details = (await client.get('/v1/actions/'+chat['action_id'], headers=auth)).json()
                    chat_decision = await client.post('/v1/approvals/'+chat_details['approval_id']+'/decision', headers=auth,
                        json={'decision':'approve','expected_args_hash':chat_details['payload_hash']})
                    assert chat_decision.status_code == 200
                from workbench_agent.delivery.local_simulator import LocalSimulator
                from workbench_agent.delivery.dispatcher import Dispatcher
                from workbench_agent.delivery.reconcile import ActionReconciler
                simulator=LocalSimulator(tmp_path/'journey-jira.sqlite3','jira',lose_first_response=True)
                platform.backend._delivery_senders={'local.mock.jira':simulator}
                dispatcher=Dispatcher(platform.backend,'journey-dispatcher')
                assert await dispatcher.dispatch_one()=='unknown'
                assert await dispatcher.dispatch_one() is None
                import sqlite3
                with sqlite3.connect(tmp_path/'journey-jira.sqlite3') as fake_db:
                    assert fake_db.execute('SELECT count(*) FROM simulator_receipts').fetchone()[0]==1
                from workbench_agent.domain.identity import AuthContext
                approver=AuthContext(team_id=platform.auth.team_id,subject='journey-reader',roles=('operator',),identity_kind='development')
                reconciled=await ActionReconciler(platform.backend).reconcile(approver,proposed['action_id'])
                assert reconciled['state']=='succeeded'
                with sqlite3.connect(tmp_path/'journey-jira.sqlite3') as fake_db:
                    assert fake_db.execute('SELECT count(*) FROM simulator_receipts').fetchone()[0]==1
                if chat_ref is not None:
                    from workbench_agent.cli.local_dispatcher import dispatch_local_once
                    assert (await dispatch_local_once(settings,'seatalk',tmp_path/'journey-seatalk.sqlite3'))['status']=='succeeded'
                updated=(await client.get('/v1/incidents/'+matches[0]['incident_id'],headers=auth)).json()
                links={link['external_system']:link for link in updated['delivery_links']}
                assert set(links)==({'local.mock.jira','local.mock.seatalk'} if chat_ref else {'local.mock.jira'})
                assert links['local.mock.jira']['action_id']==proposed['action_id']
                assert links['local.mock.jira']['external_id']==reconciled['receipt']['external_id']
                if chat_ref is not None:
                    assert links['local.mock.seatalk']['action_id']==chat['action_id']
                    with sqlite3.connect(tmp_path/'journey-jira.sqlite3') as fake_db:
                        jira_receipts=fake_db.execute('SELECT kind,action_id,external_id FROM simulator_receipts').fetchall()
                    with sqlite3.connect(tmp_path/'journey-seatalk.sqlite3') as fake_db:
                        chat_receipts=fake_db.execute('SELECT kind,action_id,external_id FROM simulator_receipts').fetchall()
                    assert jira_receipts==[('jira',proposed['action_id'],links['local.mock.jira']['external_id'])]
                    assert chat_receipts==[('seatalk',chat['action_id'],links['local.mock.seatalk']['external_id'])]
                    assert jira_receipts[0][1] != chat_receipts[0][1]
                    assert jira_receipts[0][2] != chat_receipts[0][2]
                evidence_ids=view.json()['outcome']['evidence_refs']
                assert len(evidence_ids)==2
                async with platform.backend.create_uow().begin_read() as uow:
                    scope=await platform.backend.runs.current_scope(uow,platform.auth.team_id,run_id)
                    rows=await platform.backend.runs.current_evidence(uow,platform.auth.team_id,run_id)
                assert {row['source'] for row in rows}=={'local.metrics_query','local.logs_search'}
                expected_end=datetime.fromtimestamp(now,timezone.utc)
                expected_start=expected_end-timedelta(seconds=600)
                values=json.loads(scope['scope_json'])
                assert {key:values[key] for key in ('region','service','environment')}=={'region':'id','service':'checkout','environment':'test'}
                assert datetime.fromisoformat(values['start_at'])==expected_start
                assert datetime.fromisoformat(values['end_at'])==expected_end
                for row in rows:
                    assert row['evidence_id'] in evidence_ids and row['scope_id']==scope['scope_id']
                    assert row['quality']=='valid'
                    window=json.loads(row['time_window'])
                    assert datetime.fromisoformat(window['start_at'])==expected_start
                    assert datetime.fromisoformat(window['end_at'])==expected_end
                    response=await client.get(f"/v1/runs/{run_id}/evidence/{row['evidence_id']}",headers=auth)
                    assert response.status_code==200
                    data=response.json()
                    assert data['source']==row['source'] and data['quality']=='valid' and data['truncated'] is False
                    upstream=data['content']['evidence_refs']
                    assert upstream and all(item['scope']=={'team':'ExampleTeam','environment':'test','region':'id',
                        'service':'checkout','source_id':'metrics' if row['source']=='local.metrics_query' else 'logs'}
                        and item['query_scope_ref']==scope['scope_id']
                        and item['quality_status']=='valid' and item['source_locator']
                        and datetime.fromisoformat(item['time_window']['start'])==expected_start
                        and datetime.fromisoformat(item['time_window']['end'])==expected_end for item in upstream)
                rules['users']['journey-reader']=[]
                denied=await client.get(f"/v1/runs/{run_id}/evidence/{evidence_ids[0]}",headers=auth)
                assert denied.status_code==404
                hidden=(await client.get('/v1/runs/'+run_id,headers=auth)).json()
                assert hidden['outcome']['evidence_refs']==[]



@pytest.mark.asyncio
async def test_synthetic_alert_rejects_unrecognized_link_region(platform):
    from integration.test_synthetic_alert_v2 import event, ingress, publish_v13, signed
    from workbench_agent.application.errors import DomainError

    await publish_v13(platform)
    alert = event()
    alert['link_region'] = 'PH'
    with pytest.raises(DomainError) as error:
        await ingress(platform).accept(*signed(alert))
    assert error.value.code == 'INVALID_INGRESS'
    assert await platform.count_rows('runs') == 0
