import subprocess
import pytest
from workbench_agent.execution.snapshots import archive_commit


def git(repo, *args):
    return subprocess.check_output(['git', '-C', str(repo), *args], stderr=subprocess.DEVNULL).decode().strip()


@pytest.mark.asyncio
async def test_archive_uses_full_fixed_commit_and_ignores_worktree(tmp_path):
    repo = tmp_path / 'repo'; repo.mkdir()
    git(repo, 'init', '-q')
    git(repo, 'config', 'user.name', 'Tester')
    git(repo, 'config', 'user.email', 'test@example.invalid')
    (repo / 'app.py').write_text('old')
    git(repo, 'add', 'app.py'); git(repo, 'commit', '-qm', 'first')
    commit = git(repo, 'rev-parse', 'HEAD')
    original = await archive_commit(repo, commit)
    (repo / 'app.py').write_text('new')
    assert await archive_commit(repo, commit) == original
    with pytest.raises(ValueError):
        await archive_commit(repo, 'HEAD')


@pytest.mark.asyncio
async def test_archive_rejects_symlink(tmp_path):
    repo = tmp_path / 'repo'; repo.mkdir()
    git(repo, 'init', '-q'); git(repo, 'config', 'user.name', 'Tester'); git(repo, 'config', 'user.email', 'test@example.invalid')
    (repo / 'escape').symlink_to('/etc/passwd')
    git(repo, 'add', 'escape'); git(repo, 'commit', '-qm', 'unsafe')
    with pytest.raises(ValueError):
        await archive_commit(repo, git(repo, 'rev-parse', 'HEAD'))


def test_deployment_repository_binding_requires_absolute_local_path(tmp_path):
    from workbench_agent.settings import RepositorySource
    with pytest.raises(ValueError):
        RepositorySource(team_id='t', agent_id='w', repo_id='app', path='relative')
    source = RepositorySource(team_id='t', agent_id='w', repo_id='app', path=tmp_path)
    assert source.path == tmp_path
