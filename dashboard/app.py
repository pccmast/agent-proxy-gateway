"""Agent Proxy Gateway Dashboard — Streamlit UI for observability.

Usage:
    streamlit run dashboard/app.py
"""

import httpx
import pandas as pd
import streamlit as st

from shared.constants import DEFAULT_GATEWAY_URL, DEFAULT_GATEWAY_PORT

st.set_page_config(
    page_title="Agent Gateway Dashboard",
    page_icon="🛡️",
    layout="wide",
    initial_sidebar_state="expanded",
)

# Global CSS styling
st.markdown(
    """
    <style>
    .stMetric {
        background-color: #f8f9fa;
        border-radius: 8px;
        padding: 10px;
        border-left: 4px solid #4CAF50;
    }
    .stMetric[data-testid="stMetricLabel"] {
        font-size: 0.85rem;
        color: #666;
    }
    .stMetric[data-testid="stMetricValue"] {
        font-size: 1.5rem;
        font-weight: 600;
        color: #333;
    }
    .stAlert {
        border-radius: 8px;
    }
    div[data-testid="stDataFrame"] {
        border-radius: 8px;
        overflow: hidden;
    }
    </style>
    """,
    unsafe_allow_html=True,
)

# Sidebar navigation
st.sidebar.title("🛡️ Agent Gateway")
st.sidebar.caption("v1.0.0 — Production Ready")

page = st.sidebar.radio(
    "Navigation",
    ["Overview", "Traces", "Budget", "Eval"],
    index=0,
)

# Config — update this to match your gateway URL
GATEWAY_API_URL = DEFAULT_GATEWAY_URL


# --- Overview Page ---
if page == "Overview":
    st.title("🛡️ Agent Proxy Gateway")
    st.markdown(
        "**Transparent proxy gateway between AI Agents and LLM / Tool APIs** — "
        "intercepts, traces, guardrails, evaluates, and controls all Agent traffic."
    )

    # ---------- Live status banner ----------
    st.divider()
    status_cols = st.columns(5)
    health_ok = False
    health_payload: dict[str, object] = {}
    try:
        resp = httpx.get(f"{GATEWAY_API_URL}/health", timeout=3)
        if resp.status_code == 200:
            health_ok = True
            health_payload = resp.json() if isinstance(resp.json(), dict) else {}
    except Exception:
        pass

    with status_cols[0]:
        st.metric("Gateway", "🟢 Online" if health_ok else "🔴 Offline")
    with status_cols[1]:
        st.metric("Host", str(health_payload.get("host", "—")))
    with status_cols[2]:
        st.metric("Port", str(health_payload.get("port", "—")))
    with status_cols[3]:
        try:
            stats = httpx.get(f"{GATEWAY_API_URL}/api/traces/stats", timeout=3).json()
            st.metric("Total Traces", stats.get("total_traces", 0))
        except Exception:
            st.metric("Total Traces", "—")
    with status_cols[4]:
        try:
            gr = httpx.get(f"{GATEWAY_API_URL}/api/guardrails/stats", timeout=3).json()
            st.metric("Guardrail Hits", gr.get("total_hits", 0))
        except Exception:
            st.metric("Guardrail Hits", "—")

    if not health_ok:
        st.error(
            f"**Gateway Offline** — Cannot reach `{GATEWAY_API_URL}`\n\n"
            f"**Troubleshooting:**\n"
            f"1. Start gateway: `uv run gateway`\n"
            f"2. Verify port: `netstat -an | findstr {DEFAULT_GATEWAY_PORT}`\n"
            f"3. Test: `curl http://127.0.0.1:{DEFAULT_GATEWAY_PORT}/health`"
        )

    # ---------- Golden Signals ----------
    st.divider()
    st.subheader("📊 Golden Signals")

    gs_cols = st.columns(4)
    with gs_cols[0]:
        try:
            stats = httpx.get(f"{GATEWAY_API_URL}/api/traces/stats", timeout=3).json()
            total = stats.get("total_traces", 0)
            st.metric("Requests (24h)", f"{total:,}")
        except Exception:
            st.metric("Requests (24h)", "—")
    with gs_cols[1]:
        try:
            stats = httpx.get(f"{GATEWAY_API_URL}/api/traces/stats", timeout=3).json()
            errors = stats.get("error_count", 0)
            total = stats.get("total_traces", 1)
            rate = (errors / total * 100) if total > 0 else 0
            st.metric("Error Rate", f"{rate:.1f}%", delta=f"{errors} errors")
        except Exception:
            st.metric("Error Rate", "—")
    with gs_cols[2]:
        try:
            stats = httpx.get(f"{GATEWAY_API_URL}/api/traces/stats", timeout=3).json()
            p95 = stats.get("p95_latency_ms", 0)
            st.metric("P95 Latency", f"{p95:.0f} ms")
        except Exception:
            st.metric("P95 Latency", "—")
    with gs_cols[3]:
        try:
            budget = httpx.get(f"{GATEWAY_API_URL}/api/budget/status", params={"agent_id": "default"}, timeout=3).json()
            ratio = budget.get("daily_ratio", 0)
            st.metric("Daily Budget", f"{ratio*100:.1f}%", delta="OK" if budget.get("budget_ok") else "EXCEEDED")
        except Exception:
            st.metric("Daily Budget", "—")

    # ---------- Architecture Diagram ----------
    st.divider()
    st.subheader("🏗️ Architecture")

    st.markdown(
        """
        ```
        ┌─────────────┐     ┌─────────────────────────────────────────┐     ┌─────────────┐
        │   Agent     │────→│  Agent Proxy Gateway                    │────→│   OpenAI    │
        │   SDK       │     │  ┌─────────┐ ┌─────────┐ ┌───────────┐ │     │   Anthropic │
        │   (Any)     │     │  │Guardrails│ │Rate Limit│ │Circuit    │ │     │   DeepSeek  │
        └─────────────┘     │  │Engine    │ │(Sliding) │ │Breaker    │ │     │   ...       │
                            │  └────┬────┘ └────┬────┘ └─────┬─────┘ │     └─────────────┘
                            │       │           │            │       │
                            │  ┌────┴───────────┴────────────┴─────┐ │
                            │  │         Protocol Adapter           │ │
                            │  │   OpenAI  ·  Anthropic  ·  SSE    │ │
                            │  └──────────────────────────────────┘ │
                            │  ┌──────────────────────────────────┐ │
                            │  │         Trace Engine            │ │
                            │  │   SQLite span tree · async      │ │
                            │  └──────────────────────────────────┘ │
                            │  ┌──────────────────────────────────┐ │
                            │  │         Eval Pipeline             │ │
                            │  │   Heuristic + LLM-as-Judge       │ │
                            │  └──────────────────────────────────┘ │
                            └─────────────────────────────────────────┘
                                           │
                                           ↓
                            ┌─────────────────────────────────────────┐
                            │         Dashboard (This UI)             │
                            │   Overview · Traces · Budget · Eval     │
                            └─────────────────────────────────────────┘
        ```
        """
    )

    # ---------- Request flow (architecture in plain language) ----------
    st.divider()
    st.subheader("🔁 Request Flow")

    st.markdown(
        """
        Every Agent request passes through **6 stages** before reaching the LLM backend:

        ```
        Agent SDK → [1] Guardrails (PII / Injection / Content)
                   → [2] Rate Limit (RPM / TPM sliding window)
                   → [3] Circuit Breaker (CLOSED → OPEN → HALF_OPEN)
                   → [4] Protocol Adapter (OpenAI / Anthropic)
                   → [5] Upstream LLM / Tool API
                   ← [6] Eval Pipeline (Heuristic + LLM-as-Judge)  ← response
        ```

        Each stage records data into the **Trace Engine** (SQLite span tree),
        which is what the rest of this dashboard visualizes.
        """
    )

    st.info(
        "💡 Use the **sidebar** to drill into specific pages: "
        "`Traces` to inspect request lifecycles, "
        "`Budget` to track token consumption, and `Eval` to review quality scores."
    )


