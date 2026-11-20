"""Admin-only projections of deployment-owned MCP connections."""

from __future__ import annotations

import json

from workbench_agent.application.authorization import Authorization
from workbench_agent.application.errors import DomainError, not_found
from workbench_agent.application.idempotency import Idempotency, canonical, digest
from workbench_agent.domain.admin_jobs import AdminAuth, AdminJob, ClaimedAdminJob
from workbench_agent.jobs.admin import AdminJobGuard


class ToolManagement:
    def __init__(self, backend, deployments):
        self.backend, self.deployments = backend, tuple(deployments)

    async def _admin(self, uow, auth, agent_id):
        if auth.agent_scope is not None or auth.identity_kind == 'connector':
            raise not_found()
        await Authorization(self.backend).current(uow, auth, agent_id, 'admin')

    def _connections(self, auth, agent_id):
        return [
            item for item in self.deployments
            if item.team_id == auth.team_id and item.agent_id == agent_id
        ]

    async def connections(self, auth, agent_id):
        async with self.backend.create_uow().begin_read() as uow:
            await self._admin(uow, auth, agent_id)
        return {
            'items': [
                {
                    'server': item.server,
                    'credential_scope': item.credential_scope,
                    'read_tools': list(item.read_tools),
                    'status': 'configured',
                }
                for item in self._connections(auth, agent_id)
            ],
            'next_cursor': None,
        }

    async def enqueue_discovery(self, auth, agent_id, *, server, credential_scope, key):
        async with self.backend.create_uow().begin_write() as uow:
            await self._admin(uow, auth, agent_id)
            matches = [
                item for item in self._connections(auth, agent_id)
                if item.server == server
                and item.credential_scope == credential_scope
            ]
            if len(matches) != 1:
                raise not_found()
            async def operation():
                payload = {
                    'server': server,
                    'credential_scope': credential_scope,
                    'subject': auth.subject,
                    'identity_kind': auth.identity_kind,
                    'agent_scope': auth.agent_scope,
                }
                principal = AdminAuth(
                    auth.team_id, agent_id, auth.subject, True
                )
                job = await self.backend.admin_jobs.submit(
                    uow,
                    principal,
                    'tool_discovery',
                    canonical(payload),
                    digest(payload),
                    key,
                )
                return {
                    'job_id': job.job_id,
                    'team_id': job.team_id,
                    'agent_id': job.agent_id,
                    'kind': job.kind,
                    'status': job.status,
                    'result_ref': job.result_ref,
                }
            result = await Idempotency(self.backend).execute(
                uow,
                auth,
                agent_id,
                'tool.discovery',
                key,
                {'server': server, 'credential_scope': credential_scope},
                operation,
                status=202,
            )
            return AdminJob(**result.body)

    async def list_bindings(self, auth, agent_id, *, limit=50, after=None):
        if not 1 <= limit <= 100:
            raise DomainError('INVALID_REQUEST', 422)
        async with self.backend.create_uow().begin_read() as uow:
            await self._admin(uow, auth, agent_id)
            drafts = await self.backend.configuration.rows(
                uow,
                'tool_binding_drafts',
                team_id=auth.team_id,
                agent_id=agent_id,
            )
            versions = await self.backend.configuration.rows(
                uow,
                'tool_bindings',
                team_id=auth.team_id,
                agent_id=agent_id,
            )
            aliases = sorted({row['alias'] for row in (*drafts, *versions)})
            if after:
                aliases = [alias for alias in aliases if alias > after]
            page = aliases[:limit + 1]
            items = []
            for alias in page[:limit]:
                matching = sorted(
                    (row for row in versions if row['alias'] == alias),
                    key=lambda row: row['version'],
                    reverse=True,
                )
                draft = next(
                    (row for row in drafts if row['alias'] == alias), None
                )
                items.append(
                    {
                        'alias': alias,
                        'draft_revision': draft['revision'] if draft else None,
                        'latest_ref': (
                            {
                                'resource_id': matching[0]['binding_id'],
                                'version': str(matching[0]['version']),
                                'content_hash': matching[0]['hash'],
                            }
                            if matching else None
                        ),
                    }
                )
        return {
            'items': items,
            'next_cursor': items[-1]['alias'] if len(page) > limit else None,
        }

    async def get_binding(self, auth, agent_id, alias):
        async with self.backend.create_uow().begin_read() as uow:
            await self._admin(uow, auth, agent_id)
            draft = await self.backend.configuration.one(
                uow,
                'tool_binding_drafts',
                team_id=auth.team_id,
                agent_id=agent_id,
                alias=alias,
            )
            rows = await self.backend.configuration.rows(
                uow,
                'tool_bindings',
                team_id=auth.team_id,
                agent_id=agent_id,
            )
            versions = sorted(
                (row for row in rows if row['alias'] == alias),
                key=lambda row: row['version'],
                reverse=True,
            )
            if not draft and not versions:
                raise not_found()
            return {
                'alias': alias,
                'draft': json.loads(draft['payload']) if draft else None,
                'revision': draft['revision'] if draft else None,
                'versions': [
                    {
                        'binding': json.loads(row['schema']),
                        'ref': {
                            'resource_id': row['binding_id'],
                            'version': str(row['version']),
                            'content_hash': row['hash'],
                        },
                    }
                    for row in versions[:50]
                ],
            }

    async def get_version(self, auth, agent_id, alias, version):
        async with self.backend.create_uow().begin_read() as uow:
            await self._admin(uow, auth, agent_id)
            row = await self.backend.configuration.one(
                uow,
                'tool_bindings',
                team_id=auth.team_id,
                agent_id=agent_id,
                alias=alias,
                version=version,
            )
            if not row:
                raise not_found()
            return {
                'binding': json.loads(row['schema']),
                'ref': {
                    'resource_id': row['binding_id'],
                    'version': str(row['version']),
                    'content_hash': row['hash'],
                },
            }


