"""Gateway configuration loader using Pydantic Settings."""

from pydantic_settings import BaseSettings, SettingsConfigDict


class GatewaySettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_prefix="GATEWAY_",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    host: str = "0.0.0.0"
    port: int = 8080
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
    return settings