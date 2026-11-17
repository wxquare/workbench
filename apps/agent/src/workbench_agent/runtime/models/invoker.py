"""Budgeted, fenced model invocations; no external calls inside a transaction."""
import asyncio
import json
import logging
import time
from dataclasses import dataclass
from uuid import uuid4,uuid5,UUID
from langchain_core.messages import SystemMessage, HumanMessage, messages_to_dict
from langchain_core.utils.function_calling import convert_to_openai_tool
from workbench_agent.application.errors import DomainError
from workbench_agent.application.profiles import ProfileService
from workbench_agent.domain.context import RunContext
from workbench_agent.domain.identity import AuthContext
from workbench_agent.domain.usage import ReservationRequest,ActualUsage
from workbench_agent.persistence.errors import StaleExecution
from workbench_agent.runtime.budgets.service import BudgetService
from workbench_agent.runtime.models.json_output import validate_json_output
from workbench_agent.observability import current_telemetry, span_or_none

logger=logging.getLogger("workbench.models")

def retry_after_seconds(value, now=None):
    import math
    from datetime import datetime, timezone
    from email.utils import parsedate_to_datetime
    if not value: return 0
    try:
        seconds=float(value)
        return max(0,seconds) if math.isfinite(seconds) else 0
    except (TypeError,ValueError):
        try:
            date=parsedate_to_datetime(value)
            if date.tzinfo is None: return 0
            return max(0,(date-(now or datetime.now(timezone.utc))).total_seconds())
        except (TypeError,ValueError,OverflowError): return 0

@dataclass(frozen=True)
class ModelResult:
    status: str
    requested_model: str
    response_model: str | None = None
    usage: ActualUsage | None = None
    cost_state: str = 'pending'
    text: str | None = None
    validated_data: object | None = None
    message: object | None = None
    error_code: str | None = None

