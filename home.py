import streamlit as st
import pandas as pd
from polygon import RESTClient, WebSocketClient
from polygon.websocket.models import WebSocketMessage
import threading
import time
import asyncio
import re
from datetime import datetime
from collections import deque
import pytz

# --- PAGE CONFIG ---
st.set_page_config(page_title="FlowTrend Pro", layout="wide")

# --- INITIALIZATION ---
if "init_done" not in st.session_state:
    st.session_state.clear()
    st.session_state["init_done"] = True

if "scanner_data" not in st.session_state:
    st.session_state["scanner_data"] = deque(maxlen=10000)
if "api_key" not in st.session_state:
    st.session_state["api_key"] = ""

# --- HELPERS ---
def get_est_time(nanosecs):
    try:
        dt = datetime.fromtimestamp(nanosecs / 1e9, tz=pytz.utc)
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
    page = st.radio("Navigate", ["🏠 Home", "🔍 Contract Inspector", "⚡ Live Whale Scanner"])
    st.divider()
    api_input = st.text_input("Polygon API Key", value=st.session_state["api_key"], type="password")
    if api_input: st.session_state["api_key"] = api_input.strip()

# ==========================================
# PAGE 1 & 2
# ==========================================
def render_home():
    st.title("Welcome to FlowTrend Pro")
    st.info("Select a tool from the sidebar.")

def render_inspector():
    st.title("🔍 Contract Inspector")
    st.info("Please focus on the Live Whale Scanner tab.")

# ==========================================
# PAGE 3: UNLIMITED FIREHOSE SCANNER
# ==========================================
def render_scanner():
    st.title("⚡ Live Whale Stream (No Limits)")
    api_key = st.session_state["api_key"]
    if not api_key: return st.error("Enter API Key.")

    # --- 1. FIREHOSE BACKFILL (FIXED) ---
    def run_firehose_backfill(key, tickers, min_val):
        client = RESTClient(key)
        status = st.status("⏳ Downloading trade tape...", expanded=True)
        
        # FIX 1: REPLACE DEQUE INSTEAD OF CLEAR()
        st.session_state["scanner_data"] = deque(maxlen=10000)
        
        # Temp list to hold new data before sorting
        all_new_rows = []

        for t in tickers:
            try:
                status.write(f"📥 Identifying Active Contracts for {t}...")
                
                # 1. Get ALL Active Contracts
                chain = client.list_snapshot_options_chain(t, params={"limit": 1000})
                
                active_contracts = []
                for c in chain:
                    # FIX 2: STRICT NONE-TYPE CHECK
                    # Prevents "TypeError: > not supported between NoneType and int"
                    if c.day and c.day.volume is not None and c.day.volume > 0:
                        active_contracts.append(c.details.ticker)
                
                status.write(f"🔎 Scanning {len(active_contracts)} active contracts for {t}...")
                
                # 2. Pull Trades for ALL Active Contracts
                for contract_sym in active_contracts:
                    try:
                        # Get recent trades
                        trades = client.list_trades(contract_sym, limit=50)
                        
                        _, expiry, strike, side = parse_details(contract_sym)
                        
                        for tr in trades:
                            val = tr.price * tr.size * 100
                            
                            # USER FILTER ONLY
                            if val >= min_val:
                                ts = get_est_time(tr.participant_timestamp)
                                
                                tag = "Block"
                                if tr.size < 5: tag = "Small"
                                elif tr.size > 1000: tag = "Whale"
                                
                                all_new_rows.append({
                                    "Stock Symbol": t,
                                    "Strike Price": strike,
                                    "Call or Put": side,
                                    "Contract Volume": tr.size, 
                                    "Dollar Amount": val,       
                                    "Time": ts,
                                    "Tags": tag
                                })
                    except: continue
                    
            except Exception as e:
                print(f"Error on {t}: {e}")
                continue
        
        # Sort by Time (Newest First) and add to state
        all_new_rows.sort(key=lambda x: x["Time"], reverse=True)
        for row in all_new_rows:
            st.session_state["scanner_data"].append(row)
        
        status.update(label=f"Done! Loaded {len(all_new_rows)} trades.", state="complete", expanded=False)

    # --- 2. WEBSOCKET LISTENER ---
    def start_listener(key, tickers, min_val):
        try: asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
        except: pass

        def handle_msg(msgs: list[WebSocketMessage]):
            for m in msgs:
                if m.event_type == "T":
                    try:
                        found = next((t for t in tickers if t in m.symbol), None)
                        if not found: continue

                        val = m.price * m.size * 100
                        if val >= min_val:
                            _, _, strike, side = parse_details(m.symbol)
                            ts = get_est_time(m.participant_timestamp)
                            
                            st.session_state["scanner_data"].appendleft({
                                "Stock Symbol": found,
                                "Strike Price": strike,
                                "Call or Put": side,
                                "Contract Volume": m.size,
                                "Dollar Amount": val,
                                "Time": ts,
                                "Tags": "⚡ Live"
                            })
                    except: continue

        ws = WebSocketClient(api_key=key, feed="delayed.polygon.io", market="options", subscriptions=["T.*"], verbose=False)
        ws.run(handle_msg)

    # --- 3. CONTROLS ---
    c1, c2, c3 = st.columns([2, 1, 1])
    with c1:
        watch = st.multiselect("Watchlist", ["NVDA", "TSLA", "AAPL", "AMD", "SPY", "QQQ", "AMZN", "MSFT", "IWM"], default=["NVDA", "TSLA"])
    with c2:
        min_val = st.number_input("Filter: Min Trade Value ($)", value=5000, step=1000)
    with c3:
        st.write("")
        if st.button("🚀 Start Firehose"):
            run_firehose_backfill(api_key, watch, min_val)
            if "thread_started" not in st.session_state:
                st.session_state["thread_started"] = True
                t = threading.Thread(target=start_listener, args=(api_key, watch, min_val), daemon=True)
                t.start()

    # --- 4. DISPLAY ---
    data = list(st.session_state["scanner_data"])
    if len(data) > 0:
        df = pd.DataFrame(data)
        
        # Sort by Dollar Amount (Largest First)
        df = df.sort_values(by="Dollar Amount", ascending=False)
        
        def style_rows(row):
            c = '#d4f7d4' if row['Call or Put'] == 'Call' else '#f7d4d4'
            return [f'background-color: {c}; color: black'] * len(row)

        st.dataframe(
            df.style.apply(style_rows, axis=1),
            use_container_width=True,
            height=800,
            column_config={
                "Dollar Amount": st.column_config.ProgressColumn("Trade Value", format="$%.0f", min_value=0, max_value=max(df["Dollar Amount"].max(), 100_000)),
                "Contract Volume": st.column_config.NumberColumn("Size", format="%d"),
            },
            hide_index=True
        )
    else:
        st.info("Click 'Start Firehose' to pull data.")

# ==========================================
# ROUTER
# ==========================================
if page == "🏠 Home": render_home()
elif page == "🔍 Contract Inspector": render_inspector()
elif page == "⚡ Live Whale Scanner": render_scanner()
