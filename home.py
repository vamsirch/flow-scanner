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
st.set_page_config(page_title="FlowTrend Pro (Debug)", layout="wide")

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
    st.caption("DEBUG MODE")
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
# PAGE 3: LIVE SCANNER (DEBUG MODE)
# ==========================================
def render_scanner():
    st.title("⚡ Live Whale Stream (Debug Mode)")
    api_key = st.session_state["api_key"]
    if not api_key: return st.error("Enter API Key.")

    # --- 1. DEEP TRADE SCANNER (INDIVIDUAL TRADES) ---
    def run_deep_scan(key, tickers, threshold):
        client = RESTClient(key)
        new_data = []
        status = st.status("⏳ Starting Deep Scan...", expanded=True)
        
        # We clear the old data
        st.session_state["scanner_data"].clear()

        for t in tickers:
            try:
                status.write(f"🔎 Scanning active contracts for **{t}**...")
                
                # 1. Find the Hot Contracts (Snapshot)
                chain = client.list_snapshot_options_chain(t, params={"limit": 20, "sort": "day_volume", "order": "desc"})
                
                hot_contracts = []
                for c in chain:
                    if c.day and c.day.volume:
                        hot_contracts.append(c.details.ticker)
                
                status.write(f"   --> Found {len(hot_contracts)} contracts. Drilling down...")

                # 2. Fetch TRADE TAPE for each Hot Contract
                for contract_sym in hot_contracts:
                    try:
                        # Get last 50 specific trades
                        trades = client.list_trades(contract_sym, limit=50)
                        
                        _, expiry, strike, side = parse_details(contract_sym)
                        
                        count_found = 0
                        for tr in trades:
                            # Calculate Value of THIS specific trade
                            trade_val = tr.price * tr.size * 100
                            
                            if trade_val >= threshold:
                                ts = datetime.fromtimestamp(tr.participant_timestamp / 1e9).strftime('%H:%M:%S')
                                
                                tag = "🧱 BLOCK"
                                if tr.size > 2000: tag = "🐋 WHALE"
                                
                                new_data.append({
                                    "Symbol": t,
                                    "Strike": strike,
                                    "Expiry": expiry,
                                    "Side": side,
                                    "Trade Size": tr.size,      
                                    "Trade Value": trade_val,   
                                    "Time": ts,
                                    "Tags": tag
                                })
                                count_found += 1
                        
                        if count_found > 0:
                            status.write(f"   --> Found {count_found} big trades for {contract_sym}")
                            
                    except: continue
                    
            except Exception as e:
                status.error(f"Error scanning {t}: {e}")
                continue
        
        # Sort by Time
        new_data.sort(key=lambda x: x["Time"], reverse=True)
        
        status.update(label=f"Scan Complete! Found {len(new_data)} trades.", state="complete", expanded=False)
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
        watch = st.multiselect("Watchlist", ["NVDA", "TSLA", "AAPL", "AMD", "SPY", "QQQ", "AMZN", "MSFT"], default=["NVDA", "TSLA"])
    with col2:
        # LOW DEFAULT ($1000) TO TEST CONNECTION
        min_val = st.number_input("Min Trade Value ($)", value=1000, step=1000)
    with col3:
        st.write("") 
        if st.button("🔄 Start / Refresh"):
            run_deep_scan(api_key, watch, min_val)
            if "thread_started" not in st.session_state:
                st.session_state["thread_started"] = True
                t = threading.Thread(target=start_listener, args=(api_key, watch, min_val), daemon=True)
                t.start()

    # --- 4. DISPLAY ---
    data = list(st.session_state["scanner_data"])
    if len(data) > 0:
        df = pd.DataFrame(data)
        
        # Sort by Value Descending
        df = df.sort_values(by="Trade Value", ascending=False)
        
        def style_rows(row):
            c = '#d4f7d4' if row['Side'] == 'Call' else '#f7d4d4'
            if "SWEEP" in row['Tags']: return [f'background-color: {c}; font-weight: bold; border-left: 4px solid #gold'] * len(row)
            return [f'background-color: {c}; color: black'] * len(row)
            
        st.dataframe(
            df.style.apply(style_rows, axis=1).format({"Trade Value": "${:,.0f}"}),
            use_container_width=True,
            height=800,
            column_config={
                "Trade Value": st.column_config.ProgressColumn("Dollar Amount", format="$%.0f", min_value=0, max_value=max(df["Trade Value"].max(), 100_000)),
                "Trade Size": st.column_config.NumberColumn("Size", format="%d"),
            },
            hide_index=True
        )
    else:
        st.info("Click 'Start / Refresh' to mine trades.")

# ==========================================
# ROUTER
# ==========================================
if page == "🏠 Home": render_home()
elif page == "🔍 Contract Inspector": render_inspector()
elif page == "⚡ Live Whale Scanner": render_scanner()
