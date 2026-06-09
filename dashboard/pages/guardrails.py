"""Guardrails dashboard page — rule hit rates and trigger details.

Requires the gateway to be running at GATEWAY_API_URL.
"""

import streamlit as st
import pandas as pd
import httpx

GATEWAY_API_URL = "http://localhost:8080"

st.title("Guardrails Dashboard")
st.markdown("Monitor guardrail rule hits and active rules.")

try:
    # Fetch stats
    stats_resp = httpx.get(f"{GATEWAY_API_URL}/api/guardrails/stats", timeout=5)
    rules_resp = httpx.get(f"{GATEWAY_API_URL}/api/guardrails/rules", timeout=5)

    if stats_resp.status_code == 200 and rules_resp.status_code == 200:
        stats_data = stats_resp.json()
        rules_data = rules_resp.json()

        # --- Hit Rate Chart ---
        col1, col2 = st.columns([1, 2])

        with col1:
            st.metric("Total Hits", stats_data.get("total_hits", 0))
            st.metric("Active Rules", rules_data.get("count", 0))

        with col2:
            stats_entries = stats_data.get("stats", {})
            if stats_entries:
                chart_data = pd.DataFrame(
                    {"rule_id": list(stats_entries.keys()), "hits": list(stats_entries.values())}
                )
                st.bar_chart(chart_data.set_index("rule_id"), use_container_width=True)
            else:
                st.info("No guardrail hits recorded yet.")

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
