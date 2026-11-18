"""Immutable pinned graph implementations and the explicit M1 worker seam."""
import json
from langgraph.graph.state import CompiledStateGraph
from workbench_agent.jobs.worker import ExecutorRegistry
from workbench_agent.persistence.checkpoints.codec import CheckpointCodec
from workbench_agent.runtime.checkpoints.portable import PortableCheckpointSaver, InvocationGuard
from workbench_agent.runtime.graphs.foundation import build_foundation
from workbench_agent.runtime.state.schema import STATE_SCHEMA_VERSION

class GraphRegistry:
    def __init__(self): self._graphs={}; self._schemas={}

    def register(self,graph_version,state_schema_version,graph):
        if graph_version in self._schemas: raise ValueError('pinned graph implementation already registered')
        self._graphs[(graph_version,state_schema_version)]=graph
        self._schemas[graph_version]=state_schema_version

    def resolve(self,graph_version,state_schema_version) -> CompiledStateGraph:
        try: return self._graphs[(graph_version,state_schema_version)]
        except KeyError: raise ValueError('unsupported pinned graph/schema version') from None

    def state_schema_version(self,graph_version):
        try: return self._schemas[graph_version]
        except KeyError: raise ValueError('unsupported pinned graph version') from None

class FoundationExecutor:
    def __init__(self,registry,saver,guard_provider):
        self.registry,self.saver,self.guard_provider=registry,saver,guard_provider

    async def execute(self,execution):
        version=execution.claim.context.graph_version
        graph=self.registry.resolve(version,self.registry.state_schema_version(version))
        config={'configurable':{'thread_id':execution.guard.run_id}, 'recursion_limit': 128}
        with self.guard_provider.bind(execution.guard):
            saved=await self.saver.aget_tuple(config)
            if saved is None:
                async with execution.backend.create_uow().begin_read() as uow:
                    message=await execution.backend.runs.one(uow,'messages',team_id=execution.guard.team_id,run_id=execution.guard.run_id)
                if message is None: raise ValueError('admitted input missing')
                request=json.loads(message['content_ref'])
                initial={'run_id':execution.guard.run_id,'input_text':request['input']['text'],'messages':[]}
                if version in ('dod.v2','dod.v3','dod.v9','dod.v10','dod.v13','dod.v14','dod.v15'): initial['actor']=execution.claim.context.subject
                if version in ('dod.v4','dod.v5','dod.v6','dod.v7','dod.v8','dod.v11','dod.v12','dod.v16','dod.v17','dod.v18','dod.v19'): initial['mode']=request['mode']
                if version in ('dod.v9','dod.v10','dod.v13','dod.v14','dod.v15'): initial.update(mode=request['mode'], business_context=request['business_context'])
                if version=='dod.v3':
                    from workbench_agent.application.foundation_context import recording_fields
                    initial.update(recording_fields(request['input'],request['business_context']))
            else:
                # Resume only runnable persisted tasks; an interrupt requires T07's
                # accepted ResumeRequest, never an automatic invented response.
                snapshot=await graph.aget_state(config)
                if any(task.interrupts for task in snapshot.tasks): return saved.config
                initial=None
            # v10 must persist its cross-source authority before any tool dispatch.
            if version in ('dod.v10', 'dod.v11', 'dod.v12', 'dod.v13', 'dod.v14', 'dod.v15', 'dod.v17', 'dod.v18'):
                await graph.ainvoke(initial,config,context=execution,durability='sync')
            else:
                await graph.ainvoke(initial,config,context=execution)
            latest=await self.saver.aget_tuple(config)
            return latest.config

class FoundationProgressProjector:
    """Park real checkpoint progress until T07/T08 install lifecycle projectors.

    This is deliberately not a reconciler and does not claim an Outcome/waiting
    transition. The recovery marker prevents ordinary execution from replaying it.
    """
    state_schema_version=STATE_SCHEMA_VERSION
    def __init__(self,backend,saver): self.backend,self.saver=backend,saver

    async def project(self,uow,claim,result):
        rows=await self.saver.store.rows(uow,claim.guard,'')
        if not rows: raise ValueError('foundation checkpoint missing')
        latest=self.saver.decode(rows[0],await self.saver.store.pending(uow,rows[0]))
        interrupted=any(channel=='__interrupt__' for _,channel,_ in latest.pending_writes)
        completed=latest.checkpoint['channel_values'].get('progress')=='composed'
        if not interrupted and not completed: raise ValueError('foundation progress not projectable')
        await self.backend.execution_events.append_execution(uow,claim.guard,'phase.changed',{
            'checkpoint_id':rows[0]['checkpoint_id'],'state_schema_version':self.state_schema_version,
            'progress':'interrupt' if interrupted else 'composed'})
        await self.backend.jobs.release(uow,claim.guard,status='queued',job_status='capability_blocked',
            recovery_phase='projection' if interrupted else 'finalizing',
            error_code='FOUNDATION_INPUT_PROJECTION_PENDING' if interrupted else 'FOUNDATION_FINALIZATION_PENDING')


