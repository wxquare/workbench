import pytest


async def test_ordinary_run_cannot_issue_child_authority(platform):
    from support.skills import execution_for_run
    run=await platform.create_run(key='ordinary')
    parent=await execution_for_run(platform,run)
    with pytest.raises(PermissionError):
        with parent._tool_owner.expert_execution(parent, 'knowledge'):
            pass
