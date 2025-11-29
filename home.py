import streamlit as st
import pandas as pd
from polygon import RESTClient, WebSocketClient
from polygon.websocket.models import WebSocketMessage
import threading
import time
import asyncio
from datetime import datetime
from collections import deque

# --- PAGE CONFIG ---
st.set_page_config(page_title="FlowTrend Pro", layout="wide")

# --- GLOBAL STATE ---
if "scanner_data" not in st.session_state:
    st.session_state["scanner_data"] = deque(maxlen=1000)
if "running" not in st.session_state:
    st.session_state["running"] = False
if "api_key" not in st.session_state:
    st.session_state["api_key"] = ""

# --- SIDEBAR NAVIGATION ---
with st.sidebar:
    st.title("🐋 FlowTrend AI")
    page = st.radio("Navigate", ["🏠 Home", "🔍 Contract Inspector", "⚡ Live Whale Scanner"])
    st.divider()
    
    # Global API Key Input
    api_input = st.text_input("Polygon API Key", value=st.session_state["api_key"], type="password")
    if api_input:
        st.session_state["api_key"] = api_input

    st.caption("v1.0.0 | Institutional Analytics")

# ==========================================
# PAGE 1: HOME
# ==========================================
def render_home():
    st.title("Welcome to FlowTrend Pro")
    st.write("### Institutional Options Analytics Terminal")
    
    st.info("""
    **How to use this dashboard:**
    
    1. **Enter your Polygon API Key** in the sidebar (left).
    2. **Use the Radio Buttons** above the key to switch tools:
       * **🔍 Contract Inspector:** Analyze individual option contracts, charts, and Greeks.
       * **⚡ Live Whale Scanner:** Watch for real-time Block Trades and Sweeps.
    """)
    
    if st.session_state["api_key"]:
        st.success("✅ API Key detected. Select a tool from the sidebar to begin!")
    else:
        st.warning("⬅️ Please enter your API Key in the sidebar to unlock the tools.")

# ==========================================
# PAGE 2: CONTRACT INSPECTOR
# ==========================================
def render_inspector():
    st.title("🔍 Contract Inspector")
    
    api_key = st.session_state["api_key"]
    if not api_key:
        st.error("Please enter API Key in the sidebar.")
        return

    client = RESTClient(api_key)
    
    # Layout
    c1, c2 = st.columns([1, 3])
    
    with c1:
        st.subheader("1. Setup")
        tickers = ["NVDA", "TSLA", "AAPL", "AMD", "SPY", "QQQ", "AMZN", "MSFT", "META", "GOOGL"]
        target = st.selectbox("Ticker", tickers)
        
        # Price Check
        try:
            snap = client.get_snapshot_ticker("stocks", target)
            price = snap.last_trade.price if snap.last_trade else snap.day.close
            st.info(f"📍 {target}: ${price:.2f}")
        except:
            price = 0
            st.warning("Connecting...")
            
        expiry = st.date_input("Expiration", value=datetime.now().date())
        side = st.radio("Side", ["Call", "Put"], horizontal=True)
        
        st.write("---")
        
        # Strike Logic
        try:
            contracts = client.list_options_contracts(
                underlying_ticker=target,
                expiration_date=expiry.strftime("%Y-%m-%d"),
                contract_type="call" if side == "Call" else "put",
                limit=1000
            )
            strikes = sorted(list(set([c.strike_price for c in contracts])))
            
            if strikes:
                def_ix = min(range(len(strikes)), key=lambda i: abs(strikes[i]-price)) if price > 0 else 0
                sel_strike = st.selectbox("Strike", strikes, index=def_ix)
                
                # Build Symbol
                d_str = expiry.strftime("%y%m%d")
                t_char = "C" if side == "Call" else "P"
                s_str = f"{int(sel_strike*1000):08d}"
                final_sym = f"O:{target}{d_str}{t_char}{s_str}"
                
                if st.button("Analyze", type="primary"):
                    st.session_state['active_sym'] = final_sym
            else:
                st.error("No strikes found.")
        except Exception as e:
            st.error(f"Error: {e}")

    with c2:
        if 'active_sym' in st.session_state:
            sym = st.session_state['active_sym']
            st.subheader(f"Analysis: {sym}")
            try:
                snap = client.get_snapshot_option(target, sym)
                if snap:
                    m1, m2, m3, m4 = st.columns(4)
                    p = snap.last_trade.price if snap.last_trade else (snap.day.close if snap.day else 0)
                    v = snap.day.volume if snap.day else 0
                    m1.metric("💰 Price", f"${p}", f"Vol: {v}")
                    if snap.greeks:
                        m2.metric("Delta", f"{snap.greeks.delta:.2f}")
                        m3.metric("Gamma", f"{snap.greeks.gamma:.2f}")
                    
                    st.write("### ⚡ Intraday Chart")
                    today = datetime.now().strftime("%Y-%m-%d")
                    aggs = client.get_aggs(sym, 5, "minute", today, today)
                    if aggs:
                        df = pd.DataFrame(aggs)
                        df['Time'] = pd.to_datetime(df['timestamp'], unit='ms')
                        st.area_chart(df.set_index('Time')['close'], color="#00FF00")
                    else:
                        st.info("No trades today.")
            except:
                st.error("Data load failed.")

