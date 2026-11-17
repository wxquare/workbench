"""Readiness-only startup and bounded signal-driven M1 recovery scheduling."""
from workbench_agent.persistence.readiness import storage_ready
import asyncio
import signal
from dataclasses import asdict
from workbench_agent.jobs.scheduler import Scheduler
from workbench_agent.jobs.worker import DeploymentConnectorPolicy
from workbench_agent.persistence.backends.sqlite import SQLiteBackend
from workbench_agent.runtime.graphs.registry import foundation_worker_registry

async def run_scheduler(settings,*,once=False):
    from workbench_agent.tools.permissions import DeploymentToolPermissions
    backend=SQLiteBackend(settings.storage.database_url.get_secret_value(),
        tool_permissions=DeploymentToolPermissions(lambda:settings.service.tool_permissions))
    telemetry = None
    if settings.observability.tracing.enabled:
        from workbench_agent.observability import Telemetry
        telemetry = Telemetry(settings.observability, 'scheduler', settings.environment)
        backend._telemetry = telemetry
    scheduler=Scheduler(backend,foundation_worker_registry(backend),connector_policy=DeploymentConnectorPolicy(lambda:settings.service.connector_identities))
    loop=asyncio.get_running_loop();signals=[]
    try:
        if not await storage_ready(settings,backend): return {'ready':False,'status':'storage_unavailable'}
        if once: return {'ready':True,**asdict(await scheduler.tick())}
        for signum in (signal.SIGTERM,signal.SIGINT):
            loop.add_signal_handler(signum,scheduler.stop);signals.append(signum)
        await scheduler.run()
        return {'ready':True,'status':'stopped'}
    finally:
        for signum in signals: loop.remove_signal_handler(signum)
        await backend.close()
        if telemetry is not None:
            from workbench_agent.observability import shutdown_telemetry
            await shutdown_telemetry(telemetry)
