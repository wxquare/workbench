from uuid import uuid4
from workbench_agent.domain.identity import ROLE_LEVEL
from workbench_agent.application.authorization import Authorization
from workbench_agent.application.errors import DomainError, not_found
from workbench_agent.application.idempotency import Idempotency


class TeamService:
    def __init__(self, backend, *, settings=None):
        from workbench_agent.settings import LLMSettings

        self.llm = settings.llm if settings else LLMSettings()
        self.backend, self.store = backend, backend.configuration
        self.authorization, self.idempotency = Authorization(backend), Idempotency(backend)

    async def list_teams(self, auth):
        async with self.backend.create_uow().begin_read() as uow:
            await self.authorization.current(uow, auth)
            return [dict(await self.store.one(uow, 'teams', team_id=auth.team_id))]

    async def get_member(self, auth, subject):
        async with self.backend.create_uow().begin_read() as uow:
            await self.authorization.current(uow, auth, minimum='admin')
            member = await self.store.one(
                uow, 'team_members', team_id=auth.team_id, subject=subject
            )
            if not member:
                raise not_found()
            team = await self.store.one(uow, 'teams', team_id=auth.team_id)
            return dict(member, revision=team['revision'])

    async def set_member(
        self, auth, subject, role, status, expected_revision, *, key, receipt=False
    ):
        if (
            role not in ROLE_LEVEL
            or status not in {'active', 'revoked'}
            or not 1 <= len(subject) <= 128
        ):
            raise DomainError('INVALID_MEMBER', 422)
        async with self.backend.create_uow().begin_write() as uow:
            await self.authorization.current(uow, auth, minimum='admin')

            async def operation():
                team = await self.store.one(uow, 'teams', team_id=auth.team_id)
                if team['revision'] != expected_revision:
                    raise DomainError('REVISION_CONFLICT', 409)
                member = await self.store.one(
                    uow, 'team_members', team_id=auth.team_id, subject=subject
                )
                now = await self.backend.clock.now_ms(uow)
                if member:
                    await self.store.change(
                        uow,
                        'team_members',
                        {'member_id': member['member_id']},
                        {'role': role, 'status': status, 'updated_at': now},
                    )
                else:
                    await self.store.insert(
                        uow,
                        'team_members',
                        dict(
                            member_id=str(uuid4()),
                            team_id=auth.team_id,
                            subject=subject,
                            role=role,
                            status=status,
                            created_at=now,
                            updated_at=now,
                        ),
                    )
                await self.store.change(
                    uow,
                    'teams',
                    {'team_id': auth.team_id, 'revision': expected_revision},
                    {'revision': expected_revision + 1, 'updated_at': now},
                )
                return {
                    'subject': subject,
                    'role': role,
                    'status': status,
                    'revision': expected_revision + 1,
                }

            result = await self.idempotency.execute_team(
                uow,
                auth,
                'team.member',
                key,
                dict(
                    subject=subject, role=role, status=status, expected_revision=expected_revision
                ),
                operation,
            )
        return result if receipt else result.body

    async def create_team(self, auth, name, *, key, receipt=False):
        async with self.backend.create_uow().begin_write() as uow:
            await self.authorization.current(uow, auth, minimum='admin')

            async def operation():
                team_id, agent_id = str(uuid4()), str(uuid4())
                from workbench_agent.application.bootstrap import BootstrapService
                from workbench_agent.settings import BootstrapSettings

                seed = BootstrapSettings(
                    team_id=team_id, agent_id=agent_id, team_name=name, admin_subject=auth.subject
                )
                await BootstrapService(self.backend).seed(uow, seed, self.llm)
                return {'team_id': team_id, 'name': name, 'revision': 0, 'agent_id': agent_id}

            result = await self.idempotency.execute_team(
                uow, auth, 'team.create', key, {'name': name}, operation, status=201
            )
        return result if receipt else result.body
