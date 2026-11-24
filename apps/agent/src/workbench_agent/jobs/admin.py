"""Durable management jobs; route-specific authorization supplies AdminAuth."""

import hashlib
import json
from typing import TYPE_CHECKING
from workbench_agent.domain.admin_jobs import AdminAuth, AdminJob, AdminJobView, ADMIN_JOB_KINDS

if TYPE_CHECKING:
    from workbench_agent.persistence.ports.backend import StorageBackend


class AdminJobService:
    def __init__(self, backend: 'StorageBackend'):
        self.backend = backend

    async def submit(self, auth: AdminAuth, kind: str, payload: dict, key: str) -> AdminJob:
        if not auth.can_manage:
            raise PermissionError('management permission required')
        if kind not in ADMIN_JOB_KINDS or not 1 <= len(key) <= 128:
            raise ValueError('invalid admin job kind or idempotency key')
        body = json.dumps(
            payload, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False
        )
        if len(body.encode('utf-8')) > 65536:
            raise ValueError('admin job input exceeds 64 KiB')
        digest = hashlib.sha256(body.encode('utf-8')).hexdigest()
        async with self.backend.create_uow().begin_write() as uow:
            return await self.backend.admin_jobs.submit(uow, auth, kind, body, digest, key)

    async def get(self, auth: AdminAuth, job_id: str) -> AdminJobView:
        if not auth.can_manage:
            raise PermissionError('management permission required')
        async with self.backend.create_uow().begin_read() as uow:
            job = await self.backend.admin_jobs.get(uow, auth, job_id)
        if job is None:
            raise LookupError('admin job not found')
        return job


class AdminJobGuard:
    """Fence management writes using their independent owner/revision lease."""

    def __init__(self, store):
        self.store = store

    async def validate(self, uow, claim):
        from workbench_agent.domain.admin_jobs import ClaimedAdminJob

        if not isinstance(claim, ClaimedAdminJob):
            raise TypeError('AdminJobGuard requires ClaimedAdminJob')
        await self.store.guard(uow, claim)
