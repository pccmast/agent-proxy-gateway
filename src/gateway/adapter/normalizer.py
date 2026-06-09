"""AdapterRegistry — select and manage protocol adapters."""

from .base import ProtocolAdapter, AdapterRegistry

# Re-export for convenience
__all__ = ["AdapterRegistry", "ProtocolAdapter", "create_registry"]


def create_registry() -> AdapterRegistry:
    """Create and configure the adapter registry with all built-in adapters."""
    registry = AdapterRegistry()

    # Import adapters lazily to avoid circular imports
    from .openai import OpenAIAdapter

    registry.register(OpenAIAdapter())

    return registry
