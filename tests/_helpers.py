"""
Test helpers — stub FastMCP for capturing tool functions without booting a real server.
"""

from collections.abc import Callable


class StubMCP:
    """Duck-typed stand-in for fastmcp.FastMCP that just captures tool registrations."""

    def __init__(self):
        self.tools: dict[str, Callable] = {}

    def tool(self, *args, **kwargs):
        def decorator(func):
            self.tools[func.__name__] = func
            return func

        return decorator


def collect_tools(provider) -> dict[str, Callable]:
    """Register a provider's MCP tools onto a stub and return the {name: func} map."""
    stub = StubMCP()
    provider.register_mcp_tools(stub)
    return stub.tools
