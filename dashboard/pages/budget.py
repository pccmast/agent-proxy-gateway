"""Budget dashboard page — token consumption trends and budget status."""

import streamlit as st
import pandas as pd
import httpx

from shared.constants import DEFAULT_GATEWAY_URL

GATEWAY_API_URL = DEFAULT_GATEWAY_URL

st.title("Budget & Rate Control")

try:
    resp = httpx.get(f"{GATEWAY_API_URL}/api/budget/status", params={"agent_id": "default"}, timeout=5)

    if resp.status_code == 200:
        data = resp.json()

        col1, col2, col3 = st.columns(3)
        with col1:
            st.metric("Hourly Used", f"{data.get('hourly_used', 0):,}")
            st.progress(min(data.get("hourly_ratio", 0), 1.0), text=f"{data.get('hourly_ratio', 0):.1%}")
        with col2:
            st.metric("Daily Used", f"{data.get('daily_used', 0):,}")
            st.progress(min(data.get("daily_ratio", 0), 1.0), text=f"{data.get('daily_ratio', 0):.1%}")
        with col3:
            status = "OK" if data.get("budget_ok") else "EXCEEDED"
            st.metric("Budget Status", status)

        st.subheader("Limits")
        c1, c2 = st.columns(2)
        with c1:
            st.metric("Hourly Limit", f"{data.get('hourly_limit', 0):,}")
        with c2:
            st.metric("Daily Limit", f"{data.get('daily_limit', 0):,}")

    elif resp.status_code == 503:
        st.info("Budget module not configured. Add budget config to default.yaml.")
    else:
        st.warning(f"API returned {resp.status_code}")

except Exception as e:
    st.warning(f"Cannot reach gateway: {e}")