# --- Traces Page ---
elif page == "Traces":
    st.title("Traces")
    st.markdown("View and inspect request traces.")

    # Filters
    filter_cols = st.columns(3)
    with filter_cols[0]:
        status_filter = st.selectbox("Status", ["All", "success", "error", "blocked"], index=0)
    with filter_cols[1]:
        agent_filter = st.text_input("Agent ID", placeholder="Filter by agent...")
    with filter_cols[2]:
        search_query = st.text_input("Search", placeholder="Trace ID or content...")

    try:
        resp = httpx.get(f"{GATEWAY_API_URL}/api/traces", params={"limit": 100})
        if resp.status_code == 200:
            data = resp.json()
            traces = data.get("traces", [])

            if not traces:
                st.info("No traces recorded yet. Send a request through the gateway to populate traces.")
            else:
                # Apply filters
                filtered = traces
                if status_filter != "All":
                    filtered = [t for t in filtered if t.get("status", "") == status_filter]
                if agent_filter:
                    filtered = [t for t in filtered if agent_filter.lower() in t.get("agent_id", "").lower()]
                if search_query:
                    filtered = [t for t in filtered if search_query.lower() in t.get("trace_id", "").lower()]

                # Status distribution
                st.subheader("Status Distribution")
                status_counts = {}
                for t in traces:
                    s = t.get("status", "unknown")
                    status_counts[s] = status_counts.get(s, 0) + 1
                if status_counts:
                    status_df = pd.DataFrame({"Status": list(status_counts.keys()), "Count": list(status_counts.values())})
                    st.bar_chart(status_df.set_index("Status"), use_container_width=True)

                st.metric("Total Traces", len(traces))
                st.metric("Filtered Traces", len(filtered))

                # Table view
                rows = []
                for t in filtered:
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
                    [t.get("trace_id", "") for t in filtered] if filtered else [""],
                    format_func=lambda tid: f"{tid[:12]}... — {next((t.get('status','?') for t in filtered if t.get('trace_id')==tid),'?')}" if tid else "—",
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
            st.error(f"Gateway API error: {resp.status_code}. Check gateway logs for details.")
    except Exception as e:
        st.error(f"Connection failed: {e}. Ensure gateway is running at {GATEWAY_API_URL}.")


