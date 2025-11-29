import streamlit as st

st.set_page_config(page_title="FlowTrend AI", layout="centered")

st.title("🐋 FlowTrend AI Terminal")
st.write("### Institutional Options Analytics")

st.info("""
**Select a tool from the sidebar:**

* **🔍 Contract Inspector:** Deep-dive analysis on specific option contracts (Charts, Greeks, History).
* **⚡ Live Whale Scanner:** Real-time feed of Block Trades, Sweeps, and Whale activity.
""")

st.divider()

# API Key Storage (Shared across pages)
api_key = st.text_input("Enter Polygon API Key (Once)", type="password")
if api_key:
    st.session_state["api_key"] = api_key
    st.success("Key Saved! Navigate to a tool in the sidebar 👈")
