"""A private one-use MCP link reaches OAuth without a dashboard login cookie."""

import asyncio
from urllib.parse import urlsplit

from fastapi.testclient import TestClient

from gateway.mcp_user_links import create_user_connect_link
from tools.mcp_user_identity import MCPUserIdentity


def test_user_link_handoff_is_public_but_one_use(tmp_path, monkeypatch):
    from hermes_cli import web_server, web_server_mcp

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setattr("gateway.mcp_user_links._delegated_server", lambda name: name == "crm")
    monkeypatch.setattr("hermes_cli.profiles.get_active_profile_name", lambda: "default")
    monkeypatch.setattr("hermes_cli.dashboard_auth.prefix.resolve_public_url", lambda: "https://hermes.example")
    monkeypatch.setattr("hermes_cli.mcp_config._get_mcp_servers", lambda: {
        "crm": {"url": "https://mcp.example/mcp", "auth": "oauth", "oauth": {"identity": "messaging_user"}},
    })

    def fake_worker(flow, _cfg):
        asyncio.run(flow.publish_authorization_url("https://idp.example/authorize?state=one"))

    monkeypatch.setattr(web_server_mcp, "_run_dashboard_mcp_oauth", fake_worker)
    web_server_mcp._mcp_oauth_flows.clear()
    monkeypatch.setattr(web_server.app.state, "auth_required", True, raising=False)
    actor = MCPUserIdentity("slack", "T1", "U1")
    link = create_user_connect_link("crm", actor)
    path = urlsplit(link).path + "?" + urlsplit(link).query

    client = TestClient(web_server.app)
    assert client.get(path).status_code == 200
    assert client.get(path).status_code == 200  # link-preview GETs do not spend it
    started = client.post(path, follow_redirects=False)
    assert started.status_code == 303
    assert started.headers["location"] == "https://idp.example/authorize?state=one"
    assert len(web_server_mcp._mcp_oauth_flows) == 1
    flow = next(iter(web_server_mcp._mcp_oauth_flows.values()))
    assert flow.user_identity == actor
    assert client.post(path).status_code == 410
    web_server_mcp._mcp_oauth_flows.clear()


def test_user_oauth_worker_writes_only_actor_scoped_token(tmp_path, monkeypatch):
    from hermes_cli import web_server_mcp
    from tools.mcp_dashboard_oauth import DashboardOAuthFlow
    from tools.mcp_oauth import HermesTokenStorage
    from tools.mcp_user_identity import current_user_identity

    actor = MCPUserIdentity("teams", "tenant", "user")
    flow = DashboardOAuthFlow(
        flow_id="user-worker", server_name="crm", profile=None,
        hermes_home=str(tmp_path), redirect_uri="https://hermes.example/api/mcp/oauth/callback/crm",
        user_identity=actor,
    )
    seen = []

    def fake_probe(name, _cfg, *, connect_timeout):
        seen.append((name, current_user_identity(), connect_timeout))
        path = HermesTokenStorage(name, user_identity=actor)._tokens_path()
        path.parent.mkdir(parents=True)
        path.write_text('{"access_token":"user"}', encoding="utf-8")
        return [("crm_search", "Search CRM")]

    monkeypatch.setattr("hermes_cli.mcp_config._probe_single_server", fake_probe)
    monkeypatch.setattr("hermes_cli.mcp_config._save_mcp_server", lambda *_: seen.append("shared-save"))
    web_server_mcp._run_dashboard_mcp_oauth(flow, {
        "url": "https://mcp.example/mcp", "auth": "oauth", "oauth": {"identity": "messaging_user"},
    })
    assert flow.status == "approved"
    assert seen[0][1] == actor
    assert "shared-save" not in seen
    assert HermesTokenStorage("crm", hermes_home=tmp_path, user_identity=actor).has_cached_tokens()
    assert not HermesTokenStorage("crm", hermes_home=tmp_path).has_cached_tokens()
