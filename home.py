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

# --- PAGE CONFIG ---
st.set_page_config(page_title="FlowTrend Pro", layout="wide")

# --- GLOBAL STATE ---
if "scanner_data" not in st.session_state:
    st.session_state["scanner_data"] = deque(maxlen=10000) # Increased buffer for "Everything"
if "api_key" not in st.session_state:
    st.session_state["api_key"] = ""

# --- HELPER: PARSE SYMBOL ---
def parse_details(symbol):
    try:
        match = re.match(r"O:([A-Z]+)(\d{6})([CP])(\d{8})", symbol)
        if match:
            ticker, date_str, type_char, strike_str = match.groups()
            expiry = f"20{date_str[0:2]}-{date_str[2:4]}-{date_str[4:6]}"
            strike_val = float(strike_str) / 1000.0
            side = "Call" if type_char == 'C' else "Put"
            return ticker, expiry, f"${strike_val:,.2f}", side
    except:
        pass
    return symbol, "-", "-", "-"

# --- SIDEBAR ---
with st.sidebar:
    st.title("🐋 FlowTrend AI")
    page = st.radio("Navigate", ["🏠 Home", "🔍 Contract Inspector", "⚡ Live Whale Scanner"])
    st.divider()
    api_input = st.text_input("Polygon API Key", value=st.session_state["api_key"], type="password")
    if api_input: st.session_state["api_key"] = api_input

# ==========================================
# PAGE 1: HOME
# ==========================================
def render_home():
    st.title("Welcome to FlowTrend Pro")
    st.info("Select a tool from the sidebar to begin.")

# ==========================================
# PAGE 2: INSPECTOR (unchanged)
# ==========================================
def render_inspector():
    st.title("🔍 Contract Inspector")
    api_key = st.session_state["api_key"]
    if not api_key: return st.error("Enter API Key.")
    client = RESTClient(api_key)
    
    c1, c2 = st.columns([1, 3])
    with c1:
        st.subheader("1. Setup")
        target = st.selectbox("Ticker", ["NVDA", "TSLA", "AAPL", "AMD", "SPY", "QQQ", "AMZN", "MSFT", "META", "GOOGL"])
        try:
            snap = client.get_snapshot_ticker("stocks", target)
            price = snap.last_trade.price if snap.last_trade else snap.day.close
            st.success(f"📍 {target}: ${price:.2f}")
        except: st.warning("Price Unavailable")
        
        expiry = st.date_input("Expiration", value=datetime.now().date())
        side = st.radio("Side", ["Call", "Put"], horizontal=True)
        st.write("---")
        
        try:
            contracts = client.list_options_contracts(target, expiry.strftime("%Y-%m-%d"), "call" if side=="Call" else "put", limit=1000)
            strikes = sorted(list(set([c.strike_price for c in contracts])))
            if strikes:
                def_ix = min(range(len(strikes)), key=lambda i: abs(strikes[i]-price)) if price > 0 else 0 # Fix: Ensure price is defined or handle error
                sel_strike = st.selectbox("Strike", strikes, index=def_ix)
                d = expiry.strftime("%y%m%d")
                t = "C" if side == "Call" else "P"
                s = f"{int(sel_strike*1000):08d}"
                final_sym = f"O:{target}{d}{t}{s}"
                if st.button("Analyze"): st.session_state['active_sym'] = final_sym
            else: st.error("No strikes.")
        except: st.error("Error fetching strikes.")

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
                    m1.metric("Price", f"${p}", f"Vol: {v}")
                    if snap.greeks:
                        m2.metric("Delta", f"{snap.greeks.delta:.2f}")
                        m3.metric("Gamma", f"{snap.greeks.gamma:.2f}")
                    
                    st.write("### ⚡ Price Chart")
                    today = datetime.now().strftime("%Y-%m-%d")
                    try:
                        aggs = client.get_aggs(sym, 5, "minute", today, today)
                        if aggs:
                            df = pd.DataFrame(aggs)
                            df['Time'] = pd.to_datetime(df['timestamp'], unit='ms')
                            st.area_chart(df.set_index('Time')['close'], color="#00FF00")
                        else: st.info("No trades today.")
                    except: st.info("Chart unavailable.")
            except: st.error("Data Load Error")

