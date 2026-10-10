"""Cross-check registered Codex MCP mappings against immutable assigned servers."""

import re
from typing import Any

from .errors import DomainError
from .schemas import MCPManifest, ScopedFilesScope, ToolManifest, codex_tool_name

SCOPED_TOOLS = {"read_marker", "read_file"}
OWNED_ARGV = [
    ["python3", "-m", "adf_runtime_worker.scoped_mcp"],
    ["python", "-m", "adf_runtime_worker.scoped_mcp"],
]


def scoped_server(server: dict[str, Any]) -> tuple[str, set[str]]:
    manifest = MCPManifest.model_validate(server)
    name = server.get("name")
    if (
        not isinstance(name, str)
        or not re.fullmatch(r"[A-Za-z][A-Za-z0-9_-]{0,63}", name)
        or "__" in name
    ):
        raise DomainError(
            "INVALID_MANIFEST", "Scoped MCP requires an unambiguous server name.", 422
        )
    if manifest.trusted is not True or manifest.argv not in OWNED_ARGV:
        raise DomainError(
            "CAPABILITY_UNSUPPORTED",
            "Scoped MCP requires the trusted ADF-owned stdio server.",
            422,
        )
    ScopedFilesScope.model_validate(manifest.scope)
    tools = set(manifest.allowed_tools)
    if not tools or tools - SCOPED_TOOLS or len(tools) != len(manifest.allowed_tools):
        raise DomainError(
            "TOOL_SCOPE_DENIED",
            "Scoped MCP accepts only unique registered read tools.",
            403,
        )
    return name, tools


def validate_tool_assignments(
    provider: str, tools: list[dict[str, Any]], servers: list[dict[str, Any]]
) -> None:
    if provider != "codex":
        return
    allowed: dict[str, set[str]] = {}
    for server in servers:
        name, names = scoped_server(server)
        if name in allowed:
            raise DomainError(
                "INVALID_MANIFEST", "Assigned MCP server names must be unique.", 422
            )
        allowed[name] = names
    for tool in tools:
        manifest = ToolManifest.model_validate(tool)
        mappings = manifest.provider_mappings.get("codex")
        if mappings is None or not mappings:
            raise DomainError(
                "CAPABILITY_UNSUPPORTED",
                "Codex tool assignments require explicit scoped MCP mappings.",
                422,
            )
        for mapping in mappings:
            components = codex_tool_name(mapping)
            if (
                components is None
                or components[0] not in allowed
                or components[1] not in allowed[components[0]]
            ):
                raise DomainError(
                    "TOOL_SCOPE_DENIED",
                    "Tool mapping must name an assigned MCP server and its allowlisted tool.",
                    403,
                )
