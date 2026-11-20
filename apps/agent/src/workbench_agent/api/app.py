"""FastAPI composition root: runtime startup only reads storage, never migrates/seeds."""
from workbench_agent.persistence.readiness import storage_ready
from contextlib import asynccontextmanager
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from workbench_agent.settings import load_settings, resolve_env_reference
from workbench_agent.persistence.backends.sqlite import SQLiteBackend
from workbench_agent.application.profiles import ProfileService
from workbench_agent.application.context import ContextResolver
from workbench_agent.application.teams import TeamService
from workbench_agent.api.auth import OIDCVerifier, DevelopmentVerifier, IdentityResolver
from workbench_agent.api import teams, agents, runs, threads, model_profiles, team_model_profiles, admin_jobs
from workbench_agent.application.runs import RunService
from workbench_agent.api.errors import install_error_handlers


def create_app(settings, *, backend=None, verifier=None, artifact_signer=None, event_hints=None, secret_availability=None):
    if isinstance(settings, (str, bytes)) or hasattr(settings, '__fspath__'):
        settings = load_settings(settings)
    own_backend = backend is None
    if backend is None:
        from workbench_agent.tools.permissions import DeploymentToolPermissions
        backend = SQLiteBackend(settings.storage.database_url.get_secret_value(), tool_permissions=DeploymentToolPermissions(lambda: settings.service.tool_permissions))
    from workbench_agent.tools.mcp import configured_connection_policy
    if not hasattr(backend, '_mcp_connection_policy'):
        backend._mcp_connection_policy = configured_connection_policy(lambda: settings.service.mcp_connections)
    telemetry = None
    if settings.observability.tracing.enabled and not hasattr(backend, '_telemetry'):
        from workbench_agent.observability import Telemetry
        telemetry = Telemetry(settings.observability, 'api', settings.environment)
        backend._telemetry = telemetry
    if settings.environment == 'production' and settings.service.synthetic_ingress:
        raise ValueError('synthetic ingress is disabled in production')
    for binding in settings.service.synthetic_ingress.values():
        subjects = binding.actors.values() if binding.protocol == 'synthetic-chat-v1' else (binding.actor_subject,)
        for subject in subjects:
            identities=settings.service.connector_identities
            candidates=([identities.get(binding.identity_id)] if binding.protocol=='synthetic-alert-v2' else identities.values())
            if not any(item and item['subject'] == subject and item['team_id'] == binding.team_id
                    and binding.agent_id in item['agent_ids'] for item in candidates):
                raise ValueError('synthetic ingress requires matching connector identity')
    if settings.environment == 'production' and settings.service.auth_mode != 'oidc':
        raise ValueError('production rejects development identity')
    if verifier is None:
        if settings.service.auth_mode == 'development':
            verifier = DevelopmentVerifier()
        else:
            verifier = OIDCVerifier(resolve_env_reference(settings.service.oidc_issuer_ref),
                settings.service.oidc_audience, jwks_url=settings.service.oidc_jwks_url)
    if settings.service.auth_mode == 'oidc':
        if (not isinstance(verifier, OIDCVerifier)
            or verifier.issuer != resolve_env_reference(settings.service.oidc_issuer_ref)
            or verifier.audience != settings.service.oidc_audience):
            raise ValueError('OIDC verifier must match configured issuer and audience')
    if settings.environment == 'production' and not isinstance(verifier, OIDCVerifier):
        raise ValueError('production requires OIDC verifier')

    from workbench_agent.runtime.streaming import RedisHints
    own_hints=event_hints is None
    event_hints=event_hints if event_hints is not None else RedisHints.configured(settings)

    @asynccontextmanager
    async def lifespan(app):
        # health is a read-only schema/PRAGMA check, with no external paid probes.
        app.state.storage_ready = await storage_ready(settings,backend)
        yield
        if own_hints and event_hints is not None: await event_hints.close()
        if own_backend:
            await backend.close()
        if telemetry is not None:
            from workbench_agent.observability import shutdown_telemetry
            await shutdown_telemetry(telemetry)

    app = FastAPI(lifespan=lifespan)
    app.state.backend, app.state.settings = backend, settings
    backend._mcp_connections = settings.service.mcp_connections
    app.state.identity = IdentityResolver(backend, verifier, connectors=settings.service.connector_identities)
    app.state.profiles, app.state.contexts, app.state.teams = ProfileService(backend), ContextResolver(backend), TeamService(backend, settings=settings)
    from workbench_agent.connectors.managed_model_secrets import configured_managed_model_secrets
    from workbench_agent.connectors.secrets import SecretReferenceAvailability
    app.state.managed_model_secrets = configured_managed_model_secrets(settings)
    app.state.runs = RunService(backend, secret_availability=secret_availability or SecretReferenceAvailability(app.state.managed_model_secrets))
    from workbench_agent.persistence.events import RunEventStore
    from workbench_agent.api import events
    app.state.events=RunEventStore(backend,event_hints)
    from workbench_agent.application.artifacts import ArtifactService
    from workbench_agent.api import artifacts, tool_bindings, tool_connections, evidence, skills, skill_library, repositories, uploads, knowledge, kb_internal
    from workbench_agent.knowledge.internal_auth import KBInternalAccess
    app.state.knowledge_access = KBInternalAccess(settings.knowledge, resolve_env_reference)
    app.state.artifacts=ArtifactService(backend,artifact_signer)
    from workbench_agent.api import ingress, schedules, actions, incidents
    from workbench_agent.application.actions import ActionService
    app.state.actions=ActionService(backend)
    app.include_router(actions.router)
    app.include_router(incidents.router)
    from workbench_agent.application.schedules import ScheduleService
    app.state.schedules=ScheduleService(backend)
    app.include_router(schedules.router)
    app.include_router(ingress.router)
    app.include_router(uploads.router)
    app.include_router(repositories.router)
    app.include_router(skills.router)
    app.include_router(skill_library.router)
    app.include_router(knowledge.router)
    app.include_router(kb_internal.router)
    app.include_router(evidence.router)
    app.include_router(tool_bindings.router)
    app.include_router(tool_connections.router)
    app.include_router(artifacts.router)
    app.include_router(events.router)
    from workbench_agent.api import feedback, usage
    app.include_router(feedback.router)
    app.include_router(usage.router)
    app.include_router(runs.router)
    app.include_router(threads.router)
    app.include_router(teams.router)
    from workbench_agent.api import graphs
    from workbench_agent.api import statistics
    app.include_router(graphs.router)
    app.include_router(statistics.router)
    app.include_router(agents.router)
    app.include_router(model_profiles.router)
    app.include_router(team_model_profiles.router)
    app.include_router(admin_jobs.router)

    from workbench_agent.api.frontend import router as web_router
    app.include_router(web_router)

    install_error_handlers(app)

    @app.get('/health/live')
    async def live():
        return {'live': True}

    @app.get('/health/ready')
    async def ready():
        healthy = await storage_ready(settings,backend)
        return JSONResponse({'ready': healthy}, status_code=200 if healthy else 503)

    from workbench_agent.api.admission import StorageAdmissionMiddleware
    app.add_middleware(StorageAdmissionMiddleware,backend=backend,settings=settings)
    from workbench_agent.observability import RequestObservabilityMiddleware
    app.add_middleware(RequestObservabilityMiddleware, backend=backend)

    return app
