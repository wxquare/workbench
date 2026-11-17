import pytest
from workbench_agent.settings import Settings, load_settings

@pytest.mark.parametrize("url", ["sqlite+aiosqlite:///:memory:", "sqlite+aiosqlite:///relative.sqlite3"])
def test_rejects_ephemeral_database(config_data, url):
    config_data["storage"]["database_url"] = url
    with pytest.raises(ValueError, match="persistent absolute"):
        Settings.model_validate(config_data)

@pytest.mark.parametrize("url", ["postgresql+psycopg://host/db", "sqlite:///absolute", "sqlite+aiosqlite:////tmp/db?mode=memory", "sqlite+aiosqlite://host/db"])
def test_rejects_invalid_driver_or_uri(config_data, url):
    config_data["storage"]["database_url"] = url
    with pytest.raises(ValueError):
        Settings.model_validate(config_data)

def test_production_rejects_development_identity(config_data):
    config_data["environment"] = "production"
    config_data["service"] = {"auth_mode": "development"}
    with pytest.raises(ValueError, match="identity"):
        Settings.model_validate(config_data)

def test_missing_asset_root_never_falls_back_to_cwd(config_data, monkeypatch):
    config_data.pop("asset_root")
    monkeypatch.delenv("WORKBENCH_ASSET_ROOT", raising=False)
    with pytest.raises(ValueError, match="asset_root"):
        Settings.model_validate(config_data)

def test_secret_reference_resolution_and_redaction(config_data, tmp_path, monkeypatch):
    import yaml
    url = config_data["storage"].pop("database_url")
    config_data["storage"]["database_url_ref"] = "env://WORKBENCH_DATABASE_URL"
    monkeypatch.setenv("WORKBENCH_DATABASE_URL", url)
    monkeypatch.setenv("WORKBENCH_ASSET_ROOT", str(tmp_path))
    path = tmp_path / "settings.yaml"
    path.write_text(yaml.safe_dump(config_data))
    settings = load_settings(path)
    assert settings.asset_root == tmp_path
    assert settings.storage.database_url.get_secret_value() == url
    assert url not in repr(settings)


def test_shared_skill_storage_can_be_configured_by_environment(config_data, tmp_path, monkeypatch):
    import yaml
    path = tmp_path / 'settings.yaml'
    path.write_text(yaml.safe_dump(config_data))
    monkeypatch.setenv('WORKBENCH_SKILL_OBJECT_ROOT', str(tmp_path / 'skill-objects'))
    assert load_settings(path).storage.skill_object_root == tmp_path / 'skill-objects'

@pytest.mark.parametrize("section,value", [("environment", "prodution"), ("worker", {"concurrency": 3}), ("jobs", {"lease_seconds": 44}), ("storage", {"sqlite": {"synchronous": "NORMAL"}})])
def test_invalid_operational_contract_rejected(config_data, section, value):
    if section == "storage":
        config_data[section].update(value)
    else:
        config_data[section] = value
    with pytest.raises(ValueError):
        Settings.model_validate(config_data)

@pytest.mark.parametrize("root", ["relative-assets", "/definitely/nonexistent/dod-assets"])
def test_invalid_environment_asset_root_rejected(config_data, monkeypatch, root):
    config_data.pop("asset_root")
    monkeypatch.setenv("WORKBENCH_ASSET_ROOT", root)
    with pytest.raises(ValueError, match="asset_root"):
        Settings.model_validate(config_data)


def test_missing_database_reference_rejected_without_secret(config_data, monkeypatch):
    config_data["storage"].pop("database_url")
    config_data["storage"]["database_url_ref"] = "env://MISSING_WORKBENCH_TEST_DATABASE"
    monkeypatch.delenv("MISSING_WORKBENCH_TEST_DATABASE", raising=False)
    with pytest.raises(ValueError, match="unset"):
        Settings.model_validate(config_data)

@pytest.mark.parametrize("storage", [None, "invalid-storage", []])
def test_doctor_invalid_storage_with_persistent_override_returns_json(tmp_path, monkeypatch, storage):
    import json
    import subprocess
    import sys
    import yaml

    monkeypatch.setenv("WORKBENCH_PERSISTENT_ROOT", str(tmp_path))
    path = tmp_path / "invalid-storage.yaml"
    path.write_text(yaml.safe_dump({"storage": storage}))
    process = subprocess.run(
        [sys.executable, "-m", "workbench_agent.cli.main", "doctor", "--config", str(path)],
        capture_output=True, text=True,
    )
    assert process.returncode == 1
    assert process.stderr == ""
    assert json.loads(process.stdout) == {
        "ready": False, "errors": ["invalid or unreadable configuration"]
    }
