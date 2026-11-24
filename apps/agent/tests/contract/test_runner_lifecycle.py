import asyncio
from pathlib import Path
import pytest
from workbench_agent.runner.policy import RunnerRequest
from workbench_agent.runner.service import ContainerRunner


def fake_cli(tmp_path):
    log = tmp_path / 'commands'
    cli = tmp_path / 'fake-docker'
    cli.write_text(
        '#!/usr/bin/env python3\nimport sys,time\nfrom pathlib import Path\nPath('
        + repr(str(log))
        + ').open("a").write(" ".join(sys.argv[1:3])+"\\n")\nif sys.argv[1] == "info": print("daemon-one");sys.exit(0)\nif sys.argv[1] == "start":\n print("started", flush=True); time.sleep(60)\n'
    )
    cli.chmod(0o755)
    return cli, log


def request(daemon_id='daemon-one'):
    return RunnerRequest(
        image='sha256:' + 'a' * 64,
        argv=['python', '/skill/check.py'],
        snapshot_volume='dod-snapshot-' + 'b' * 32,
        task_volume='dod-task-' + 'c' * 32,
        timeout_seconds=5,
        daemon_id=daemon_id,
    )


async def test_timeout_force_removes_container(tmp_path):
    cli, log = fake_cli(tmp_path)
    result = await ContainerRunner(str(cli)).execute(request())
    assert result.status == 'timeout'
    assert b'started' in result.stdout
    assert 'create --name' in log.read_text() and 'rm -f' in log.read_text()
    assert 'volume create' in log.read_text()
    assert 'volume rm' in log.read_text()


async def test_cancel_force_removes_container(tmp_path):
    cli, log = fake_cli(tmp_path)
    values = request()
    task = asyncio.create_task(ContainerRunner(str(cli)).execute(values))
    for _ in range(100):
        if log.exists() and 'start' in log.read_text():
            break
        await asyncio.sleep(0.01)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert 'rm -f' in log.read_text()


async def test_cancel_does_not_hide_cleanup_error():
    from workbench_agent.tools.execution_executor import cancel_runner

    async def runner():
        try:
            await asyncio.sleep(60)
        finally:
            raise RuntimeError('cleanup failed')

    task = asyncio.create_task(runner())
    await asyncio.sleep(0)
    with pytest.raises(RuntimeError, match='cleanup failed'):
        await cancel_runner(task)


async def test_cancel_during_volume_create_still_attempts_removal(tmp_path):
    cli = tmp_path / 'cli'
    log = tmp_path / 'log'
    cli.write_text(
        '#!/usr/bin/env python3\nimport sys,time\nfrom pathlib import Path\nPath('
        + repr(str(log))
        + ').open("a").write(" ".join(sys.argv[1:3])+"\\n")\nif sys.argv[1] == "info": print("daemon-one");sys.exit(0)\nif sys.argv[1:3] == ["volume", "create"]: time.sleep(60)\n'
    )
    cli.chmod(0o755)
    task = asyncio.create_task(ContainerRunner(str(cli)).execute(request()))
    for _ in range(200):
        if log.exists() and 'volume create' in log.read_text():
            break
        await asyncio.sleep(0.01)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert 'volume rm' in log.read_text()


async def test_cancel_reports_failed_container_and_volume_cleanup(tmp_path):
    cli = tmp_path / 'cli'
    log = tmp_path / 'log'
    cli.write_text(
        '#!/usr/bin/env python3\nimport sys,time\nfrom pathlib import Path\nPath('
        + repr(str(log))
        + ').open("a").write(" ".join(sys.argv[1:3])+"\\n")\nif sys.argv[1] == "info": print("daemon-one");sys.exit(0)\nif sys.argv[1] == "start": time.sleep(60)\nif sys.argv[1] == "rm" or sys.argv[1:3] == ["volume", "rm"]: sys.exit(1)\n'
    )
    cli.chmod(0o755)
    task = asyncio.create_task(ContainerRunner(str(cli)).execute(request()))
    for _ in range(200):
        if log.exists() and 'start' in log.read_text():
            break
        await asyncio.sleep(0.01)
    task.cancel()
    with pytest.raises(RuntimeError, match='cleanup'):
        await task
    assert 'rm -f' in log.read_text() and 'volume rm' in log.read_text()


