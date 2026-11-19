import os
import shutil
from pathlib import Path
import subprocess
import sys
import zipfile
import pytest


class SubprocessEnvironment(dict):
    """Preserve child environment semantics without exposing values in failures."""
    def __repr__(self): return "<subprocess environment redacted>"


def run_test_process(*args,**kwargs):
    """Keep stdlib launch locals and captured child output out of pytest failures."""
    try:
        return subprocess.run(*args,**kwargs)
    except (OSError,subprocess.SubprocessError) as error:
        failure_type=type(error).__name__
        returncode=getattr(error,'returncode',None)
    # Outside the except block: discard the original traceback/context, whose
    # Popen internals can contain an expanded env_list despite a redacted mapping.
    raise RuntimeError(f'test subprocess failed: {failure_type}, returncode={returncode}; captured output omitted') from None


@pytest.mark.parametrize('failure',['exit','launch'])
def test_subprocess_failure_traceback_redacts_environment(failure):
    import traceback
    environment=SubprocessEnvironment({'TEST_ONLY_SECRET':'fake-secret-for-redaction-test'})
    command=[sys.executable,'-c','raise SystemExit(3)'] if failure=='exit' else ['/definitely/absent/dod-test-executable']
    try:
        run_test_process(command,env=environment,check=True,capture_output=True)
    except (OSError,subprocess.SubprocessError,RuntimeError) as error:
        rendered=''.join(traceback.TracebackException.from_exception(error,capture_locals=True).format())
    else: raise AssertionError('expected child failure')
    assert 'fake-secret-for-redaction-test' not in rendered
    assert environment['TEST_ONLY_SECRET']=='fake-secret-for-redaction-test'


def copy_project_source(project, source):
    shutil.copytree(project, source, ignore=shutil.ignore_patterns(".venv", "var", ".pytest_cache", "__pycache__", "build", "dist", "*.egg-info", "*.pcap", "*.pcapng"))


def test_project_copy_excludes_local_virtual_environment(tmp_path):
    project = tmp_path / "project"
    (project / ".venv" / "lib").mkdir(parents=True)
    (project / ".venv" / "lib" / "dependency.py").write_text("dependency")
    (project / "pyproject.toml").write_text("[project]\n")
    (project / "var").mkdir()
    (project / "var" / "deepseek.env").write_text("FAKE_TEST_CREDENTIAL=not-a-secret")
    (project / "capture.pcap").write_bytes(b"fake packet capture")
    (project / "trace.pcapng").write_bytes(b"fake packet capture")
    source = tmp_path / "source"
    copy_project_source(project, source)
    assert not (source / ".venv").exists()
    assert not (source / "var").exists()
    assert not (source / "capture.pcap").exists()
    assert not (source / "trace.pcapng").exists()
    assert (source / "pyproject.toml").is_file()


