"""
providers/__init__.py
Registry loader for custom MCP proxy SaaS providers.
Registers all available integrations; the active provider is selected at runtime
via the ACTIVE_PROVIDER env var.
"""

from providers.base import registry
from providers.figma import FigmaProvider
from providers.lucid import LucidProvider
from providers.miro import MiroProvider

registry.register(MiroProvider())
registry.register(FigmaProvider())
registry.register(LucidProvider())

__all__ = ["registry"]
