import pytest
from pydantic import ValidationError
from workbench_agent.domain.profiles import ProfileDraft, VersionRef


def test_legacy_profile_canonical_form_omits_missing_freshness():
    model = VersionRef(resource_id='model', version='1', content_hash='a' * 64)
    profile = ProfileDraft(default_model=model)
    assert 'knowledge_max_age_seconds' not in profile.model_dump(mode='json')
    with pytest.raises(ValidationError):
        ProfileDraft(default_model=model, knowledge_max_age_seconds=0)
    with pytest.raises(ValidationError):
        ProfileDraft(default_model=model, knowledge_max_age_seconds=True)


@pytest.mark.asyncio
async def test_published_profile_freezes_freshness_in_run_context(platform):
    before = await platform.contexts.resolve(platform.auth, platform.agent_id)
    agent = await platform.profiles.get_agent(platform.auth, platform.agent_id)
    draft = await platform.profiles.save_draft(platform.auth, platform.agent_id,
        ProfileDraft(default_model=before.default_model, graph_version='dod.v16', knowledge_max_age_seconds=3600),
        agent['revision'], key='freshness-draft')
    await platform.profiles.publish(platform.auth, platform.agent_id, draft['revision'], key='freshness-publish')
    accepted = await platform.contexts.resolve(platform.auth, platform.agent_id)
    agent = await platform.profiles.get_agent(platform.auth, platform.agent_id)
    draft = await platform.profiles.save_draft(platform.auth, platform.agent_id,
        ProfileDraft(default_model=before.default_model, graph_version='dod.v16', knowledge_max_age_seconds=7200),
        agent['revision'], key='freshness-draft-2')
    await platform.profiles.publish(platform.auth, platform.agent_id, draft['revision'], key='freshness-publish-2')
    after = await platform.contexts.resolve(platform.auth, platform.agent_id)
    assert accepted.knowledge_max_age_seconds == 3600
    assert after.knowledge_max_age_seconds == 7200
    assert before.knowledge_max_age_seconds is None
