"""Gateway configuration loader using Pydantic Settings."""

from pathlib import Path

import yaml
from pydantic_settings import BaseSettings, SettingsConfigDict

from shared.constants import DEFAULT_GATEWAY_HOST, DEFAULT_GATEWAY_PORT


class GatewaySettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_prefix="GATEWAY_",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    host: str = DEFAULT_GATEWAY_HOST
    port: int = DEFAULT_GATEWAY_PORT
    config_dir: str = "./config"
    upstream_timeout: int = 120

    # Provider API keys (read from env)
    openai_api_key: str = ""
    anthropic_api_key: str = ""

    # Database
    db_path: str = "data/gateway.db"

    # Dashboard
    dashboard_port: int = 8501


def load_config(config_dir: str | None = None) -> GatewaySettings:
    """Load gateway configuration from env + YAML."""
    settings = GatewaySettings()
    if config_dir:
        settings.config_dir = config_dir

    # Load proxy settings from YAML config files
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
            except Exception:
                pass  # YAML errors are non-fatal; fall back to defaults

    return settings