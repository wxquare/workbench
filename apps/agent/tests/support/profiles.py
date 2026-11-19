"""Published model binding helpers use real publication services."""
from uuid import uuid4
from workbench_agent.domain.profiles import ProfileDraft

async def publish_agent_default(profiles, auth, agent_id, model_ref):
    current = await profiles.get_agent(auth, agent_id)
    changed = await profiles.save_draft(auth, agent_id, ProfileDraft(default_model=model_ref,graph_id="conversation.multi_turn"),
        expected_revision=current['revision'], key=str(uuid4()))
    return await profiles.publish(auth, agent_id, expected_revision=changed['revision'], key=str(uuid4()))
