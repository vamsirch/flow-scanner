import streamlit as st
import pandas as pd
from polygon import RESTClient
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
    st.session_state["scanner_data"] = deque(maxlen=10000)
if "api_key" not in st.session_state:
    st.session_state["api_key"] = ""

# --- SIDEBAR ---
with st.sidebar:
    st.title("🐋 FlowTrend AI")
    st.caption("Mode: Mag 7 Volume Scanner")
    page = st.radio("Navigate", ["🏠 Home", "🔍 Contract Inspector", "⚡ Live Whale Scanner"])
    st.divider()
    api_input = st.text_input("Polygon API Key", value=st.session_state["api_key"], type="password")
    if api_input: st.session_state["api_key"] = api_input.strip()

# --- HELPERS ---
def get_stock_price(client, ticker):
    """Gets the current stock price to find ATM contracts"""
    try:
        # Try Snapshot First
        snap = client.get_snapshot_ticker("stocks", ticker)
        return snap.last_trade.price
    except:
        # Fallback to yesterday's close
        try:
            prev = client.get_previous_close_agg(ticker)
            return prev[0].close
        except:
            return 0

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
# PAGE 3: MAG 7 VOLUME SCANNER
# ==========================================
def render_scanner():
    st.title("⚡ Mag 7 Volume Scanner")
    st.caption("Scans available contracts one-by-one to find where the Volume is.")
    
    api_key = st.session_state["api_key"]
    if not api_key: return st.error("Enter API Key.")

    # --- THE SCAN ENGINE ---
    def run_volume_scan(key, tickers):
        client = RESTClient(key)
        status = st.status("⏳ Initializing Mag 7 Scan...", expanded=True)
        
        results = []
        today = datetime.now().strftime("%Y-%m-%d")
        
        for t in tickers:
            try:
                # 1. Get Stock Price (to find relevant contracts)
                status.write(f"📉 Checking Price for {t}...")
                stock_price = get_stock_price(client, t)
                if stock_price == 0:
                    status.warning(f"Could not get price for {t}, skipping...")
                    continue
                
                # 2. Get Contract List (Near the Money)
                # We look for strikes within +/- 10% of stock price to save time
                min_strike = stock_price * 0.90
                max_strike = stock_price * 1.10
                
                status.write(f"📥 Fetching {t} contracts near ${stock_price:.2f}...")
                
                contracts = client.list_options_contracts(
                    underlying_ticker=t,
                    expiration_date_gte=today,
                    strike_price_gte=min_strike,
                    strike_price_lte=max_strike,
                    limit=50, # Check top 50 most relevant contracts
                    sort="expiration_date",
                    order="asc"
                )
                
                contract_list = [c for c in contracts]
                
                # 3. Check Volume for Each Contract (The Manual Snapshot)
                status.write(f"🔎 Checking Volume on {len(contract_list)} {t} contracts...")
                
                progress_bar = status.progress(0)
                for i, c in enumerate(contract_list):
                    if i % 5 == 0: progress_bar.progress((i + 1) / len(contract_list))
                    
                    try:
                        # Get Daily Stats (Open/High/Low/Close/Volume)
                        # This works even if Snapshot is blocked
                        aggs = client.get_aggs(c.ticker, 1, "day", today, today)
                        
                        if aggs:
                            day_stat = aggs[0] # The bar for today
                            if day_stat.volume > 0:
                                results.append({
                                    "Symbol": t,
                                    "Strike": f"${c.strike_price:.2f}",
                                    "Type": c.contract_type.upper(),
                                    "Expiry": c.expiration_date,
                                    "Volume": day_stat.volume,
                                    "Volume $": day_stat.volume * day_stat.close * 100,
                                    "Close Price": day_stat.close,
                                    "Contract": c.ticker
                                })
                    except: continue
                    
            except Exception as e:
                print(f"Error on {t}: {e}")
                continue
        
        # Sort results by Volume (Highest First)
        results.sort(key=lambda x: x["Volume $"], reverse=True)
        
        status.update(label=f"Scan Complete! Found {len(results)} active contracts.", state="complete", expanded=False)
        return pd.DataFrame(results)

    # --- CONTROLS ---
    c1, c2 = st.columns([3, 1])
    with c1:
        # Pre-select Mag 7
        default_list = ["NVDA", "TSLA", "AAPL", "AMD", "SPY", "AMZN", "MSFT"]
        watch = st.multiselect("Mag 7 Watchlist", default_list, default=default_list)
    with c2:
        st.write("")
        st.write("")
        if st.button("🚀 Scan Volume"):
            df = run_volume_scan(api_key, watch)
            st.session_state["mag7_data"] = df

    # --- DISPLAY ---
    if "mag7_data" in st.session_state:
        df = st.session_state["mag7_data"]
        
        if not df.empty:
            # Color styling
            def style_rows(row):
                color = '#d4f7d4' if row['Type'] == 'CALL' else '#f7d4d4'
                return [f'background-color: {color}; color: black'] * len(row)

            st.dataframe(
                df.style.apply(style_rows, axis=1).format({"Volume $": "${:,.0f}", "Close Price": "${:.2f}"}),
                use_container_width=True,
                height=800,
                column_config={
                    "Volume $": st.column_config.ProgressColumn("Dollar Volume", format="$%.0f", min_value=0, max_value=max(df["Volume $"].max(), 100_000)),
                    "Volume": st.column_config.NumberColumn("Vol", format="%d"),
                },
                hide_index=True
            )
        else:
            st.warning("No volume found. (Market might be closed or contracts are inactive)")

# ==========================================
# ROUTER
# ==========================================
if page == "🏠 Home": render_home()
elif page == "🔍 Contract Inspector": render_inspector()
elif page == "⚡ Live Whale Scanner": render_scanner()