def test_installed_wheel_works_outside_repository(tmp_path):
    project = Path(__file__).resolve().parents[2]
    source = tmp_path / "source"
    copy_project_source(project, source)
    wheels = tmp_path / "wheels"
    run_test_process([sys.executable, "-m", "build", "--wheel", "--no-isolation", "--outdir", str(wheels), str(source)], check=True, capture_output=True)
    wheel, = wheels.glob("*.whl")
    with zipfile.ZipFile(wheel) as archive:
        assert not any(name.split('/')[0] in {"skills", "prompts", "config", "web"} for name in archive.namelist())
        assert not any(name.startswith("runner/") and not name.endswith(".py") for name in archive.namelist())
    env = tmp_path / "clean"
    run_test_process(["uv", "venv", "--python", sys.executable, str(env)], check=True, capture_output=True)
    run_test_process(["uv", "pip", "install", "--python", str(env / "bin/python"), str(wheel)], check=True, capture_output=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    environment = SubprocessEnvironment(os.environ)
    environment.pop("PYTHONPATH", None)
    environment["WORKBENCH_ASSET_ROOT"] = str(outside)
    with zipfile.ZipFile(wheel) as archive:
        wheel_names = set(archive.namelist())
    assert {'workbench_agent/api/frontend.py', 'workbench_agent/delivery/dispatcher.py', 'workbench_agent/persistence/migrations/env.py'} <= wheel_names
    code = "import workbench_agent.api.app, workbench_agent.delivery.dispatcher, workbench_agent.evaluation.report"
    code += ";from workbench_agent.settings import Settings; import os; settings = Settings.model_validate({'storage': {'database_url': 'sqlite+aiosqlite:////var/lib/workbench/test.sqlite3'}}); assert str(settings.asset_root) == os.environ['WORKBENCH_ASSET_ROOT']"
    code += ";from workbench_agent.runtime.checkpoints.portable import PortableCheckpointSaver; from workbench_agent.runtime.graphs.registry import foundation_worker_registry; from workbench_agent.persistence.backends.sqlite import SQLiteBackend; registry = foundation_worker_registry(SQLiteBackend(settings.storage.database_url.get_secret_value())); versions = registry.capabilities().graph_ids; assert set(versions) == {'sop.conditional', 'rca.synthetic', 'rca.proposal', 'rca.batch_proposal', 'knowledge.scoped_excerpt', 'conversation.single_turn', 'conversation.multi_turn', 'knowledge.citation_review'}; assert all(registry._executors[v].registry.resolve(v, registry._executors[v].registry.state_schema_version(v)) for v in versions)"
    run_test_process([str(env / "bin/python"), "-c", code], cwd=outside, env=environment, check=True, capture_output=True)
    help_result = run_test_process([str(env / "bin/workbench"), "--help"], cwd=outside, env=environment, check=True, capture_output=True, text=True)
    assert "doctor" in help_result.stdout

    # The installed wheel must create an API app and serve separately supplied assets.
    frontend = outside / "web" / "dist"
    frontend.mkdir(parents=True)
    (frontend / "index.html").write_text("installed wheel frontend")
    api_code = """
import asyncio
from httpx import ASGITransport, AsyncClient
from workbench_agent.settings import Settings
from workbench_agent.api.app import create_app
settings = Settings.model_validate({'storage': {'database_url': 'sqlite+aiosqlite:////var/lib/workbench/test.sqlite3'}})
app = create_app(settings)
async def check():
    async with AsyncClient(transport=ASGITransport(app=app), base_url='http://test') as client:
        response = await client.get('/app/', headers={'Accept': 'text/html'})
        assert response.status_code == 200 and response.text == 'installed wheel frontend'
asyncio.run(check())
"""
    run_test_process([str(env / "bin/python"), "-c", api_code], cwd=outside, env=environment, check=True, capture_output=True)
    dataset = source / "tests" / "fixtures" / "business" / "manifest.json"
    report = outside / "quality-report.json"
    evaluation = run_test_process([str(env / "bin/workbench"), "evaluate", "--dataset", str(dataset), "--output", str(report)], cwd=outside, env=environment, capture_output=True, text=True)
    assert evaluation.returncode == 1  # pending reviews deliberately fail the release gate
    import json
    assert json.loads(report.read_text())["total_cases"] == 30

    import yaml
    config_path = outside / "local.yaml"
    config_path.write_text(yaml.safe_dump({"environment":"development","asset_root":str(outside),
        "storage":{"database_url":f"sqlite+aiosqlite:///{outside}/storage.db","persistent_root":str(outside)}}))
    migrated = run_test_process([str(env / "bin/workbench"), "db", "migrate", "--config", str(config_path)],
        cwd=outside, env=environment, capture_output=True, text=True)
    assert migrated.returncode == 0, migrated.stdout + migrated.stderr
    import json
    assert json.loads(migrated.stdout)["ready"] is True


def test_wheel_contains_namespaced_frontend_and_delivery(tmp_path):
    project = Path(__file__).resolve().parents[2]
    wheels = tmp_path / "wheels"
    run_test_process([sys.executable, "-m", "build", "--wheel", "--no-isolation", "--outdir", str(wheels), str(project)], check=True, capture_output=True)
    with zipfile.ZipFile(next(wheels.glob("*.whl"))) as wheel:
        names = set(wheel.namelist())
    assert {"workbench_agent/api/frontend.py", "workbench_agent/delivery/dispatcher.py", "workbench_agent/persistence/migrations/env.py"} <= names
    assert not any(name.startswith("dod_agent/") for name in names)
