"""Eval dashboard page — quality score distribution."""

import streamlit as st
import pandas as pd
import httpx

GATEWAY_API_URL = "http://localhost:8080"

st.title("Eval Pipeline")
st.markdown("Response quality evaluation scores (stored per-trace span).")

try:
    resp = httpx.get(f"{GATEWAY_API_URL}/api/eval/metrics", timeout=5)
    if resp.status_code == 200:
        data = resp.json()
        metrics = data.get("metrics", [])

        st.subheader("Evaluation Metrics")
        st.markdown("""
        | Metric | Type | Description |
        |--------|------|-------------|
        | response_length | Heuristic | Flags abnormally short/long responses |
        | repetition | Heuristic | Detects repetitive content patterns |
        | latency | Heuristic | Flags high-latency responses |
        | tool_call | Heuristic | Checks tool call completeness |
        | relevance | LLM Judge | How well response addresses query |
        | safety | LLM Judge | Harmful content detection |
        | coherence | LLM Judge | Logical structure quality |
        """)

        st.info("Eval scores are recorded per-span. Use **Traces** page → select a trace → check `eval_scores` in span tree to view individual results.")
    else:
        st.warning(f"API returned {resp.status_code}")
except Exception as e:
    st.warning(f"Cannot reach gateway: {e}")