def foundation_worker_registry(backend):
    provider=InvocationGuard(); saver=PortableCheckpointSaver(backend,provider,CheckpointCodec())
    graphs=GraphRegistry(); graphs.register('dod.v1',STATE_SCHEMA_VERSION,build_foundation(saver))
    from workbench_agent.runtime.graphs.interrupts import build_interrupt_graph
    graphs.register('dod.v2','dod-state-v2',build_interrupt_graph(saver))
    # v3 pins a new public-input mapping; the immutable v2 graph/state is reused.
    graphs.register('dod.v3','dod-state-v2',build_interrupt_graph(saver))
    from workbench_agent.runtime.graphs.dod import build_dod, STATE_SCHEMA_VERSION as DOD_STATE_VERSION
    graphs.register('dod.v4',DOD_STATE_VERSION,build_dod(saver))
    from workbench_agent.runtime.graphs.qa import build_dod as build_qa, STATE_SCHEMA_VERSION as QA_STATE_VERSION
    graphs.register('dod.v5',QA_STATE_VERSION,build_qa(saver))
    from workbench_agent.runtime.graphs.qa_v16 import build_dod as build_qa_v16, STATE_SCHEMA_VERSION as QA_V16_STATE_VERSION
    graphs.register('dod.v16', QA_V16_STATE_VERSION, build_qa_v16(saver))
    from workbench_agent.runtime.graphs.qa_v19 import build_dod as build_qa_v19, STATE_SCHEMA_VERSION as QA_V19_STATE_VERSION
    graphs.register('dod.v19', QA_V19_STATE_VERSION, build_qa_v19(saver))
    from workbench_agent.runtime.graphs.direct_qa import build_direct_qa, STATE_SCHEMA_VERSION as DIRECT_QA_STATE_VERSION
    graphs.register('dod.v17', DIRECT_QA_STATE_VERSION, build_direct_qa(saver))
    from workbench_agent.runtime.graphs.conversation_qa import build_conversation_qa, STATE_SCHEMA_VERSION as CONVERSATION_QA_STATE_VERSION
    graphs.register('dod.v18', CONVERSATION_QA_STATE_VERSION, build_conversation_qa(saver))
    from workbench_agent.runtime.graphs.code import build_dod as build_code, STATE_SCHEMA_VERSION as CODE_STATE_VERSION
    graphs.register('dod.v6',CODE_STATE_VERSION,build_code(saver))
    from workbench_agent.runtime.graphs.multi import build_dod as build_multi, STATE_SCHEMA_VERSION as MULTI_STATE_VERSION
    graphs.register('dod.v7',MULTI_STATE_VERSION,build_multi(saver))
    from workbench_agent.runtime.graphs.extractive import build_dod as build_extractive, STATE_SCHEMA_VERSION as EXTRACTIVE_STATE_VERSION
    graphs.register('dod.v8',EXTRACTIVE_STATE_VERSION,build_extractive(saver))
    from workbench_agent.runtime.graphs.rca import build_rca, STATE_SCHEMA_VERSION as RCA_STATE_VERSION
    graphs.register('dod.v9', RCA_STATE_VERSION, build_rca(saver))
    from workbench_agent.runtime.graphs.rca_baseline import build_rca as build_baseline, STATE_SCHEMA_VERSION as BASELINE_STATE_VERSION
    graphs.register('dod.v10', BASELINE_STATE_VERSION, build_baseline(saver))
    from workbench_agent.runtime.graphs.rca_synthetic import build_rca as build_synthetic
    graphs.register('dod.v13', 'dod-state-v13', build_synthetic(saver))
    from workbench_agent.runtime.graphs.rca_proposal import build_rca as build_proposal
    graphs.register('dod.v14', 'dod-state-v14', build_proposal(saver))
    from workbench_agent.runtime.graphs.rca_batch import build_rca as build_batch
    graphs.register('dod.v15', 'dod-state-v15', build_batch(saver))
    from workbench_agent.runtime.graphs.sop_graph import build_sop, STATE_SCHEMA_VERSION as SOP_STATE_VERSION
    graphs.register('dod.v11', SOP_STATE_VERSION, build_sop(saver))
    graphs.register('dod.v12', 'dod-sop-v2', build_sop(saver, version='dod.v12'))
    from workbench_agent.runtime.results import ResultProjector
    registry=ExecutorRegistry()
    registry.register('dod.v1',FoundationExecutor(graphs,saver,provider),ResultProjector(backend,FoundationProgressProjector(backend,saver)))
    registry.register('dod.v2',InterruptExecutor(graphs,saver,provider),ResultProjector(backend,InterruptProgressProjector(backend,saver)))
    registry.register('dod.v3',InterruptExecutor(graphs,saver,provider),ResultProjector(backend,InterruptProgressProjector(backend,saver)))
    registry.register('dod.v4',FoundationExecutor(graphs,saver,provider),ResultProjector(backend,FoundationProgressProjector(backend,saver)))
    registry.register('dod.v5',FoundationExecutor(graphs,saver,provider),ResultProjector(backend,FoundationProgressProjector(backend,saver)))
    registry.register('dod.v16',FoundationExecutor(graphs,saver,provider),ResultProjector(backend,FoundationProgressProjector(backend,saver)))
    registry.register('dod.v19',FoundationExecutor(graphs,saver,provider),ResultProjector(backend,FoundationProgressProjector(backend,saver)))
    registry.register('dod.v17',FoundationExecutor(graphs,saver,provider),ResultProjector(backend,FoundationProgressProjector(backend,saver)))
    registry.register('dod.v18',FoundationExecutor(graphs,saver,provider),ResultProjector(backend,FoundationProgressProjector(backend,saver)))
    registry.register('dod.v6',FoundationExecutor(graphs,saver,provider),ResultProjector(backend,FoundationProgressProjector(backend,saver)))
    registry.register('dod.v7',FoundationExecutor(graphs,saver,provider),ResultProjector(backend,FoundationProgressProjector(backend,saver)))
    registry.register('dod.v8',FoundationExecutor(graphs,saver,provider),ResultProjector(backend,FoundationProgressProjector(backend,saver)))
    registry.register('dod.v9',InterruptExecutor(graphs,saver,provider),ResultProjector(backend,InterruptProgressProjector(backend,saver)))
    registry.register('dod.v10',InterruptExecutor(graphs,saver,provider),ResultProjector(backend,InterruptProgressProjector(backend,saver)))
    registry.register('dod.v13',InterruptExecutor(graphs,saver,provider),ResultProjector(backend,InterruptProgressProjector(backend,saver)))
    registry.register('dod.v14',InterruptExecutor(graphs,saver,provider),ResultProjector(backend,InterruptProgressProjector(backend,saver)))
    registry.register('dod.v15',InterruptExecutor(graphs,saver,provider),ResultProjector(backend,InterruptProgressProjector(backend,saver)))
    registry.register('dod.v11',FoundationExecutor(graphs,saver,provider),ResultProjector(backend,FoundationProgressProjector(backend,saver)))
    registry.register('dod.v12',FoundationExecutor(graphs,saver,provider),ResultProjector(backend,FoundationProgressProjector(backend,saver)))
    from workbench_agent.jobs.reconciler import Reconciler
    registry.reconciler=Reconciler(backend,registry)
    return registry

