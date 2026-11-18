import pytest

def pytest_addoption(parser):
    parser.addoption("--run-live", action="store_true", default=False)

def pytest_collection_modifyitems(config, items):
    if not config.getoption("--run-live"):
        for item in items:
            if "live" in item.keywords:
                item.add_marker(pytest.mark.skip(reason="requires --run-live"))

@pytest.fixture
def config_data(tmp_path):
    return {"environment": "development", "asset_root": str(tmp_path),
            "storage": {"backend": "sqlite", "database_url": f"sqlite+aiosqlite:///{tmp_path}/db.sqlite3",
                        "persistent_root": str(tmp_path)}}

pytest_plugins = ['support.runs', 'support.platform', 'support.kb']

@pytest.fixture(autouse=True)
def fictional_model_reference(monkeypatch,request):
    # Live tests use the explicitly supplied environment; offline tests use no real credential.
    if request.node.get_closest_marker('live'): return
    monkeypatch.setenv('DEEPSEEK_API_KEY','fictional-test-key')
