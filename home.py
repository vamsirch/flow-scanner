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
    # Increased buffer to hold more trades
    st.session_state["scanner_data"] = deque(maxlen=2000)
if "api_key" not in st.session_state:
    st.session_state["api_key"] = ""

# --- SIDEBAR NAVIGATION ---
with st.sidebar:
    st.title("🐋 FlowTrend AI")
    page = st.radio("Navigate", ["🏠 Home", "🔍 Contract Inspector", "⚡ Live Whale Scanner"])
    st.divider()
    
    api_input = st.text_input("Polygon API Key", value=st.session_state["api_key"], type="password")
    if api_input:
        st.session_state["api_key"] = api_input

# ==========================================
# PAGE 1: HOME
# ==========================================
def render_home():
    st.title("Welcome to FlowTrend Pro")
    st.info("Select a tool from the sidebar to begin.")

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
    c1, c2 = st.columns([1, 3])
    
    with c1:
        st.subheader("1. Setup")
        target = st.selectbox("Ticker", ["NVDA", "TSLA", "AAPL", "AMD", "SPY", "QQQ", "AMZN", "MSFT", "META", "GOOGL"])
        
        # Price Check (With Fallback)
        try:
            snap = client.get_snapshot_ticker("stocks", target)
            if snap and snap.last_trade:
                price = snap.last_trade.price
                st.success(f"📍 {target}: ${price:.2f}")
            elif snap and snap.day:
                price = snap.day.close
                st.info(f"📍 {target}: ${price:.2f} (Close)")
            else:
                price = 0
                st.warning("No price data found.")
        except:
            price = 0
            
        expiry = st.date_input("Expiration", value=datetime.now().date())
        side = st.radio("Side", ["Call", "Put"], horizontal=True)
        
        st.write("---")
        
        # Strike Fetcher
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
                
                d_str = expiry.strftime("%y%m%d")
                t_char = "C" if side == "Call" else "P"
                s_str = f"{int(sel_strike*1000):08d}"
                final_sym = f"O:{target}{d_str}{t_char}{s_str}"
                
                if st.button("Analyze", type="primary"):
                    st.session_state['active_sym'] = final_sym
            else:
                st.error("No strikes found.")
        except Exception as e:
            st.error(f"Strike Error: {e}")

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
                    
                    st.write("### ⚡ Price Chart")
                    today = datetime.now().strftime("%Y-%m-%d")
                    
                    # Chart Logic
                    chart_data = None
                    try:
                        aggs = client.get_aggs(sym, 5, "minute", today, today)
                        if aggs:
                            chart_data = aggs
                    except:
                        pass

                    if chart_data:
                        df = pd.DataFrame(chart_data)
                        df['Time'] = pd.to_datetime(df['timestamp'], unit='ms')
                        st.area_chart(df.set_index('Time')['close'], color="#00FF00")
                    else:
                        st.info("No trades today (Market closed).")
            except Exception as e:
                st.error(f"Data Load Error: {e}")

