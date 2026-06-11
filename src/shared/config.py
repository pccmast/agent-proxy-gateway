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
    dashboard_port: int = 8501

    # Provider configs loaded from YAML (base_url, api_key_env, default_model)
    _provider_configs: dict[str, dict[str, str]] = {}

    def get_api_key(self, provider: str) -> str:
        """Get API key for a provider by name.

        Reads from the corresponding field (e.g. openai_api_key).
        Falls back to reading from the environment variable name
        specified in YAML provider config's api_key_env.
        """
        # Direct field lookup
        field_name = f"{provider}_api_key"
        if hasattr(self, field_name):
            value = getattr(self, field_name)
            if value:
                return value

        # Fallback: read from env var specified in YAML config
        provider_cfg = self._provider_configs.get(provider, {})
        env_name = provider_cfg.get("api_key_env", f"{provider.upper()}_API_KEY")
        return os.environ.get(env_name, "")

    def get_base_url(self, provider: str) -> str:
        """Get base URL for a provider from YAML config."""
        provider_cfg = self._provider_configs.get(provider, {})
        return provider_cfg.get("base_url", "")


def load_config(config_dir: str | None = None) -> GatewaySettings:
    """Load gateway configuration from .env + YAML."""
    settings = GatewaySettings()
    if config_dir:
        settings.config_dir = config_dir

    # Load proxy settings and provider configs from YAML
    cfg_path = Path(settings.config_dir)
    if cfg_path.exists():
        for yaml_file in sorted(cfg_path.glob("*.yaml")):
            try:
                with open(yaml_file, "r", encoding="utf-8") as fh:
                    data = yaml.safe_load(fh)
                if isinstance(data, dict) and "proxy" in data:
                    proxy = data["proxy"]
                    if isinstance(proxy, dict):
                        if "host" in proxy:
                            settings.host = proxy["host"]
                        if "port" in proxy:
                            settings.port = proxy["port"]
                        if "upstream_timeout" in proxy:
                            settings.upstream_timeout = proxy["upstream_timeout"]
                        # Load provider configs for dynamic API key lookup
                        if "providers" in proxy and isinstance(proxy["providers"], dict):
                            settings._provider_configs = proxy["providers"]
            except Exception:
                pass  # YAML errors are non-fatal; fall back to defaults

    return settings