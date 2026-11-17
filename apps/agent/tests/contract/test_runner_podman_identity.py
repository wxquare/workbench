import pytest

from workbench_agent.runner.service import ContainerRunner


@pytest.mark.asyncio
async def test_podman_identity_checks_preprovisioned_volume_and_fails_closed(tmp_path):
    cli = tmp_path / 'docker'
    cli.write_text('''#!/usr/bin/env python3
import os, sys
from pathlib import Path
Path(os.environ["TEST_LOG"]).open("a").write(" ".join(sys.argv[1:]) + "\\n")
if sys.argv[1:3] == ["volume", "inspect"]:
    if Path(os.environ["MARKER_PRESENT"]).exists():
        print(sys.argv[-1]); sys.exit(0)
    sys.exit(1)
sys.exit(0)
''')
    cli.chmod(0o755)
    marker = tmp_path / 'marker'
    log = tmp_path / 'log'
    identity = 'podman:' + 'a' * 32
    runner = ContainerRunner(str(cli), identity_mode='podman_volume',
        environment={'TEST_LOG': str(log), 'MARKER_PRESENT': str(marker)},
        docker_host='unix:///tmp/approved.sock')
    with pytest.raises(RuntimeError, match='identity'):
        await runner.require_daemon(identity)
    marker.touch()
    assert await runner.require_daemon(identity) == identity
    await runner.cleanup_resources('dod-run-' + 'b' * 32, 'dod-task-' + 'c' * 32,
        daemon_id=identity)
    calls = log.read_text().splitlines()
    assert sum(line.startswith('volume inspect ') for line in calls) >= 7
    assert 'volume create' not in log.read_text()
    with pytest.raises(RuntimeError, match='identity'):
        await runner.require_daemon('engine-id')


@pytest.mark.asyncio
async def test_podman_cleanup_kills_before_removing_container(tmp_path):
    cli = tmp_path / 'docker'
    log = tmp_path / 'log'
    cli.write_text('''#!/usr/bin/env python3
import os,sys
from pathlib import Path
Path(os.environ["TEST_LOG"]).open("a").write(" ".join(sys.argv[1:])+"\\n")
if sys.argv[1:3]==["volume","inspect"]: print(sys.argv[-1])
''')
    cli.chmod(0o755)
    runner = ContainerRunner(str(cli), identity_mode='podman_volume',environment={'TEST_LOG':str(log)})
    name='dod-run-'+'b'*32
    await runner.cleanup_resources(name,'dod-task-'+'c'*32,daemon_id='podman:'+'a'*32)
    calls=log.read_text().splitlines()
    assert calls.index('kill '+name) < calls.index('rm -f '+name)


def test_podman_identity_mode_is_explicit_and_requires_uuid():
    from pydantic import ValidationError
    from workbench_agent.settings import StorageSettings
    url = 'sqlite+aiosqlite:////tmp/example.db'
    assert StorageSettings(database_url=url).runner_identity_mode == 'docker_engine'
    with pytest.raises(ValidationError):
        StorageSettings(database_url=url, runner_identity_mode='podman_volume', runner_daemon_id='engine-A')
    with pytest.raises(ValidationError):
        StorageSettings(database_url=url, runner_daemon_id='podman:' + 'a' * 32)
    assert StorageSettings(database_url=url, runner_identity_mode='podman_volume',
        runner_daemon_id='podman:' + 'a' * 32).runner_daemon_id.startswith('podman:')


@pytest.mark.asyncio
async def test_missing_identity_volume_never_removes_resources(tmp_path):
    log=tmp_path/'log'; cli=tmp_path/'docker'
    cli.write_text('''#!/usr/bin/env python3
import os,sys
from pathlib import Path
Path(os.environ["TEST_LOG"]).open("a").write(" ".join(sys.argv[1:])+"\\n")
if sys.argv[1:3]==["volume","inspect"]: sys.exit(1)
''')
    cli.chmod(0o755)
    runner=ContainerRunner(str(cli),identity_mode='podman_volume',environment={'TEST_LOG':str(log)})
    with pytest.raises(RuntimeError,match='identity'):
        await runner.cleanup_resources('dod-run-'+'b'*32,'dod-task-'+'c'*32,daemon_id='podman:'+'a'*32)
    assert ' rm ' not in (' '+log.read_text())
    assert 'kill ' not in log.read_text()
