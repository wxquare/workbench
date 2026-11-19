"""The SPA has a bounded resource root and cannot swallow API routes."""
import pytest


@pytest.mark.asyncio
async def test_spa_deep_links_and_assets(client, platform):
    root = platform.settings.asset_root / 'web' / 'dist'
    (root / 'assets').mkdir(parents=True)
    (root / 'index.html').write_text('<html>DoD app</html>')
    (root / 'assets' / 'app.js').write_text('console.log("app")')
    assert (await client.get('/app/', headers={'Accept': 'text/html'})).text == '<html>DoD app</html>'
    assert (await client.get('/app/runs/abc', headers={'Accept': 'text/html'})).text == '<html>DoD app</html>'
    assert (await client.get('/app/graphs/', headers={'Accept': 'text/html'})).text == '<html>DoD app</html>'
    assert (await client.get('/app/graphs/conversation.multi_turn', headers={'Accept': 'text/html'})).text == '<html>DoD app</html>'
    assert (await client.get('/app/graphs/unknown.graph', headers={'Accept': 'text/html'})).status_code == 404
    assert (await client.get('/app/graphs/unknown', headers={'Accept': 'text/html'})).status_code == 404
    assert (await client.get('/app/graphs/conversation.multi_turn')).status_code == 404
    assert (await client.get('/app/assets/app.js')).text == 'console.log("app")'
    assert (await client.get('/app/assets/missing.js', headers={'Accept': 'text/html'})).status_code == 404
    assert (await client.get('/app/assets/missing', headers={'Accept': 'text/html'})).status_code == 404
    assert (await client.get('/v1/nonexistent', headers={'Accept': 'text/html'})).status_code == 404


@pytest.mark.asyncio
async def test_spa_missing_build_is_reported(client):
    result = await client.get('/app/', headers={'Accept': 'text/html'})
    assert result.status_code == 503


@pytest.mark.asyncio
async def test_spa_index_cannot_escape_asset_root(client, platform, tmp_path):
    root = platform.settings.asset_root / 'web' / 'dist'
    root.mkdir(parents=True)
    secret = tmp_path / 'outside.txt'
    secret.write_text('must not leak')
    (root / 'index.html').symlink_to(secret)
    result = await client.get('/app/', headers={'Accept': 'text/html'})
    assert result.status_code == 503
    assert 'must not leak' not in result.text


@pytest.mark.asyncio
async def test_explicit_frontend_build_root_is_served(client, platform, tmp_path):
    root = tmp_path / "separate-frontend"
    root.mkdir()
    (root / "index.html").write_text("configured frontend")
    client._transport.app.state.settings = platform.settings.model_copy(update={"frontend_dist_root": root})
    response = await client.get("/app/", headers={"Accept": "text/html"})
    assert response.status_code == 200
    assert response.text == "configured frontend"


def test_frontend_root_rejects_relative_path(config_data):
    from pydantic import ValidationError
    from workbench_agent.settings import Settings
    with pytest.raises(ValidationError):
        Settings.model_validate({**config_data, "frontend_dist_root": "relative/dist"})


def test_frontend_environment_overrides_config(config_data, tmp_path, monkeypatch):
    import yaml
    from workbench_agent.settings import load_settings
    config = tmp_path / "config.yaml"
    config.write_text(yaml.safe_dump({**config_data, "frontend_dist_root": str(tmp_path / "yaml")}))
    monkeypatch.setenv("WORKBENCH_FRONTEND_DIST_ROOT", str(tmp_path / "env"))
    assert load_settings(config).frontend_dist_root == tmp_path / "env"
