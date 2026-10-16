"""Bind a verified messaging actor to an enterprise MCP gateway turn."""

from __future__ import annotations

from contextlib import contextmanager


@contextmanager
def gateway_mcp_user_scope(ctx):
    """Only direct Slack/Teams ingress may activate delegated OAuth connections.

    API-created and scheduled/background turns do not carry a fresh platform
    authentication assertion, so they intentionally remain outside this scope.
    """
    from gateway.session_api_turn import api_execution
    from tools.mcp_user_identity import (
        delegated_oauth_server, identity_from_gateway_source, mcp_user_identity, user_registry_scope,
    )

    actor = None
    if (ctx.inbound_message_id and ctx.persist_user_display_kind is None
            and not ctx.scheduled_heartbeat and api_execution.get() is None):
        actor = identity_from_gateway_source(ctx.source)
    config = ctx.user_config if isinstance(ctx.user_config, dict) else {}
    servers = config.get("mcp_servers") or {}
    delegated_names = [name for name, server in servers.items()
                       if isinstance(server, dict) and delegated_oauth_server(server)] if isinstance(servers, dict) else []
    if actor is None or not delegated_names:
        yield
        return

    from hermes_constants import get_hermes_home
    from tools.mcp_tool import _mcp_registry_scope
    from tools.registry import session_tool_scope

    base_scope = _mcp_registry_scope() or ""
    scope = user_registry_scope(actor, get_hermes_home(), base_scope)
    with mcp_user_identity(actor, delegated_servers=delegated_names), session_tool_scope(scope):
        from tools.mcp_oauth import HermesTokenStorage, suppress_interactive_oauth
        from tools.mcp_tool_discovery import discover_mcp_tools

        ready = [name for name in delegated_names
                 if servers[name].get("auth") == "oauth"
                 and HermesTokenStorage(name, user_identity=actor).has_cached_tokens()]
        if ready:
            with suppress_interactive_oauth():
                discover_mcp_tools(allowed_mcp_names=ready)
        yield
