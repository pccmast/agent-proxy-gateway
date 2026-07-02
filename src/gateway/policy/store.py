"""PolicyStore — centralized configuration management with YAML loading and hot-reload.

Reads config/*.yaml at startup, merges into a unified GatewayPolicy object,
and optionally watches files for changes.
"""

import threading
from pathlib import Path
from typing import Any

import yaml

from shared.logging import get_logger

from .loader import BudgetConfig, EvalConfig, GatewayPolicy, GuardrailsConfig, RateLimitConfig

logger = get_logger()


class PolicyStore:
    """Central store for all gateway policies loaded from YAML configuration files.

    Provides typed accessors (``guardrails_config``, ``budget_config``, etc.)
    and supports on-demand hot-reload when config files change.
    """

    def __init__(self, config_dir: str = "./config", watch: bool = False) -> None:
        self._config_dir = Path(config_dir)
        self._policy: GatewayPolicy | None = None
        self._file_mtimes: dict[str, float] = {}
        self._watch: bool = watch
        self._watch_thread: threading.Thread | None = None
        self._stop_event = threading.Event()

    # ------------------------------------------------------------------ public API

    @property
    def policy(self) -> GatewayPolicy:
        """Return the current GatewayPolicy, loading on first access."""
        if self._policy is None:
            self.reload()
            assert self._policy is not None
        return self._policy

    def guardrails_config(self) -> GuardrailsConfig:
        return self.policy.guardrails

    def budget_config(self) -> BudgetConfig:
        return self.policy.budget

    def rate_limit_config(self) -> RateLimitConfig:
        return self.policy.rate_limit

    def eval_config(self) -> EvalConfig:
        return self.policy.eval

    # -------------------------------------------------------------- YAML loading

    def reload(self) -> None:
        """(Re-)load all YAML config files and merge into a unified GatewayPolicy."""
        raw: dict[str, Any] = {}

        yaml_files = sorted(self._config_dir.glob("*.yaml"))
        if not yaml_files:
            logger.warning("no_yaml_configs", config_dir=str(self._config_dir))

        for path in yaml_files:
            try:
                with open(path, encoding="utf-8") as fh:
                    data = yaml.safe_load(fh)
                if isinstance(data, dict):
                    raw.update(data)  # shallow merge — top-level keys from later files win
                self._file_mtimes[str(path)] = path.stat().st_mtime
                logger.debug("config_loaded", file=str(path))
            except yaml.YAMLError as e:
                logger.error("yaml_parse_error", file=str(path), error=str(e))
            except OSError as e:
                logger.error("config_read_error", file=str(path), error=str(e))

        # Validate and build typed policy
        try:
            self._policy = GatewayPolicy(**raw)
        except Exception as e:
            logger.error("policy_validation_error", error=str(e))
            raise

        logger.info("policy_loaded", guardrail_rules=len(self.policy.guardrails.rules))

    # ------------------------------------------------------------- hot-reload

    def start_watching(self, interval: float = 5.0) -> None:
        """Start a background thread that checks for config file changes."""
        self._watch = True
        self._stop_event.clear()
        self._watch_thread = threading.Thread(
            target=self._watch_loop,
            args=(interval,),
            daemon=True,
            name="policy-watcher",
        )
        self._watch_thread.start()
        logger.info("policy_watcher_started", interval_s=interval)

    def stop_watching(self) -> None:
        """Stop the background watcher thread."""
        self._stop_event.set()
        if self._watch_thread:
            self._watch_thread.join(timeout=5.0)
            self._watch_thread = None

    def _watch_loop(self, interval: float) -> None:
        while not self._stop_event.wait(interval):
            try:
                if self._files_changed():
                    logger.info("config_files_changed")
                    self.reload()
            except Exception as e:
                logger.warning("watch_loop_error", error=str(e))

    def _files_changed(self) -> bool:
        for path_str, old_mtime in list(self._file_mtimes.items()):
            try:
                new_mtime = Path(path_str).stat().st_mtime
                if abs(new_mtime - old_mtime) > 0.01:
                    return True
            except OSError:
                return True  # file deleted → reload
        # Check for new files
        current_files = {str(p) for p in self._config_dir.glob("*.yaml")}
        if current_files != set(self._file_mtimes.keys()):
            return True
        return False


def create_policy_store(config_dir: str = "./config") -> PolicyStore:
    """Factory: create and load a PolicyStore."""
    store = PolicyStore(config_dir=config_dir)
    store.reload()
    return store
