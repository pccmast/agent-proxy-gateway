"""Live Traffic dashboard page — real-time request monitoring.

Requires the gateway to be running at GATEWAY_API_URL.
"""

import time

import httpx
import pandas as pd
import streamlit as st

from shared.constants import DEFAULT_GATEWAY_URL

GATEWAY_API_URL = DEFAULT_GATEWAY_URL

st.title("📡 Live Traffic")
st.markdown("Real-time request monitoring and traffic analysis.")

# Auto-refresh
auto_refresh = st.checkbox("Auto-refresh (5s)", value=True)
if auto_refresh:
    st.empty()
    time.sleep(5)
    st.rerun()

# --- Recent Requests ---
st.subheader("Recent Requests")
try:
    resp = httpx.get(f"{GATEWAY_API_URL}/api/traces", params={"limit": 20}, timeout=3)
    if resp.status_code == 200:
        data = resp.json()
        traces = data.get("traces", [])

        if not traces:
            st.info("No recent requests. Send a request through the gateway to see live traffic.")
        else:
            rows = []
            for t in traces:
                rows.append(
                    {
                        "Time": t.get("created_at", "")[-8:] if t.get("created_at") else "—",
                        "Agent": t.get("agent_id", "-"),
                        "Status": t.get("status", "?").upper(),
                        "Tokens": t.get("total_tokens", 0),
                        "Latency (ms)": round(t.get("total_latency_ms", 0), 1),
                        "Trace ID": t.get("trace_id", "")[:12] + "...",
                    }
                )

            df = pd.DataFrame(rows)
            st.dataframe(df, use_container_width=True, hide_index=True)

            # Traffic summary
            st.subheader("Traffic Summary (Last 20)")
            total_tokens = sum(t.get("total_tokens", 0) for t in traces)
            avg_latency = sum(t.get("total_latency_ms", 0) for t in traces) / len(traces) if traces else 0
            success_count = sum(1 for t in traces if t.get("status") == "success")
            error_count = sum(1 for t in traces if t.get("status") == "error")

            col1, col2, col3, col4 = st.columns(4)
            with col1:
                st.metric("Requests", len(traces))
            with col2:
                st.metric("Total Tokens", f"{total_tokens:,}")
            with col3:
                st.metric("Avg Latency", f"{avg_latency:.0f} ms")
            with col4:
                st.metric("Success Rate", f"{success_count}/{len(traces)}")
    else:
        st.error(f"Failed to fetch traces: {resp.status_code}")
except Exception as e:
    st.warning(f"Cannot reach gateway at {GATEWAY_API_URL}: {e}")
