"""No user-scoped OAuth lookup may fall back to a profile-shared credential."""

import pytest

from tools.mcp_user_identity import MCPUserIdentity


def test_identity_requires_verified_shape():
    with pytest.raises(ValueError):
        MCPUserIdentity("telegram", "tenant", "user")
    with pytest.raises(ValueError):
        MCPUserIdentity("slack", "", "user")


def test_token_storage_isolated_by_platform_scope_and_user(tmp_path, monkeypatch):
    from tools.mcp_oauth import HermesTokenStorage

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    a = MCPUserIdentity("slack", "T1", "U1")
    b = MCPUserIdentity("slack", "T1", "U2")
    c = MCPUserIdentity("slack", "T2", "U1")
    d = MCPUserIdentity("teams", "T1", "U1")
    shared = HermesTokenStorage("crm")
    shared._tokens_path().parent.mkdir(parents=True)
    shared._tokens_path().write_text("shared", encoding="utf-8")

    paths = {HermesTokenStorage("crm", user_identity=actor)._tokens_path() for actor in (a, b, c, d)}
    assert len(paths) == 4
    assert shared._tokens_path() not in paths
    assert all(not HermesTokenStorage("crm", user_identity=actor).has_cached_tokens()
               for actor in (a, b, c, d))


def test_manager_keys_live_providers_by_user(tmp_path):
    from tools.mcp_oauth_manager import MCPOAuthManager

    manager = MCPOAuthManager()
    a = MCPUserIdentity("slack", "T1", "U1")
    b = MCPUserIdentity("slack", "T1", "U2")
    keys = {manager._key("crm", tmp_path), manager._key("crm", tmp_path, a),
            manager._key("crm", tmp_path, b)}
    assert len(keys) == 3


def test_user_scope_keeps_ordinary_mcp_but_never_borrows_shared_delegated_connection(tmp_path, monkeypatch):
    from types import SimpleNamespace

    from tools import mcp_tool as core
    from tools.mcp_tool_scope import _resolve_server_key
    from tools.mcp_user_identity import mcp_user_identity, user_registry_scope
    from tools.registry import session_tool_scope

    ordinary = SimpleNamespace(_config={"url": "https://mcp.example/ordinary", "auth": "oauth"})
    delegated = SimpleNamespace(_config={
        "url": "https://mcp.example/delegated", "auth": "oauth", "oauth": {"identity": "messaging_user"},
    })
    monkeypatch.setitem(core._servers, "ordinary", ordinary)
    monkeypatch.setitem(core._servers, "delegated", delegated)
    actor = MCPUserIdentity("slack", "T1", "U1")
    scope = user_registry_scope(actor, tmp_path)
    with mcp_user_identity(actor), session_tool_scope(scope):
        assert _resolve_server_key("ordinary") == "ordinary"
        assert _resolve_server_key("delegated") == (scope, "delegated")
    # A config edit can mark a still-live old shared connection as delegated.
    # The current turn's policy, not that old connection's config, wins.
    with mcp_user_identity(actor, delegated_servers=("ordinary",)), session_tool_scope(scope):
        assert _resolve_server_key("ordinary") == (scope, "ordinary")


def test_delegated_server_is_invisible_to_unscoped_discovery(tmp_path):
    from tools.mcp_tool_discovery import _visible_servers_for_actor
    from tools.mcp_user_identity import mcp_user_identity, user_registry_scope
    from tools.registry import session_tool_scope

    servers = {
        "ordinary": {"url": "https://mcp.example/ordinary", "auth": "oauth"},
        "delegated": {"url": "https://mcp.example/delegated", "auth": "oauth",
                      "oauth": {"identity": "messaging_user"}},
    }
    assert set(_visible_servers_for_actor(servers)) == {"ordinary"}
    actor = MCPUserIdentity("teams", "tenant", "user")
    with mcp_user_identity(actor), session_tool_scope(user_registry_scope(actor, tmp_path)):
        assert set(_visible_servers_for_actor(servers)) == {"delegated"}


def test_removing_user_token_blocks_existing_live_connection(tmp_path, monkeypatch):
    from types import SimpleNamespace

    from tools import mcp_tool as core
    from tools.mcp_oauth import HermesTokenStorage
    from tools.mcp_tool_discovery import _get_connected_server_for_call
    from tools.mcp_user_identity import mcp_user_identity, user_registry_scope
    from tools.registry import session_tool_scope

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    actor = MCPUserIdentity("slack", "T1", "U1")
    scope = user_registry_scope(actor, tmp_path)
    server = SimpleNamespace(session=object())
    monkeypatch.setitem(core._servers, (scope, "crm"), server)
    storage = HermesTokenStorage("crm", user_identity=actor)
    path = storage._tokens_path()
    path.parent.mkdir(parents=True)
    path.write_text('{"access_token":"user"}', encoding="utf-8")
    with mcp_user_identity(actor, delegated_servers=("crm",)), session_tool_scope(scope):
        assert _get_connected_server_for_call("crm") is server
        path.unlink()
        assert _get_connected_server_for_call("crm") is None