# ==========================================
# PAGE 3: DEEP DRAGNET SCANNER
# ==========================================
def render_scanner():
    st.title("⚡ Live Whale Stream")
    api_key = st.session_state["api_key"]
    if not api_key: return st.error("Enter API Key.")

    # --- 1. DRAGNET FUNCTION (Scans Everything) ---
    def run_dragnet(key, tickers, threshold):
        client = RESTClient(key)
        new_data = []
        status = st.status("🚀 Initializing Deep Dragnet...", expanded=True)
        
        for t in tickers:
            status.write(f"📂 Opening Option Chain for **{t}**...")
            
            try:
                # 1. GET ALL CONTRACTS (No Limit - Uses Pagination)
                # This pulls potentially thousands of contracts
                chain = client.list_snapshot_options_chain(t)
                
                potential_whales = []
                
                # 2. FAST FILTER (Math Check)
                # We loop through ALL contracts but only keep the ones
                # where Total Day Value > Threshold.
                for c in chain:
                    if c.day and c.day.volume and c.day.close:
                        total_val = c.day.close * c.day.volume * 100
                        if total_val >= threshold:
                            potential_whales.append(c.details.ticker)

                status.write(f"🔎 Found {len(potential_whales)} active contracts for {t}. Drilling down...")
                
                # 3. DEEP DIVE (Fetch Trades for Survivors)
                # Now we pull the trade tape for the specific contracts that passed the math check
                for sym in potential_whales:
                    try:
                        trades = client.list_trades(sym, limit=50) # Get last 50 trades per contract
                        _, expiry, strike, side = parse_details(sym)
                        
                        for tr in trades:
                            val = tr.price * tr.size * 100
                            
                            if val >= threshold:
                                ts = datetime.fromtimestamp(tr.participant_timestamp / 1e9).strftime('%H:%M:%S')
                                
                                # Tagging
                                tag = "🧱 BLOCK"
                                if tr.size > 500: tag = "🐋 WHALE"
                                
                                new_data.append({
                                    "Symbol": t,
                                    "Strike": strike,
                                    "Expiry": expiry,
                                    "Side": side,
                                    "Trade Size": tr.size,
                                    "Trade Value": val,
                                    "Time": ts,
                                    "Tags": tag
                                })
                    except: continue
                    
            except Exception as e:
                status.warning(f"Error scanning {t}: {e}")
                continue
        
        # Sort by Time (Newest Trades First)
        new_data.sort(key=lambda x: x["Time"], reverse=True)
        
        status.update(label=f"Dragnet Complete! Captured {len(new_data)} trades.", state="complete", expanded=False)
        
        st.session_state["scanner_data"].clear()
        for row in new_data: st.session_state["scanner_data"].append(row)

    # --- 2. WEBSOCKET ---
    def start_listener(key, tickers, threshold):
        try: asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
        except: pass

        def handle_msg(msgs: list[WebSocketMessage]):
            for m in msgs:
                if m.event_type == "T":
                    try:
                        found = next((t for t in tickers if t in m.symbol), None)
                        if not found: continue

                        val = m.price * m.size * 100
                        if val >= threshold:
                            _, expiry, strike, side = parse_details(m.symbol)
                            conds = m.conditions if hasattr(m, 'conditions') and m.conditions else []
                            tag = "🧹 SWEEP" if 14 in conds else "⚡ LIVE"
                            
                            st.session_state["scanner_data"].appendleft({
                                "Symbol": found,
                                "Strike": strike,
                                "Expiry": expiry,
                                "Side": side,
                                "Trade Size": m.size,
                                "Trade Value": val,
                                "Time": time.strftime("%H:%M:%S"),
                                "Tags": tag
                            })
                    except: continue

        ws = WebSocketClient(api_key=key, feed="delayed.polygon.io", market="options", subscriptions=["T.*"], verbose=False)
        ws.run(handle_msg)

    # --- 3. CONTROLS ---
    col1, col2, col3 = st.columns([2, 1, 1])
    with col1:
        watch = st.multiselect("Watchlist", ["NVDA", "TSLA", "AAPL", "AMD", "SPY", "QQQ", "AMZN", "MSFT"], default=["NVDA", "TSLA", "AAPL", "AMD"])
    with col2:
        min_val = st.number_input("Min Trade Value ($)", value=50_000, step=10_000)
    with col3:
        st.write("") 
        if st.button("🚀 Run Dragnet"):
            run_dragnet(api_key, watch, min_val)
            if "thread_started" not in st.session_state:
                st.session_state["thread_started"] = True
                t = threading.Thread(target=start_listener, args=(api_key, watch, min_val), daemon=True)
                t.start()

    # --- 4. DISPLAY ---
    data = list(st.session_state["scanner_data"])
    if len(data) > 0:
        df = pd.DataFrame(data)
        
        # Sort by Time (Newest First) to match Bullflow
        df = df.sort_values(by="Time", ascending=False)
        
        def style_rows(row):
            c = '#d4f7d4' if row['Side'] == 'Call' else '#f7d4d4'
            if "SWEEP" in row['Tags']: return [f'background-color: {c}; font-weight: bold; border-left: 4px solid #gold'] * len(row)
            return [f'background-color: {c}; color: black'] * len(row)
            
        st.dataframe(
            df.style.apply(style_rows, axis=1).format({"Trade Value": "${:,.0f}"}),
            use_container_width=True,
            height=800,
            column_config={
                "Trade Value": st.column_config.ProgressColumn("Value", format="$%.0f", min_value=0, max_value=max(df["Trade Value"].max(), 100_000)),
                "Trade Size": st.column_config.NumberColumn("Size", format="%d"),
            },
            hide_index=True
        )
    else:
        st.info("Click 'Run Dragnet' to scan ALL contracts for trades.")

# ==========================================
# ROUTER
# ==========================================
if page == "🏠 Home": render_home()
elif page == "🔍 Contract Inspector": render_inspector()
elif page == "⚡ Live Whale Scanner": render_scanner()
