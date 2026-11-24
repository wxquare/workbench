import pytest
from workbench_agent.runner.policy import RunnerRequest, build_container_args


def test_runner_enforces_isolation_limits_and_argv():
    request = RunnerRequest(
        image='sha256:' + 'a' * 64,
        argv=['python', '/skill/check.py'],
        snapshot_volume='dod-snapshot-' + 'b' * 32,
        task_volume='dod-task-' + 'c' * 32,
    )
    command = build_container_args(request, 'dod-run-' + 'd' * 32)
    assert command[:3] == ['docker', 'run', '--name']
    assert '--network=none' in command and '--read-only' in command
    assert '--cpus=2' in command and '--memory=2g' in command
    assert '--pids-limit=128' in command
    assert (
        '--mount=type=volume,src=dod-snapshot-' + 'b' * 32 + ',dst=/workspace,readonly' in command
    )
    assert command[-2:] == ['python', '/skill/check.py']


@pytest.mark.parametrize(
    'overrides',
    [
        {'image': 'ubuntu:latest'},
        {'argv': []},
        {'argv': ['sh', '-c', 'echo x']},
        {'snapshot_volume': '../secret'},
        {'timeout_seconds': 61},
        {'output_bytes': 20971521},
    ],
)
def test_runner_rejects_unsafe_requests(overrides):
    values = dict(
        image='sha256:' + 'a' * 64,
        argv=['python', '/skill/check.py'],
        snapshot_volume='dod-snapshot-' + 'b' * 32,
        task_volume='dod-task-' + 'c' * 32,
    )
    values.update(overrides)
    with pytest.raises(ValueError):
        RunnerRequest(**values)


def test_verified_script_wrapper_rejects_changed_volume_content(tmp_path):
    import hashlib
    import subprocess
    from workbench_agent.runner.policy import verified_script_argv

    script = tmp_path / 'approved.py'
    script.write_text('print("approved")')
    expected = hashlib.sha256(script.read_bytes()).hexdigest()
    argv = verified_script_argv('python3', str(script), expected)
    ok = subprocess.run(argv, capture_output=True, text=True)
    assert ok.returncode == 0 and ok.stdout.strip() == 'approved'
    script.write_text('print("changed")')
    changed = subprocess.run(argv, capture_output=True, text=True)
    assert changed.returncode != 0 and 'changed' not in changed.stdout


def test_verified_script_checks_full_snapshot_manifest(tmp_path):
    import hashlib
    import subprocess
    from workbench_agent.runner.policy import verified_script_argv

    script = tmp_path / 'check.py'
    script.write_text('print("ok")')
    source = tmp_path / 'code.py'
    source.write_text('value=1')
    digest = lambda p: hashlib.sha256(p.read_bytes()).hexdigest()
    manifest = {'check.py': digest(script), 'code.py': digest(source)}
    argv = verified_script_argv(
        'python3', str(script), digest(script), root=str(tmp_path), manifest=manifest
    )
    assert subprocess.run(argv, capture_output=True).returncode == 0
    source.write_text('value=2')
    assert subprocess.run(argv, capture_output=True).returncode != 0


def test_configured_docker_binary_uses_deployment_absolute_path(tmp_path):
    from workbench_agent.settings import StorageSettings
    from workbench_agent.runner.service import ContainerRunner

    binary = tmp_path / 'docker'
    config = StorageSettings(
        database_url='sqlite+aiosqlite:////tmp/example.db', docker_binary=binary
    )
    assert config.docker_binary == binary
    with pytest.raises(ValueError):
        StorageSettings(
            database_url='sqlite+aiosqlite:////tmp/example.db', docker_binary='relative/docker'
        )
    runner = ContainerRunner(str(binary))
    assert runner.docker_binary == str(binary)
    assert '/usr/local/bin' in runner.environment['PATH'].split(':')


def test_runner_receipt_redacts_known_secrets_before_bounding():
    from workbench_agent.tools.execution_executor import _safe_output

    value, truncated = _safe_output(
        b'Authorization: Bearer abcdef\npassword="foo bar"\nkey=sk-testsecret\n' + b'x' * 10000
    )
    assert '[REDACTED]' in value
    assert all(secret not in value for secret in ('abcdef', 'foo bar', 'sk-testsecret'))
    assert len(value.encode()) <= 8192 and truncated


def test_runner_receipt_redacts_unterminated_private_key():
    from workbench_agent.tools.execution_executor import _safe_output

    value, _ = _safe_output(b'normal\n-----BEGIN PRIVATE KEY-----\nunmarked-key-material')
    assert 'normal' in value and 'unmarked-key-material' not in value


def test_runner_binding_requires_stable_cleanup_daemon_identity(tmp_path):
    from workbench_agent.settings import StorageSettings

    binding = dict(
        alias='run',
        team_id='team',
        agent_id='agent',
        snapshot_id='snapshot',
        commit='a' * 40,
        snapshot_hash='b' * 64,
        image='sha256:' + 'c' * 64,
        snapshot_volume='dod-snapshot-' + 'd' * 32,
        script='check.py',
        script_sha256='e' * 64,
    )
    with pytest.raises(ValueError):
        StorageSettings(
            database_url='sqlite+aiosqlite:////tmp/example.db',
            skill_object_root=tmp_path,
            runner_bindings=[binding],
        )
    configured = StorageSettings(
        database_url='sqlite+aiosqlite:////tmp/example.db',
        skill_object_root=tmp_path,
        runner_bindings=[binding],
        runner_daemon_id='machine-one',
    )
    assert configured.runner_daemon_id == 'machine-one'


def test_docker_host_is_pinned_and_context_cannot_override_it():
    from workbench_agent.runner.service import ContainerRunner

    runner = ContainerRunner(
        docker_host='unix:///tmp/trusted-docker.sock',
        environment={'DOCKER_HOST': 'unix:///tmp/wrong.sock', 'DOCKER_CONTEXT': 'wrong'},
    )
    assert runner.environment['DOCKER_HOST'] == 'unix:///tmp/trusted-docker.sock'
    assert 'DOCKER_CONTEXT' not in runner.environment
    with pytest.raises(ValueError):
        ContainerRunner(docker_host='tcp://untrusted:2375')
