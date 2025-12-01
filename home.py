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

# --- HELPER: PARSE OPTION SYMBOL ---
def parse_symbol(symbol):
    """
    Extracts details from OCC Symbol: O:NVDA251205C00185000
    Returns: (Ticker, Expiry, Strike, Side)
    """
    try:
        # Regex to split: O: + Ticker + 6-digit Date + C/P + 8-digit Price
        match = re.match(r"O:([A-Z]+)(\d{6})([CP])(\d{8})", symbol)
        if match:
            ticker, date_str, type_char, strike_str = match.groups()
            
            # Format Date: 251205 -> 2025-12-05
            expiry = f"20{date_str[0:2]}-{date_str[2:4]}-{date_str[4:6]}"
            
            # Format Strike: 00185000 -> 185.0
            strike = float(strike_str) / 1000.0
            
            side = "Call" if type_char == 'C' else "Put"
            return ticker, expiry, strike, side
    except:
        pass
    return None, None, None, None

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
        
        # --- FIXED: SILENT PRICE FALLBACK ---
        price = 0
        try:
            # Try Real-Time
            snap = client.get_snapshot_ticker("stocks", target)
            if snap and snap.last_trade:
                price = snap.last_trade.price
                st.success(f"📍 {target}: ${price:.2f}")
            elif snap and snap.day:
                price = snap.day.close
                st.info(f"📍 {target}: ${price:.2f} (Close)")
        except:
            # Fallback to Previous Close without error popup
            try:
                prev = client.get_previous_close_agg(target)
                if prev:
                    price = prev[0].close
                    st.info(f"📍 {target}: ${price:.2f} (Prev)")
            except:
                st.warning("Price Unavailable")
            
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

    # --- 1. BACKFILL (Historical Day Summary) ---
    def run_backfill(key, tickers, threshold):
        client = RESTClient(key)
        new_data = []
        status = st.status("⏳ Loading Day's Most Active Contracts...", expanded=True)
        
        for t in tickers:
            try:
                status.write(f"Checking {t}...")
                chain = client.list_snapshot_options_chain(t, params={"limit": 250})
                
                ticker_contracts = []
                for c in chain:
                    if c.day and c.day.volume and c.day.close:
                        flow = c.day.close * c.day.volume * 100
                        
                        if flow >= threshold:
                            side = "Call" if c.details.contract_type == "call" else "Put"
                            ticker_contracts.append({
                                "Symbol": t,
                                "Strike": f"${c.details.strike_price:.1f}",
                                "Expiry": c.details.expiration_date,
                                "Side": side,
                                "Size": c.day.volume, # It is Day Volume here
                                "Premium": flow,
                                "Time": "Day Sum",
                                "Tags": "📊 DAY VOL"
                            })
                
                # Sort manually
                ticker_contracts.sort(key=lambda x: x["Premium"], reverse=True)
                new_data.extend(ticker_contracts[:15]) 
                
            except Exception as e:
                continue
        
        status.update(label=f"Backfill Complete! Loaded {len(new_data)} contracts.", state="complete", expanded=False)
        for row in new_data:
            st.session_state["scanner_data"].append(row)

    # --- 2. WEBSOCKET (Real-Time Individual Trades) ---
    def start_listener(key, tickers, threshold):
        try:
            asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
        except: pass

        def handle_msg(msgs: list[WebSocketMessage]):
            for m in msgs:
                if m.event_type == "T":
                    try:
                        # 1. Check Watchlist
                        found = next((t for t in tickers if t in m.symbol), None)
                        if not found: continue

                        # 2. Filter Value
                        flow = m.price * m.size * 100
                        if flow < threshold: continue

                        # 3. Parse Symbol for details
                        # Ticker, Expiry, Strike, Side = parse_symbol(m.symbol)
                        _, expiry, strike, side = parse_symbol(m.symbol)
                        
                        # 4. Tags
                        conds = m.conditions if hasattr(m, 'conditions') and m.conditions else []
                        tag = "🧹 SWEEP" if 14 in conds else "⚡ TRADE"
                        
                        st.session_state["scanner_data"].appendleft({
                            "Symbol": found,
                            "Strike": f"${strike}", 
                            "Expiry": expiry,
                            "Side": side,
                            "Size": m.size, # This is Trade Size
                            "Premium": flow,
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
        min_val = st.number_input("Min Premium ($)", value=20_000, step=10_000)
    with col3:
        st.write("") 
        if st.button("🔄 Start / Refresh"):
            st.session_state["scanner_data"].clear()
            run_backfill(api_key, watch, min_val)
            
            # Start Thread if not running
            if "thread_started" not in st.session_state:
                st.session_state["thread_started"] = True
                t = threading.Thread(target=start_listener, args=(api_key, watch, min_val), daemon=True)
                t.start()

    # --- 4. DISPLAY ---
    data = list(st.session_state["scanner_data"])
    if len(data) > 0:
        df = pd.DataFrame(data)
        
        def style_rows(row):
            c = '#d4f7d4' if row['Side'] == 'Call' else '#f7d4d4'
            # Highlight Sweeps brighter
            if "SWEEP" in row['Tags']:
                return [f'background-color: {c}; font-weight: bold; border-left: 4px solid #gold'] * len(row)
            return [f'background-color: {c}; color: black'] * len(row)
            
        st.dataframe(
            df.style.apply(style_rows, axis=1).format({"Premium": "${:,.0f}"}),
            use_container_width=True,
            height=800,
            column_config={
                "Premium": st.column_config.ProgressColumn("Dollar Amount", format="$%f", min_value=0, max_value=max(df["Premium"].max(), 100_000)),
                "Size": st.column_config.NumberColumn("Vol / Size", format="%d"),
            },
            hide_index=True
        )
    else:
        st.info("Click 'Start / Refresh' to scan.")

# ==========================================
# MAIN ROUTER
# ==========================================
if page == "🏠 Home":
    render_home()
elif page == "🔍 Contract Inspector":
    render_inspector()
elif page == "⚡ Live Whale Scanner":
    render_scanner()
