"""Agent Proxy Gateway Dashboard — Streamlit UI for observability.

Usage:
    streamlit run dashboard/app.py
"""

import httpx
import pandas as pd
import streamlit as st

st.set_page_config(
    page_title="Agent Gateway Dashboard",
    page_icon="🛡️",
    layout="wide",
    initial_sidebar_state="expanded",
)

# Sidebar navigation
st.sidebar.title("🛡️ Agent Gateway")
st.sidebar.caption("v1.0.0 — All Sprints Complete")

page = st.sidebar.radio(
    "Navigation",
    ["Overview", "Traces", "Guardrails", "Budget", "Eval"],
    index=0,
)

# Config — update this to match your gateway URL
GATEWAY_API_URL = "http://localhost:8080"


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
        st.warning(
            f"Cannot reach gateway at `{GATEWAY_API_URL}`. "
            "Start it with `uv run gateway` in another terminal."
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

    # ---------- Feature matrix (Sprint status) ----------
    st.divider()
    st.subheader("📦 Feature Matrix")

    feature_rows = [
        # Sprint 1
        ("Sprint 1", "Transparent Proxy", "✅", "Agent only changes `base_url` — no code changes"),
        ("Sprint 1", "OpenAI Adapter", "✅", "normalize → forward → normalize"),
        ("Sprint 1", "Trace Engine", "✅", "trace_id / span_id / span tree → SQLite (aiosqlite)"),
        # Sprint 2
        ("Sprint 2", "Anthropic Adapter", "✅", "Path `/v1/messages`, `content_block_delta` SSE parsing"),
        ("Sprint 2", "PII Guardrail", "✅", "Email / phone / ID / bank card via Presidio + regex"),
        ("Sprint 2", "Injection Guardrail", "✅", "Pattern + heuristic + confidence score"),
        ("Sprint 2", "Content Safety", "✅", "Keyword blacklist, action: block / redact / log"),
        ("Sprint 2", "Policy Hot-Reload", "✅", "YAML + Pydantic, file-watcher auto reload"),
        # Sprint 3
        ("Sprint 3", "Sliding Window Rate Limit", "✅", "RPM / TPM per agent × model × provider"),
        ("Sprint 3", "Token Budget", "✅", "Hourly / daily caps, 80% warning threshold"),
        ("Sprint 3", "Circuit Breaker", "✅", "Tri-state machine, auto-recovery probe"),
        ("Sprint 3", "Heuristic Evals", "✅", "Length / repetition / latency / tool-call"),
        ("Sprint 3", "LLM-as-Judge", "✅", "GPT-4o-mini, async, sampled, relevance / safety / coherence"),
        # Sprint 4
        ("Sprint 4", "Dashboard", "✅", "Streamlit: Traces / Guardrails / Budget / Eval / Overview"),
        ("Sprint 4", "Demo + Seed Scripts", "✅", "`scripts/demo.py` 7-step E2E walkthrough"),
        ("Sprint 4", "Docker", "✅", "Multi-stage Dockerfile + docker-compose (gateway + dashboard)"),
        ("Sprint 4", "Documentation", "✅", "Bilingual README (English / 简体中文) + architecture diagram"),
    ]
    feature_df = pd.DataFrame(
        feature_rows, columns=["Sprint", "Feature", "Status", "Description"]
    )
    st.dataframe(feature_df, use_container_width=True, hide_index=True)

    # ---------- Middleware priority (key design decision) ----------
    st.divider()
    st.subheader("⚙️ Middleware Priority Chain")

    st.markdown(
        """
        Middlewares run by **priority number** (lower = earlier, cheaper checks first).
        This is the core architectural decision — guards must be cheap so they can fail-fast
        before we spend tokens on the LLM call.
        """
    )

    mw_df = pd.DataFrame(
        [
            (10, "GuardrailsEngine", "PII redact · injection block · content safety"),
            (15, "SlidingWindowRateLimiter", "RPM / TPM sliding-window throttling"),
            (50, "CircuitBreaker", "CLOSED → OPEN → HALF_OPEN, fail-fast on upstream errors"),
            (90, "EvalPipeline", "Heuristic (sync) + LLM-as-Judge (async)"),
        ],
        columns=["Priority", "Middleware", "Responsibility"],
    )
    st.dataframe(mw_df, use_container_width=True, hide_index=True)

    # ---------- Quick start (in-app reminder) ----------
    st.divider()
    st.subheader("🚀 Quick Start")

    st.code(
        "# 1. Install dependencies\n"
        "uv pip install -e \".[dev]\"\n\n"
        "# 2. Set your API key\n"
        "export OPENAI_API_KEY=sk-...\n\n"
        "# 3. Start the gateway\n"
        "uv run gateway                    # → http://localhost:8080\n\n"
        "# 4. Start the dashboard (this UI)\n"
        "uv run streamlit run dashboard/app.py   # → http://localhost:8501\n\n"
        "# 5. Try it out\n"
        "curl -X POST http://localhost:8080/v1/chat/completions \\\n"
        "  -H 'Content-Type: application/json' \\\n"
        "  -H 'Authorization: Bearer any-key' \\\n"
        "  -d '{\"model\":\"gpt-4o-mini\",\"messages\":[{\"role\":\"user\",\"content\":\"Hi!\"}]}'",
        language="bash",
    )

    st.info(
        "💡 Use the **sidebar** to drill into specific pages: "
        "`Traces` to inspect request lifecycles, `Guardrails` to see hit rates, "
        "`Budget` to track token consumption, and `Eval` to review quality scores."
    )


# --- Traces Page ---
elif page == "Traces":
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


# --- Budget Page ---
elif page == "Budget":
    st.title("Budget & Rate Control")
    st.markdown("Token consumption and rate limits.")

    try:
        resp = httpx.get(f"{GATEWAY_API_URL}/api/budget/status", params={"agent_id": "default"}, timeout=5)
        if resp.status_code == 200:
            data = resp.json()
            col1, col2, col3 = st.columns(3)
            with col1:
                st.metric("Hourly", f"{data.get('hourly_used', 0):,}")
                st.progress(min(data.get("hourly_ratio", 0), 1.0))
            with col2:
                st.metric("Daily", f"{data.get('daily_used', 0):,}")
                st.progress(min(data.get("daily_ratio", 0), 1.0))
            with col3:
                st.metric("Status", "OK" if data.get("budget_ok") else "EXCEEDED")
        else:
            st.info("Budget module not active. Configure budget settings in default.yaml.")
    except Exception as e:
        st.warning(f"Cannot reach gateway: {e}")


# --- Eval Page ---
elif page == "Eval":
    st.title("Eval Pipeline")
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
    st.info("Use **Traces** page → select trace → check `eval_scores` in span tree.")