class ToolDiscovery:
    def __init__(self, backend, adapter, deployments):
        self.backend, self.adapter = backend, adapter
        self.deployments = tuple(deployments)

    async def execute(self, claim: ClaimedAdminJob):
        import asyncio
        from workbench_agent.domain.identity import AuthContext
        payload = json.loads(claim.payload)
        matches = [
            item for item in self.deployments
            if item.team_id == claim.team_id
            and item.agent_id == claim.agent_id
            and item.server == payload['server']
            and item.credential_scope == payload['credential_scope']
        ]
        if len(matches) != 1:
            return json.dumps({'connection': 'missing', 'tools': []})
        deployment = matches[0]
        async with self.backend.create_uow().begin_write() as uow:
            await AdminJobGuard(self.backend.admin_jobs).validate(uow, claim)
            await Authorization(self.backend).current(
                uow,
                AuthContext(
                    team_id=claim.team_id,
                    subject=payload['subject'],
                    identity_kind=payload['identity_kind'],
                    agent_scope=tuple(payload.get('agent_scope') or ()) or None,
                    roles=(),
                ),
                claim.agent_id,
                'admin',
            )
        async with self.backend.create_uow().begin_read() as uow:
            claimed = await self.backend.admin_jobs.get(
                uow,
                AdminAuth(
                    claim.team_id,
                    claim.agent_id,
                    payload['subject'],
                    True,
                ),
                claim.job_id,
            )
            if claimed is None or claimed.status != 'running':
                raise RuntimeError('discovery job is no longer owned')
        if self.adapter is None:
            return json.dumps({'connection': 'unavailable', 'tools': []})
        try:
            catalog = await asyncio.wait_for(
                self.adapter.discover(
                    payload['server'],
                    payload['credential_scope'],
                    team_id=claim.team_id,
                    agent_id=claim.agent_id,
                ),
                10,
            )
        except TimeoutError:
            return json.dumps({'connection': 'timeout', 'tools': []})
        except PermissionError:
            return json.dumps({'connection': 'denied', 'tools': []})
        except Exception:
            return json.dumps({'connection': 'unavailable', 'tools': []})
        tools = []
        total = 0
        allowed = set(deployment.read_tools)
        for raw in catalog.tools:
            item = raw if isinstance(raw, dict) else raw.model_dump()
            name = item.get('name')
            if name not in allowed:
                continue
            description = (item.get('description') or '')[:4000]
            schema = item.get('inputSchema') or item.get('input_schema') or {}
            encoded = json.dumps(
                {'name': name, 'description': description, 'schema': schema},
                ensure_ascii=False,
                sort_keys=True,
            )
            if total + len(encoded.encode()) > 256 * 1024:
                break
            total += len(encoded.encode())
            tools.append(
                {
                    'name': name,
                    'description': description,
                    'schema': schema,
                    'read_only': True,
                }
            )
        return json.dumps(
            {'connection': 'ok', 'tools': tools},
            ensure_ascii=False,
            sort_keys=True,
            separators=(',', ':'),
        )
