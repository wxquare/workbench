"""Real SQLite/API fixtures, explicitly extendable by later tasks."""

from dataclasses import dataclass
from uuid import uuid4
import pytest
from httpx import ASGITransport, AsyncClient
from workbench_agent.settings import Settings, ModelProfile
from workbench_agent.cli.db import migrate
from workbench_agent.cli.bootstrap import BootstrapService
from workbench_agent.domain.identity import AuthContext
from support.profiles import publish_agent_default
from workbench_agent.application.profiles import ProfileService
from workbench_agent.application.context import ContextResolver
from workbench_agent.application.teams import TeamService
from workbench_agent.persistence.backends.sqlite import SQLiteBackend
from workbench_agent.api.app import create_app
from workbench_agent.application.runs import RunService
from workbench_agent.domain.runs import CreateRun
from support.runs import make_run_payload


@dataclass
class Platform:
    settings: Settings
    backend: SQLiteBackend
    profiles: ProfileService
    teams: TeamService
    contexts: ContextResolver
    auth: AuthContext
    other_auth: AuthContext

    @property
    def agent_id(self):
        return self.settings.bootstrap.agent_id

    @property
    def budgets(self):
        from workbench_agent.runtime.budgets.service import BudgetService

        return BudgetService(self.backend)

    async def reserve_usage(self, guard, **kwargs):
        from support.budgets import reserve_usage

        return await reserve_usage(self, guard, **kwargs)

    @staticmethod
    def is_reserved(result):
        from support.budgets import is_reserved

        return is_reserved(result)

    async def create_run(
        self,
        key='r1',
        thread_id=None,
        mode='qa',
        *,
        model_call_limit=None,
        tool_call_limit=None,
        token_limit=None,
        wall_seconds=None,
        summary_token_reserve=None,
    ):
        payload = make_run_payload(self.agent_id, thread_id=thread_id, mode=mode)
        payload['budgets'] = {
            key: value
            for key, value in dict(
                model_calls=model_call_limit,
                tool_calls=tool_call_limit,
                tokens=token_limit,
                wall_seconds=wall_seconds,
            ).items()
            if value is not None
        }
        if summary_token_reserve is not None:
            payload['summary_token_reserve'] = summary_token_reserve
        request = CreateRun.model_validate(payload)
        return await RunService(self.backend).create(self.auth, request, key)

    async def get_run(self, run_id):
        return await RunService(self.backend).get(self.auth, run_id)

    async def count_rows(self, table):
        from sqlalchemy import select, func
        from workbench_agent.persistence import schema

        async with self.backend.create_uow().begin_read() as uow:
            return (
                await uow.execute(select(func.count()).select_from(schema.metadata.tables[table]))
            ).scalar_one()

    async def bootstrap(self):
        return await BootstrapService(self.backend).apply(self.settings)

    async def active_model(self, auth, agent_id=None):
        async with self.backend.create_uow().begin_read() as uow:
            agent = await self.profiles.agent(uow, auth, agent_id)
            profile = await self.profiles.published(uow, auth, agent['agent_id'])
            return await self.profiles.model_version(uow, auth, profile.default_model)

    async def publish_model(self, model_id):
        version = await self.publish_model_version(ModelProfile(model_id=model_id))
        await publish_agent_default(self.profiles, self.auth, self.agent_id, version.ref)
        return version

    async def publish_model_version(self, model):
        from workbench_agent.application.model_profiles import ModelProfileService
        from workbench_agent.domain.profiles import ModelProfileVersion

        service = ModelProfileService(self.backend)
        current = await service.get(self.auth, self.agent_id, model.name)
        patched = await service.patch(
            self.auth,
            self.agent_id,
            model.name,
            model.model_dump(mode='json'),
            current['revision'],
            str(uuid4()),
        )
        published = await service.publish(
            self.auth, self.agent_id, model.name, patched.body['revision'], str(uuid4())
        )
        return ModelProfileVersion.model_validate(published.body)


@pytest.fixture
async def platform(config_data):
    settings = Settings.model_validate(config_data)
    migrate(settings.storage.database_url.get_secret_value())
    backend = SQLiteBackend(settings.storage.database_url.get_secret_value())
    profiles = ProfileService(backend)
    other = settings.model_copy(
        update={
            'bootstrap': settings.bootstrap.model_copy(
                update={
                    'team_id': str(uuid4()),
                    'agent_id': str(uuid4()),
                    'admin_subject': 'other-admin',
                }
            )
        }
    )
    await BootstrapService(backend).apply(settings)
    await BootstrapService(backend).apply(other)
    value = Platform(
        settings,
        backend,
        profiles,
        TeamService(backend),
        ContextResolver(backend),
        AuthContext(
            team_id=settings.bootstrap.team_id,
            subject=settings.bootstrap.admin_subject,
            roles=('admin',),
        ),
        AuthContext(team_id=other.bootstrap.team_id, subject='other-admin', roles=('admin',)),
    )
    from workbench_agent.domain.profiles import ProfileDraft

    for auth, agent in ((value.auth, value.agent_id), (value.other_auth, other.bootstrap.agent_id)):
        async with backend.create_uow().begin_read() as uow:
            published = await profiles.published(uow, auth, agent)
        current = await profiles.get_agent(auth, agent)
        changed = await profiles.save_draft(
            auth,
            agent,
            ProfileDraft(default_model=published.default_model, graph_id='conversation.multi_turn'),
            expected_revision=current['revision'],
            key=str(uuid4()),
        )
        await profiles.publish(auth, agent, expected_revision=changed['revision'], key=str(uuid4()))
    yield value
    await backend.close()


@pytest.fixture
async def client(platform):
    app = create_app(platform.settings, backend=platform.backend)
    async with app.router.lifespan_context(app):
        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url='http://test',
            headers={
                'Authorization': f'Bearer development:{platform.auth.team_id}:{platform.auth.subject}'
            },
        ) as value:
            yield value
