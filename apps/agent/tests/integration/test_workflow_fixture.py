import pytest

from workbench_agent.domain.workflows import WorkflowDefinition


@pytest.mark.asyncio
async def test_fixture_creates_distinct_immutable_versions(platform):
    from support.workflows import create_workflow_version

    first_definition = WorkflowDefinition.model_validate(
        {'name': 'fixture', 'steps': [{'step_id': 'a', 'tool': 'read'}]}
    )
    second_definition = WorkflowDefinition.model_validate(
        {'name': 'fixture', 'steps': [{'step_id': 'b', 'tool': 'read'}]}
    )
    first = await create_workflow_version(platform, first_definition)
    second = await create_workflow_version(platform, second_definition)
    assert (first.version, second.version) == (1, 2)
    assert first.workflow_version_id != second.workflow_version_id
    assert first.content_hash != second.content_hash
    async with platform.backend.create_uow().begin_read() as uow:
        row = await platform.backend.configuration.one(
            uow,
            'workflow_versions',
            team_id=platform.auth.team_id,
            workflow_version_id=first.workflow_version_id,
        )
    assert row['hash'] == first.content_hash
