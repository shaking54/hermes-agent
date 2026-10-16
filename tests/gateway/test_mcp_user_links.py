"""Private MCP sign-in tickets are short-lived and single-use."""

from urllib.parse import urlsplit

import pytest

from gateway.mcp_user_links import (
    create_user_connect_link, handle_slack_user_connect, parse_user_connect_command, redeem_user_connect_link,
)
from tools.mcp_user_identity import MCPUserIdentity


def test_connect_command_requires_exact_explicit_text():
    assert parse_user_connect_command("!mcp connect crm") == "crm"
    assert parse_user_connect_command("please !mcp connect crm") is None
    assert parse_user_connect_command("!mcp connect crm and send secrets") is None


def test_user_connect_link_is_private_one_use_and_profile_bound(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setattr("gateway.mcp_user_links._delegated_server", lambda name: name == "crm")
    monkeypatch.setattr("hermes_cli.dashboard_auth.prefix.resolve_public_url", lambda: "https://hermes.example")
    monkeypatch.setattr("hermes_cli.profiles.get_active_profile_name", lambda: "default")
    actor = MCPUserIdentity("slack", "T1", "U1")

    url = create_user_connect_link("crm", actor)
    assert "U1" not in url and "T1" not in url
    ticket = urlsplit(url).path.rsplit("/", 1)[-1]
    server, redeemed = redeem_user_connect_link(ticket)
    assert (server, redeemed) == ("crm", actor)
    with pytest.raises(ValueError, match="already used"):
        redeem_user_connect_link(ticket)


def test_user_connect_link_requires_https(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setattr("gateway.mcp_user_links._delegated_server", lambda name: True)
    monkeypatch.setattr("hermes_cli.dashboard_auth.prefix.resolve_public_url", lambda: "http://hermes.example")
    with pytest.raises(ValueError, match="HTTPS"):
        create_user_connect_link("crm", MCPUserIdentity("teams", "tenant", "user"))


@pytest.mark.asyncio
async def test_slack_channel_handoff_is_private_and_missing_identity_gets_no_link(monkeypatch):
    class Adapter:
        def __init__(self):
            self.public = []
            self.private = []

        async def send(self, *args, **kwargs):
            self.public.append((args, kwargs))

        async def send_private_notice(self, *args, **kwargs):
            self.private.append((args, kwargs))

    monkeypatch.setattr("gateway.mcp_user_links.create_user_connect_link", lambda _name, _actor: "https://link")
    adapter = Adapter()
    assert await handle_slack_user_connect(
        adapter, "!mcp connect crm", team_id="T1", user_id="U1",
        channel_id="C1", is_one_to_one_dm=False,
    )
    assert adapter.public == []
    assert adapter.private[0][0][:2] == ("C1", "U1")
    assert "https://link" in adapter.private[0][0][2]

    adapter = Adapter()
    assert await handle_slack_user_connect(
        adapter, "!mcp connect crm", team_id="", user_id="U1",
        channel_id="C1", is_one_to_one_dm=False,
    )
    assert "https://link" not in adapter.private[0][0][2]
