"""Verified messaging principal used to isolate delegated MCP OAuth credentials.

The gateway creates this from a platform-authenticated sender and workspace/tenant.
Never construct it from message text, a model-supplied tool argument, or a display name.
"""

from __future__ import annotations

import hashlib
import json
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator


@dataclass(frozen=True)
class MCPUserIdentity:
    platform: str
    scope_id: str
    user_id: str

    def __post_init__(self) -> None:
        if self.platform not in {"slack", "teams"}:
            raise ValueError("MCP user identity requires Slack or Teams")
        if not self.scope_id or not self.user_id:
            raise ValueError("MCP user identity requires tenant/workspace and user IDs")

    @property
    def storage_key(self) -> str:
        """Opaque, collision-resistant directory key; raw platform IDs stay out of paths."""
        value = json.dumps([self.platform, self.scope_id, self.user_id], separators=(",", ":"))
        return hashlib.sha256(value.encode("utf-8")).hexdigest()


_current_identity: ContextVar[MCPUserIdentity | None] = ContextVar("mcp_user_identity", default=None)
_delegated_servers: ContextVar[frozenset[str]] = ContextVar("mcp_delegated_servers", default=frozenset())


def current_user_identity() -> MCPUserIdentity | None:
    return _current_identity.get()


def current_delegated_servers() -> frozenset[str]:
    return _delegated_servers.get()


@contextmanager
def mcp_user_identity(identity: MCPUserIdentity | None, *, delegated_servers=()) -> Iterator[None]:
    token = _current_identity.set(identity)
    servers_token = _delegated_servers.set(frozenset(delegated_servers))
    try:
        yield
    finally:
        _delegated_servers.reset(servers_token)
        _current_identity.reset(token)


def identity_from_gateway_source(source) -> MCPUserIdentity | None:
    """Actor only for authenticated Slack/Teams sources carrying tenant/workspace IDs."""
    platform = getattr(getattr(source, "platform", None), "value", getattr(source, "platform", None))
    if platform not in {"slack", "teams"}:
        return None
    scope_id = str(getattr(source, "scope_id", None) or "")
    user_id = str(getattr(source, "user_id", None) or "")
    if not scope_id or not user_id or user_id.startswith("system:"):
        return None
    return MCPUserIdentity(platform, scope_id, user_id)


def delegated_oauth_server(config: dict) -> bool:
    """Explicit opt-in; legacy profile-scoped OAuth remains unchanged."""
    oauth = config.get("oauth")
    return isinstance(oauth, dict) and oauth.get("identity") == "messaging_user"


def user_registry_scope(identity: MCPUserIdentity, profile_home: str | Path, base_scope: str = "") -> str:
    """Connection/registry scope includes profile and actor, not chat or thread."""
    home = str(Path(profile_home).expanduser().resolve(strict=False))
    digest = hashlib.sha256(f"{home}\0{identity.storage_key}".encode()).hexdigest()
    return f"mcp-user:{base_scope}:{digest}"
