"""Test-only helpers for real admitted runs and guarded Worker claims."""
from uuid import uuid4
from workbench_agent.domain.profiles import ProfileDraft
from workbench_agent.jobs.worker import Worker, ExecutorRegistry
from support.jobs import claim

async def execution_for_run(platform, run):
    claimed = await claim(platform, worker_id='skill-worker-' + uuid4().hex)
    assert claimed is not None and claimed.guard.run_id == run.run_id
    worker = Worker(platform.backend, claimed.guard.worker_id, ExecutorRegistry())
    worker._active_tool_claims[id(claimed)] = claimed  # harness mirrors run_once after its real DB claim
    return worker._bind_tool_execution(claimed)

async def pinned_execution(platform, refs):
    current = await platform.contexts.resolve(platform.auth, platform.agent_id)
    agent = await platform.profiles.get_agent(platform.auth, platform.agent_id)
    draft = ProfileDraft(default_model=current.default_model, graph_version='dod.v1', skills=tuple(refs))
    changed = await platform.profiles.save_draft(platform.auth, platform.agent_id, draft, agent['revision'], key=str(uuid4()))
    await platform.profiles.publish(platform.auth, platform.agent_id, changed['revision'], key=str(uuid4()))
    run = await platform.create_run(key=str(uuid4()))
    return await execution_for_run(platform, run)
