"""Durable, bounded upload staging and fenced management jobs."""
import asyncio
from contextlib import asynccontextmanager
import fcntl
from dataclasses import asdict
from hashlib import sha256
import json
import os
from pathlib import Path
import re
import stat
from uuid import uuid4

from workbench_agent.application.artifacts import ArtifactService
from workbench_agent.application.authorization import Authorization
from workbench_agent.application.errors import not_found
from workbench_agent.application.errors import DomainError
from workbench_agent.application.idempotency import canonical, digest
from workbench_agent.application.profiles import ProfileService
from workbench_agent.domain.admin_jobs import AdminAuth, AdminJob
from workbench_agent.domain.identity import AuthContext
from workbench_agent.jobs.admin import AdminJobGuard
from workbench_agent.persistence.errors import StaleExecution

MAX_BYTES = 20 * 1024 * 1024

class UploadStaging:
    def __init__(self, root, *, max_bytes=256*1024*1024, max_files=1024):
        if max_bytes<0 or max_files<1: raise ValueError('invalid staging limits')
        self.max_bytes,self.max_files=max_bytes,max_files
        self.root = Path(root)
        self.root.mkdir(parents=True, mode=0o700, exist_ok=True)
        if self.root.is_symlink() or not self.root.is_dir(): raise ValueError('invalid staging root')

    def _dir(self):
        return os.open(self.root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)

    @asynccontextmanager
    async def locked(self, backend):
        directory=self._dir(); fd=None
        try:
            fd=os.open('.lock',os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW,0o600,dir_fd=directory)
            deadline=asyncio.get_running_loop().time()+5
            while True:
                try:
                    fcntl.flock(fd,fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    if asyncio.get_running_loop().time()>=deadline: raise DomainError('UPLOAD_STAGING_BUSY',503)
                    await asyncio.sleep(.05)
            # A staging directory is owned by exactly one database. This prevents
            # a different deployment from treating in-flight files as orphans.
            owner=sha256(str(backend.path.resolve()).encode()).hexdigest().encode()
            for name in os.listdir(directory):
                if re.fullmatch(r'\.owner-pending-[0-9a-f]{32}',name):
                    info=os.stat(name,dir_fd=directory,follow_symlinks=False)
                    if not stat.S_ISREG(info.st_mode): raise ValueError('invalid staging marker')
                    os.unlink(name,dir_fd=directory)
            try:
                marker=os.open('.owner',os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK,dir_fd=directory)
            except FileNotFoundError:
                temporary='.owner-pending-'+uuid4().hex
                out=os.open(temporary,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600,dir_fd=directory)
                try:
                    with os.fdopen(out,'wb') as file:
                        file.write(owner);file.flush();os.fsync(file.fileno())
                    os.link(temporary,'.owner',src_dir_fd=directory,dst_dir_fd=directory,follow_symlinks=False)
                    os.fsync(directory)
                finally:
                    os.unlink(temporary,dir_fd=directory)
                marker=os.open('.owner',os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK,dir_fd=directory)
            with os.fdopen(marker,'rb') as file:
                if not stat.S_ISREG(os.fstat(file.fileno()).st_mode) or file.read(65)!=owner:
                    raise ValueError('staging belongs to another database')
            yield
        finally:
            if fd is not None:
                fcntl.flock(fd,fcntl.LOCK_UN);os.close(fd)
            os.close(directory)

    def collect(self,active):
        directory=self._dir();count=0;total=0
        try:
            for name in os.listdir(directory):
                if name in ('.lock','.owner'): continue
                if not re.fullmatch(r'[0-9a-f]{32}',name): raise ValueError('unexpected staging entry')
                info=os.stat(name,dir_fd=directory,follow_symlinks=False)
                if not stat.S_ISREG(info.st_mode): raise ValueError('invalid staging entry')
                if name not in active:
                    os.unlink(name,dir_fd=directory)
                else:
                    count+=1;total+=info.st_size
            os.fsync(directory)
            return count,total
        finally: os.close(directory)

    def write(self, data):
        if not isinstance(data, bytes) or len(data) > MAX_BYTES: raise ValueError('invalid staged object')
        key=uuid4().hex; directory=self._dir(); fd=None
        created=False
        try:
            fd=os.open(key,os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,0o600,dir_fd=directory)
            created=True
            with os.fdopen(fd,'wb') as file:
                fd=None; file.write(data); file.flush(); os.fsync(file.fileno())
            os.fsync(directory)
        except BaseException:
            if created:
                try: os.unlink(key,dir_fd=directory)
                except FileNotFoundError: pass
            raise
        finally:
            if fd is not None: os.close(fd)
            os.close(directory)
        return key

    def read(self,key):
        if not re.fullmatch(r'[0-9a-f]{32}',key): raise ValueError('invalid staging key')
        directory=self._dir()
        try:
            fd=os.open(key,os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,dir_fd=directory)
            with os.fdopen(fd,'rb') as file:
                info=os.fstat(file.fileno())
                if not stat.S_ISREG(info.st_mode) or info.st_size>MAX_BYTES: raise ValueError('invalid staged object')
                return file.read(MAX_BYTES+1)
        finally: os.close(directory)

    def remove(self,key):
        if not re.fullmatch(r'[0-9a-f]{32}',key): raise ValueError('invalid staging key')
        directory=self._dir()
        try: os.unlink(key,dir_fd=directory)
        finally: os.close(directory)

class ArtifactUploadQueue:
    def __init__(self,backend,staging):
        self.backend,self.staging=backend,staging
        self.artifacts=ArtifactService(backend); self.profiles=ProfileService(backend)

    async def _receipt(self,uow,auth,artifact_id,key):
        artifact=await self.backend.results.artifact(uow,auth.team_id,artifact_id)
        if artifact is None or artifact['run_id'] is None: raise not_found()
        run=await self.backend.runs.one(uow,'runs',team_id=auth.team_id,run_id=artifact['run_id'])
        if run is None: raise not_found()
        await Authorization(self.backend).current(uow,auth,run['agent_id'],'operator')
        try:
            result=await self.profiles.idempotency.lookup(uow,auth,run['agent_id'],'artifact.upload',key,{'artifact_id':artifact_id})
        except DomainError as error:
            if error.code!='IDEMPOTENCY_CONFLICT': raise
            result=await self.profiles.idempotency.lookup(uow,auth,run['agent_id'],'artifact.upload',key,{'artifact_id':artifact_id,'sha256':artifact['sha256']})
        return artifact,run,result

    async def replay_info(self,auth,artifact_id,key):
        async with self.backend.create_uow().begin_read() as uow:
            artifact,_,result=await self._receipt(uow,auth,artifact_id,key)
            return (AdminJob(**result.body) if result else None,artifact['size'],artifact['sha256'])

    async def replay(self,auth,artifact_id,key):
        job,_,_=await self.replay_info(auth,artifact_id,key)
        return job

    @staticmethod
    def _check_replay(data,artifact):
        # None is an explicit bodyless lookup; bytes are always checked.
        if data is not None and (not isinstance(data,bytes) or len(data)!=artifact['size'] or sha256(data).hexdigest()!=artifact['sha256']):
            raise DomainError('IDEMPOTENCY_CONFLICT',409)

    async def _reap_locked(self):
        async with self.backend.create_uow().begin_read() as uow:
            active=await self.backend.admin_jobs.upload_stage_references(uow)
        return await asyncio.to_thread(self.staging.collect,active)

    async def reap(self):
        async with self.staging.locked(self.backend):
            return await self._reap_locked()

    async def submit(self,auth,artifact_id,data,*,key):
        async with self.backend.create_uow().begin_write() as uow:
            artifact,run,receipt=await self._receipt(uow,auth,artifact_id,key)
            if receipt:
                self._check_replay(data,artifact)
                return AdminJob(**receipt.body)
            artifact=await self.artifacts._upload(uow,auth,artifact_id)
        if not isinstance(data,bytes) or len(data)!=artifact['size'] or sha256(data).hexdigest()!=artifact['sha256']:
            raise DomainError('UPLOAD_DIGEST_MISMATCH',422)
        async with self.staging.locked(self.backend):
            # A competing submission may have committed while this one waited.
            async with self.backend.create_uow().begin_read() as uow:
                replay_artifact,_,receipt=await self._receipt(uow,auth,artifact_id,key)
                if receipt:
                    self._check_replay(data,replay_artifact)
                    return AdminJob(**receipt.body)
            count,total=await self._reap_locked()
            if count>=self.staging.max_files or total+len(data)>self.staging.max_bytes:
                raise DomainError('UPLOAD_STAGING_FULL',503)
            stage=await asyncio.to_thread(self.staging.write,data)
            try:
                async with self.backend.create_uow().begin_write() as uow:
                    _,_,receipt=await self._receipt(uow,auth,artifact_id,key)
                    if receipt:
                        result=receipt
                    else:
                        await self.artifacts._upload(uow,auth,artifact_id)
                        async def operation():
                            payload=dict(artifact_id=artifact_id,stage=stage,subject=auth.subject,identity_kind=auth.identity_kind,agent_scope=auth.agent_scope)
                            principal=AdminAuth(auth.team_id,run['agent_id'],auth.subject,True)
                            job=await self.backend.admin_jobs.submit(uow,principal,'artifact_upload',canonical(payload),digest({'artifact_id':artifact_id,'sha256':artifact['sha256']}),key)
                            return asdict(job)
                        result=await self.profiles.idempotency.execute(uow,auth,run['agent_id'],'artifact.upload',key,{'artifact_id':artifact_id},operation,status=202)
            except BaseException:
                # A failed commit may be uncertain. Reap only after a fresh read
                # proves no live Job refers to the file; otherwise preserve it.
                try: await self._reap_locked()
                except Exception: pass
                raise
            await self._reap_locked()
            return AdminJob(**result.body)

class ArtifactUploadWorker:
    def __init__(self,backend,artifacts,staging): self.backend,self.artifacts,self.staging=backend,artifacts,staging

    async def execute(self,claim):
        payload=json.loads(claim.payload)
        auth=AuthContext(team_id=claim.team_id,subject=payload['subject'],roles=(),identity_kind=payload['identity_kind'],agent_scope=payload['agent_scope'])
        async def validate(uow): await AdminJobGuard(self.backend.admin_jobs).validate(uow,claim)
        try:
            async with self.backend.create_uow().begin_write() as uow:
                await validate(uow)
                await self.artifacts._upload(uow,auth,payload['artifact_id'])
            data=await asyncio.to_thread(self.staging.read,payload['stage'])
            result=await self.artifacts.complete(auth,payload['artifact_id'],data,validate_uow=validate)
            return canonical({'artifact_id':result['artifact_id'],'sha256':result['sha256']})
        except Exception as error:
            if isinstance(error,StaleExecution): raise
            permanent=isinstance(error,DomainError) and error.status in (403,404,422)
            if permanent or claim.attempt>=3:
                async with self.backend.create_uow().begin_write() as uow:
                    await self.backend.admin_jobs.fail(uow,claim)
            raise


async def reap_periodically(queue,stop,*,interval=30):
    import logging
    while not stop.is_set():
        try: await queue.reap()
        except Exception: logging.getLogger(__name__).warning('upload staging recovery failed')
        try: await asyncio.wait_for(stop.wait(),timeout=interval)
        except TimeoutError: pass