class ModelInvoker:
    def __init__(self,backend,factory,secrets,*,execution=None):
        self.backend,self.factory,self.secrets=backend,factory,secrets
        self.budgets=BudgetService(backend)
        self.execution=execution

    async def _profile(self,guard,role):
        async with self.backend.create_uow().begin_write() as uow:
            await self.backend.lease_guard.require(uow,guard,{'running'})
            row=await self.backend.runs.one(uow,'runs',team_id=guard.team_id,run_id=guard.run_id)
            ctx=RunContext.model_validate_json(row['versions'])
            auth=AuthContext(team_id=ctx.team_id,subject=ctx.subject,identity_kind=ctx.identity_kind,roles=ctx.roles,agent_scope=ctx.agent_scope)
            return await ProfileService(self.backend).model_version(uow,auth,ctx.model_for(role)),ctx

    async def _settle(self,guard,ident,receipt):
        try:
            async with self.backend.create_uow().begin_write() as uow:
                await self.budgets.settle(guard,ident,receipt,uow=uow)
        except (StaleExecution,DomainError) as error:
            if isinstance(error,DomainError) and error.code not in ('FORBIDDEN','NOT_FOUND'):
                raise
            async with self.backend.create_uow().begin_write() as uow:
                await self.budgets.quarantine(guard,ident,receipt,uow=uow)
            raise

    async def invoke(self,guard,role,messages,output_schema=None, *, tools=None,logical_call_id=None,dispatch_check=None):
        profile,context=await self._profile(guard,role)
        config=profile.configuration
        if self.execution is not None:
            from workbench_agent.jobs.worker import Execution
            if not isinstance(self.execution,Execution) or self.execution.guard!=guard or self.execution.claim.context!=context:
                raise DomainError('STALE_EXECUTION',409)
        if context.identity_kind=='connector' and self.execution is None:
            from workbench_agent.jobs.worker import CapabilityBlocked
            raise CapabilityBlocked('current connector execution policy unavailable')
        model=self.factory.build(profile,self.secrets,team_id=context.team_id,agent_id=context.agent_id)
        if tools:
            if output_schema is not None: raise DomainError('MODEL_TOOLS_WITH_JSON_UNSUPPORTED',422)
            model=model.bind_tools(tools)
        prompt=list(messages)
        if output_schema is not None:
            schema=json.dumps(output_schema.model_json_schema(),ensure_ascii=False)
            prompt=[SystemMessage(content='Return one JSON object matching this schema. Example: use valid JSON strings, numbers and arrays. Schema: '+schema),*prompt]
        repairs=0
        attempts=0
        while True:
            response=None
            for attempt in range(config.invocation_max_attempts):
                if self.execution is not None: await self.execution.check_operation()
                attempts+=1
                # Stable IDs let a checkpoint replay detect an already sent
                # logical model node, including the settlement/checkpoint gap.
                ident=(str(uuid5(UUID(logical_call_id), f'attempt:{attempts}'))
                       if logical_call_id is not None else str(uuid4()))
                # UTF-8 bytes, envelope allowance and declared completion cap form
                # a conservative bound; refuse requests larger than frozen context.
                envelope={'messages':messages_to_dict(prompt),'tools':[convert_to_openai_tool(t) for t in (tools or [])]}
                size=len(json.dumps(envelope,ensure_ascii=False,separators=(',',':')).encode('utf-8'))+256*len(prompt)+config.max_output_tokens
                request=ReservationRequest(invocation_id=ident,kind='summary' if role=='summary' else ('evaluation' if role=='evaluation' else 'expert'),
                    model_attempts=1,token_upper_bound=size,role=role)
                async with self.backend.create_uow().begin_write() as uow:
                    if dispatch_check is not None: await dispatch_check(uow)
                    await self.budgets.reserve(guard,request,uow=uow)
                    permit=await self.budgets.mark_sent(guard,ident,uow=uow)
                    if not permit.dispatch_allowed: raise DomainError('INVOCATION_ALREADY_SENT',409)
                    run,_=await self.budgets._owned(uow,guard)
                    now=await self.budgets._dispatch_deadline(uow,guard,run)
                    from workbench_agent.domain.transitions import active_elapsed
                    limits=json.loads(run['budgets'])
                    remaining=min(run['wall_deadline_at']-now,limits['wall_seconds']*1000-active_elapsed(run,now,await self.backend.runs.active_until(uow,guard.team_id,guard.run_id)))
                    timeout=min(config.request_timeout_seconds,max(0,remaining/1000))
                logger.info('model attempt',extra={'event':'model.attempt','run_id':guard.run_id,'invocation_id':ident,'role':role,'attempt':attempts})
                started=time.monotonic()
                try:
                    kwargs={'response_format':{'type':'json_object'}} if output_schema else {}
                    attributes = {'langfuse.observation.type': 'generation',
                        'langfuse.observation.model.name': config.model_id,
                        'workbench.invocation_id': ident, 'workbench.role': role, 'workbench.attempt': attempts,
                        'workbench.skills': json.dumps([ref.model_dump(mode='json') for ref in context.skills], separators=(',', ':'))}
                    with span_or_none(self.backend, 'model.generate', attributes=attributes) as span:
                        telemetry = current_telemetry(self.backend)
                        capture = (telemetry is not None and telemetry.settings.langfuse.content_mode == 'redacted_content'
                                   and getattr(self.factory, 'managed_secrets', None) is None)
                        if span is not None and capture:
                            from workbench_agent.observability import redacted_content
                            safe_input, truncated = redacted_content([
                                {'role': item.type, 'content': item.content}
                                for item in prompt if item.type in ('system', 'human', 'ai') and isinstance(item.content, str)
                            ], telemetry.settings.langfuse.max_input_bytes)
                            if safe_input is not None:
                                span.set_attribute('langfuse.observation.input', safe_input)
                                span.set_attribute('workbench.input_truncated', truncated)
                        try:
                            response=await asyncio.wait_for(model.ainvoke(prompt,**kwargs),timeout=timeout)
                        except BaseException:
                            if span is not None:
                                span.set_attribute('langfuse.observation.level', 'ERROR')
                                span.set_attribute('langfuse.observation.status_message', 'MODEL_ATTEMPT_FAILED')
                            raise
                        if span is not None:
                            usage_values = getattr(response, 'usage_metadata', None) or {}
                            token_values = {key: usage_values[source] for key, source in
                                (('input', 'input_tokens'), ('output', 'output_tokens'))
                                if isinstance(usage_values.get(source), int)}
                            if token_values:
                                span.set_attribute('langfuse.observation.usage_details', json.dumps(token_values))
                            if capture and isinstance(response.content, str):
                                safe_output, truncated = redacted_content(response.content, telemetry.settings.langfuse.max_output_bytes)
                                if safe_output is not None:
                                    span.set_attribute('langfuse.observation.output', safe_output)
                                    span.set_attribute('workbench.output_truncated', truncated)
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    await self._settle(guard,ident,ActualUsage())
                    status=getattr(exc,'status_code',None)
                    if status in (401,403): code='MODEL_CREDENTIAL_UNAVAILABLE'
                    elif status==404: code='MODEL_INVALID'
                    elif status==429: code='MODEL_RATE_LIMITED'
                    elif status and status>=500: code='MODEL_PROVIDER_UNAVAILABLE'
                    elif isinstance(exc,TimeoutError) or type(exc).__name__=='APITimeoutError': code='MODEL_TIMEOUT'
                    elif type(exc).__name__=='APIConnectionError': code='MODEL_PROVIDER_UNAVAILABLE'
                    else: code='MODEL_PROVIDER_ERROR'
                    logger.warning('model failed',extra={'event':'model.failed','run_id':guard.run_id,
                        'invocation_id':ident,'role':role,'attempt':attempts,'error_code':code,
                        'duration_ms':int((time.monotonic()-started)*1000)})
                    if code in ('MODEL_RATE_LIMITED','MODEL_PROVIDER_UNAVAILABLE','MODEL_TIMEOUT') and attempts<config.invocation_max_attempts:
                        headers=getattr(getattr(exc,'response',None),'headers',{})
                        delay=retry_after_seconds(headers.get('retry-after'))
                        if delay:
                            async with self.backend.create_uow().begin_write() as uow:
                                run,_=await self.budgets._owned(uow,guard)
                                now=await self.budgets._dispatch_deadline(uow,guard,run)
                                from workbench_agent.domain.transitions import active_elapsed
                                limits=json.loads(run['budgets'])
                                remaining=min(run['wall_deadline_at']-now,limits['wall_seconds']*1000-active_elapsed(run,now,await self.backend.runs.active_until(uow,guard.team_id,guard.run_id)))
                            if delay*1000>=remaining: return ModelResult('error',config.model_id,error_code='BUDGET_EXCEEDED')
                            await asyncio.sleep(delay)
                        continue
                    return ModelResult('error',config.model_id,error_code=code)
                meta=response.response_metadata or {}
                usage=response.usage_metadata or {}
                logger.info('model completed',extra={'event':'model.completed','run_id':guard.run_id,
                    'invocation_id':ident,'role':role,'attempt':attempts,'model_id':config.model_id,
                    'duration_ms':int((time.monotonic()-started)*1000),
                    'input_tokens':usage.get('input_tokens'),'output_tokens':usage.get('output_tokens')})
                receipt=ActualUsage(input_tokens=usage.get('input_tokens'),output_tokens=usage.get('output_tokens'))
                await self._settle(guard,ident,receipt)
                break
            text=response.content if isinstance(response.content,str) else ''
            result=dict(requested_model=config.model_id,response_model=meta.get('model_name'),usage=receipt,text=text,message=response)
            if output_schema is None:
                if not text.strip() and not getattr(response,'tool_calls',None): return ModelResult('error',error_code='MODEL_EMPTY_OUTPUT',**result)
                return ModelResult('ok',**result)
            try:
                return ModelResult('ok',validated_data=validate_json_output(text,output_schema,meta.get('finish_reason')),**result)
            except ValueError:
                if repairs>=1: return ModelResult('invalid_output',error_code='MODEL_INVALID_OUTPUT',**result)
                if attempts>=config.invocation_max_attempts: return ModelResult('invalid_output',error_code='MODEL_INVALID_OUTPUT',**result)
                repairs+=1
                prompt=[*prompt,response,HumanMessage(content='The prior JSON was invalid. Return a corrected JSON object only, matching the schema.')]
