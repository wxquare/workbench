"""Bounded, non-business model capability probes for management Jobs."""
import asyncio
import json
from dataclasses import dataclass,asdict
from langchain_core.messages import HumanMessage,ToolMessage
from langchain_core.tools import tool
from pydantic import BaseModel,StrictStr
from workbench_agent.application.authorization import Authorization
from workbench_agent.application.errors import DomainError
from workbench_agent.application.profiles import ProfileService
from workbench_agent.domain.identity import AuthContext
from workbench_agent.domain.profiles import VersionRef
from workbench_agent.runtime.models.json_output import validate_json_output

class Answer(BaseModel):
    summary: StrictStr

@tool
def local_sum(a:int,b:int)->int:
    """Add two small integers locally; has no external side effects."""
    return a+b

@dataclass(frozen=True)
class ProbeReport:
    model_version_id:str
    version:str
    content_hash:str
    requested_model:str
    response_model:str|None
    capabilities:dict[str,bool]
    configuration:str='valid'
    discovery:str='not_applicable'
    invocation:str='failed'
    remote_cancel:str='unknown'

class ModelProbe:
    def __init__(self,backend,factory,secrets):
        self.backend,self.factory,self.secrets=backend,factory,secrets

    async def run(self,auth,agent,ref):
        async with self.backend.create_uow().begin_read() as uow:
            await Authorization(self.backend).current(uow,auth,agent,'admin')
            version=await ProfileService(self.backend).model_version(uow,auth,ref)
            owner=await self.backend.configuration.one(uow,'model_profiles',team_id=auth.team_id,profile_id=version.profile_id)
            if not owner or owner['agent_id'] not in (None,agent): raise DomainError('NOT_FOUND',404)
        config=version.configuration
        caps={k:False for k in ('text','tool','stream','json','usage','client_stream_close')}
        try:
            model=self.factory.build(version,self.secrets,team_id=auth.team_id,agent_id=agent)
        except DomainError as error:
            state='credential_unavailable' if error.code=='MODEL_CREDENTIAL_UNAVAILABLE' else 'invalid'
            return ProbeReport(version.model_version_id,version.version,version.content_hash,config.model_id,None,caps,configuration=state)
        response_model=None
        try:
            reply=await asyncio.wait_for(model.ainvoke('Reply with exactly OK.'),config.request_timeout_seconds)
            caps['text']=bool(reply.content)
            caps['usage']=bool(reply.usage_metadata and reply.usage_metadata.get('input_tokens') is not None and reply.usage_metadata.get('output_tokens') is not None)
            response_model=(reply.response_metadata or {}).get('model_name')
            if not isinstance(response_model,str) or len(response_model)>128: response_model=None
        except Exception: pass
        try:
            user=HumanMessage(content='Use the local_sum tool to add 2 and 3.')
            first=await asyncio.wait_for(model.bind_tools([local_sum],tool_choice='required').ainvoke([user]),config.request_timeout_seconds)
            call=first.tool_calls[0]
            if call['name']=='local_sum':
                value=local_sum.invoke(call['args'])
                second=await asyncio.wait_for(model.ainvoke([user,first,ToolMessage(content=str(value),tool_call_id=call['id'])]),config.request_timeout_seconds)
                caps['tool']=bool(second.content) and value==5
        except Exception: pass
        try:
            stream=model.astream('Count from one to one hundred.')
            try:
                async with asyncio.timeout(config.request_timeout_seconds):
                    async for part in stream:
                        if part.content:
                            caps['stream']=True
                            break
            finally:
                await stream.aclose()
                caps['client_stream_close']=caps['stream']
        except Exception: pass
        try:
            reply=await asyncio.wait_for(model.ainvoke('Return JSON like {"summary":"ok"}.',response_format={'type':'json_object'}),config.request_timeout_seconds)
            caps['json']=bool(validate_json_output(reply.content,Answer,(reply.response_metadata or {}).get('finish_reason')).summary)
        except Exception: pass
        return ProbeReport(version.model_version_id,version.version,version.content_hash,config.model_id,response_model,caps,
            invocation='passed' if all(caps.values()) else 'failed')

    async def execute(self,claim):
        payload=json.loads(claim.payload)
        auth=AuthContext(team_id=claim.team_id,subject=payload['subject'],identity_kind=payload['identity_kind'],
            agent_scope=payload['agent_scope'],roles=())
        ref=VersionRef(resource_id=payload['model_version_id'],version=str(payload['version']),content_hash=payload['hash'])
        report=await self.run(auth,claim.agent_id,ref)
        # This bounded report is safe metadata; no provider text or credentials.
        return json.dumps(asdict(report),sort_keys=True,separators=(',',':'))
