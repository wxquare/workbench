import os
import subprocess
import sys
from pathlib import Path

import yaml

from tests.support.fixtures import config_data
from tests.support.http_server import upstream


def run_check(config, tmp_path, missing=False):
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(config))
    env = {
        **os.environ,
        "PYTHONPATH": str(Path("src").resolve()),
        "TEST_AGENT_TOKEN": "private-inbound-value",
        "TEST_UPSTREAM_TOKEN": "private-outbound-value",
    }
    if missing:
        env.pop("TEST_UPSTREAM_TOKEN")
    return subprocess.run(
        [
            sys.executable,
            "-m",
            "workbench_mcp_server.interfaces.cli",
            "check",
            "--config",
            str(path),
        ],
        env=env,
        capture_output=True,
        text=True,
        timeout=5,
    )


def test_check_validates_configuration_without_discovery_or_business_calls(tmp_path):
    with upstream({"matches": []}) as (endpoint, requests):
        result = run_check(config_data(endpoint), tmp_path)
    assert result.returncode == 0, result.stderr
    assert '"business_query": "not_checked"' in result.stdout
    assert requests == []
    assert "private-inbound-value" not in result.stdout + result.stderr
    assert "private-outbound-value" not in result.stdout + result.stderr


def test_check_missing_secret_fails_without_traceback_or_secret_values(tmp_path):
    result = run_check(config_data(), tmp_path, missing=True)
    assert result.returncode == 1
    assert "Traceback" not in result.stderr
    assert "private-inbound-value" not in result.stdout + result.stderr


def test_invalid_yaml_does_not_echo_configuration_contents(tmp_path):
    path = tmp_path / "broken.yaml"
    path.write_text("server: [invalid-private-value\n")
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "workbench_mcp_server.interfaces.cli",
            "check",
            "--config",
            str(path),
        ],
        env={**os.environ, "PYTHONPATH": str(Path("src").resolve())},
        capture_output=True,
        text=True,
        timeout=5,
    )
    assert result.returncode == 1
    assert "invalid-private-value" not in result.stderr
    assert "Traceback" not in result.stderr