# --- Budget Page ---
elif page == "Budget":
    st.title("💰 Budget & Rate Control")
    st.markdown("Token consumption, rate limits, and budget status.")

    # Agent selector
    try:
        agents_resp = httpx.get(f"{GATEWAY_API_URL}/api/agents", timeout=3)
        agents = ["default"]
        if agents_resp.status_code == 200:
            agents_data = agents_resp.json()
            if isinstance(agents_data, list):
                agents = agents_data
        agent_id = st.selectbox("Agent", agents, index=0)
    except Exception:
        agent_id = "default"

    try:
        resp = httpx.get(f"{GATEWAY_API_URL}/api/budget/status", params={"agent_id": agent_id}, timeout=5)
        if resp.status_code == 200:
            data = resp.json()

            # Summary metrics
            col1, col2, col3, col4 = st.columns(4)
            with col1:
                st.metric("Hourly Used", f"{data.get('hourly_used', 0):,}")
            with col2:
                st.metric("Daily Used", f"{data.get('daily_used', 0):,}")
            with col3:
                st.metric("Hourly Limit", f"{data.get('hourly_limit', 0):,}")
            with col4:
                st.metric("Daily Limit", f"{data.get('daily_limit', 0):,}")

            # Progress bars
            st.subheader("Budget Utilization")
            h_ratio = min(data.get("hourly_ratio", 0), 1.0)
            d_ratio = min(data.get("daily_ratio", 0), 1.0)

            h_col, d_col = st.columns(2)
            with h_col:
                st.metric("Hourly", f"{h_ratio*100:.1f}%")
                st.progress(h_ratio)
            with d_col:
                st.metric("Daily", f"{d_ratio*100:.1f}%")
                st.progress(d_ratio)

            # Status
            if data.get("budget_ok"):
                st.success("✅ Budget OK — within limits")
            else:
                st.error("❌ Budget EXCEEDED — requests may be throttled")
        else:
            st.info("Budget module not active. Configure budget settings in `config/default.yaml`.")
    except Exception as e:
        st.error(f"Connection failed: {e}. Ensure gateway is running at {GATEWAY_API_URL}.")


# --- Eval Page ---
elif page == "Eval":
    st.title("📊 Eval Pipeline")
    st.markdown("""
    Response quality evaluation. Scores are recorded per-trace span.

    | Metric | Type | Description |
    |--------|------|-------------|
    | response_length | Heuristic | Abnormal length detection |
    | repetition | Heuristic | Content repetition detection |
    | latency | Heuristic | High-latency flagging |
    | tool_call | Heuristic | Tool call completeness |
    | relevance | LLM Judge | Response relevance |
    | safety | LLM Judge | Harmful content detection |
    | coherence | LLM Judge | Logical structure |
    """)

    # Fetch eval stats
    try:
        stats_resp = httpx.get(f"{GATEWAY_API_URL}/api/eval/stats", timeout=5)
        if stats_resp.status_code == 200:
            eval_data = stats_resp.json()

            # Summary metrics
            col1, col2, col3 = st.columns(3)
            with col1:
                total_evals = eval_data.get("total_evaluations", 0)
                st.metric("Total Evaluations", total_evals)
            with col2:
                pass_rate = eval_data.get("pass_rate", 0)
                st.metric("Pass Rate", f"{pass_rate*100:.1f}%")
            with col3:
                avg_score = eval_data.get("average_score", 0)
                st.metric("Avg Score", f"{avg_score:.2f}")

            # Score distribution
            st.subheader("Score Distribution")
            scores = eval_data.get("score_distribution", {})
            if scores:
                score_df = pd.DataFrame({
                    "Score Range": list(scores.keys()),
                    "Count": list(scores.values())
                })
                st.bar_chart(score_df.set_index("Score Range"), use_container_width=True)
            else:
                st.info("No evaluation scores recorded yet.")

            # Metric breakdown
            st.subheader("Metric Breakdown")
            metrics = eval_data.get("metrics", {})
            if metrics:
                metric_rows = []
                for metric_name, metric_data in metrics.items():
                    if isinstance(metric_data, dict):
                        metric_rows.append({
                            "Metric": metric_name,
                            "Avg Score": metric_data.get("average", 0),
                            "Pass Rate": f"{metric_data.get('pass_rate', 0)*100:.1f}%",
                            "Count": metric_data.get("count", 0),
                        })
                if metric_rows:
                    metric_df = pd.DataFrame(metric_rows)
                    st.dataframe(metric_df, use_container_width=True, hide_index=True)
        else:
            st.info("Eval module not active. Configure eval settings in `config/default.yaml`.")
    except Exception as e:
        st.error(f"Connection failed: {e}. Ensure gateway is running at {GATEWAY_API_URL}.")

    st.info("💡 Use **Traces** page → select trace → check `eval_scores` in span tree for detailed per-trace scores.")
