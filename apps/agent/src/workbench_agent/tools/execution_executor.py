"""Guarded script execution using deployment-pinned execution bindings."""
import asyncio
from dataclasses import dataclass
from hashlib import sha256
from io import BytesIO
from pathlib import PurePosixPath
import re
import tarfile
from uuid import uuid4

from workbench_agent.application.errors import DomainError, not_found
from workbench_agent.application.idempotency import canonical, digest
from workbench_agent.application.repository_identity import snapshot_identity_hash
from workbench_agent.domain.profiles import VersionRef
from workbench_agent.domain.usage import ActualUsage, ReservationRequest
from workbench_agent.jobs.worker import Execution, Worker
from workbench_agent.persistence.errors import StaleExecution
from workbench_agent.runner.policy import RunnerRequest, verified_script_argv
from workbench_agent.runner.service import ContainerRunner
from workbench_agent.runner.cleanup import RunnerCleanup
from workbench_agent.runtime.budgets.service import BudgetService


@dataclass(frozen=True)
class ExecutionBinding:
    team_id: str
    agent_id: str
    snapshot_ref: VersionRef
    image: str
    snapshot_volume: str
    script: str
    script_sha256: str
    interpreter: str

    def __post_init__(self):
        parts=self.script.split('/') if isinstance(self.script,str) else []
        if not parts or any(p in ('','.','..') for p in parts) or PurePosixPath(self.script).is_absolute() or '\\' in self.script:
            raise ValueError('invalid script path')
        if not re.fullmatch(r'[0-9a-f]{64}',self.script_sha256) or self.interpreter not in ('python','python3'):
            raise ValueError('invalid script authorization')
        RunnerRequest(self.image,[self.interpreter,'/workspace/'+self.script],self.snapshot_volume,'dod-task-'+'0'*32)


_BEARER = re.compile(r'(?i)\bBearer\s+[A-Za-z0-9._~+/=-]+')
_SECRET = re.compile(r'(?i)\bsk-[A-Za-z0-9._~+/=-]+')
_ASSIGNMENT = re.compile(r"(?i)([\"']?(?:password|passwd|secret|token|api[_-]?key|authorization|cookie|session[_-]?key)[\"']?\s*[:=]\s*)(?:[\"'][^\"'\r\n]*[\"']|[^\s,;]+)")
_PEM_BEGIN = re.compile(r'-----BEGIN [^-\r\n]*PRIVATE KEY-----')
_PEM_END = re.compile(r'-----END [^-\r\n]*PRIVATE KEY-----')

def _safe_output(data):
    value=data.decode('utf-8','replace')
    # Scan linearly: an unterminated key redacts the remainder instead of
    # leaking partial key material or triggering quadratic regex matching.
    lines=[]; in_pem=False
    for line in value.splitlines(keepends=True):
        if in_pem:
            if _PEM_END.search(line): in_pem=False
            continue
        begin=_PEM_BEGIN.search(line)
        if begin:
            lines.append(line[:begin.start()]+'[REDACTED]\n')
            in_pem=not bool(_PEM_END.search(line,begin.end()))
        else: lines.append(line)
    value=''.join(lines)
    value=_BEARER.sub('[REDACTED]',value)
    value=_SECRET.sub('[REDACTED]',value)
    value=_ASSIGNMENT.sub(lambda match:match.group(1)+'[REDACTED]',value)
    encoded=value.encode('utf-8')
    return encoded[:8192].decode('utf-8','ignore'),len(encoded)>8192


async def cancel_runner(task):
    task.cancel()
    try: await task
    except asyncio.CancelledError: pass


