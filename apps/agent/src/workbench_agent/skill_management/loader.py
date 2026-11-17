"""Load a verified resource from a Run's frozen, published skill reference."""
from dataclasses import dataclass
from hashlib import sha256
from workbench_agent.application.context import ContextResolver
from workbench_agent.domain.context import RunContext
from workbench_agent.domain.profiles import VersionRef
from workbench_agent.jobs.worker import CapabilityBlocked, Execution, Worker
from workbench_agent.skill_management.validator import validate_bundle, validate_bundle_path, InvalidBundle
from workbench_agent.observability import span_or_none
import logging

@dataclass(frozen=True)
class VerifiedResource:
    content: str | bytes
    content_hash: str
    skill_version_ref: VersionRef

class SkillLoader:
    def __init__(self, backend, objects):
        self.backend, self.objects = backend, objects

    async def _check_execution(self, execution, context):
        owner = getattr(execution, '_tool_owner', None)
        if (not isinstance(execution, Execution) or not isinstance(owner, Worker) or owner.backend is not self.backend
                or not owner._owns_tool_execution(execution) or execution.claim.context != context):
            raise CapabilityBlocked('current Worker execution required')
        await execution.check_operation()
        async with self.backend.create_uow().begin_write() as uow:
            await self.backend.lease_guard.require(uow, execution.guard, {'running'})
            run = await self.backend.runs.one(uow, 'runs', team_id=execution.guard.team_id, run_id=execution.guard.run_id)
            if not run or RunContext.model_validate_json(run['versions']) != context:
                raise CapabilityBlocked('Run context does not match frozen context')
            await ContextResolver(self.backend).revalidate(context, uow=uow)

    async def load(self, context, skill_ref: VersionRef, resource_path: str, *, execution=None) -> VerifiedResource:
        with span_or_none(self.backend, 'skill.load', attributes={'langfuse.observation.type': 'span',
                'workbench.skill_version_id': str(skill_ref.resource_id), 'workbench.skill_version': str(skill_ref.version)}):
            try:
                result = await self._load(context, skill_ref, resource_path, execution=execution)
            except Exception:
                logging.getLogger('workbench.skills').warning('skill.unavailable', extra={'event': 'skill.unavailable',
                    'error_code': 'SKILL_UNAVAILABLE'})
                raise
            logging.getLogger('workbench.skills').info('skill.loaded', extra={'event': 'skill.loaded'})
            return result

    async def _load(self, context, skill_ref: VersionRef, resource_path: str, *, execution=None) -> VerifiedResource:
        await self._check_execution(execution, context)
        try:
            path = validate_bundle_path(resource_path)
            if skill_ref not in context.skills:
                raise CapabilityBlocked('skill version is not pinned')
            async with self.backend.create_uow().begin_read() as uow:
                await ContextResolver(self.backend).revalidate(context, uow=uow)
                row = await self.backend.configuration.one(uow, 'skill_versions', team_id=context.team_id,
                    skill_version_id=skill_ref.resource_id, hash=skill_ref.content_hash)
                if not row or str(row['version']) != skill_ref.version or row['status'] != 'published':
                    raise CapabilityBlocked('pinned skill version unavailable')
                owner = await self.backend.configuration.one(uow, 'skills', team_id=context.team_id, skill_id=row['skill_id'])
                if not owner or owner['agent_id'] not in (None, context.agent_id):
                    raise CapabilityBlocked('pinned skill version unavailable')
                name, ref = owner['name'], row['bundle_ref']
            from workbench_agent.skill_management.manifest import PREFIX, read_manifest, read_resource, validate_skill_metadata
            if ref.startswith(PREFIX):
                manifest = read_manifest(self.objects, ref, name, skill_ref.content_hash)
                skill_doc = read_resource(self.objects, manifest, 'SKILL.md')
                validate_skill_metadata(manifest, skill_doc)
                content = skill_doc if path == 'SKILL.md' else read_resource(self.objects, manifest, path)
            else:
                bundle = validate_bundle(self.objects.get(ref), name, allow_binary=True)
                if bundle.bundle_hash != skill_ref.content_hash:
                    raise CapabilityBlocked('pinned skill hash mismatch')
                content = bundle.resources[path]
            await self._check_execution(execution, context)
            try:
                decoded = content.decode('utf-8')
                value = content if '\x00' in decoded else decoded
            except UnicodeDecodeError:
                value = content
            return VerifiedResource(value, sha256(content).hexdigest(), skill_ref)
        except (OSError, ValueError, KeyError, UnicodeError, InvalidBundle) as exc:
            raise CapabilityBlocked('pinned skill resource unavailable') from exc
