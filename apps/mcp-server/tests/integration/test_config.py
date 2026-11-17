from copy import deepcopy

import pytest

from tests.support.fixtures import config_data


@pytest.mark.parametrize(
    "change",
    ["unknown", "duplicate", "empty_scopes", "url_credentials", "secret_provider", "remote_http"],
)
def test_configuration_rejects_unsafe_or_ambiguous_values(change):
    from workbench_mcp_server.infrastructure.config.settings import Settings

    value = deepcopy(config_data())
    if change == "unknown":
        value["typo"] = True
    elif change == "duplicate":
        value["sources"].append(value["sources"][0])
    elif change == "empty_scopes":
        value["principals"][0]["scopes"] = []
    elif change == "url_credentials":
        value["sources"][0]["endpoint"] = "https://user:password@example.test/query"
    elif change == "secret_provider":
        value["sources"][0]["auth_secret_ref"] = "secret://unsupported"
    else:
        value["sources"][0]["endpoint"] = "http://remote.example.test/query"
    with pytest.raises(ValueError):
        Settings.model_validate(value)


def test_missing_secret_does_not_include_secret_value_in_error(monkeypatch):
    from workbench_mcp_server.infrastructure.config.secrets import EnvSecrets

    monkeypatch.delenv("ABSENT_TEST_SECRET", raising=False)
    with pytest.raises(ValueError, match="Missing configured secret"):
        EnvSecrets().resolve("env://ABSENT_TEST_SECRET")


def test_authentication_checks_all_bindings_and_rejects_unknown_token(monkeypatch):
    from workbench_mcp_server.infrastructure.auth.bearer import BearerAuthenticator
    from workbench_mcp_server.infrastructure.config.secrets import EnvSecrets
    from workbench_mcp_server.infrastructure.config.settings import Settings

    monkeypatch.setenv("TEST_AGENT_TOKEN", "test-agent-token-value")
    auth = BearerAuthenticator(Settings.model_validate(config_data()), EnvSecrets())
    assert auth.authenticate("test-agent-token-value").id == "agent"
    assert auth.authenticate("bad-token") is None


def test_knowledge_v2_requires_explicit_exclusive_source_isolation():
    from workbench_mcp_server.infrastructure.config.settings import Settings

    value = config_data()
    value["sources"][0]["adapter"] = "dp-knowledge-v2"
    with pytest.raises(ValueError):
        Settings.model_validate(value)


def test_source_isolation_is_never_assumed_by_default():
    from workbench_mcp_server.infrastructure.config.settings import Settings

    value = config_data()
    value["sources"][0].pop("scope_enforced", None)
    with pytest.raises(ValueError):
        Settings.model_validate(value)


@pytest.mark.parametrize(
    "endpoint",
    [
        "https://api.example:abc/query",
        "https://api.example:70000/query",
        "https://bad host/query",
        "https://api.example/\nquery",
    ],
)
def test_invalid_endpoint_is_rejected_during_configuration_check(endpoint):
    from workbench_mcp_server.infrastructure.config.settings import Settings

    value = config_data(endpoint)
    with pytest.raises(ValueError):
        Settings.model_validate(value)