class ExecutionExecutor:
    def __init__(self,backend,execution):
        owner=getattr(execution,'_tool_owner',None)
        if not isinstance(execution,Execution) or not isinstance(owner,Worker) or owner.backend is not backend or not owner._owns_tool_execution(execution):
            raise ValueError('current Worker execution required')
        self.backend,self.execution=backend,execution
        self.budgets=BudgetService(backend)

    async def receipt(self,invocation_id):
        import json
        execution=self.execution;guard=execution.guard
        if not execution._tool_owner._owns_tool_execution(execution): raise DomainError('STALE_EXECUTION',409)
        await execution.check_operation()
        permissions=execution.tool_permissions
        if permissions is None or not permissions.allows(execution.claim.context,'runner.execute',execution._expert_name): raise DomainError('FORBIDDEN',403)
        async with self.backend.create_uow().begin_write() as uow:
            await self.budgets._owned(uow,guard)
            row=await self.backend.tools.invocation(uow,guard.team_id,guard.run_id,invocation_id)
            if row is None: raise not_found()
            return json.loads(row['receipt_ref']) if row['receipt_ref'] else {'status':'unknown'}

    async def execute(self,alias,*,invocation_id):
        execution=self.execution; context=execution.claim.context; guard=execution.guard
        if not execution._tool_owner._owns_tool_execution(execution): raise DomainError('STALE_EXECUTION',409)
        await execution.check_operation()
        bindings=getattr(self.backend,'_runner_bindings',{})
        binding=bindings.get(alias) if isinstance(bindings,dict) else None
        objects=getattr(self.backend,'_snapshot_objects',None); runner=getattr(self.backend,'_runner',None)
        if not isinstance(binding,ExecutionBinding) or not isinstance(runner,ContainerRunner) or objects is None:
            raise DomainError('RUNNER_UNAVAILABLE',503)
        if (binding.team_id,binding.agent_id)!=(context.team_id,context.agent_id) or binding.snapshot_ref not in context.repositories:
            raise not_found()
        permissions=execution.tool_permissions
        if permissions is None or not permissions.allows(context,'runner.execute',execution._expert_name): raise DomainError('FORBIDDEN',403)
        async with self.backend.create_uow().begin_write() as uow:
            run,actual=await self.budgets._owned(uow,guard)
            if actual!=context: raise DomainError('STALE_EXECUTION',409)
            row=await self.backend.configuration.one(uow,'repo_snapshots',team_id=context.team_id,agent_id=context.agent_id,snapshot_id=binding.snapshot_ref.resource_id,status='ready')
            if not row or row['commit_sha']!=binding.snapshot_ref.version or snapshot_identity_hash(row)!=binding.snapshot_ref.content_hash: raise not_found()
            if await self.backend.tools.invocation(uow,guard.team_id,guard.run_id,invocation_id): raise DomainError('INVOCATION_ALREADY_STARTED',409)
            object_ref=row['storage_ref']
        archive=await asyncio.to_thread(objects.get,object_ref)
        try:
            with tarfile.open(fileobj=BytesIO(archive),mode='r:') as tar:
                manifest={}; total=0
                for member in tar.getmembers():
                    path=PurePosixPath(member.name)
                    if path.is_absolute() or '..' in path.parts or not (member.isfile() or member.isdir()): raise ValueError('unsafe snapshot')
                    if member.isfile():
                        name=path.as_posix()
                        if name in manifest or len(manifest)>=10000: raise ValueError('invalid snapshot')
                        total+=member.size
                        if total>20*1024*1024: raise ValueError('snapshot too large')
                        content=tar.extractfile(member).read()
                        if len(content)!=member.size: raise ValueError('invalid snapshot')
                        manifest[name]=sha256(content).hexdigest()
                if manifest.get(binding.script)!=binding.script_sha256: raise ValueError('unapproved script')
        except (KeyError,tarfile.TarError,AttributeError,ValueError) as error: raise DomainError('RUNNER_SCRIPT_UNAVAILABLE',409) from error
        try: verified_argv=verified_script_argv(binding.interpreter,'/workspace/'+binding.script,binding.script_sha256,root='/workspace',manifest=manifest)
        except ValueError as error: raise DomainError('RUNNER_SCRIPT_UNAVAILABLE',409) from error
        await execution.check_operation()
        if not execution._tool_owner._owns_tool_execution(execution) or not permissions.allows(context,'runner.execute',execution._expert_name): raise DomainError('FORBIDDEN',403)
        attempt=str(uuid4())
        container_name='dod-run-'+uuid4().hex;task_volume='dod-task-'+uuid4().hex
        daemon_id=getattr(self.backend,'_runner_daemon_id',None)
        try:
            cleanup=RunnerCleanup(self.backend,runner,daemon_id)
            await runner.require_daemon(daemon_id)
        except (ValueError,RuntimeError) as error: raise DomainError('RUNNER_UNAVAILABLE',503) from error
        async with self.backend.create_uow().begin_write() as uow:
            run,actual=await self.budgets._owned(uow,guard)
            if actual!=context: raise DomainError('STALE_EXECUTION',409)
            if await self.backend.tools.invocation(uow,guard.team_id,guard.run_id,invocation_id): raise DomainError('INVOCATION_ALREADY_STARTED',409)
            await self.budgets.reserve(guard,ReservationRequest(invocation_id=attempt,kind='tool',tool_attempts=1),uow=uow)
            permit=await self.budgets.mark_sent(guard,attempt,uow=uow)
            if not permit.dispatch_allowed: raise DomainError('INVOCATION_ALREADY_STARTED',409)
            now=await self.budgets._dispatch_deadline(uow,guard,run)
            from workbench_agent.domain.transitions import active_elapsed
            import json
            limits=json.loads(run['budgets'])
            remaining=min(run['wall_deadline_at']-now,limits['wall_seconds']*1000-active_elapsed(run,now,await self.backend.runs.active_until(uow,guard.team_id,guard.run_id)))
            if remaining<1000: raise DomainError('BUDGET_EXCEEDED',409)
            await self.backend.tools.insert_invocation(uow,dict(team_id=guard.team_id,invocation_id=attempt,run_id=guard.run_id,logical_call_id=invocation_id,tool_version=binding.snapshot_ref.resource_id,args_hash=digest({'alias':alias,'script':binding.script_sha256}),attempt=1,status='possibly_sent',receipt_ref=None,created_at=now,updated_at=now))
            await cleanup.register(uow,guard,attempt,container_name,task_volume)
        async def lifecycle(stage):
            async with self.backend.create_uow().begin_write() as uow:
                await cleanup.step(uow,guard,attempt,stage)
        request=RunnerRequest(binding.image,verified_argv,binding.snapshot_volume,task_volume,timeout_seconds=min(60,remaining//1000),container_name=container_name,daemon_id=daemon_id,lifecycle=lifecycle)
        running=asyncio.create_task(runner.execute(request)); cancelled=asyncio.create_task(execution.cancelled.wait())
        try:
            done,_=await asyncio.wait((running,cancelled),return_when=asyncio.FIRST_COMPLETED)
            if cancelled in done:
                await cancel_runner(running)
                raise asyncio.CancelledError()
            result=await running
            # The adapter returns only after both resources were removed. The
            # original owner may mark the obligation only while still fenced.
            async with self.backend.create_uow().begin_write() as uow:
                await cleanup.mark_clean(uow,guard,attempt)
        finally:
            cancelled.cancel()
            if not running.done(): await cancel_runner(running)
            elif not running.cancelled() and running.exception() is not None: raise running.exception()
            try:
                async with self.backend.create_uow().begin_write() as uow:
                    await self.budgets.settle(guard,attempt,ActualUsage(input_tokens=0,output_tokens=0),uow=uow)
            except (StaleExecution,DomainError) as error:
                if isinstance(error,DomainError) and error.code not in ('FORBIDDEN','NOT_FOUND'): raise
                async with self.backend.create_uow().begin_write() as uow:
                    await self.budgets.quarantine(guard,attempt,ActualUsage(input_tokens=0,output_tokens=0),uow=uow)
                raise
        if not execution._tool_owner._owns_tool_execution(execution) or not permissions.allows(context,'runner.execute',execution._expert_name):
            raise DomainError('FORBIDDEN',403)
        if context.identity_kind=='connector':
            if execution.connector_policy is None: raise DomainError('FORBIDDEN',403)
            await execution.connector_policy.require_current(context)
        terminal = ('succeeded' if result.exit_code==0 else 'failed') if result.status=='completed' else result.status
        if terminal not in ('succeeded','failed','timeout','output_limit'): raise DomainError('RUNNER_RESULT_UNKNOWN',409)
        stdout,stdout_truncated=_safe_output(result.stdout)
        stderr,stderr_truncated=_safe_output(result.stderr)
        summary={'status':terminal,'exit_code':result.exit_code,
            'stdout':stdout,'stdout_truncated':stdout_truncated,
            'stderr':stderr,'stderr_truncated':stderr_truncated,
            'stdout_sha256':sha256(result.stdout).hexdigest(),'stdout_bytes':len(result.stdout),
            'stderr_sha256':sha256(result.stderr).hexdigest(),'stderr_bytes':len(result.stderr)}
        async with self.backend.create_uow().begin_write() as uow:
            await self.budgets._owned(uow,guard)
            await self.backend.lease_guard.require(uow,guard,{'running','finalizing'})
            now=await self.backend.clock.now_ms(uow)
            if not await self.backend.tools.complete_invocation(uow,guard.team_id,guard.run_id,attempt,terminal,canonical(summary),now):
                raise DomainError('INVOCATION_ALREADY_FINISHED',409)
        # Persist a known timeout even when the wall deadline has just elapsed;
        # the public result still cannot be returned after the deadline.
        await execution.check_operation()
        return result
