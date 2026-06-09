"""Agent Proxy Gateway Dashboard — Streamlit UI for observability.

Usage:
    streamlit run dashboard/app.py
"""

import streamlit as st

st.set_page_config(
    page_title="Agent Gateway Dashboard",
    page_icon="🛡️",
    layout="wide",
    initial_sidebar_state="expanded",
)

# Sidebar navigation
st.sidebar.title("🛡️ Agent Gateway")
st.sidebar.caption("v0.1.0 — Sprint 1")

page = st.sidebar.radio(
    "Navigation",
    ["Overview", "Traces"],
    index=0,
)

# Config — update this to match your gateway URL
GATEWAY_API_URL = "http://localhost:8080"


# --- Overview Page ---
if page == "Overview":
    st.title("Agent Proxy Gateway")
    st.markdown("""
    A transparent proxy gateway that sits between AI Agents and LLM/Tool APIs,
    providing **observability**, **guardrails**, and **control**.

    ### Current Status (Sprint 1)

    | Component | Status |
    |-----------|--------|
    | Proxy Core | ✅ Implemented |
    | OpenAI Adapter | ✅ Implemented |
    | Trace Engine (SQLite) | ✅ Implemented |
    | Guardrails | ⏳ Sprint 2 |
    | Budget & Rate Control | ⏳ Sprint 3 |
    | Eval Pipeline | ⏳ Sprint 3 |
    | Anthropic Adapter | ⏳ Sprint 3 |

    ### Architecture
    ```
    Agent → Gateway (Proxy + Middleware) → LLM API
              ↓
           Trace Store (SQLite)
    ```
    """)

    # Quick health check
    st.subheader("Gateway Health")
    try:
        import httpx
        resp = httpx.get(f"{GATEWAY_API_URL}/health")
        if resp.status_code == 200:
            st.success(f"Gateway is running: {resp.json()}")
        else:
            st.error(f"Gateway returned {resp.status_code}")
    except Exception:
        st.warning(f"Cannot reach gateway at {GATEWAY_API_URL}")


# --- Traces Page ---
elif page == "Traces":
    import httpx
    import pandas as pd
    import json

    st.title("Traces")
    st.markdown("View and inspect request traces.")

    try:
        resp = httpx.get(f"{GATEWAY_API_URL}/api/traces", params={"limit": 100})
        if resp.status_code == 200:
            data = resp.json()
            traces = data.get("traces", [])

            if not traces:
                st.info("No traces recorded yet. Send a request through the gateway to populate traces.")
            else:
                st.metric("Total Traces", len(traces))

                # Table view
                rows = []
                for t in traces:
                    rows.append({
                        "Trace ID": t.get("trace_id", "")[:8] + "...",
                        "Agent": t.get("agent_id", "-"),
                        "Status": t.get("status", "?").upper(),
                        "Tokens": t.get("total_tokens", 0),
                        "Latency (ms)": round(t.get("total_latency_ms", 0), 1),
                        "Created": t.get("created_at", ""),
                    })

                df = pd.DataFrame(rows)
                st.dataframe(df, use_container_width=True, hide_index=True)

                # Trace detail
                st.subheader("Trace Detail")
                selected = st.selectbox(
                    "Select a trace to inspect",
                    [t.get("trace_id", "") for t in traces],
                    format_func=lambda tid: f"{tid[:12]}... — {next((t.get('status','?') for t in traces if t.get('trace_id')==tid),'?')}",
                )

                if selected:
                    detail = httpx.get(f"{GATEWAY_API_URL}/api/traces/{selected}")
                    if detail.status_code == 200:
                        detail_data = detail.json()
                        trace = detail_data.get("trace", {})
                        span_tree = detail_data.get("span_tree")

                        col1, col2, col3 = st.columns(3)
                        with col1:
                            st.metric("Status", trace.get("status", "?").upper())
                        with col2:
                            st.metric("Tokens", trace.get("total_tokens", 0))
                        with col3:
                            st.metric("Latency (ms)", round(trace.get("total_latency_ms", 0), 1))

                        if span_tree:
                            st.subheader("Span Tree")
                            st.json(span_tree, expanded=False)
        else:
            st.error(f"Failed to fetch traces: {resp.status_code}")
    except Exception as e:
        st.warning(f"Cannot reach gateway at {GATEWAY_API_URL}: {e}")


# --- Guardrails Page ---
elif page == "Guardrails":
    import httpx
    import pandas as pd

    st.title("Guardrails")
    st.markdown("Security and safety checks for Agent traffic.")

    try:
        stats_resp = httpx.get(f"{GATEWAY_API_URL}/api/guardrails/stats", timeout=5)
        rules_resp = httpx.get(f"{GATEWAY_API_URL}/api/guardrails/rules", timeout=5)

        if stats_resp.status_code == 200 and rules_resp.status_code == 200:
            stats_data = stats_resp.json()
            rules_data = rules_resp.json()

            col1, col2 = st.columns(2)
            with col1:
                st.metric("Total Hits", stats_data.get("total_hits", 0))
            with col2:
                st.metric("Active Rules", rules_data.get("count", 0))

            st.subheader("Rule Hit Distribution")
            stats_entries = stats_data.get("stats", {})
            if stats_entries:
                chart_data = pd.DataFrame(
                    {"Rule": list(stats_entries.keys()), "Hits": list(stats_entries.values())}
                )
                st.bar_chart(chart_data.set_index("Rule"), use_container_width=True)
            else:
                st.info("No guardrail hits recorded yet. Send a request to populate data.")

            st.subheader("Rules")
            rules = rules_data.get("rules", [])
            if rules:
                df = pd.DataFrame(rules)
                df["enabled"] = df["enabled"].apply(lambda x: "Yes" if x else "No")
                st.dataframe(
                    df.rename(columns={
                        "id": "Rule ID", "action": "Action",
                        "confidence_threshold": "Threshold", "enabled": "Enabled",
                    }),
                    use_container_width=True,
                    hide_index=True,
                )
        else:
            st.error(f"API unavailable: stats={stats_resp.status_code}, rules={rules_resp.status_code}")

    except Exception as e:
        st.warning(f"Cannot reach gateway at {GATEWAY_API_URL}: {e}")
        st.info("Start the gateway with `uv run gateway` and ensure guardrails are enabled.")
