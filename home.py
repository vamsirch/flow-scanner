import streamlit as st
import pandas as pd
from polygon import RESTClient
import datetime
import pytz

st.set_page_config(layout="wide")
st.title("🛠️ Polygon Data Diagnostic")

# 1. API Key Input
api_key = st.text_input("Enter Polygon API Key", type="password")

if st.button("RUN DIAGNOSTIC TEST"):
    if not api_key:
        st.error("Please enter an API Key.")
        st.stop()

    client = RESTClient(api_key)
    status = st.status("Running tests...", expanded=True)

    # --- TEST 1: CONNECTION & SNAPSHOT ---
    try:
        status.write("1️⃣ Testing Connection & Snapshot...")
        # Get Top 5 Active NVDA Contracts
        chain = client.list_snapshot_options_chain("NVDA", params={"limit": 50})
        
        # Sort manually to be safe
        active = []
        for c in chain:
            if c.day and c.day.volume:
                active.append(c)
        active.sort(key=lambda x: x.day.volume, reverse=True)

        if not active:
            st.error("❌ Connection Successful, but NO active contracts found. Market might be closed or API Key has no options access.")
            st.stop()
        
        top_contract = active[0]
        symbol = top_contract.details.ticker
        volume = top_contract.day.volume
        
        status.write(f"✅ Success! Found Active Contract: **{symbol}**")
        status.write(f"📊 Daily Volume for this contract: **{volume:,}**")
        
    except Exception as e:
        st.error(f"❌ CRITICAL ERROR on Snapshot: {e}")
        st.stop()

    # --- TEST 2: TRADE DATA FETCH ---
    try:
        status.write(f"2️⃣ Force-downloading trades for {symbol}...")
        
        # Request last 10 trades strictly
        trades = client.list_trades(symbol, limit=10)
        trade_list = list(trades) # Force iterator to list
        
        if len(trade_list) == 0:
            st.warning(f"⚠️ Snapshot said volume was {volume}, but Trade Endpoint returned 0 trades.")
            st.write("Possible Causes:")
            st.write("1. You are on 'Delayed Data' and the market opened less than 15 mins ago.")
            st.write("2. Your API Key is valid for 'Snapshots' but not 'Trades' (Unlikely).")
        else:
            status.write(f"✅ Success! Downloaded **{len(trade_list)}** raw trades.")
            
            # Display Raw Data
            data = []
            for t in trade_list:
                # Convert time
                ts = datetime.datetime.fromtimestamp(t.participant_timestamp / 1e9, tz=pytz.utc)
                ts_est = ts.astimezone(pytz.timezone('US/Eastern')).strftime('%H:%M:%S')
                
                data.append({
                    "Time (EST)": ts_est,
                    "Price": t.price,
                    "Size": t.size,
                    "Value ($)": t.price * t.size * 100,
                    "Conditions": t.conditions
                })
            
            st.write("### 🔎 Raw Trade Tape (Proof of Life)")
            st.dataframe(pd.DataFrame(data))

    except Exception as e:
        st.error(f"❌ CRITICAL ERROR on Trade Fetch: {e}")
    
    status.update(label="Diagnostic Complete", state="complete", expanded=True)
