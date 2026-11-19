"""Bind confirmed local simulated receipts to the originating synthetic incident."""
from workbench_agent.runtime.graphs.ids import GraphId
import json
from types import SimpleNamespace
from uuid import UUID,uuid5
from sqlalchemy import select,insert
from workbench_agent.domain.context import RunContext
from workbench_agent.application.errors import DomainError
from workbench_agent.application.synthetic_source_policy import require_bound_source
from workbench_agent.incidents.synthetic import source_scope
from workbench_agent.persistence import schema


async def project_delivery_link(backend,uow,action,outbox,receipt,now):
    if outbox['destination'] not in ('local.mock.jira','local.mock.dod','local.mock.seatalk'): return
    try:
        latest=await backend.actions.one(uow,'actions',team_id=action['team_id'],action_id=action['action_id'])
        box=await backend.actions.one(uow,'outbox',team_id=action['team_id'],outbox_id=outbox['outbox_id'])
        if (not latest or latest['state']!='succeeded' or not box or box['status']!='succeeded'
            or json.loads(latest['receipt'])!=receipt or json.loads(box['receipt_ref'])!=receipt): return
        run=await backend.runs.one(uow,'runs',team_id=action['team_id'],run_id=action['run_id'])
        context=RunContext.model_validate_json(run['versions'])
        if context.graph_id not in (GraphId.RCA_SYNTHETIC,GraphId.RCA_PROPOSAL,GraphId.RCA_BATCH_PROPOSAL): return
        await require_bound_source(uow,context,action['run_id'])
        source,event,scope,matches=await source_scope(backend,uow,SimpleNamespace(team_id=action['team_id'],run_id=action['run_id']))
        if not matches or scope['scope_id']!=action['scope']: return
        incident=(await uow.execute(select(schema.incidents).where(schema.incidents.c.team_id==action['team_id'],
            schema.incidents.c.run_id==action['run_id'],schema.incidents.c.source=='synthetic-alert-v2'))).mappings().all()
        if len(incident)!=1: return
        row=incident[0]
        if (row['service']!=event.service or row['severity']!='unknown'
            or json.loads(row['conclusion'])!={'rule':event.rule,'status':'alert_observed'}
            or json.loads(row['evidence'])!={'scope_id':scope['scope_id'],'source_hash':source['payload_hash']}): return
        external_id=receipt['external_id']
        from workbench_agent.delivery.local_simulator import expected_external_id
        if external_id!=expected_external_id(action['action_id'],outbox['destination'].removeprefix('local.mock.')): return
        existing=(await uow.execute(select(schema.incident_action_links).where(
            schema.incident_action_links.c.team_id==action['team_id'],schema.incident_action_links.c.action_id==action['action_id']))).mappings().first()
        if existing: return
        collision=(await uow.execute(select(schema.incident_links).where(schema.incident_links.c.incident_id==row['incident_id'],
            schema.incident_links.c.external_system==outbox['destination'],schema.incident_links.c.external_id==external_id))).mappings().first()
        if collision: return
        link_id=str(uuid5(UUID(action['action_id']),'incident-delivery-link:1'))
        await uow.execute(insert(schema.incident_links).values(team_id=action['team_id'],link_id=link_id,incident_id=row['incident_id'],
            run_id=action['run_id'],external_system=outbox['destination'],external_id=external_id,created_at=now,updated_at=now))
        await uow.execute(insert(schema.incident_action_links).values(team_id=action['team_id'],link_id=link_id,action_id=action['action_id']))
    except (DomainError,ValueError,TypeError,KeyError,AttributeError):
        # A valid receipt remains a fact even if the historical incident is no longer readable.
        return
