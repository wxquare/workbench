"""Container process lifecycle; execution authorization is the caller's responsibility."""
import asyncio
from dataclasses import dataclass
import os
import signal
import re
from uuid import uuid4
from workbench_agent.runner.policy import build_container_args


def validate_docker_host(value):
    from urllib.parse import urlsplit
    if not isinstance(value,str): raise ValueError('invalid Docker host')
    parsed=urlsplit(value)
    if (parsed.scheme!='unix' or parsed.netloc or not parsed.path.startswith('/') or parsed.query or parsed.fragment
        or not re.fullmatch(r'/[a-zA-Z0-9_./-]+',parsed.path) or '..' in parsed.path.split('/')):
        raise ValueError('Docker host must be a local unix socket')
    return value


@dataclass(frozen=True)
class RunnerResult:
    status: str
    exit_code: int | None
    stdout: bytes
    stderr: bytes


class ContainerRunner:
    def __init__(self, docker_binary='docker', *, environment=None, cleanup_timeout=5,docker_host='unix:///var/run/docker.sock',identity_mode='docker_engine'):
        if not 0 < cleanup_timeout <= 5: raise ValueError('invalid cleanup timeout')
        if identity_mode not in {'docker_engine', 'podman_volume'}: raise ValueError('invalid runner identity mode')
        self.identity_mode = identity_mode
        self.cleanup_timeout = cleanup_timeout
        self.docker_binary = docker_binary
        safe={key:value for key,value in (environment or {}).items() if key!='PATH' and not key.startswith('DOCKER_')}
        self.environment = {**safe,'PATH':'/usr/local/bin:/opt/homebrew/bin:/usr/bin:/bin','DOCKER_HOST':validate_docker_host(docker_host)}

    async def require_daemon(self,expected):
        """Bind each operation to Docker's reported Engine ID, not a local alias."""
        if not isinstance(expected,str) or not re.fullmatch(r'[a-zA-Z0-9_.:-]{1,128}',expected):
            raise RuntimeError('runner daemon identity unavailable')
        if self.identity_mode == 'podman_volume':
            if not re.fullmatch(r'podman:[0-9a-f]{32}', expected): raise RuntimeError('runner daemon identity unavailable')
            actual_expected = 'dod-runner-identity-' + expected.removeprefix('podman:')
            arguments = ('volume', 'inspect', '--format', '{{.Name}}', actual_expected)
        else:
            if expected.startswith('podman:'): raise RuntimeError('runner daemon identity unavailable')
            actual_expected = expected
            arguments = ('info', '--format', '{{.ID}}')
        try:
            process=await asyncio.create_subprocess_exec(self.docker_binary,*arguments,
                stdout=asyncio.subprocess.PIPE,stderr=asyncio.subprocess.DEVNULL,
                env=self.environment,start_new_session=True)
        except OSError as error: raise RuntimeError('runner daemon identity unavailable') from error
        async def query():
            data=await process.stdout.read(257)
            if len(data)>256: raise ValueError('invalid daemon identity')
            if await process.wait()!=0: raise ValueError('daemon query failed')
            return data.decode('ascii').strip()
        try:
            actual=await asyncio.wait_for(query(),5)
        except (TimeoutError,ValueError,UnicodeError) as error:
            raise RuntimeError('runner daemon identity unavailable') from error
        finally:
            if process.returncode is None:
                try: os.killpg(process.pid,signal.SIGKILL)
                except ProcessLookupError: pass
                await process.wait()
        if not re.fullmatch(r'[a-zA-Z0-9_.:-]{1,128}',actual) or actual!=actual_expected:
            raise RuntimeError('runner daemon identity mismatch')
        return expected

    async def cleanup_resources(self,name,volume,*,daemon_id,remove_container=True,remove_volume=True,allow_container_absent=True,allow_volume_absent=True):
        if not re.fullmatch(r'dod-run-[0-9a-f]{32}',name) or not re.fullmatch(r'dod-task-[0-9a-f]{32}',volume):
            raise ValueError('invalid cleanup identity')
        await self.require_daemon(daemon_id)
        failures=[]
        async def remove(arguments,missing,allow_absent):
            await self.require_daemon(daemon_id)
            try:
                process=await asyncio.create_subprocess_exec(self.docker_binary,*arguments,
                    stdout=asyncio.subprocess.DEVNULL,stderr=asyncio.subprocess.PIPE,
                    env=self.environment,start_new_session=True)
            except OSError:
                failures.append('cleanup failed: '+str(arguments[-1]));return
            async def finish():
                error=await process.stderr.read(8193)
                if len(error)>8192: raise ValueError('cleanup response too large')
                await process.wait()
                return error.decode('utf-8','replace').strip()
            try: error=await asyncio.wait_for(finish(),self.cleanup_timeout)
            except (TimeoutError,ValueError):
                try: os.killpg(process.pid,signal.SIGKILL)
                except ProcessLookupError: pass
                await process.wait()
                failures.append('cleanup timed out or invalid: '+str(arguments[-1]));return
            await self.require_daemon(daemon_id)
            absent=error.endswith(missing+str(arguments[-1]))
            if process.returncode and not (allow_absent and absent):
                failures.append('cleanup failed: '+str(arguments[-1]))
        if remove_container and self.identity_mode == 'podman_volume':
            # Docker's Podman compatibility API can wait for the container stop
            # grace period on rm -f. Signal first, then let rm determine success.
            await self.require_daemon(daemon_id)
            try:
                process = await asyncio.create_subprocess_exec(self.docker_binary, 'kill', name,
                    stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
                    env=self.environment, start_new_session=True)
                try: await asyncio.wait_for(process.wait(), self.cleanup_timeout)
                except TimeoutError:
                    try: os.killpg(process.pid,signal.SIGKILL)
                    except ProcessLookupError: pass
                    await process.wait()
            except OSError:
                pass
            await self.require_daemon(daemon_id)
        if remove_container: await remove(('rm','-f',name),'No such container: ',allow_container_absent)
        if remove_volume: await remove(('volume','rm','-f',volume),'No such volume: ',allow_volume_absent)
        await self.require_daemon(daemon_id)
        if failures: raise RuntimeError('; '.join(failures))

    async def execute(self, request):
        await self.require_daemon(request.daemon_id)
        name = request.container_name or 'dod-run-' + uuid4().hex
        create = build_container_args(request, name)
        create[:2] = [self.docker_binary, 'create']
        current = None
        stdout, stderr = bytearray(), bytearray()
        status, code = 'unavailable', None
        created = False
        container_attempted = False
        volume_created = False
        volume_attempted = False
        async def stop_process():
            nonlocal current
            if current is not None and current.returncode is None:
                try: os.killpg(current.pid, signal.SIGKILL)
                except ProcessLookupError: pass
                await current.wait()
            current = None
        async def cleanup():
            await stop_process()
            await self.cleanup_resources(name,request.task_volume,daemon_id=request.daemon_id,remove_container=container_attempted,remove_volume=volume_attempted,
                allow_container_absent=created,allow_volume_absent=volume_created)
        async def collect(stream, target):
            while chunk := await stream.read(65536):
                remaining = request.output_bytes - len(stdout) - len(stderr)
                target.extend(chunk[:max(remaining, 0)])
                if len(chunk) > remaining:
                    raise OverflowError('container output exceeds limit')
        async def run():
            nonlocal current, code, created, container_attempted, volume_created, volume_attempted
            if request.lifecycle is not None: await request.lifecycle('volume_dispatch')
            await self.require_daemon(request.daemon_id)
            volume_attempted = True
            current = await asyncio.create_subprocess_exec(self.docker_binary, 'volume', 'create',
                '--driver', 'local', '--opt', 'type=tmpfs', '--opt', 'device=tmpfs', '--opt',
                'o=size='+str(request.output_bytes)+',uid=65534,gid=65534,mode=0700', request.task_volume,
                stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
                env=self.environment, start_new_session=True)
            if await current.wait(): raise RuntimeError('bounded task volume creation failed')
            await self.require_daemon(request.daemon_id)
            volume_created = True
            if request.lifecycle is not None: await request.lifecycle('volume_ack')
            if request.lifecycle is not None: await request.lifecycle('container_dispatch')
            await self.require_daemon(request.daemon_id)
            container_attempted = True
            current = await asyncio.create_subprocess_exec(*create, stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL, env=self.environment, start_new_session=True)
            if await current.wait():
                raise RuntimeError('container creation failed')
            await self.require_daemon(request.daemon_id)
            created = True
            if request.lifecycle is not None: await request.lifecycle('container_ack')
            await self.require_daemon(request.daemon_id)
            current = await asyncio.create_subprocess_exec(self.docker_binary, 'start', '-a', name,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
                env=self.environment, start_new_session=True)
            await asyncio.gather(collect(current.stdout, stdout), collect(current.stderr, stderr))
            code = await current.wait()
        try:
            try:
                await asyncio.wait_for(run(), request.timeout_seconds)
                status = 'completed'
            except TimeoutError:
                status = 'timeout'
            except OverflowError:
                status = 'output_limit'
        finally:
            cleanup_task=asyncio.create_task(cleanup())
            interrupted=False
            while True:
                try:
                    await asyncio.shield(cleanup_task)
                    break
                except asyncio.CancelledError:
                    if cleanup_task.done(): cleanup_task.result()
                    interrupted=True
            if interrupted: raise asyncio.CancelledError()
        return RunnerResult(status, code, bytes(stdout), bytes(stderr))
