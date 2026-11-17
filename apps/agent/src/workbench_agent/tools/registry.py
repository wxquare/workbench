"""Publish immutable, content-addressed tool bindings."""
import json
from uuid import uuid4
from workbench_agent.application.errors import DomainError
from workbench_agent.application.idempotency import canonical,digest
from workbench_agent.application.profiles import ProfileService
from workbench_agent.domain.profiles import VersionRef
from workbench_agent.domain.tools import ToolBinding
from jsonschema import Draft202012Validator

def validate_tool_schema(schema):
    """Permit only nonrecursive local definitions; never resolve network references."""
    Draft202012Validator.check_schema(schema)
    defs=schema.get('$defs',{})
    def walk(value,inside_definition=False):
        if isinstance(value,dict):
            for key,item in value.items():
                if key in ('$dynamicRef','$recursiveRef','$id','$anchor','$dynamicAnchor'):
                    raise ValueError('unsupported schema reference')
                if key=='$ref':
                    prefix='#/$defs/'
                    if inside_definition or not isinstance(item,str) or not item.startswith(prefix) or item[len(prefix):] not in defs:
                        raise ValueError('unsupported schema reference')
                elif key=='$defs':
                    for definition in item.values(): walk(definition,True)
                else: walk(item,inside_definition)
        elif isinstance(value,list):
            for item in value: walk(item,inside_definition)
    walk(schema)

