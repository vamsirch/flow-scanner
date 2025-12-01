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
    st.session_state["scanner_data"] = deque(maxlen=2000)
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
# PAGE 2: CONTRACT INSPECTOR
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
        
        # Silent Price Check
        price = 0
        try:
            snap = client.get_snapshot_ticker("stocks", target)
            if snap and snap.last_trade:
                price = snap.last_trade.price
                st.success(f"📍 {target}: ${price:.2f}")
            elif snap and snap.day:
                price = snap.day.close
                st.info(f"📍 {target}: ${price:.2f} (Close)")
        except:
            try:
                prev = client.get_previous_close_agg(target)
                if prev:
                    price = prev[0].close
                    st.info(f"📍 {target}: ${price:.2f} (Prev)")
            except: st.warning("Price Unavailable")
            
        expiry = st.date_input("Expiration", value=datetime.now().date())
        side = st.radio("Side", ["Call", "Put"], horizontal=True)
        st.write("---")
        
        try:
            contracts = client.list_options_contracts(target, expiry.strftime("%Y-%m-%d"), "call" if side=="Call" else "put", limit=1000)
            strikes = sorted(list(set([c.strike_price for c in contracts])))
            if strikes:
                def_ix = min(range(len(strikes)), key=lambda i: abs(strikes[i]-price)) if price > 0 else 0
                sel_strike = st.selectbox("Strike", strikes, index=def_ix)
                
                d = expiry.strftime("%y%m%d")
                t = "C" if side == "Call" else "P"
                s = f"{int(sel_strike*1000):08d}"
                final_sym = f"O:{target}{d}{t}{s}"
                
                if st.button("Analyze", type="primary"):
                    st.session_state['active_sym'] = final_sym
            else:
                st.error("No strikes found.")
        except Exception as e:
            st.error(f"API Error: {e}")

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
                    chart_data = None
                    try:
                        aggs = client.get_aggs(sym, 5, "minute", today, today)
                        if aggs: chart_data = aggs
                    except: pass 
                    if not chart_data:
                        try:
                            start = (datetime.now() - timedelta(days=30)).strftime("%Y-%m-%d")
                            aggs = client.get_aggs(sym, 1, "day", start, today)
                            if aggs: chart_data = aggs
                        except: pass

                    if chart_data:
                        df = pd.DataFrame(chart_data)
                        df['Time'] = pd.to_datetime(df['timestamp'], unit='ms')
                        st.area_chart(df.set_index('Time')['close'], color="#00FF00")
                    else:
                        st.info("No trades today.")
            except: st.error("Data Load Error")

# ==========================================
# PAGE 3: LIVE SCANNER
# ==========================================
def render_scanner():
    st.title("⚡ Live Whale Stream")
    api_key = st.session_state["api_key"]
    if not api_key: return st.error("Enter API Key.")

    # --- 1. CRASH-PROOF DEEP MINER ---
    def run_deep_miner(key, tickers, threshold):
        client = RESTClient(key)
        new_data = []
        status = st.status("⏳ Mining individual trade tickets...", expanded=True)
        
        for t in tickers:
            try:
                status.write(f"⛏️ Digging into {t}...")
                
                # 1. FETCH BROAD SNAPSHOT (No Sorting Params to avoid 400 Error)
                # We pull 500 contracts to ensure we catch the active ones
                chain = client.list_snapshot_options_chain(t, params={"limit": 500})
                
                # 2. PYTHON SORT (Safety)
                # We manually find the contracts with the highest volume TODAY
                contract_list = []
                for c in chain:
                    if c.day and c.day.volume:
                        contract_list.append(c)
                
                # Sort by Volume Descending
                contract_list.sort(key=lambda x: x.day.volume, reverse=True)
                
                # Pick the Top 15 Hottest Contracts to drill into
                hot_contracts = [c.details.ticker for c in contract_list[:15]]
                
                status.write(f"🔎 Scanning Top 15 contracts for {t}...")

                # 3. DRILL DOWN (Fetch Trades)
                for contract_sym in hot_contracts:
                    try:
                        trades = client.list_trades(contract_sym, limit=50)
                        _, expiry, strike, side = parse_details(contract_sym)
                        
                        for tr in trades:
                            # Individual Trade Value
                            val = tr.price * tr.size * 100
                            
                            if val >= threshold:
                                ts = datetime.fromtimestamp(tr.participant_timestamp / 1e9).strftime('%H:%M:%S')
                                tag = "🧱 BLOCK"
                                if tr.size > 1000: tag = "🐋 WHALE"
                                
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
                # Actually show the error this time so we know if it fails
                status.warning(f"Error scanning {t}: {e}")
                continue
        
        # Final Sort by Time
        new_data.sort(key=lambda x: x["Time"], reverse=True)
        
        status.update(label=f"Mining Complete! Found {len(new_data)} specific trades.", state="complete", expanded=False)
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
                            
                            st.session_state