async def test_repeated_cancellation_waits_for_cleanup_failure(tmp_path):
    cli = tmp_path / 'cli'
    log = tmp_path / 'log'
    cli.write_text(
        '#!/usr/bin/env python3\nimport sys,time\nfrom pathlib import Path\nPath('
        + repr(str(log))
        + ').open("a").write(" ".join(sys.argv[1:3])+"\\n")\nif sys.argv[1] == "info": print("daemon-one");sys.exit(0)\nif sys.argv[1] == "start": time.sleep(60)\nif sys.argv[1] == "rm": time.sleep(.3);sys.exit(1)\n'
    )
    cli.chmod(0o755)
    task = asyncio.create_task(ContainerRunner(str(cli)).execute(request()))
    for _ in range(200):
        if log.exists() and 'start' in log.read_text():
            break
        await asyncio.sleep(0.01)
    task.cancel()
    for _ in range(200):
        if 'rm -f' in log.read_text():
            break
        await asyncio.sleep(0.01)
    task.cancel()
    with pytest.raises(RuntimeError, match='cleanup'):
        await task
    assert 'volume rm' in log.read_text()


@pytest.mark.parametrize('slow', ['container', 'volume'])
async def test_cleanup_timeout_is_reported_and_both_removals_attempted(tmp_path, slow):
    cli = tmp_path / 'cli'
    log = tmp_path / 'log'
    cli.write_text(
        '#!/usr/bin/env python3\nimport sys,time\nfrom pathlib import Path\nPath('
        + repr(str(log))
        + ').open("a").write(" ".join(sys.argv[1:3])+"\\n")\nif sys.argv[1] == "info": print("daemon-one");sys.exit(0)\nif sys.argv[1] == "start": time.sleep(60)\nif '
        + repr(slow)
        + ' == "container" and sys.argv[1] == "rm": time.sleep(60)\nif '
        + repr(slow)
        + ' == "volume" and sys.argv[1:3] == ["volume", "rm"]: time.sleep(60)\n'
    )
    cli.chmod(0o755)
    task = asyncio.create_task(ContainerRunner(str(cli), cleanup_timeout=0.1).execute(request()))
    for _ in range(200):
        if log.exists() and 'start' in log.read_text():
            break
        await asyncio.sleep(0.01)
    task.cancel()
    with pytest.raises(RuntimeError, match='timed out'):
        await task
    assert 'rm -f' in log.read_text() and 'volume rm' in log.read_text()


async def test_cleanup_treats_confirmed_absent_resources_as_success(tmp_path):
    cli = tmp_path / 'docker'
    cli.write_text(
        '#!/usr/bin/env python3\nimport sys\nif sys.argv[1]=="info": print("daemon-one");sys.exit(0)\nkind="container" if sys.argv[1]=="rm" else "volume"\nprint("Error: No such "+kind+": "+sys.argv[-1],file=sys.stderr)\nsys.exit(1)\n'
    )
    cli.chmod(0o755)
    runner = ContainerRunner(str(cli))
    await runner.cleanup_resources(
        'dod-run-' + 'a' * 32, 'dod-task-' + 'b' * 32, daemon_id='daemon-one'
    )
    await runner.cleanup_resources(
        'dod-run-' + 'a' * 32, 'dod-task-' + 'b' * 32, daemon_id='daemon-one'
    )


async def test_wrong_daemon_cannot_create_any_resource(tmp_path):
    cli = tmp_path / 'cli'
    log = tmp_path / 'log'
    cli.write_text(
        '#!/usr/bin/env python3\nimport sys\nfrom pathlib import Path\nPath('
        + repr(str(log))
        + ').open("a").write(" ".join(sys.argv[1:3])+"\\n")\nif sys.argv[1]=="info": print("engine-B")\n'
    )
    cli.chmod(0o755)
    with pytest.raises(RuntimeError, match='daemon'):
        await ContainerRunner(str(cli)).execute(request('engine-A'))
    assert 'volume create' not in log.read_text() and 'create --name' not in log.read_text()


async def test_daemon_switch_during_creation_never_acknowledges_resource(tmp_path):
    from workbench_agent.runner.policy import RunnerRequest

    mode = tmp_path / 'mode'
    mode.write_text('A')
    log = tmp_path / 'log'
    cli = tmp_path / 'cli'
    cli.write_text(
        '#!/usr/bin/env python3\nimport sys\nfrom pathlib import Path\nmode=Path('
        + repr(str(mode))
        + ');log=Path('
        + repr(str(log))
        + ')\nlog.open("a").write(" ".join(sys.argv[1:3])+"\\n")\nif sys.argv[1]=="info": print("engine-"+mode.read_text());sys.exit(0)\nif sys.argv[1:3]==["volume","create"]: mode.write_text("B")\n'
    )
    cli.chmod(0o755)
    stages = []

    async def lifecycle(stage):
        stages.append(stage)

    req = RunnerRequest(
        image='sha256:' + 'a' * 64,
        argv=['python', '/skill/check.py'],
        snapshot_volume='dod-snapshot-' + 'b' * 32,
        task_volume='dod-task-' + 'c' * 32,
        daemon_id='engine-A',
        lifecycle=lifecycle,
    )
    with pytest.raises(RuntimeError, match='daemon'):
        await ContainerRunner(str(cli)).execute(req)
    assert stages == ['volume_dispatch']
    assert 'create --name' not in log.read_text()
