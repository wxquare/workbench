"""Queue seed imports; validate in an authorized, leased management worker."""
from dataclasses import asdict
import asyncio
from io import BytesIO
from hashlib import sha256
import json
import os
import re
import stat
import zipfile
from workbench_agent.application.artifacts import ArtifactService
from workbench_agent.application.authorization import Authorization
from workbench_agent.application.errors import DomainError
from workbench_agent.application.idempotency import canonical, digest
from workbench_agent.application.profiles import ProfileService
from workbench_agent.domain.admin_jobs import AdminAuth, AdminJob
from workbench_agent.domain.identity import AuthContext
from workbench_agent.jobs.admin import AdminJobGuard
from workbench_agent.skill_management.validator import MAX_ARCHIVE_BYTES, MAX_FILE_BYTES, MAX_FILES, validate_bundle

_NAME = re.compile(r'^[a-z0-9]+(?:-[a-z0-9]+)*$')

class SkillImporter:
    def __init__(self, backend):
        self.backend = backend
        self.profiles = ProfileService(backend)

    async def import_seed(self, auth, agent_id, name, *, key):
        return await self._submit(auth, agent_id, name, {'source': 'seed'}, key)

    async def import_git(self, auth, agent_id, name, repository, commit, *, key):
        if not _NAME.fullmatch(repository) or not re.fullmatch(r'(?:[0-9a-f]{40}|[0-9a-f]{64})', commit):
            raise DomainError('INVALID_GIT_REF', 422)
        return await self._submit(auth, agent_id, name, {'source': 'git', 'repository': repository, 'commit': commit}, key)

    async def import_artifact(self, auth, agent_id, name, artifact_id, *, key):
        return await self._submit(auth, agent_id, name, {'source': 'artifact', 'artifact_id': artifact_id}, key)

    async def _submit(self, auth, agent_id, name, source, key):
        if not _NAME.fullmatch(name) or len(name) > 128:
            raise DomainError('INVALID_SKILL_NAME', 422)
        async with self.backend.create_uow().begin_write() as uow:
            await self.profiles.agent(uow, auth, agent_id, 'admin')
            async def operation():
                if source['source'] == 'artifact':
                    await _authorized_artifact(self.backend, auth, agent_id, source['artifact_id'])
                payload = {'name': name, **source, 'subject': auth.subject, 'identity_kind': auth.identity_kind,
                    'agent_scope': auth.agent_scope}
                principal = AdminAuth(auth.team_id, agent_id, auth.subject, True)
                job = await self.backend.admin_jobs.submit(uow, principal, 'skill_import', canonical(payload), digest({'name': name, **source}), key)
                return asdict(job)
            result = await self.profiles.idempotency.execute(uow, auth, agent_id, 'skill.import', key, {'name': name, **source}, operation, status=202)
            return AdminJob(**result.body)


async def _authorized_artifact(backend, auth, agent_id, artifact_id):
    artifact = await ArtifactService(backend)._authorized(auth, artifact_id)
    async with backend.create_uow().begin_read() as uow:
        run = await backend.runs.one(uow, 'runs', team_id=auth.team_id, run_id=artifact['run_id'])
        if not run or run['agent_id'] != agent_id:
            from workbench_agent.application.errors import not_found
            raise not_found()
    return artifact


def _read_seed(root, name):
    # Directory descriptors and NOFOLLOW prevent path replacement/symlink traversal.
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    descriptors = []
    try:
        directory = os.open(root, flags); descriptors.append(directory)
        directory = os.open('skills', flags, dir_fd=directory); descriptors.append(directory)
        directory = os.open(name, flags, dir_fd=directory); descriptors.append(directory)
        output = BytesIO(); total = 0; count = 0
        with zipfile.ZipFile(output, 'w', compression=zipfile.ZIP_STORED) as archive:
            def walk(fd, prefix):
                nonlocal total, count
                for entry in sorted(os.listdir(fd)):
                    info = os.stat(entry, dir_fd=fd, follow_symlinks=False)
                    path = prefix + '/' + entry
                    if stat.S_ISDIR(info.st_mode):
                        child = os.open(entry, flags, dir_fd=fd)
                        try: walk(child, path)
                        finally: os.close(child)
                    elif stat.S_ISREG(info.st_mode):
                        count += 1
                        if count > MAX_FILES or info.st_size > MAX_FILE_BYTES: raise ValueError('seed exceeds limits')
                        filefd = os.open(entry, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=fd)
                        with os.fdopen(filefd, 'rb') as file:
                            current = os.fstat(file.fileno())
                            if not stat.S_ISREG(current.st_mode): raise ValueError('invalid seed resource')
                            data = file.read(MAX_FILE_BYTES + 1)
                        total += len(data)
                        if len(data) > MAX_FILE_BYTES or total > MAX_ARCHIVE_BYTES: raise ValueError('seed exceeds limits')
                        archive.writestr(path, data)
                    else:
                        raise ValueError('seed contains link or special file')
            walk(directory, name)
        return output.getvalue()
    finally:
        for fd in reversed(descriptors): os.close(fd)


