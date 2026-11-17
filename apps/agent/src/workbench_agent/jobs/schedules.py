"""Database-backed, bounded schedule scanning; no process-local fire ownership."""
import json
from datetime import datetime,timezone
from uuid import uuid4
from workbench_agent.application.authorization import Authorization
from workbench_agent.application.errors import DomainError
from workbench_agent.application.runs import RunService
from workbench_agent.domain.identity import AuthContext
from workbench_agent.domain.runs import CreateRun
from workbench_agent.domain.schedules import ScheduleSpec,next_due

WINDOW_MS=6*3600*1000

def _next(expression,zone,ms):
    return int(next_due(expression,zone,datetime.fromtimestamp(ms/1000,timezone.utc)).timestamp()*1000)

class ScheduleScanner:
    def __init__(self,backend): self.backend=backend; self.after=None
    async def tick(self):
        async with self.backend.create_uow().begin_read() as uow:
            now=await self.backend.clock.now_ms(uow)
            rows=await self.backend.schedules.due(uow,now,after=self.after)
            if not rows and self.after is not None:
                self.after=None
                rows=await self.backend.schedules.due(uow,now)
        self.after=(rows[-1]['next_due_at'],rows[-1]['schedule_id']) if rows else None
        results=[]
        for row in rows:
            try:
                result=await self._fire_one(row)
            except DomainError:
                # The caller-owned UoW rolled back all Run/fire writes. Continue
                # other teams instead of starving the scan on one invalid profile.
                result={'schedule_id':row['schedule_id'],'status':'unavailable','run_id':None}
            if result is not None: results.append(result)
        return results
    async def _fire_one(self,row):
            async with self.backend.create_uow().begin_write() as uow:
                now=await self.backend.clock.now_ms(uow)
                schedule=await self.backend.schedules.one(uow,row['team_id'],row['schedule_id'])
                if not schedule or schedule['status']!='active' or schedule['next_due_at']>now: return None
                due=schedule['next_due_at']; first=due
                if due < now-WINDOW_MS:
                    await self.backend.schedules.fire(uow,dict(team_id=schedule['team_id'],fire_id=str(uuid4()),schedule_id=schedule['schedule_id'],due_at=due,run_id=None,status='missed',created_at=now,updated_at=now))
                    due=_next(schedule['cron'],schedule['timezone'],now-WINDOW_MS-1)
                latest=None
                # A cron has at most one occurrence per minute per UTC instant;
                # bound the catch-up to a six-hour window.
                for _ in range(362):
                    if due>now: break
                    latest=due
                    due=_next(schedule['cron'],schedule['timezone'],due)
                else: raise ValueError('schedule catch-up bound exceeded')
                fire_status=None; run_id=None
                if latest is not None:
                    if await self.backend.schedules.active_run(uow,schedule['team_id'],schedule['schedule_id']):
                        fire_status='overlap_skipped'
                    else:
                        payload=json.loads(schedule['payload']); spec=ScheduleSpec.model_validate(payload['spec'])
                        auth=AuthContext.model_validate(payload['owner'])
                        row=await self.backend.configuration.one(uow,'workflow_versions',team_id=schedule['team_id'],agent_id=schedule['agent_id'],workflow_version_id=str(spec.workflow_version_id))
                        try:
                            await Authorization(self.backend).current(uow,auth,schedule['agent_id'],'operator')
                            permitted=True
                        except DomainError:
                            permitted=False
                        if not permitted:
                            fire_status='denied'
                        elif not row or row['hash']!=payload['workflow_hash'] or row['version']!=payload['workflow_version']:
                            fire_status='unavailable'
                        else:
                            request=CreateRun.model_validate({'agent_id':schedule['agent_id'],'mode':'inspection',
                                'workflow_version_id':str(spec.workflow_version_id),'input':{'text':spec.input_text},'client_message_id':f"schedule:{schedule['schedule_id']}:{latest}"})
                            accepted=await RunService(self.backend).create(auth,request,f"schedule:{schedule['schedule_id']}:{latest}",_uow=uow)
                            run_id=accepted.run_id
                            if spec.query_scope is not None:
                                from workbench_agent.application.context import ContextResolver
                                await ContextResolver(self.backend).query_scope(uow,auth,run_id,spec.query_scope.values(latest))
                            fire_status='started'
                    await self.backend.schedules.fire(uow,dict(team_id=schedule['team_id'],fire_id=str(uuid4()),schedule_id=schedule['schedule_id'],due_at=latest,run_id=run_id,status=fire_status,created_at=now,updated_at=now))
                if not await self.backend.schedules.advance(uow,schedule['team_id'],schedule['schedule_id'],schedule['revision'],
                    {'next_due_at':due,'revision':schedule['revision']+1,'updated_at':now}): raise RuntimeError('schedule revision lost')
                return {'schedule_id':schedule['schedule_id'],'due_at':latest if latest is not None else first,'status':fire_status or 'missed','run_id':run_id}
