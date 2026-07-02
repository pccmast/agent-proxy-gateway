"""Gateway configuration loader using Pydantic Settings.

All API keys are read from environment variables via .env file.
Never hardcode API keys in source code or YAML configs.
"""

import os
from pathlib import Path

import yaml
from pydantic_settings import BaseSettings, SettingsConfigDict

from shared.constants import DEFAULT_GATEWAY_HOST, DEFAULT_GATEWAY_PORT


class GatewaySettings(BaseSettings):
    """Gateway settings loaded from .env file + YAML configs.

    Environment variable names match field names directly
    (no prefix), e.g. OPENAI_API_KEY, ANTHROPIC_API_KEY.
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    host: str = DEFAULT_GATEWAY_HOST
    port: int = DEFAULT_GATEWAY_PORT
    config_dir: str = "./config"
    upstream_timeout: int = 120

    # Provider API keys — read from .env, never hardcoded
    openai_api_key: str = ""
    anthropic_api_key: str = ""
    deepseek_api_key: str = ""

    # Eval LLM Judge API key
    eval_llm_api_key: str = ""

    # Database
    db_path: str = "data/gateway.db"

    # Dashboard
    dashboard_port: int = 8502

    # Provider configs loaded from YAML (base_url, api_key_env, default_model)
    _provider_configs: dict[str, dict[str, str]] = {}

    def get_api_key(self, provider: str) -> str:
        """Get API key for a provider by name.

        Priority:
        1. Environment variable specified in YAML provider config's api_key_env
        2. Direct field lookup (e.g. openai_api_key from .env)
        3. Default env var name
        """
        # Priority 1: read from env var specified in YAML config (allows per-provider override)
        provider_cfg = self._provider_configs.get(provider, {})
        env_name = provider_cfg.get("api_key_env", f"{provider.upper()}_API_KEY")
        env_value = os.environ.get(env_name, "")
        if env_value:
            return env_value

        # Priority 2: direct field lookup (e.g. openai_api_key from .env)
        field_name = f"{provider}_api_key"
        if hasattr(self, field_name):
            value = getattr(self, field_name)
            if value:
                return value

        # Priority 3: default env var
        return os.environ.get(f"{provider.upper()}_API_KEY", "")

    def get_base_url(self, provider: str) -> str:
        """Get base URL for a provider from YAML config."""
        provider_cfg = self._provider_configs.get(provider, {})
        return provider_cfg.get("base_url", "")


def load_config(config_dir: str | None = None) -> GatewaySettings:
    """Load gateway configuration with priority: env vars > .env > YAML > defaults."""
    settings = GatewaySettings()
    if config_dir:
        settings.config_dir = config_dir

    # Load proxy settings and provider configs from YAML
    # YAML provides base values, but env vars / .env have higher priority.
    cfg_path = Path(settings.config_dir)
    if cfg_path.exists():
        for yaml_file in sorted(cfg_path.glob("*.yaml")):
            try:
                with open(yaml_file, encoding="utf-8") as fh:
                    data = yaml.safe_load(fh)
                if isinstance(data, dict) and "proxy" in data:
                    proxy = data["proxy"]
                    if isinstance(proxy, dict):
                        # Only apply YAML values that ARE NOT already set via env/.env.
                        # Check: if the current value equals the code default, it hasn't
                        # been overridden by env/.env, so YAML can provide the value.
                        if "host" in proxy and settings.host == DEFAULT_GATEWAY_HOST:
                            settings.host = proxy["host"]
                        if "port" in proxy and settings.port == DEFAULT_GATEWAY_PORT:
                            settings.port = proxy["port"]
                        if "upstream_timeout" in proxy and settings.upstream_timeout == 120:
                            settings.upstream_timeout = proxy["upstream_timeout"]
                        # Load provider configs (always from YAML — no env override here)
                        if "providers" in proxy and isinstance(proxy["providers"], dict):
                            settings._provider_configs = proxy["providers"]
            except Exception:
                pass  # YAML errors are non-fatal; fall back to defaults

    return settings
