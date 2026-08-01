"""Cost estimation with an externalizable, safe-fallback pricing table.

Pricing is a 4-tier per-model table (USD **per million** tokens, for
human-friendly editing):

    input          — regular prompt tokens
    cached_input   — prompt tokens served from a provider cache (discounted)
    cache_creation — tokens written into a provider cache (often a premium)
    output         — completion tokens

Two design guarantees (safe mode — no exception on missing/malformed config):
  * If ``config/pricing.yaml`` is missing or fails to parse, the in-code
    ``DEFAULT_PRICING`` is used instead.
  * Unknown models always return ``0.0`` cost, so a newly-added model can
    never break trace recording or budget reporting.

Note: prompt-cache hit/miss is decided by the upstream provider and reported
in the response ``usage`` object with vendor-specific field names. This module
models the discounted *rates*; actually feeding cache token counts into
``estimate_cost`` requires parsing those vendor-specific fields at the call
site (currently not wired — see cheat sheet / interview notes).
"""

from __future__ import annotations

# _PRICING is an intentionally-mutated module-level cache (hot-reloaded by
# load_pricing); suppress pyright's "constant redefinition" false positive.
# pyright: reportConstantRedefinition=false

from pathlib import Path

import yaml

# In-code default pricing — USD per 1M tokens. Used when the YAML override is
# absent or unparseable. Cached/cache-creation rates reflect common vendor
# discounts (OpenAI ~0.5x, Anthropic cache_read ~0.1x / creation ~1.25x);
# models without a published cache rate default to the input rate (conservative).
DEFAULT_PRICING: dict[str, dict[str, float]] = {
    "gpt-4o": {
        "input": 5.0,
        "cached_input": 2.5,
        "cache_creation": 5.0,
        "output": 15.0,
    },
    "gpt-4o-mini": {
        "input": 0.15,
        "cached_input": 0.075,
        "cache_creation": 0.15,
        "output": 0.6,
    },
    "gpt-3.5-turbo": {
        "input": 0.5,
        "cached_input": 0.25,
        "cache_creation": 0.5,
        "output": 1.5,
    },
    "claude-3-5-sonnet-latest": {
        "input": 3.0,
        "cached_input": 0.30,
        "cache_creation": 3.75,
        "output": 15.0,
    },
    "claude-3-opus-latest": {
        "input": 15.0,
        "cached_input": 1.50,
        "cache_creation": 18.75,
        "output": 75.0,
    },
    "claude-3-haiku-20240307": {
        "input": 0.25,
        "cached_input": 0.025,
        "cache_creation": 0.30,
        "output": 1.25,
    },
    "deepseek-chat": {
        "input": 0.27,
        "cached_input": 0.27,
        "cache_creation": 0.27,
        "output": 1.10,
    },
    "grok-2": {
        "input": 2.0,
        "cached_input": 2.0,
        "cache_creation": 2.0,
        "output": 10.0,
    },
}

# Active pricing table (per-token), seeded from defaults and replaceable by
# load_pricing(). Kept module-global so trace engine / budget share one table.
_PRICING: dict[str, dict[str, float]] = {}
_TIERS = ("input", "cached_input", "cache_creation", "output")


def _per_million_to_per_token(table: dict[str, dict[str, float]]) -> dict[str, dict[str, float]]:
    """Convert a per-million pricing table into a per-token table."""
    return {model: {tier: value / 1_000_000 for tier, value in rates.items()} for model, rates in table.items()}


# Seed at import time so the module is usable even before load_pricing() runs.
_PRICING = _per_million_to_per_token(DEFAULT_PRICING)


def load_pricing(config_dir: str | None = None) -> dict[str, dict[str, float]]:
    """Load per-model pricing from ``<config_dir>/pricing.yaml``.

    Safe by construction: a missing file or any parse/shape error falls back to
    the in-code ``DEFAULT_PRICING`` (no exception raised). The YAML may override
    a subset of models/tiers; missing tiers inherit the default, so partial
    files work. Rates in YAML are per-million tokens (same unit as DEFAULT_PRICING).
    """
    global _PRICING
    if not config_dir:
        _PRICING = _per_million_to_per_token(DEFAULT_PRICING)
        return _PRICING

    path = Path(config_dir) / "pricing.yaml"
    if not path.exists():
        _PRICING = _per_million_to_per_token(DEFAULT_PRICING)
        return _PRICING

    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        if not isinstance(data, dict):
            _PRICING = _per_million_to_per_token(DEFAULT_PRICING)
            return _PRICING

        merged_per_million: dict[str, dict[str, float]] = {}
        for model, rates in data.items():
            base = dict(DEFAULT_PRICING.get(model, {}))
            if isinstance(rates, dict):
                for tier in _TIERS:
                    if tier in rates:
                        try:
                            base[tier] = float(rates[tier])
                        except (TypeError, ValueError):
                            pass
            merged_per_million[model] = base
        # Keep default-only models available even if not mentioned in YAML.
        for model, rates in DEFAULT_PRICING.items():
            merged_per_million.setdefault(model, dict(rates))
        _PRICING = _per_million_to_per_token(merged_per_million)
    except Exception:
        _PRICING = _per_million_to_per_token(DEFAULT_PRICING)
    return _PRICING


def pricing_for(model: str) -> dict[str, float] | None:
    """Return the active per-token 4-tier rate dict for ``model`` or None if unknown."""
    return _PRICING.get(model)


def estimate_cost(
    model: str,
    prompt_tokens: int,
    completion_tokens: int,
    cached_tokens: int = 0,
    cache_creation_tokens: int = 0,
) -> float:
    """Estimate USD cost for a completion.

    ``cached_tokens`` / ``cache_creation_tokens`` default to 0, so callers that
    do not yet track prompt-cache usage get a standard input+output estimate.
    Unknown models return ``0.0`` so callers must never fail on a new model.
    """
    rates = _PRICING.get(model)
    if not rates:
        return 0.0
    input_rate = rates.get("input", 0.0)
    cached_rate = rates.get("cached_input", input_rate)
    creation_rate = rates.get("cache_creation", input_rate)
    output_rate = rates.get("output", 0.0)
    return round(
        prompt_tokens * input_rate
        + cached_tokens * cached_rate
        + cache_creation_tokens * creation_rate
        + completion_tokens * output_rate,
        6,
    )
