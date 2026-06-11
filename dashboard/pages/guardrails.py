"""Guardrails dashboard page — rule hit rates and trigger details.

Requires the gateway to be running at GATEWAY_API_URL.
"""

import streamlit as st
import pandas as pd
import httpx

from shared.constants import DEFAULT_GATEWAY_URL

GATEWAY_API_URL = DEFAULT_GATEWAY_URL

st.title("🛡️ Guardrails Dashboard")
st.markdown("Monitor guardrail rule hits, active rules, and protection coverage.")

try:
    # Fetch stats
    stats_resp = httpx.get(f"{GATEWAY_API_URL}/api/guardrails/stats", timeout=5)
    rules_resp = httpx.get(f"{GATEWAY_API_URL}/api/guardrails/rules", timeout=5)

    if stats_resp.status_code == 200 and rules_resp.status_code == 200:
        stats_data = stats_resp.json()
        rules_data = rules_resp.json()

        # --- Summary Metrics ---
        col1, col2, col3, col4 = st.columns(4)
        with col1:
            st.metric("Total Hits", stats_data.get("total_hits", 0))
        with col2:
            st.metric("Active Rules", rules_data.get("count", 0))
        with col3:
            block_count = sum(
                v.get("block", 0) if isinstance(v, dict) else 0
                for v in stats_data.get("stats", {}).values()
            )
            st.metric("Blocks", block_count)
        with col4:
            redact_count = sum(
                v.get("redact", 0) if isinstance(v, dict) else 0
                for v in stats_data.get("stats", {}).values()
            )
            st.metric("Redactions", redact_count)

        # --- Hit Rate Chart ---
        st.subheader("Rule Hit Distribution")
        stats_entries = stats_data.get("stats", {})
        if stats_entries:
            if isinstance(next(iter(stats_entries.values()), None), dict):
                chart_rows = [
                    {"rule_id": k, "hits": v.get("total", 0) if isinstance(v, dict) else v}
                    for k, v in stats_entries.items()
                ]
            else:
                chart_rows = [
                    {"rule_id": k, "hits": v} for k, v in stats_entries.items()
                ]
            chart_data = pd.DataFrame(chart_rows)
            st.bar_chart(chart_data.set_index("rule_id"), use_container_width=True)
        else:
            st.info("No guardrail hits recorded yet.")

        # --- Action Breakdown ---
        st.subheader("Action Breakdown")
        action_data = {"Block": block_count, "Redact": redact_count, "Log": stats_data.get("total_hits", 0) - block_count - redact_count}
        if any(action_data.values()):
            action_df = pd.DataFrame({"Action": list(action_data.keys()), "Count": list(action_data.values())})
            st.bar_chart(action_df.set_index("Action"), use_container_width=True)

        # --- Rules Table ---
        st.subheader("Active Rules")
        rules = rules_data.get("rules", [])
        if rules:
            df = pd.DataFrame(rules)
            df["enabled"] = df["enabled"].apply(lambda x: "✅" if x else "⛔")
            st.dataframe(
                df.rename(columns={
                    "id": "Rule",
                    "action": "Action",
                    "confidence_threshold": "Confidence",
                    "enabled": "Enabled",
                }),
                use_container_width=True,
                hide_index=True,
            )
    else:
        st.error(f"Gateway API returned: stats={stats_resp.status_code}, rules={rules_resp.status_code}")

except Exception as e:
    st.warning(f"Cannot reach gateway at {GATEWAY_API_URL}: {e}")
    st.info("Start the gateway with `uv run gateway` and ensure guardrails are enabled in config.")
