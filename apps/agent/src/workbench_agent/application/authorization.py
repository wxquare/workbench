from workbench_agent.domain.identity import ROLE_LEVEL
from workbench_agent.application.errors import DomainError, not_found


class Authorization:
    def __init__(self, backend):
        self.store = backend.configuration

    async def current(self, uow, auth, agent_id=None, minimum='viewer'):
        team = await self.store.one(uow, 'teams', team_id=auth.team_id, status='active')
        member = await self.store.one(
            uow, 'team_members', team_id=auth.team_id, subject=auth.subject, status='active'
        )
        if not team or not member or member['role'] not in ROLE_LEVEL:
            raise not_found()
        if agent_id is not None:
            agent = await self.store.one(
                uow, 'agents', team_id=auth.team_id, agent_id=agent_id, status='active'
            )
            if not agent or (auth.agent_scope is not None and agent_id not in auth.agent_scope):
                raise not_found()
        if ROLE_LEVEL[member['role']] < ROLE_LEVEL[minimum]:
            raise DomainError('FORBIDDEN', 403)
        if auth.identity_kind == 'connector' and minimum in {'maintainer', 'admin'}:
            raise DomainError('FORBIDDEN', 403)
        return auth.model_copy(update={'roles': (member['role'],)})