class ToolRegistry:
    def __init__(self,backend):
        self.backend=backend
        self.profiles=ProfileService(backend)
    async def publish(self,auth,agent_id,binding:ToolBinding,*,key):
        data=binding.model_dump(mode='json')
        for schema in (binding.input_schema,binding.output_schema):
            if schema is not None:
                try:
                    validate_tool_schema(schema)
                except Exception:
                    raise DomainError('INVALID_TOOL_SCHEMA',422) from None
        async with self.backend.create_uow().begin_write() as uow:
            await self.profiles.agent(uow,auth,agent_id,'admin')
            async def operation():
                version=await self.backend.tools.next_version(uow,auth.team_id,agent_id,binding.alias)
                ident=str(uuid4()); now=await self.backend.clock.now_ms(uow)
                await self.backend.configuration.insert(uow,'tool_bindings',dict(team_id=auth.team_id,binding_id=ident,
                    agent_id=agent_id,alias=binding.alias,provider=binding.provider,schema=canonical(data),
                    scope=canonical({'permission':binding.permission}),auth_ref=binding.credential_scope,
                    version=version,hash=digest(data),created_at=now,updated_at=now))
                return VersionRef(resource_id=ident,version=str(version),content_hash=digest(data)).model_dump()
            result=await self.profiles.idempotency.execute(uow,auth,agent_id,'tool.publish',key,data,operation,status=201)
            return VersionRef.model_validate(result.body)

    async def save_draft(self,auth,agent_id,binding,*,expected_revision,key):
        data=binding.model_dump(mode='json')
        async with self.backend.create_uow().begin_write() as uow:
            await self.profiles.agent(uow,auth,agent_id,'admin')
            async def operation():
                row=await self.backend.configuration.one(uow,'tool_binding_drafts',team_id=auth.team_id,agent_id=agent_id,alias=binding.alias)
                if (row['revision'] if row else 0)!=expected_revision: raise DomainError('REVISION_CONFLICT',409)
                now=await self.backend.clock.now_ms(uow)
                if row:
                    changed=await self.backend.configuration.change(uow,'tool_binding_drafts',{'team_id':auth.team_id,'draft_id':row['draft_id'],'revision':expected_revision},{'payload':canonical(data),'revision':expected_revision+1,'updated_at':now})
                    if not changed: raise DomainError('REVISION_CONFLICT',409)
                else:
                    await self.backend.configuration.insert(uow,'tool_binding_drafts',dict(team_id=auth.team_id,draft_id=str(uuid4()),agent_id=agent_id,alias=binding.alias,payload=canonical(data),revision=1,created_at=now,updated_at=now))
                return {'alias':binding.alias,'revision':expected_revision+1,'draft':data}
            result=await self.profiles.idempotency.execute(uow,auth,agent_id,'tool.draft',key,{'binding':data,'expected_revision':expected_revision},operation)
            return result.body

    async def publish_draft(self,auth,agent_id,alias,*,expected_revision,key):
        async with self.backend.create_uow().begin_write() as uow:
            await self.profiles.agent(uow,auth,agent_id,'admin')
            async def operation():
                row=await self.backend.configuration.one(uow,'tool_binding_drafts',team_id=auth.team_id,agent_id=agent_id,alias=alias)
                if not row: raise DomainError('DRAFT_REQUIRED',409)
                if row['revision']!=expected_revision: raise DomainError('REVISION_CONFLICT',409)
                binding=ToolBinding.model_validate_json(row['payload']); data=binding.model_dump(mode='json')
                for value in (binding.input_schema,binding.output_schema):
                    if value is not None:
                        try:
                            validate_tool_schema(value)
                        except Exception: raise DomainError('INVALID_TOOL_SCHEMA',422) from None
                version=await self.backend.tools.next_version(uow,auth.team_id,agent_id,alias)
                ident=str(uuid4()); now=await self.backend.clock.now_ms(uow)
                await self.backend.configuration.insert(uow,'tool_bindings',dict(team_id=auth.team_id,binding_id=ident,agent_id=agent_id,alias=alias,provider=binding.provider,schema=canonical(data),scope=canonical({'permission':binding.permission}),auth_ref=binding.credential_scope,version=version,hash=digest(data),created_at=now,updated_at=now))
                return VersionRef(resource_id=ident,version=str(version),content_hash=digest(data)).model_dump()
            result=await self.profiles.idempotency.execute(uow,auth,agent_id,'tool.publish_draft',key,{'alias':alias,'expected_revision':expected_revision},operation,status=201)
            return VersionRef.model_validate(result.body)

    async def probe_draft(self,auth,agent_id,alias,adapter):
        async with self.backend.create_uow().begin_read() as uow:
            await self.profiles.agent(uow,auth,agent_id,'admin')
            row=await self.backend.configuration.one(uow,'tool_binding_drafts',team_id=auth.team_id,agent_id=agent_id,alias=alias)
            if not row: raise DomainError('DRAFT_REQUIRED',409)
            binding=ToolBinding.model_validate_json(row['payload'])
        try:
            catalog=await adapter.discover(binding.server,binding.credential_scope,team_id=auth.team_id,agent_id=agent_id)
        except PermissionError:
            return {'discovery':'denied','schema':'unknown','invocation':'not_run'}
        except Exception:
            return {'discovery':'unavailable','schema':'unknown','invocation':'not_run'}
        if binding.tool_name not in catalog.names: return {'discovery':'ok','schema':'missing','invocation':'not_run'}
        tool=catalog.tools[catalog.names.index(binding.tool_name)]
        schema=tool.get('inputSchema') if isinstance(tool,dict) else tool.inputSchema
        return {'discovery':'ok','schema':'match' if schema==binding.input_schema else 'mismatch','invocation':'not_run'}

    async def enqueue_probe(self,auth,agent_id,alias,*,key):
        from dataclasses import asdict
        from workbench_agent.domain.admin_jobs import AdminAuth,AdminJob
        async with self.backend.create_uow().begin_write() as uow:
            await self.profiles.agent(uow,auth,agent_id,'admin')
            async def operation():
                row=await self.backend.configuration.one(uow,'tool_binding_drafts',team_id=auth.team_id,agent_id=agent_id,alias=alias)
                if not row: raise DomainError('DRAFT_REQUIRED',409)
                payload={'binding':json.loads(row['payload']),'revision':row['revision'],'subject':auth.subject,
                    'identity_kind':auth.identity_kind,'agent_scope':auth.agent_scope}
                principal=AdminAuth(auth.team_id,agent_id,auth.subject,True)
                job=await self.backend.admin_jobs.submit(uow,principal,'tool_probe',canonical(payload),digest({'alias':alias}),key)
                return asdict(job)
            result=await self.profiles.idempotency.execute(uow,auth,agent_id,'tool.probe',key,{'alias':alias},operation,status=202)
            return AdminJob(**result.body)