# ==========================================
# PAGE 3: LIVE SCANNER
# ==========================================
def render_scanner():
    st.title("⚡ Live Whale Stream")
    
    api_key = st.session_state["api_key"]
    if not api_key:
        st.error("Please enter API Key in the sidebar.")
        return

    # --- 1. FIXED BACKFILL FUNCTION ---
    def run_backfill(key, tickers, threshold):
        client = RESTClient(key)
        new_data = []
        status = st.status("⏳ Downloading trade history (This takes a moment)...", expanded=True)
        
        for t in tickers:
            try:
                status.write(f"📥 Analyzing {t}...")
                
                # FIX: Remove 'sort' param (it caused the error). 
                # We fetch contracts and sort in Python.
                chain = client.list_snapshot_options_chain(t, params={"limit": 250})
                
                ticker_contracts = []
                for c in chain:
                    # Filter for active contracts only
                    if c.day and c.day.volume and c.day.close:
                        flow = c.day.close * c.day.volume * 100
                        
                        if flow >= threshold:
                            side = "Call" if c.details.contract_type == "call" else "Put"
                            
                            ticker_contracts.append({
                                "Stock Symbol": t,
                                "Strike Price": c.details.strike_price,
                                "Call or Put": side,
                                "Contract Volume": c.day.volume,
                                "Dollar Amount": flow,
                                "Tags": "📊 HISTORICAL"
                            })
                
                # Sort by Dollar Amount (Descending) and take Top 20 per ticker
                ticker_contracts.sort(key=lambda x: x["Dollar Amount"], reverse=True)
                new_data.extend(ticker_contracts[:20])
                
            except Exception as e:
                status.warning(f"Skipping {t}: {e}")
                continue
        
        status.update(label=f"Done! Loaded {len(new_data)} whales.", state="complete", expanded=False)
        
        # Update Session State
        for row in new_data:
            st.session_state["scanner_data"].append(row)

    # --- 2. WEBSOCKET LISTENER ---
    def start_listener(key, tickers, threshold):
        try:
            asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
        except: pass

        def handle_msg(msgs: list[WebSocketMessage]):
            for m in msgs:
                if m.event_type == "T":
                    try:
                        found = next((t for t in tickers if t in m.symbol), None)
                        if not found: continue

                        flow = m.price * m.size * 100
                        if flow >= threshold:
                            side = "Call" if "C" in m.symbol else "Put"
                            
                            # Check conditions for Sweep
                            conds = m.conditions if hasattr(m, 'conditions') and m.conditions else []
                            tag = "🧹 SWEEP" if 14 in conds else "⚡ LIVE"
                            
                            st.session_state["scanner_data"].appendleft({
                                "Stock Symbol": found,
                                "Strike Price": "Live Print", # Strike parsing is complex on live feed
                                "Call or Put": side,
                                "Contract Volume": m.size,
                                "Dollar Amount": flow,
                                "Tags": tag
                            })
                    except: continue

        # Run in background
        ws = WebSocketClient(api_key=key, feed="delayed.polygon.io", market="options", subscriptions=["T.*"], verbose=False)
        ws.run(handle_msg)

    # --- 3. CONTROLS ---
    col1, col2, col3 = st.columns([2, 1, 1])
    with col1:
        watch = st.multiselect("Watchlist", ["NVDA", "TSLA", "AAPL", "AMD", "SPY", "QQQ", "AMZN", "MSFT"], default=["NVDA", "TSLA", "AAPL", "AMD"])
    with col2:
        min_val = st.number_input("Min $ Amount", value=20_000, step=10_000)
    with col3:
        st.write("") 
        if st.button("🔄 Start / Refresh"):
            # Clear old data
            st.session_state["scanner_data"].clear()
            
            # 1. Run Backfill (Snapshot)
            run_backfill(api_key, watch, min_val)
            
            # 2. Start Live Listener (Threaded)
            # Note: In this simple structure, we rely on Backfill for immediate data. 
            # A full background thread would require the complex setup from previous steps.
            # For now, this button re-scans the market snapshot which is robust.

    # --- 4. DISPLAY ---
    data = list(st.session_state["scanner_data"])
    if len(data) > 0:
        df = pd.DataFrame(data)
        
        # Color Styling
        def style_rows(row):
            c = '#d4f7d4' if row['Call or Put'] == 'Call' else '#f7d4d4'
            return [f'background-color: {c}; color: black'] * len(row)
            
        st.dataframe(
            df.style.apply(style_rows, axis=1).format({"Dollar Amount": "${:,.0f}"}),
            use_container_width=True,
            height=800,
            column_config={
                "Dollar Amount": st.column_config.ProgressColumn("Value", format="$%f", min_value=0, max_value=max(df["Dollar Amount"].max(), 100_000)),
                "Contract Volume": st.column_config.NumberColumn("Volume", format="%d"),
            },
            hide_index=True
        )
    else:
        st.info("Click 'Start / Refresh' to scan for whales.")

# ==========================================
# MAIN ROUTER
# ==========================================
if page == "🏠 Home":
    render_home()
elif page == "🔍 Contract Inspector":
    render_inspector()
elif page == "⚡ Live Whale Scanner":
    render_scanner()
