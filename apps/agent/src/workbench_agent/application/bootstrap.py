"""Create-if-missing seeds, shared by explicit maintenance and authorized team creation."""

from uuid import uuid4
from workbench_agent.domain.identity import VerifiedIdentity
from workbench_agent.domain.profiles import FrozenModel, ModelProfileVersion, ProfileDraft
from workbench_agent.application.idempotency import canonical, digest


class BootstrapReport(FrozenModel):
    team_id: str
    agent_id: str
    default_model: ModelProfileVersion
    created_team: bool
    created_agent: bool


class BootstrapService:
    def __init__(self, backend):
        self.backend, self.store = backend, backend.configuration

    async def apply(self, settings, *, identity: VerifiedIdentity | None = None) -> BootstrapReport:
        seed = settings.bootstrap
        if settings.environment == 'production':
            from workbench_agent.settings import resolve_env_reference

            if (
                identity is None
                or settings.service.auth_mode != 'oidc'
                or identity.subject != seed.admin_subject
                or identity.connector_id
                or identity.issuer != resolve_env_reference(settings.service.oidc_issuer_ref)
                or seed.admin_subject == 'development-admin'
            ):
                raise PermissionError(
                    'production bootstrap requires explicit verified OIDC admin identity'
                )
        from workbench_agent.application.workflow_seeds import read_workflow_seeds

        workflow_seeds = read_workflow_seeds(settings.asset_root)
        async with self.backend.create_uow().begin_write() as uow:
            return await self.seed(
                uow, settings.bootstrap, settings.llm, workflow_seeds=workflow_seeds
            )

    async def seed(self, uow, seed, llm, *, workflow_seeds=()):
        """Caller must hold maintenance authority or current admin team-creation authority."""
        now = await self.backend.clock.now_ms(uow)
        existing = await self.store.one(uow, 'teams', team_id=seed.team_id)
        if not existing:
            await self.store.insert(
                uow,
                'teams',
                dict(
                    team_id=seed.team_id,
                    name=seed.team_name,
                    status='active',
                    created_at=now,
                    updated_at=now,
                ),
            )
            await self.store.insert(
                uow,
                'team_members',
                dict(
                    team_id=seed.team_id,
                    member_id=str(uuid4()),
                    subject=seed.admin_subject,
                    role='admin',
                    status='active',
                    created_at=now,
                    updated_at=now,
                ),
            )
        default = None
        created_models = []
        for model in llm.profiles:
            profile = await self.store.one(
                uow, 'model_profiles', team_id=seed.team_id, name=model.name
            )
            if profile is None:
                profile_id, model_id = str(uuid4()), str(uuid4())
                created_models.append(profile_id)
                data = model.model_dump(mode='json')
                await self.store.insert(
                    uow,
                    'model_profiles',
                    dict(
                        team_id=seed.team_id,
                        profile_id=profile_id,
                        name=model.name,
                        scope='team',
                        draft=canonical(data),
                        active_version=1,
                        created_at=now,
                        updated_at=now,
                    ),
                )
                await self.store.insert(
                    uow,
                    'model_profile_versions',
                    dict(
                        team_id=seed.team_id,
                        model_version_id=model_id,
                        profile_id=profile_id,
                        version=1,
                        provider=model.provider,
                        adapter=model.adapter,
                        protocol=model.protocol,
                        base_url=model.base_url,
                        model_id=model.model_id,
                        secret_ref=model.secret_ref,
                        limits=canonical(data),
                        capabilities='{}',
                        hash=digest(data),
                        created_at=now,
                        updated_at=now,
                    ),
                )
                profile = await self.store.one(
                    uow, 'model_profiles', team_id=seed.team_id, profile_id=profile_id
                )
            if model.name == llm.default_profile:
                row = await self.store.one(
                    uow,
                    'model_profile_versions',
                    team_id=seed.team_id,
                    profile_id=profile['profile_id'],
                    version=profile['active_version'],
                )
                if row is None:
                    raise ValueError('existing model has no published version')
                default = ModelProfileVersion(
                    model_version_id=row['model_version_id'],
                    profile_id=row['profile_id'],
                    name=model.name,
                    version=str(row['version']),
                    content_hash=row['hash'],
                    configuration_json=row['limits'],
                )
        agent = await self.store.one(uow, 'agents', team_id=seed.team_id, agent_id=seed.agent_id)
        if agent is None:
            await self.store.insert(
                uow,
                'agents',
                dict(
                    team_id=seed.team_id,
                    agent_id=seed.agent_id,
                    name=seed.agent_name,
                    created_at=now,
                    updated_at=now,
                ),
            )
            draft = ProfileDraft(default_model=default.ref, allowed_modes=('qa',))
            data = draft.model_dump(mode='json')
            profile_id = str(uuid4())
            for version, status in ((0, 'draft'), (1, 'published')):
                await self.store.insert(
                    uow,
                    'runtime_profiles',
                    dict(
                        team_id=seed.team_id,
                        profile_id=profile_id if version else str(uuid4()),
                        agent_id=seed.agent_id,
                        version=version,
                        graph_id=draft.graph_id,
                        profile_json=canonical(data),
                        hash=digest(data),
                        model_versions=canonical([default.ref.model_dump()]),
                        skill_versions='[]',
                        tool_versions='[]',
                        repo_versions='[]',
                        status=status,
                        created_at=now,
                        updated_at=now,
                    ),
                )
            await self.store.change(
                uow,
                'agents',
                {'team_id': seed.team_id, 'agent_id': seed.agent_id},
                {'active_profile_id': profile_id},
            )
        created_workflows = []
        for workflow in workflow_seeds:
            found = await self.store.rows(
                uow,
                'workflow_versions',
                limit=1,
                team_id=seed.team_id,
                agent_id=seed.agent_id,
                name=workflow.name,
            )
            if found:
                continue
            definition = workflow.model_dump(mode='json')
            workflow_id = str(uuid4())
            await self.store.insert(
                uow,
                'workflow_versions',
                dict(
                    team_id=seed.team_id,
                    workflow_version_id=workflow_id,
                    agent_id=seed.agent_id,
                    name=workflow.name,
                    version=1,
                    definition_json=canonical(definition),
                    hash=digest(definition),
                    created_at=now,
                    updated_at=now,
                ),
            )
            created_workflows.append(workflow_id)
        if not existing or not agent or created_models or created_workflows:
            await self.store.insert(
                uow,
                'audit_logs',
                dict(
                    team_id=seed.team_id,
                    audit_id=str(uuid4()),
                    actor=seed.admin_subject,
                    object=seed.agent_id,
                    operation='bootstrap',
                    decision='allowed',
                    trace=canonical(
                        {'created_models': created_models, 'created_workflows': created_workflows}
                    ),
                    created_at=now,
                    updated_at=now,
                ),
            )
        return BootstrapReport(
            team_id=seed.team_id,
            agent_id=seed.agent_id,
            default_model=default,
            created_team=existing is None,
            created_agent=agent is None,
        )
