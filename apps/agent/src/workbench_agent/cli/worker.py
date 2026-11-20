"""Worker composition root with the explicit M1 deterministic foundation graph."""
from workbench_agent.persistence.readiness import storage_ready
import asyncio
import signal
from workbench_agent.jobs.worker import Worker, DeploymentConnectorPolicy
from workbench_agent.runtime.graphs.registry import foundation_worker_registry
from workbench_agent.persistence.backends.sqlite import SQLiteBackend

async def run_worker(settings,*,once=False,registry=None):
    from workbench_agent.tools.permissions import DeploymentToolPermissions
    backend=SQLiteBackend(settings.storage.database_url.get_secret_value(),tool_permissions=DeploymentToolPermissions(lambda:settings.service.tool_permissions))
    from workbench_agent.tools.mcp import configured_connection_policy
    backend._mcp_connection_policy = configured_connection_policy(lambda: settings.service.mcp_connections)
    backend._mcp_connections = settings.service.mcp_connections
    telemetry = None
    if settings.observability.tracing.enabled:
        from workbench_agent.observability import Telemetry
        telemetry = Telemetry(settings.observability, 'worker', settings.environment)
        backend._telemetry = telemetry
    staging_reaper=None
    runner_reaper=None
    if registry is None:
        registry=foundation_worker_registry(backend, supported_graph_ids=settings.worker.supported_graph_ids)
        if settings.storage.skill_object_root is not None:
            from workbench_agent.skill_management.importer import SkillImportWorker
            from workbench_agent.skill_management.objects import LocalSkillObjects
            backend._skill_objects=LocalSkillObjects(settings.storage.skill_object_root)
            registry.register_admin('skill_import', SkillImportWorker(backend, LocalSkillObjects(settings.storage.skill_object_root), settings.asset_root, git_repositories=settings.storage.skill_git_repositories).execute)
            from workbench_agent.execution.repositories import RepositorySyncWorker
            sources = {(entry.team_id, entry.agent_id, entry.repo_id): entry.path for entry in settings.storage.repository_sources}
            if len(sources) != len(settings.storage.repository_sources):
                raise ValueError('duplicate repository source binding')
            registry.register_admin('repo_sync', RepositorySyncWorker(backend, LocalSkillObjects(settings.storage.skill_object_root), sources).execute)
            backend._snapshot_objects=LocalSkillObjects(settings.storage.skill_object_root)
            if settings.storage.runner_bindings:
                from workbench_agent.domain.profiles import VersionRef
                from workbench_agent.tools.execution_executor import ExecutionBinding
                from workbench_agent.runner.service import ContainerRunner
                values = {}
                for item in settings.storage.runner_bindings:
                    if item.alias in values: raise ValueError('duplicate runner alias')
                    values[item.alias] = ExecutionBinding(item.team_id, item.agent_id,
                        VersionRef(resource_id=item.snapshot_id, version=item.commit, content_hash=item.snapshot_hash),
                        item.image, item.snapshot_volume, item.script, item.script_sha256, item.interpreter)
                backend._runner_bindings=values
                backend._snapshot_objects=LocalSkillObjects(settings.storage.skill_object_root)
        if settings.storage.runner_daemon_id is not None:
            from workbench_agent.runner.service import ContainerRunner
            from workbench_agent.runner.cleanup import RunnerCleanup
            backend._runner=ContainerRunner(str(settings.storage.docker_binary) if settings.storage.docker_binary else 'docker',docker_host=settings.storage.docker_host,identity_mode=settings.storage.runner_identity_mode)
            backend._runner_daemon_id=settings.storage.runner_daemon_id
            runner_reaper=RunnerCleanup(backend,backend._runner,settings.storage.runner_daemon_id)
        if settings.storage.s3 is not None and settings.storage.upload_staging_root is not None:
            from workbench_agent.application.artifacts import ArtifactService
            from workbench_agent.persistence.objects.s3 import configured_s3
            from workbench_agent.execution.uploads import ArtifactUploadWorker, ArtifactUploadQueue, UploadStaging
            objects = configured_s3(settings.storage.s3, environment=settings.environment)
            staging = UploadStaging(settings.storage.upload_staging_root)
            staging_reaper=ArtifactUploadQueue(backend,staging)
            registry.register_admin('artifact_upload', ArtifactUploadWorker(backend, ArtifactService(backend, objects=objects), staging).execute)
        from workbench_agent.runtime.models.deepseek import ModelProbe
        from workbench_agent.runtime.models.factory import ModelFactory,ModelCredentialPolicy
        from workbench_agent.connectors.secrets import SecretProvider
        from workbench_agent.connectors.managed_model_secrets import configured_managed_model_secrets
        managed_secrets = configured_managed_model_secrets(settings)
        registry.register_admin('model_probe',ModelProbe(backend,ModelFactory(ModelCredentialPolicy(settings.service.model_credential_bindings),managed_secrets),SecretProvider()).execute)
        from workbench_agent.tools.mcp import configured_adapter
        from workbench_agent.tools.probe import ToolProbe
        probes={(b.team_id,b.agent_id,b.server,b.credential_scope,name):args for b in settings.service.mcp_connections for name,args in b.probe_requests.items()}
        registry.register_admin('tool_probe',ToolProbe(backend,configured_adapter(settings.service.mcp_connections,SecretProvider()),probe_requests=probes).execute)
        from workbench_agent.tools.management import ToolDiscovery
        registry.register_admin(
            'tool_discovery',
            ToolDiscovery(
                backend,
                configured_adapter(settings.service.mcp_connections, SecretProvider()),
                settings.service.mcp_connections,
            ).execute,
        )
        if settings.service.model_credential_bindings or managed_secrets:
            from workbench_agent.runtime.models.invoker import ModelInvoker
            model_factory=ModelFactory(ModelCredentialPolicy(settings.service.model_credential_bindings),managed_secrets)
            backend._code_factory=lambda execution: ModelInvoker(backend,model_factory,SecretProvider(),execution=execution)
        if settings.service.mcp_connections:
            from workbench_agent.tools.policy import PolicyToolExecutor
            adapter=configured_adapter(settings.service.mcp_connections,SecretProvider())
            backend._rca_metric_template=settings.service.rca_metric_template
            backend._baseline_factory=lambda execution: PolicyToolExecutor(backend,adapter,execution=execution)
        if settings.service.mcp_connections and (settings.service.model_credential_bindings or managed_secrets):
            from workbench_agent.runtime.models.invoker import ModelInvoker
            from workbench_agent.tools.policy import PolicyToolExecutor
            adapter=configured_adapter(settings.service.mcp_connections,SecretProvider())
            model_factory=ModelFactory(ModelCredentialPolicy(settings.service.model_credential_bindings),managed_secrets)
            def diagnosis_factory(execution):
                return (ModelInvoker(backend,model_factory,SecretProvider(),execution=execution),
                        PolicyToolExecutor(backend,adapter,execution=execution))
            backend._diagnosis_factory=diagnosis_factory
    worker=Worker(backend,'worker-1',registry,
        connector_policy=DeploymentConnectorPolicy(lambda:settings.service.connector_identities))
    loop=asyncio.get_running_loop()
    signals=[]
    reaper_stop=asyncio.Event()
    reaper_task=None
    runner_reaper_task=None
    try:
        if not await storage_ready(settings,backend):
            return {'ready':False,'status':'storage_unavailable'}
        if staging_reaper is not None:
            from workbench_agent.execution.uploads import reap_periodically
            reaper_task=asyncio.create_task(reap_periodically(staging_reaper,reaper_stop))
        if runner_reaper is not None:
            from workbench_agent.runner.cleanup import recover_periodically
            runner_reaper_task=asyncio.create_task(recover_periodically(runner_reaper,reaper_stop))
        if once:
            status=await worker.run_once()
            if status in {'idle','capability_blocked'} and worker.registry.capabilities().admin_kinds:
                status=await worker.run_admin_once()
            return {'ready':True,'status':status}
        for signum in (signal.SIGTERM,signal.SIGINT):
            loop.add_signal_handler(signum,worker.stop)
            signals.append(signum)
        await worker.run()
        return {'ready':True,'status':'stopped'}
    finally:
        reaper_stop.set()
        if runner_reaper_task is not None:
            await runner_reaper_task
            try: await runner_reaper.recover_once()
            except Exception:
                import logging
                logging.getLogger(__name__).warning('runner cleanup recovery failed')
        if reaper_task is not None:
            await reaper_task
            # Include any Job that finished since the last periodic scan.
            try: await staging_reaper.reap()
            except Exception:
                import logging
                logging.getLogger(__name__).warning('upload staging recovery failed')
        for signum in signals:
            loop.remove_signal_handler(signum)
        await backend.close()
        if telemetry is not None:
            from workbench_agent.observability import shutdown_telemetry
            await shutdown_telemetry(telemetry)
