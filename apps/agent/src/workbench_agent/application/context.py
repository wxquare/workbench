from workbench_agent.domain.context import RunContext, QueryScopeRevision
from workbench_agent.domain.identity import AuthContext
from workbench_agent.application.authorization import Authorization
from workbench_agent.application.profiles import ProfileService

class ContextResolver:
    def __init__(self, backend):
        self.backend, self.profiles = backend, ProfileService(backend)
        self.authorization = Authorization(backend)

    async def resolve(self, auth, agent_id, *, uow=None, source='api') -> RunContext:
        if uow is None:
            async with self.backend.create_uow().begin_read() as owned:
                return await self.resolve(auth, agent_id, uow=owned, source=source)
        current = await self.authorization.current(uow, auth, agent_id, 'operator')
        profile = await self.profiles.published(uow, auth, agent_id)
        await self.profiles.validate_refs(uow, auth, agent_id, profile)
        return RunContext(team_id=auth.team_id, agent_id=agent_id, subject=auth.subject,
            identity_kind=auth.identity_kind, agent_scope=auth.agent_scope, roles=current.roles,
            accepted_at_ms=await self.backend.clock.now_ms(uow), source=source, agent_profile=profile.ref,
            graph_version=profile.graph_version,
            system_prompt=profile.system_prompt, response_language=profile.response_language,
            response_format=profile.response_format, conversation_history_turns=profile.conversation_history_turns,
            default_model=profile.default_model, role_models=profile.role_models, skills=profile.skills,
            tools=profile.tools, repositories=profile.repositories)

    async def revalidate(self, context: RunContext, *, minimum='operator', uow=None):
        if uow is None:
            async with self.backend.create_uow().begin_read() as owned:
                return await self.revalidate(context, minimum=minimum, uow=owned)
        auth = AuthContext(team_id=context.team_id, subject=context.subject, roles=context.roles,
            identity_kind=context.identity_kind, agent_scope=context.agent_scope)
        return await self.authorization.current(uow, auth, context.agent_id, minimum)

    async def query_scope(self,uow,auth,run_id,values) -> QueryScopeRevision:
        from uuid import uuid4
        from workbench_agent.application.idempotency import canonical,digest
        from workbench_agent.application.errors import DomainError,not_found
        run=await self.backend.runs.one(uow,'runs',team_id=auth.team_id,run_id=run_id)
        if run is None: raise not_found()
        await self.authorization.current(uow,auth,run['agent_id'],'operator')
        if set(values)-{'region','service','environment','start_at','end_at'} or any(not isinstance(v,str) for v in values.values()):
            raise DomainError('INVALID_SCOPE',422)
        previous=await self.backend.runs.current_scope(uow,auth.team_id,run_id)
        if previous and previous['hash']==digest(values): return previous
        now=await self.backend.clock.now_ms(uow)
        row=QueryScopeRevision(team_id=auth.team_id,scope_id=str(uuid4()),run_id=run_id,version=previous['version']+1 if previous else 1,
            scope_json=canonical(values),hash=digest(values),created_by=auth.subject,created_at=now,updated_at=now)
        await self.backend.runs.insert(uow,'query_scope_revisions',row)
        return row

    async def revise_scope(self, auth, run_id, scope_ref, user_response):
        """Revise a region against the current revision without moving its window."""
        import json
        from workbench_agent.application.errors import DomainError, not_found
        if (not isinstance(user_response, dict) or set(user_response) != {'region'}
                or not isinstance(user_response['region'], str)
                or not user_response['region'].strip()
                or user_response['region'] != user_response['region'].strip()):
            raise DomainError('INVALID_SCOPE', 422)
        async with self.backend.create_uow().begin_write() as uow:
            run = await self.backend.runs.one(uow, 'runs', team_id=auth.team_id, run_id=run_id)
            if run is None:
                raise not_found()
            await self.authorization.current(uow, auth, run['agent_id'], 'operator')
            previous = await self.backend.runs.current_scope(uow, auth.team_id, run_id)
            if not previous or previous['scope_id'] != scope_ref:
                raise DomainError('STALE_QUERY_SCOPE', 409)
            values = json.loads(previous['scope_json'])
            if not isinstance(values, dict):
                raise DomainError('INVALID_SCOPE', 422)
            values['region'] = user_response['region']
            return await self.query_scope(uow, auth, run_id, values)

    async def current_evidence(self,uow,auth,run_id):
        from workbench_agent.application.errors import not_found
        run=await self.backend.runs.one(uow,'runs',team_id=auth.team_id,run_id=run_id)
        if run is None: raise not_found()
        await self.authorization.current(uow,auth,run['agent_id'])
        rows=await self.backend.runs.current_evidence(uow,auth.team_id,run_id)
        from workbench_agent.application.disclosure import readable_evidence
        allowed=await readable_evidence(self.backend,uow,run,auth,{row['evidence_id'] for row in rows},include_truncated=True)
        return [row for row in rows if row['evidence_id'] in allowed]

    async def evidence_content(self,uow,auth,run_id,evidence_id):
        from workbench_agent.application.errors import not_found
        import json
        run=await self.backend.runs.one(uow,'runs',team_id=auth.team_id,run_id=run_id)
        if run is None: raise not_found()
        await self.authorization.current(uow,auth,run['agent_id'])
        from workbench_agent.application.disclosure import readable_evidence
        allowed=await readable_evidence(self.backend,uow,run,auth,{evidence_id},include_truncated=True)
        evidence=await self.backend.results.evidence(uow,auth.team_id,run_id,{evidence_id})
        if evidence_id not in allowed or not evidence: raise not_found()
        payload=await self.backend.tools.evidence_payload(uow,auth.team_id,run_id,evidence_id)
        if not payload: raise not_found()
        return {'evidence_id':evidence_id,'source':evidence[0]['source'],'quality':evidence[0]['quality'],
            'truncated':bool(payload['truncated']),'content':json.loads(payload['payload'])}
