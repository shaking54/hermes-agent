"""Delegated MCP discovery is bound only to a fresh messaging-user turn."""

from types import SimpleNamespace

from gateway.mcp_user_scope import gateway_mcp_user_scope
from tools.mcp_user_identity import current_user_identity


def _turn(*, display_kind=None, message_id="m1"):
    return SimpleNamespace(
        source=SimpleNamespace(platform="slack", scope_id="T1", user_id="U1"),
        inbound_message_id=message_id, persist_user_display_kind=display_kind,
        scheduled_heartbeat=False,
        user_config={"mcp_servers": {"crm": {
            "url": "https://mcp.example/mcp", "auth": "oauth",
            "oauth": {"identity": "messaging_user"},
        }}},
    )


def test_direct_turn_discovers_only_actor_cached_delegated_server(tmp_path, monkeypatch):
    from tools.mcp_oauth import HermesTokenStorage
    from tools.registry import current_session_tool_scope

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setattr(HermesTokenStorage, "has_cached_tokens", lambda self: True)
    seen = []

    def discover(*, allowed_mcp_names):
        seen.append((allowed_mcp_names, current_user_identity(), current_session_tool_scope()))

    monkeypatch.setattr("tools.mcp_tool_discovery.discover_mcp_tools", discover)
    with gateway_mcp_user_scope(_turn()):
        assert current_user_identity().user_id == "U1"
    other = _turn()
    other.source.user_id = "U2"
    with gateway_mcp_user_scope(other):
        assert current_user_identity().user_id == "U2"
    assert len(seen) == 2
    assert seen[0][0] == ["crm"]
    assert seen[0][2].startswith("mcp-user:")
    assert seen[0][2] != seen[1][2]
    assert current_user_identity() is None


def test_internal_turn_with_pinned_sender_never_borrows_user_grant(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    with gateway_mcp_user_scope(_turn(display_kind="internal_notification")):
        assert current_user_identity() is None
    with gateway_mcp_user_scope(_turn(message_id=None)):
        assert current_user_identity() is None
