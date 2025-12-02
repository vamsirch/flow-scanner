import streamlit as st
import pandas as pd
from polygon import RESTClient
import time
import re
from datetime import datetime, timedelta

# --- PAGE CONFIG ---
st.set_page_config(page_title="FlowTrend Pro", layout="wide")

# --- INITIALIZE SESSION STATE ---
if "scanner_data" not in st.session_state:
    st.session_state["scanner_data"] = pd.DataFrame()
if "last_updated" not in st.session_state:
    st.session_state["last_updated"] = datetime.now()
if "api_key" not in st.session_state:
    st.session_state["api_key"] = ""

# --- HELPER: PARSE SYMBOL ---
def parse_details(symbol):
    """Extracts clean details from O:NVDA251205C00185000"""
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
    st.caption("v2.0 | Polling Engine")
    page = st.radio("Navigate", ["🏠 Home", "🔍 Contract Inspector", "⚡ Whale Scanner"])
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
                        st.info("No trades today.")
            except: st.error("Data Load Error")

# ==========================================
# PAGE 3: AUTO-POLLING SCANNER
# ==========================================
def render_scanner():
    st.title("⚡ Whale Scanner (Polling Engine)")
    api_key = st.session_state["api_key"]
    if not api_key: return st.error("Enter API Key.")

    # --- CONTROLS ---
    c1, c2, c3 = st.columns([3, 1, 1])
    with c1:
        watch = st.multiselect("Watchlist", ["NVDA", "TSLA", "AAPL", "AMD", "SPY", "QQQ", "AMZN", "MSFT", "IWM"], default=["NVDA", "TSLA", "AAPL", "AMD"])
    with c2:
        min_val = st.number_input("Min Trade Value ($)", value=25_000, step=5_000)
    with c3:
        st.write("")
        auto_refresh = st.checkbox("Auto-Refresh (15s)", value=True)

    # --- ENGINE: GET INDIVIDUAL TRADES ---
    def fetch_trades():
        client = RESTClient(api_key)
        all_trades = []
        
        status_text = st.empty()
        status_text.caption("⏳ Scanning market tape...")
        
        for t in watch:
            try:
                # 1. Find HOT Contracts (Highest Volume Today)
                # We fetch top 15 contracts per stock to save time
                chain = client.list_snapshot_options_chain(t, params={"limit": 15, "sort": "day_volume", "order": "desc"})
                
                # 2. Drill Down into Trades
                for c in chain:
                    # Skip if volume is too low to contain a whale
                    if not c.day or (c.day.volume * c.day.close * 100 < min_val): continue
                    
                    # Fetch last 10 trades for this contract
                    trades = client.list_trades(c.details.ticker, limit=10)
                    _, expiry, strike, side = parse_details(c.details.ticker)
                    
                    for tr in trades:
                        val = tr.price * tr.size * 100
                        
                        if val >= min_val:
                            ts = datetime.fromtimestamp(tr.participant_timestamp / 1e9).strftime('%H:%M:%S')
                            
                            tag = "🧱 BLOCK"
                            if tr.size > 2000: tag = "🐋 WHALE"
                            elif tr.size < 5: tag = "⚠️ TINY" # Flag weird small/expensive trades
                            
                            all_trades.append({
                                "Symbol": t,
                                "Strike": strike,
                                "Expiry": expiry,
                                "Side": side,
                                "Trade Size": tr.size,      # Granular Size
                                "Trade Value": val,         # Granular Value
                                "Price": tr.price,
                                "Time": ts,
                                "Tags": tag
                            })
            except: continue
            
        status_text.caption(f"✅ Updated at {datetime.now().strftime('%H:%M:%S')}")
        return pd.DataFrame(all_trades)

    # --- AUTO-REFRESH LOGIC ---
    # We use a button to force refresh, or auto-rerun
    if st.button("🔄 Scan Now") or auto_refresh:
        df = fetch_trades()
        st.session_state["scanner_data"] = df
        st.session_state["last_updated"] = datetime.now()

    # --- DISPLAY ---
    df = st.session_state["scanner_data"]
    
    if not df.empty:
        # Sort by Value so Whales are on top
        df = df.sort_values(by="Trade Value", ascending=False)
        
        def style_rows(row):
            c = '#d4f7d4' if row['Side'] == 'Call' else '#f7d4d4'
            return [f'background-color: {c}; color: black'] * len(row)
            
        st.dataframe(
            df.style.apply(style_rows, axis=1).format({"Trade Value": "${:,.0f}", "Price": "${:.2f}"}),
            use_container_width=True,
            height=800,
            column_config={
                "Trade Value": st.column_config.ProgressColumn("Value", format="$%.0f", min_value=0, max_value=max(df["Trade Value"].max(), 100_000)),
                "Trade Size": st.column_config.NumberColumn("Size", format="%d"),
            },
            hide_index=True
        )
    else:
        st.info("Waiting for data... (Market might be closed or filters too high)")

    # Loop Timer
    if auto_refresh:
        time.sleep(15) # Wait 15 seconds
        st.rerun() # Refresh page

# ==========================================
# ROUTER
# ==========================================
if page == "🏠 Home": render_home()
elif page == "🔍 Contract Inspector": render_inspector()
elif page == "⚡ Whale Scanner": render_scanner()