async def _git_output(repository, *args, limit=MAX_ARCHIVE_BYTES):
    environment = dict(os.environ, GIT_CONFIG_NOSYSTEM='1', GIT_CONFIG_GLOBAL='/dev/null', GIT_TERMINAL_PROMPT='0')
    process = await asyncio.create_subprocess_exec('git', '-C', str(repository), *args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL, env=environment)
    async def read():
        chunks = []; total = 0
        while True:
            chunk = await process.stdout.read(min(65536, limit + 1 - total))
            if not chunk: break
            total += len(chunk)
            if total > limit: raise ValueError('git archive exceeds limit')
            chunks.append(chunk)
        if await process.wait() != 0: raise ValueError('git archive unavailable')
        return b''.join(chunks)
    try:
        return await asyncio.wait_for(read(), 20)
    finally:
        if process.returncode is None:
            process.kill()
            await process.wait()

async def _read_git(repository, commit, name):
    if not os.path.isabs(repository) or not os.path.isdir(repository): raise ValueError('invalid git repository')
    if (await _git_output(repository, 'cat-file', '-t', commit, limit=64)).strip() != b'commit':
        raise ValueError('git ref is not a commit')
    raw = await _git_output(repository, 'archive', '--format=zip', commit, '--', 'skills/' + name)
    output = BytesIO()
    try:
        with zipfile.ZipFile(BytesIO(raw)) as source, zipfile.ZipFile(output, 'w') as target:
            entries = source.infolist()
            if len(entries) > MAX_FILES or sum(entry.file_size for entry in entries) > MAX_ARCHIVE_BYTES or any(entry.file_size > MAX_FILE_BYTES for entry in entries):
                raise ValueError('git archive exceeds resource limits')
            for entry in entries:
                if not entry.filename.startswith('skills/' + name + '/'):
                    if entry.filename in ('skills/', 'skills/' + name + '/'): continue
                    raise ValueError('invalid git archive path')
                entry.filename = entry.filename[len('skills/'): ]
                target.writestr(entry, source.read(entry))
    except zipfile.BadZipFile as exc:
        raise ValueError('invalid git archive') from exc
    return output.getvalue()

class SkillImportWorker:
    def __init__(self, backend, objects, asset_root, *, git_repositories=None):
        self.backend, self.objects, self.asset_root = backend, objects, asset_root
        self.git_repositories = dict(git_repositories or {})

    async def _check(self, claim, auth):
        async with self.backend.create_uow().begin_write() as uow:
            await AdminJobGuard(self.backend.admin_jobs).validate(uow, claim)
            await Authorization(self.backend).current(uow, auth, claim.agent_id, 'admin')

    async def execute(self, claim):
        payload = json.loads(claim.payload)
        name = payload['name']
        if not _NAME.fullmatch(name): raise ValueError('invalid skill name')
        auth = AuthContext(team_id=claim.team_id, subject=payload['subject'], roles=(),
            identity_kind=payload['identity_kind'], agent_scope=payload['agent_scope'])
        await self._check(claim, auth)
        if payload.get('source', 'seed') == 'seed':
            raw = _read_seed(self.asset_root, name)
        elif payload['source'] == 'git':
            repository = self.git_repositories.get(payload['repository'])
            if repository is None or not re.fullmatch(r'(?:[0-9a-f]{40}|[0-9a-f]{64})', payload['commit']):
                raise ValueError('git repository is not allowed')
            raw = await _read_git(repository, payload['commit'], name)
        elif payload['source'] == 'artifact':
            artifact = await _authorized_artifact(self.backend, auth, claim.agent_id, payload['artifact_id'])
            raw = self.objects.get(artifact['object_key'])
            if len(raw) != artifact['size'] or sha256(raw).hexdigest() != artifact['sha256']:
                raise ValueError('artifact hash or size mismatch')
            if await _authorized_artifact(self.backend, auth, claim.agent_id, payload['artifact_id']) != artifact:
                raise ValueError('artifact changed during import')
        else:
            raise ValueError('unsupported import source')
        bundle = validate_bundle(raw, name)
        from workbench_agent.skill_management.manifest import write_manifest
        ref = write_manifest(self.objects, bundle)
        await self._check(claim, auth)
        return canonical({'name': name, 'description': bundle.description, 'hash': bundle.bundle_hash, 'bundle_ref': ref})