class InterruptExecutor(FoundationExecutor):
    async def apply_resume(self,execution,*,mark=True):
        from langgraph.types import Command
        from workbench_agent.application.resume import ResumeApplier
        config={'configurable':{'thread_id':execution.guard.run_id}}
        graph=self.registry.resolve(execution.claim.context.graph_version,self.registry.state_schema_version(execution.claim.context.graph_version))
        with self.guard_provider.bind(execution.guard):
            async with self.saver.backend.create_uow().begin_read() as uow:
                pending=await self.saver.backend.runs.resume_pending(uow,execution.guard.team_id,execution.guard.run_id)
                saved=await self.saver.backend.runs.checkpoint(uow,execution.guard.team_id,execution.guard.run_id)
            if pending is None: return False
            decision=ResumeApplier().apply(pending,saved)
            async with self.saver.backend.create_uow().begin_write() as uow:
                await self.saver.backend.lease_guard.require(uow,execution.guard)
                await self.saver.backend.runs.change(uow,'resume_requests',{'request_id':pending['request_id'],'status':'submitted'},
                    {'status':'applying','updated_at':await self.saver.backend.clock.now_ms(uow)})
            if not decision.already_applied:
                # Stop at the input-only node. Both sync durability and an explicit
                # persisted reread prevent tools from preceding the replay marker.
                await graph.ainvoke(Command(resume=decision.command),config,context=execution,
                    durability='sync',interrupt_after=['input'])
            async with self.saver.backend.create_uow().begin_write() as uow:
                await self.saver.backend.lease_guard.require(uow,execution.guard)
                persisted=await self.saver.backend.runs.checkpoint(uow,execution.guard.team_id,execution.guard.run_id)
                if persisted['values'].get('last_applied_resume_id')!=pending['request_id']:
                    raise ValueError('resume marker not persisted')
                if mark:
                    now=await self.saver.backend.clock.now_ms(uow)
                    await self.saver.backend.runs.change(uow,'resume_requests',{'request_id':pending['request_id'],'status':'applying'},
                        {'status':'applied','updated_at':now})
                    await self.saver.backend.runs.change(uow,'interrupts',{'interrupt_id':pending['interrupt_id']},
                        {'status':'consumed','updated_at':now})
            return True

    async def execute(self,execution):
        if await self.apply_resume(execution):
            config={'configurable':{'thread_id':execution.guard.run_id}}
            graph=self.registry.resolve(execution.claim.context.graph_version,self.registry.state_schema_version(execution.claim.context.graph_version))
            with self.guard_provider.bind(execution.guard):
                await graph.ainvoke(None,config,context=execution,durability='sync')
                return (await self.saver.aget_tuple(config)).config
        return await super().execute(execution)

