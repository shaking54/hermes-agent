"""Short-lived, one-use browser handoff for a messaging user's MCP OAuth login."""

from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import time
from pathlib import Path
from urllib.parse import urlencode, urlsplit

from tools.mcp_user_identity import MCPUserIdentity, delegated_oauth_server

_LINK_TTL_SECONDS = 600
_CONNECT_COMMAND = re.compile(r"^!mcp\s+connect\s+([A-Za-z0-9][A-Za-z0-9._-]{0,63})\s*$", re.IGNORECASE)


def parse_user_connect_command(text: str) -> str | None:
    """An explicit chat command, never model output or a quoted phrase."""
    match = _CONNECT_COMMAND.fullmatch(text.strip())
    return match.group(1) if match else None


def _tickets_dir() -> Path:
    from hermes_constants import get_hermes_home
    return get_hermes_home() / "mcp-user-links"


def _ticket_path(token: str) -> Path:
    if len(token) < 32 or len(token) > 128 or not all(c.isalnum() or c in "-_" for c in token):
        raise ValueError("Invalid connection link")
    digest = hashlib.sha256(token.encode("ascii")).hexdigest()
    return _tickets_dir() / f"{digest}.json"


def _delegated_server(name: str) -> bool:
    from hermes_cli.config import load_config
    servers = load_config().get("mcp_servers") or {}
    cfg = servers.get(name) if isinstance(servers, dict) else None
    return (isinstance(cfg, dict) and cfg.get("auth") == "oauth"
            and bool(cfg.get("url")) and delegated_oauth_server(cfg))


def create_user_connect_link(server_name: str, actor: MCPUserIdentity) -> str:
    """Mint a bearer link only for a configured delegated OAuth server.

    The caller must deliver it privately to *actor*. No raw platform IDs appear
    in the URL; the random ticket is stored at 0600 and expires after ten minutes.
    """
    from hermes_cli.dashboard_auth.prefix import resolve_public_url
    from hermes_cli.profiles import get_active_profile_name
    from hermes_constants import get_hermes_home, mkdir_under_hermes_home, secure_parent_dir

    if not _delegated_server(server_name):
        raise ValueError("This MCP server is not configured for messaging-user OAuth")
    public_url = resolve_public_url()
    if urlsplit(public_url).scheme != "https":
        raise ValueError("Configure an HTTPS dashboard.public_url before user MCP sign-in")
    profile = get_active_profile_name()
    if profile == "custom":
        raise ValueError("User MCP sign-in requires a named Hermes profile")
    ticket = secrets.token_urlsafe(32)
    path = _ticket_path(ticket)
    mkdir_under_hermes_home(path.parent)
    secure_parent_dir(path)
    payload = {
        "platform": actor.platform, "scope_id": actor.scope_id, "user_id": actor.user_id,
        "server": server_name, "profile_home": str(get_hermes_home().resolve()),
        "expires_at": time.time() + _LINK_TTL_SECONDS,
    }
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(path, flags, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as stream:
        json.dump(payload, stream, separators=(",", ":"))
    return f"{public_url}/api/mcp/user-connect/{ticket}?{urlencode({'profile': profile})}"


def redeem_user_connect_link(ticket: str) -> tuple[str, MCPUserIdentity]:
    """Atomically consume one ticket in the selected profile; never reuse it."""
    from hermes_constants import get_hermes_home

    path = _ticket_path(ticket)
    spent = path.with_name(path.name + f".spent-{secrets.token_hex(8)}")
    try:
        path.rename(spent)
    except FileNotFoundError as exc:
        raise ValueError("Connection link expired or already used") from exc
    try:
        payload = json.loads(spent.read_text(encoding="utf-8-sig"))
    finally:
        spent.unlink(missing_ok=True)
    if (not isinstance(payload, dict) or payload.get("expires_at", 0) < time.time()
            or payload.get("profile_home") != str(get_hermes_home().resolve())):
        raise ValueError("Connection link expired or invalid")
    server = payload.get("server")
    if not isinstance(server, str) or not _delegated_server(server):
        raise ValueError("MCP server is no longer available for user sign-in")
    return server, MCPUserIdentity(payload.get("platform"), payload.get("scope_id"), payload.get("user_id"))


async def handle_slack_user_connect(adapter, text: str, *, team_id: str, user_id: str,
                                    channel_id: str, is_one_to_one_dm: bool) -> bool:
    """Consume an explicit connect command before the ordinary message route."""
    server_name = parse_user_connect_command(text)
    if server_name is None:
        return False
    try:
        link = create_user_connect_link(server_name, MCPUserIdentity("slack", str(team_id or ""), str(user_id or "")))
        notice = f"Connect your {server_name} MCP account (single-use, 10 minutes): {link}"
    except ValueError as exc:
        notice = f"MCP connection unavailable: {exc}"
    metadata = {"slack_team_id": team_id} if team_id else None
    if is_one_to_one_dm:
        await adapter.send(channel_id, notice, metadata=metadata)
    else:
        await adapter.send_private_notice(channel_id, user_id, notice, metadata=metadata)
    return True
