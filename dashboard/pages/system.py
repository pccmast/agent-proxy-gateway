"""System & Health dashboard page — gateway configuration and middleware status.

Requires the gateway to be running at GATEWAY_API_URL.
"""

import httpx
import pandas as pd
import streamlit as st

from shared.constants import DEFAULT_GATEWAY_URL

GATEWAY_API_URL = DEFAULT_GATEWAY_URL

st.title("🔧 System & Health")
st.markdown("Gateway configuration, middleware status, and system health.")

# --- Health Status ---
st.subheader("Gateway Health")
try:
    health_resp = httpx.get(f"{GATEWAY_API_URL}/health", timeout=3)
    if health_resp.status_code == 200:
        health = health_resp.json()
        col1, col2, col3 = st.columns(3)
        with col1:
            st.metric("Status", "🟢 Online" if health.get("status") == "ok" else "🔴 Offline")
        with col2:
            st.metric("Host", health.get("host", "—"))
        with col3:
            st.metric("Port", health.get("port", "—"))
    else:
        st.error(f"Health check failed: {health_resp.status_code}")
except Exception as e:
    st.warning(f"Cannot reach gateway at {GATEWAY_API_URL}: {e}")

# --- Middleware Status ---
st.subheader("Middleware Chain")
st.markdown("""
Middlewares run by **priority number** (lower = earlier, cheaper checks first).
Guards must be cheap so they can fail-fast before spending tokens on the LLM call.
""")

mw_data = [
    (10, "GuardrailsEngine", "PII redact · injection block · content safety", "🟢 Active"),
    (15, "SlidingWindowRateLimiter", "RPM / TPM sliding-window throttling", "🟢 Active"),
    (50, "CircuitBreaker", "CLOSED → OPEN → HALF_OPEN, fail-fast on upstream errors", "🟢 Active"),
    (90, "EvalPipeline", "Heuristic (sync) + LLM-as-Judge (async)", "🟢 Active"),
]
mw_df = pd.DataFrame(
    mw_data,
    columns=["Priority", "Middleware", "Responsibility", "Status"],
)
st.dataframe(mw_df, use_container_width=True, hide_index=True)

# --- Configuration ---
st.subheader("Configuration")
try:
    config_resp = httpx.get(f"{GATEWAY_API_URL}/api/config", timeout=3)
    if config_resp.status_code == 200:
        config = config_resp.json()
        st.json(config, expanded=False)
    else:
        st.info("Configuration API not available.")
except Exception:
    st.info("Configuration API not available.")

# --- Providers ---
st.subheader("Upstream Providers")
providers_data = [
    ("OpenAI", "https://api.openai.com", "Chat Completions, Embeddings"),
    ("Anthropic", "https://api.anthropic.com", "Messages API"),
    ("DeepSeek", "https://api.deepseek.com", "Chat Completions"),
]
providers_df = pd.DataFrame(
    providers_data,
    columns=["Provider", "Base URL", "Supported APIs"],
)
st.dataframe(providers_df, use_container_width=True, hide_index=True)
