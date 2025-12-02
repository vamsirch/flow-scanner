import streamlit as st
import pandas as pd
from polygon import RESTClient
import time
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
    st.session_state["scanner_data"] = pd.DataFrame()
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
    if api_input: st.session_state["api_key"] = api_input

# ==========================================
# PAGE 1: HOME
# ==========================================
def render_home():
    st.title("Welcome to FlowTrend Pro")
    st.info("Select a tool from the sidebar to begin.")

# ==========================================
# PAGE 2: INSPECTOR (Standard)
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
        
        # Safe Price Check
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
                def_ix = min(range(len(strikes)), key=lambda i: abs(strikes[i]-(price if price else 0))) if strikes else 0
                sel_strike = st.selectbox("Strike", strikes, index=def_ix)
                d = expiry.strftime("%y%m%d")
                t = "C" if side == "Call" else "P"
                s = f"{int(sel_strike*1000):08d}"
                final_sym = f"O:{target}{d}{t}{s}"
                if st.button("Analyze"): st.session_state['active_sym'] = final_sym
            else: st.error("No strikes found.")
        except: st.error("API Error")

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
                    except: st.info("Chart Unavailable")
            except: st.error("Data Load Error")

# ==========================================
# PAGE 3: ROBUST SCANNER (POLLING)
# ==========================================
def render_scanner():
    st.title("⚡ Live Whale Stream")
    api_key = st.session_state["api_key"]
    if not api_key: return st.error("Enter API Key.")

    # --- 1. THE DEEP SCAN ENGINE ---
    def scan_market(key, tickers, threshold):
        client = RESTClient(key)
        all_trades = []
        status = st.status("🚀 Scanning Market...", expanded=True)
        
        for t in tickers:
            try:
                status.write(f"📥 Getting active contracts for {t}...")
                
                # 1. Get Snapshot of ALL contracts
                # We pull 300 to be safe, sorted by volume descending (done in python for safety)
                chain = client.list_snapshot_options_chain(t, params={"limit": 300})
                
                # Filter for active contracts (Handles the None Error)
                active_contracts = []
                for c in chain:
                    # STRICT SAFETY CHECK: Ensure day, volume exist and > 0
                    if c.day and c.day.volume and c.day.volume > 0:
                        active_contracts.append(c)
                
                # Sort by Volume (Highest first) to find where the action is
                active_contracts.sort(key=lambda x: x.day.volume, reverse=True)
                
                # Take Top 25 Contracts to drill down into
                targets = [c.details.ticker for c in active_contracts[:25]]
                
                status.write(f"🔎 Mining trades from {len(targets)} active contracts...")
                
                # 2. Get TRADE TAPE for these contracts
                for sym in targets:
                    try:
                        # Get last 20 trades
                        trades = client.list_trades(sym, limit=20)
                        _, expiry, strike, side = parse_details(sym)
                        
                        for tr in trades:
                            # Calculate Value
                            val = tr.price * tr.size * 100
                            
                            # User Filter
                            if val >= threshold:
                                ts = get_est_time(tr.participant_timestamp)
                                
                                tag = "Block"
                                if tr.size > 2000: tag = "🐋 WHALE"
                                elif tr.size < 5: tag = "Small"
                                
                                all_trades.append({
                                    "Symbol": t,
                                    "Strike": strike,
                                    "Expiry": expiry,
                                    "Side": side,
                                    "Trade Size": tr.size,
                                    "Dollar Amount": val,
                                    "Price": tr.price,
                                    "Time": ts,
                                    "Tags": tag
                                })
                    except: continue
                    
            except Exception as e:
                status.warning(f"Error on {t}: {e}")
                continue
        
        status.update(label=f"Done! Found {len(all_trades)} trades matching filters.", state="complete", expanded=False)
        return pd.DataFrame(all_trades)

    # --- 2. CONTROLS ---
    c1, c2, c3 = st.columns([2, 1, 1])
    with c1:
        watch = st.multiselect("Watchlist", ["NVDA", "TSLA", "AAPL", "AMD", "SPY", "QQQ", "AMZN", "MSFT"], default=["NVDA", "TSLA", "AAPL", "AMD"])
    with c2:
        min_val = st.number_input("Min Trade Value ($)", value=20000, step=10000)
    with c3:
        st.write("")
        # The button triggers the scan
        if st.button("🔄 Scan Now"):
            df = scan_market(api_key, watch, min_val)
            st.session_state["scanner_data"] = df

    # --- 3. DISPLAY ---
    df = st.session_state["scanner_data"]
    
    if not df.empty:
        # Sort by Dollar Amount Descending (Biggest on Top)
        df = df.sort_values(by="Dollar Amount", ascending=False)
        
        def style_rows(row):
            c = '#d4f7d4' if row['Side'] == 'Call' else '#f7d4d4'
            return [f'background-color: {c}; color: black'] * len(row)
            
        st.dataframe(
            df.style.apply(style_rows, axis=1).format({"Dollar Amount": "${:,.0f}", "Price": "${:.2f}"}),
            use_container_width=True,
            height=800,
            column_config={
                "Dollar Amount": st.column_config.ProgressColumn("Trade Value", format="$%.0f", min_value=0, max_value=max(df["Dollar Amount"].max(), 100_000)),
                "Trade Size": st.column_config.NumberColumn("Size", format="%d"),
            },
            hide_index=True
        )
    else:
        st.info("Click 'Scan Now' to pull the latest trades.")

# ==========================================
# ROUTER
# ==========================================
if page == "🏠 Home": render_home()
elif page == "🔍 Contract Inspector": render_inspector()
elif page == "⚡ Live Whale Scanner": render_scanner()