# ==========================================
# PAGE 3: LIVE SCANNER
# ==========================================
def render_scanner():
    st.title("⚡ Live Whale Stream")
    
    api_key = st.session_state["api_key"]
    if not api_key:
        st.error("Please enter API Key in the sidebar.")
        return

    # Controls
    col1, col2, col3 = st.columns([2, 1, 1])
    with col1:
        watch = st.multiselect("Watchlist", ["NVDA", "TSLA", "AAPL", "AMD", "SPY", "QQQ"], default=["NVDA", "TSLA"])
    with col2:
        min_val = st.number_input("Min $ Value", value=10_000, step=5_000)
    with col3:
        st.write("") # Spacer
        if st.button("🔄 Refresh / Scan"):
            scan_market(api_key, watch, min_val)

    # Scan Logic (Manual Refresh for Stability)
    def scan_market(key, tickers, threshold):
        client = RESTClient(key)
        new_data = []
        status = st.status("Scanning market...", expanded=True)
        
        for t in tickers:
            try:
                status.write(f"Checking {t}...")
                chain = client.list_snapshot_options_chain(t, params={"limit": 50, "sort": "day_volume", "order": "desc"})
                for c in chain:
                    if c.day and c.day.volume and c.day.close:
                        flow = c.day.close * c.day.volume * 100
                        if flow >= threshold:
                            side = "CALL" if c.details.contract_type == "call" else "PUT"
                            new_data.append({
                                "Symbol": t,
                                "Strike": c.details.strike_price,
                                "Side": side,
                                "Volume": c.day.volume,
                                "Value": flow,
                                "Time": "Day Sum"
                            })
            except:
                continue
        
        status.update(label="Scan Complete", state="complete", expanded=False)
        st.session_state["scanner_data"] = new_data

    # Display Table
    data = st.session_state["scanner_data"]
    if len(data) > 0:
        df = pd.DataFrame(data)
        
        # Style
        def style_rows(row):
            c = '#d4f7d4' if row['Side'] == 'CALL' else '#f7d4d4'
            return [f'background-color: {c}; color: black'] * len(row)
            
        st.dataframe(
            df.style.apply(style_rows, axis=1).format({"Value": "${:,.0f}"}),
            use_container_width=True,
            height=800,
            column_config={"Value": st.column_config.ProgressColumn("Value", format="$%f", min_value=0, max_value=max(df["Value"].max(), 100_000))},
            hide_index=True
        )
    else:
        st.info("Click Refresh to load data.")

# ==========================================
# MAIN ROUTER
# ==========================================
if page == "🏠 Home":
    render_home()
elif page == "🔍 Contract Inspector":
    render_inspector()
elif page == "⚡ Live Whale Scanner":
    render_scanner()
