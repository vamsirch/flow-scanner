import streamlit as st
import pandas as pd
from polygon import RESTClient, WebSocketClient
from polygon.websocket.models import WebSocketMessage
import threading
import time
import asyncio
import re
from datetime import datetime, timedelta
from collections import deque
import pytz

# --- PAGE CONFIG ---
st.set_page_config(page_title="FlowTrend Pro", layout="wide")

# --- INITIALIZATION ---
if "init_done" not in st.session_state:
    st.session_state.clear()
    st.session_state["init_done"] = True

if "scanner_data" not in st.session_state:
    st.session_state["scanner_data"] = deque(maxlen=2000)
if "api_key" not in st.session_state:
    st.session_state["api_key"] = ""

# --- HELPERS ---
def get_est_time(nanosecs):
    """Converts milliseconds (aggregates use ms) to EST string"""
    try:
        # Aggregates timestamp is usually in milliseconds
        dt = datetime.fromtimestamp(nanosecs / 1000, tz=pytz.utc)
        return dt.astimezone(pytz.timezone('US/Eastern')).strftime('%H:%M:%S')
    except: return "00:00:00"

def parse_details(symbol):
    try:
        match = re.match(r"O:([A-Z]+)(\d{6})([CP])(\d{8})", symbol)
        if match:
            ticker, date_str, type_char, strike_str = match.groups()
            expiry = f"20{date_str[0:2]}-{date_str[2:4]}-{date_str[4:6]}"
            strike_val = float(strike_str) / 1000.0
            side = "Call" if type_char == 'C' else "Put"
            return ticker, expiry, f"${strike_val:,.2f}", side
    except: pass
    return symbol, "-", "-", "-"

# --- SIDEBAR ---
with st.sidebar:
    st.title("🐋 FlowTrend AI")
    st.caption("Mode: Aggregate Feed")
    page = st.radio("Navigate", ["🏠 Home", "🔍 Contract Inspector", "⚡ Live Whale Scanner"])
    st.divider()
    api_input = st.text_input("Polygon API Key", value=st.session_state["api_key"], type="password")
    if api_input: st.session_state["api_key"] = api_input

# ==========================================
# PAGE 1: HOME
# ==========================================
def render_home():
    st.title("Welcome to FlowTrend Pro")
    st.info("Select a tool from the sidebar.")

# ==========================================
# PAGE 2: CONTRACT INSPECTOR
# ==========================================
def render_inspector():
    st.title("🔍 Contract Inspector")
    st.info("Please focus on the Live Whale Scanner tab.")

