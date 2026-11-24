"""Issue URLs only for durable available objects with current access."""

from typing import Protocol
from hashlib import sha256
from uuid import uuid4
import re
from urllib.parse import urlsplit
from workbench_agent.application.authorization import Authorization
from workbench_agent.application.errors import DomainError, not_found


class ObjectURLSigner(Protocol):
    async def sign(self, key: str, *, sha256: str, size: int, expires_in: int) -> str: ...


class ArtifactService:
    def __init__(self, backend, signer: ObjectURLSigner | None = None, *, objects=None):
        self.backend, self.signer, self.objects = backend, signer, objects

    async def _authorized(self, auth, artifact_id):
        async with self.backend.create_uow().begin_read() as uow:
            artifact = await self.backend.results.artifact(uow, auth.team_id, artifact_id)
            if artifact is None or artifact['run_id'] is None:
                raise not_found()
            run = await self.backend.runs.one(
                uow, 'runs', team_id=auth.team_id, run_id=artifact['run_id']
            )
            if run is None:
                raise not_found()
            await Authorization(self.backend).current(uow, auth, run['agent_id'])
            now = await self.backend.clock.now_ms(uow)
            if (
                artifact['status'] != 'available'
                or artifact['expires_at'] is not None
                and artifact['expires_at'] <= now
            ):
                raise DomainError('ARTIFACT_UNAVAILABLE', 409)
            upload = await self.backend.results.upload_scope(uow, auth.team_id, artifact_id)
            if upload is not None:
                current = await self.backend.runs.current_scope(uow, auth.team_id, run['run_id'])
                if upload['run_id'] != run['run_id'] or upload['scope_id'] != (
                    current['scope_id'] if current else None
                ):
                    raise not_found()
            refs = await self.backend.results.artifact_scopes(uow, auth.team_id, artifact_id)
            scope = await self.backend.runs.current_scope(uow, auth.team_id, run['run_id'])
            if any(
                r['run_id'] != run['run_id'] or scope is None or r['scope_id'] != scope['scope_id']
                for r in refs
            ):
                raise not_found()
            return dict(artifact)

    async def url(self, auth, artifact_id):
        artifact = await self._authorized(auth, artifact_id)
        if self.signer is None:
            raise DomainError('OBJECT_STORAGE_UNAVAILABLE', 503)
        # Network signing never holds a database transaction.
        url = await self.signer.sign(
            artifact['object_key'], sha256=artifact['sha256'], size=artifact['size'], expires_in=60
        )
        parsed = urlsplit(url)
        if (
            parsed.scheme != 'https'
            or not parsed.hostname
            or parsed.username
            or parsed.password
            or parsed.fragment
        ):
            raise DomainError('OBJECT_STORAGE_UNAVAILABLE', 503)
        current = await self._authorized(auth, artifact_id)
        if current != artifact:
            raise DomainError('ARTIFACT_CHANGED', 409)
        return {
            'url': url,
            'expires_in': 60,
            'sha256': artifact['sha256'],
            'size': artifact['size'],
        }

    async def begin_upload(self, auth, run_id, kind, digest, size):
        if (
            not isinstance(kind, str)
            or not re.fullmatch(r'[a-z][a-z0-9_]{0,31}', kind)
            or not isinstance(digest, str)
            or not re.fullmatch(r'[0-9a-f]{64}', digest)
            or type(size) is not int
            or not 0 <= size <= 20 * 1024 * 1024
        ):
            raise DomainError('INVALID_UPLOAD', 422)
        async with self.backend.create_uow().begin_write() as uow:
            run = await self.backend.runs.one(uow, 'runs', team_id=auth.team_id, run_id=run_id)
            if run is None:
                raise not_found()
            await Authorization(self.backend).current(uow, auth, run['agent_id'], 'operator')
            current = await self.backend.runs.current_scope(uow, auth.team_id, run_id)
            now = await self.backend.clock.now_ms(uow)
            artifact_id = str(uuid4())
            artifact = dict(
                team_id=auth.team_id,
                artifact_id=artifact_id,
                run_id=run_id,
                kind=kind,
                object_key='pending:' + artifact_id,
                sha256=digest,
                size=size,
                status='uploading',
                created_at=now,
                updated_at=now,
            )
            await self.backend.results.create_upload(
                uow,
                artifact,
                dict(
                    team_id=auth.team_id,
                    artifact_id=artifact_id,
                    run_id=run_id,
                    scope_id=current['scope_id'] if current else None,
                    created_at=now,
                ),
            )
            return artifact

    async def _upload(self, uow, auth, artifact_id):
        artifact = await self.backend.results.artifact(uow, auth.team_id, artifact_id)
        scope = await self.backend.results.upload_scope(uow, auth.team_id, artifact_id)
        if artifact is None or scope is None:
            raise not_found()
        run = await self.backend.runs.one(
            uow, 'runs', team_id=auth.team_id, run_id=artifact['run_id']
        )
        if run is None:
            raise not_found()
        await Authorization(self.backend).current(uow, auth, run['agent_id'], 'operator')
        current = await self.backend.runs.current_scope(uow, auth.team_id, run['run_id'])
        if scope['run_id'] != run['run_id'] or scope['scope_id'] != (
            current['scope_id'] if current else None
        ):
            raise not_found()
        return dict(artifact)

    async def complete(self, auth, artifact_id, data, *, validate_uow=None):
        async with self.backend.create_uow().begin_write() as uow:
            if validate_uow is not None:
                await validate_uow(uow)
            artifact = await self._upload(uow, auth, artifact_id)
        if (
            not isinstance(data, bytes)
            or len(data) != artifact['size']
            or sha256(data).hexdigest() != artifact['sha256']
        ):
            raise DomainError('UPLOAD_DIGEST_MISMATCH', 422)
        if artifact['status'] == 'available':
            return artifact
        if artifact['status'] != 'uploading':
            raise DomainError('ARTIFACT_UNAVAILABLE', 409)
        if self.objects is None:
            raise DomainError('OBJECT_STORAGE_UNAVAILABLE', 503)
        key = await self.objects.put_verified(data, artifact['sha256'])
        async with self.backend.create_uow().begin_write() as uow:
            if validate_uow is not None:
                await validate_uow(uow)
            current = await self._upload(uow, auth, artifact_id)
            if current['status'] == 'available':
                if current['object_key'] != key:
                    raise DomainError('ARTIFACT_CHANGED', 409)
                return current
            if current != artifact:
                raise DomainError('ARTIFACT_CHANGED', 409)
            now = await self.backend.clock.now_ms(uow)
            if not await self.backend.results.complete_upload(
                uow, auth.team_id, artifact_id, key, now
            ):
                raise DomainError('ARTIFACT_CHANGED', 409)
            return {**artifact, 'object_key': key, 'status': 'available', 'updated_at': now}
