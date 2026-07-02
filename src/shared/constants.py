"""Gateway-wide constants and defaults.

All hard-coded defaults live here so they have a single source of truth.
Changing a value only requires editing this file — every consumer imports from here.
"""

# ---------------------------------------------------------------------------
# Network defaults
# ---------------------------------------------------------------------------
DEFAULT_GATEWAY_HOST: str = "0.0.0.0"
DEFAULT_GATEWAY_PORT: int = 18080
DEFAULT_DASHBOARD_PORT: int = 8502

# ---------------------------------------------------------------------------
# Derived URLs (convenience)
# ---------------------------------------------------------------------------
DEFAULT_GATEWAY_URL: str = f"http://127.0.0.1:{DEFAULT_GATEWAY_PORT}"
DEFAULT_DASHBOARD_URL: str = f"http://127.0.0.1:{DEFAULT_DASHBOARD_PORT}"
