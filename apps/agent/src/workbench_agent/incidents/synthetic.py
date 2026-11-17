"""Project only a bound synthetic alert, never model-derived incident facts."""
import json
from uuid import UUID, uuid5
from sqlalchemy import insert, select
from workbench_agent.application.idempotency import canonical, digest
from workbench_agent.application.errors import DomainError
from workbench_agent.domain.query_scope import AlertScopeInput, resolve_alert_scope
from workbench_agent.ingress.alerts import AlertV2
from workbench_agent.persistence import schema


async def source_scope(backend,uow,guard):
    table=schema.synthetic_alert_sources
    row=(await uow.execute(select(table).where(table.c.team_id==guard.team_id,table.c.run_id==guard.run_id))).mappings().first()
    if not row or not row['identity_id']: raise DomainError('SYNTHETIC_SOURCE_UNAVAILABLE',409)
    try:
        payload=json.loads(row['payload'])
        event=AlertV2.model_validate(payload)
        if row['payload_hash']!=digest(payload): raise ValueError()
        value=resolve_alert_scope(AlertScopeInput(team=guard.team_id,service=event.service,
            environment=event.environment,body_region=event.region,link_region=None,
            anchor=event.anchor,window_seconds=event.window_seconds)).scope
        scope=await backend.runs.current_scope(uow,guard.team_id,guard.run_id)
        expected={'region':value.region,'service':value.service,'environment':value.environment,
            'start_at':value.start.isoformat(),'end_at':value.end.isoformat()}
        matches = bool(scope and json.loads(scope['scope_json']) == expected)
    except (ValueError,TypeError,KeyError,AttributeError):
        raise DomainError('SYNTHETIC_SOURCE_UNAVAILABLE',409) from None
    return row, event, scope, matches


async def project_incident(backend,uow,guard,run,now):
    row,event,scope,matches=await source_scope(backend,uow,guard)
    if not matches: return False
    await uow.execute(insert(schema.incidents).values(team_id=guard.team_id,
        incident_id=str(uuid5(UUID(guard.run_id),'synthetic-alert-incident:1')),run_id=guard.run_id,
        source='synthetic-alert-v2',service=event.service,conclusion=canonical({'rule':event.rule,'status':'alert_observed'}),
        evidence=canonical({'scope_id':scope['scope_id'],'source_hash':row['payload_hash']}),severity='unknown',
        created_at=now,updated_at=now))

    return True