class InterruptProgressProjector(FoundationProgressProjector):
    state_schema_version="dod-state-v2"
    async def project(self,uow,claim,result):
        from uuid import uuid4
        from workbench_agent.domain.transitions import WAIT_APPROVAL_MS,WAIT_INPUT_MS
        from workbench_agent.application.idempotency import canonical
        saved=await self.backend.runs.checkpoint(uow,claim.guard.team_id,claim.guard.run_id)
        if not saved: raise ValueError('checkpoint missing')
        if not saved['interrupts']:
            # Completion remains honest pending T08 Outcome validation/projection.
            return await super().project(uow,claim,result)
        if len(saved['interrupts'])!=1: raise ValueError('one named interrupt required')
        real=saved['interrupts'][0]; descriptor=real.value
        if descriptor.get('name') not in ('foundation.input','foundation.approval'):
            raise ValueError('unknown named interrupt')
        if not descriptor.get('actor') or descriptor['actor']!=claim.context.subject:
            raise ValueError('interrupt actor mismatch')
        from workbench_agent.runtime.graphs.interrupts import INPUT_SCHEMA
        approval=descriptor['kind']=='approval'
        if descriptor['schema']!=({'type':'approval-v1'} if approval else INPUT_SCHEMA): raise ValueError('unsupported response schema')
        now=await self.backend.clock.now_ms(uow)
        run=await self.backend.runs.one(uow,'runs',team_id=claim.guard.team_id,run_id=claim.guard.run_id)
        expires=min(run['wall_deadline_at'],now+(WAIT_APPROVAL_MS if approval else WAIT_INPUT_MS))
        existing=await self.backend.runs.one(uow,'interrupts',run_id=claim.guard.run_id,graph_interrupt_id=real.id)
        interrupt_id=existing['interrupt_id'] if existing else str(uuid4())
        if existing is None:
            await self.backend.runs.insert(uow,'interrupts',dict(team_id=claim.guard.team_id,interrupt_id=interrupt_id,
                run_id=claim.guard.run_id,graph_interrupt_id=real.id,kind=descriptor['kind'],status='open',schema=canonical(descriptor),
                args_hash=descriptor.get('args_hash'),expires_at=expires,revision=1,created_at=now,updated_at=now))
        elif existing['status']!='open' or json.loads(existing['schema'])!=descriptor:
            raise ValueError('interrupt projection conflict')
        else: expires=existing['expires_at']
        await self.backend.runs.change(uow,'runs',{'run_id':claim.guard.run_id},{'waiting_deadline_at':expires})
        await self.backend.execution_events.append_execution(uow,claim.guard,'approval.required' if approval else 'input.required',
            {'interrupt_id':interrupt_id,'name':descriptor['name'],'graph_interrupt_id':real.id,'checkpoint_id':saved['checkpoint_id'],'expires_at':expires})
        await self.backend.jobs.release(uow,claim.guard,status='waiting_approval' if approval else 'waiting_input')