# ==========================================
# PAGE 3: AGGREGATE SCANNER (The Fix)
# ==========================================
def render_scanner():
    st.title("⚡ Live Whale Stream (Aggregates)")
    api_key = st.session_state["api_key"]
    if not api_key: return st.error("Enter API Key.")

    # --- 1. BACKFILL (Using 1-Min Aggregates) ---
    def run_aggregate_backfill(key, tickers, min_val):
        client = RESTClient(key)
        new_data = []
        status = st.status("⏳ Downloading Aggregate History...", expanded=True)
        
        st.session_state["scanner_data"].clear()

        for t in tickers:
            try:
                status.write(f"📥 Getting Active Contracts for {t}...")
                # 1. Get Active Contracts
                chain = client.list_snapshot_options_chain(t, params={"limit": 250})
                
                active_contracts = []
                for c in chain:
                    if c.day and c.day.volume and c.day.volume > 0:
                        active_contracts.append(c.details.ticker)
                
                status.write(f"🔎 Aggregating flow for {len(active_contracts)} contracts...")
                
                # 2. Get Aggregate Bars (1 Minute)
                today = datetime.now().strftime("%Y-%m-%d")
                for contract_sym in active_contracts:
                    try:
                        # Get 1-minute bars for today
                        aggs = client.get_aggs(contract_sym, 1, "minute", today, today)
                        if not aggs: continue
                        
                        _, expiry, strike, side = parse_details(contract_sym)
                        
                        for agg in aggs:
                            # Use VWAP if available, else Close
                            price = agg.vwap if agg.vwap else agg.close
                            val = price * agg.volume * 100
                            
                            if val >= min_val:
                                ts = get_est_time(agg.timestamp) # aggs use ms timestamp
                                
                                new_data.append({
                                    "Stock Symbol": t,
                                    "Strike Price": strike,
                                    "Call or Put": side,
                                    "Agg Volume": agg.volume,   # Total Volume in this minute
                                    "Agg Value": val,           # Total Value in this minute
                                    "Time": ts,
                                    "Tags": "📊 1-Min Agg"
                                })
                    except: continue
                    
            except Exception as e:
                continue
        
        # Sort by Time
        new_data.sort(key=lambda x: x["Time"], reverse=True)
        
        status.update(label=f"Done! Loaded {len(new_data)} aggregate blocks.", state="complete", expanded=False)
        for row in new_data: st.session_state["scanner_data"].append(row)

    # --- 2. WEBSOCKET LISTENER (SECOND AGGREGATES) ---
    def start_listener(key, tickers, min_val):
        try: asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
        except: pass

        def handle_msg(msgs: list[WebSocketMessage]):
            for m in msgs:
                # EVENT TYPE "A" = Second Aggregate (Not Trade)
                if m.event_type == "A":
                    try:
                        found = next((t for t in tickers if t in m.symbol), None)
                        if not found: continue

                        # Aggregate Math: Volume * VWAP * 100
                        # Note: 'm.volume' is the volume in that second
                        # 'm.vwap' is the volume weighted average price
                        price = m.vwap if m.vwap else m.close
                        val = price * m.volume * 100
                        
                        if val >= min_val:
                            _, _, strike, side = parse_details(m.symbol)
                            # Timestamp in aggregates is 'start_timestamp' (ms)
                            ts = get_est_time(m.start_timestamp)
                            
                            st.session_state["scanner_data"].appendleft({
                                "Stock Symbol": found,
                                "Strike Price": strike,
                                "Call or Put": side,
                                "Agg Volume": m.volume,
                                "Agg Value": val,
                                "Time": ts,
                                "Tags": "⚡ 1-Sec Agg"
                            })
                    except: continue

        # SUBSCRIBE TO "A.*" (Aggregates) INSTEAD OF "T.*"
        ws = WebSocketClient(api_key=key, feed="delayed.polygon.io", market="options", subscriptions=["A.*"], verbose=False)
        ws.run(handle_msg)

    # --- 3. CONTROLS ---
    c1, c2, c3 = st.columns([2, 1, 1])
    with c1:
        watch = st.multiselect("Watchlist", ["NVDA", "TSLA", "AAPL", "AMD", "SPY", "QQQ", "AMZN", "MSFT"], default=["NVDA", "TSLA"])
    with c2:
        # Default $10k
        min_val = st.number_input("Min Aggregate Value ($)", value=10000, step=5000)
    with c3:
        st.write("")
        if st.button("🚀 Start Aggregates"):
            run_aggregate_backfill(api_key, watch, min_val)
            if "thread_started" not in st.session_state:
                st.session_state["thread_started"] = True
                t = threading.Thread(target=start_listener, args=(api_key, watch, min_val), daemon=True)
                t.start()

    # --- 4. DISPLAY ---
    data = list(st.session_state["scanner_data"])
    if len(data) > 0:
        df = pd.DataFrame(data)
        
        # Sort by Value
        df = df.sort_values(by="Agg Value", ascending=False)
        
        st.dataframe(
            df,
            use_container_width=True,
            height=800,
            column_config={
                "Agg Value": st.column_config.ProgressColumn("Total Value", format="$%.0f", min_value=0, max_value=max(df["Agg Value"].max(), 100_000)),
                "Agg Volume": st.column_config.NumberColumn("Volume", format="%d"),
            },
            hide_index=True
        )
    else:
        st.info("Click 'Start Aggregates' to see flow.")

# ==========================================
# ROUTER
# ==========================================
if page == "🏠 Home": render_home()
elif page == "🔍 Contract Inspector": render_inspector()
elif page == "⚡ Live Whale Scanner": render_scanner()
