import streamlit as st
import pandas as pd
import requests
from supabase import create_client
from collections import defaultdict

# ===================== Supabase Connection =====================
supabase_url = st.secrets["supabase"]["url"]
supabase_key = st.secrets["supabase"]["key"]
client = create_client(supabase_url, supabase_key)

# ===================== Fetch Stock Data (auto-refresh every minute) =====================
@st.cache_data(ttl=60)
def get_stock_list():
    """
    Fetch the latest stock/cash prices from IDBourse API every minute.
    """
    try:
        response = requests.get("https://backend.idbourse.com/api_2/get_all_data", timeout=10)
        response.raise_for_status()
        data = response.json()
        df = pd.DataFrame(
            [(s.get("name", "N/A"), s.get("dernier_cours", 0)) for s in data],
            columns=["valeur", "cours"]
        )
        # Add CASH
        cash_row = pd.DataFrame([{"valeur": "Cash", "cours": 1}])
        return pd.concat([df, cash_row], ignore_index=True)
    except Exception as e:
        st.error(f"Failed to fetch stock data: {e}")
        return pd.DataFrame(columns=["valeur", "cours"])

stocks = get_stock_list()

# ===================== Helper Functions ========================
def get_all_clients():
    res = client.table("clients").select("*").execute()
    return [r["name"] for r in res.data] if res.data else []

def get_client_info(client_name):
    res = client.table("clients").select("*").eq("name", client_name).execute()
    if res.data:
        return res.data[0]
    return None

def get_client_id(client_name):
    cinfo = get_client_info(client_name)
    if not cinfo:
        return None
    # cinfo["id"] must be an integer column in DB
    return int(cinfo["id"])

def client_has_portfolio(client_name):
    cid = get_client_id(client_name)
    if not cid:
        return False
    port = client.table("portfolios").select("*").eq("client_id", cid).execute()
    return len(port.data) > 0

def get_portfolio(client_name):
    cid = get_client_id(client_name)
    if not cid:
        return pd.DataFrame()
    res = client.table("portfolios").select("*").eq("client_id", cid).execute()
    return pd.DataFrame(res.data)

# ===================== Client Management =======================
def create_client(name):
    if not name:
        st.error("Client name cannot be empty.")
        return
    try:
        client.table("clients").insert({"name": name}).execute()
        st.success(f"Client '{name}' added!")
        st.experimental_rerun()
    except Exception as e:
        st.error(f"Error adding client: {e}")

def rename_client(old_name, new_name):
    cid = get_client_id(old_name)
    if cid is None:
        st.error("Client not found.")
        return
    try:
        client.table("clients").update({"name": new_name}).eq("id", cid).execute()
        st.success(f"Renamed '{old_name}' to '{new_name}'")
        st.experimental_rerun()
    except Exception as e:
        st.error(f"Error renaming client: {e}")

def delete_client(cname):
    cid = get_client_id(cname)
    if cid is None:
        st.error("Client not found.")
        return
    try:
        client.table("clients").delete().eq("id", cid).execute()
        st.success(f"Deleted client '{cname}'")
        st.experimental_rerun()
    except Exception as e:
        st.error(f"Error deleting client: {e}")

def update_client_rates(client_name, exchange_comm, is_pea, custom_tax, mgmt_fee):
    cid = get_client_id(client_name)
    if cid is None:
        st.error("Client not found to update rates.")
        return
    try:
        final_tax = 0.0 if is_pea else float(custom_tax)
        client.table("clients").update({
            "exchange_commission_rate": float(exchange_comm),
            "tax_on_gains_rate": final_tax,
            "is_pea": is_pea,
            "management_fee_rate": float(mgmt_fee)
        }).eq("id", cid).execute()
        st.success(f"Updated rates for {client_name}")
        st.experimental_rerun()
    except Exception as e:
        st.error(f"Error updating client rates: {e}")

# ===================== Portfolio Creation =====================
def create_portfolio_rows(client_name, holdings):
    cid = get_client_id(client_name)
    if cid is None:
        st.error("Client not found.")
        return

    if client_has_portfolio(client_name):
        st.warning(f"Client '{client_name}' already has a portfolio.")
        return

    # Build rows
    rows = []
    for stock, qty in holdings.items():
        if qty > 0:
            rows.append({
                "client_id": cid,
                "valeur": str(stock),
                "quantité": float(qty),
                "vwap": 0.0,
                "cours": 0.0,
                "valorisation": 0.0
            })
    if not rows:
        st.warning("No stocks or cash provided.")
        return

    try:
        # Upsert with on_conflict
        client.table("portfolios").upsert(rows, on_conflict="client_id,valeur").execute()
        st.success(f"Portfolio created for '{client_name}'!")
        st.experimental_rerun()
    except Exception as e:
        st.error(f"Error creating portfolio: {e}")

def new_portfolio_creation_ui(client_name):
    st.subheader(f"➕ Add Initial Holdings for {client_name}")
    if "temp_holdings" not in st.session_state:
        st.session_state.temp_holdings = {}

    new_stock = st.selectbox(f"Select Stock/Cash for {client_name}", stocks["valeur"].tolist(), key=f"new_stock_{client_name}")
    qty = st.number_input(f"Quantity for {client_name}", min_value=1.0, value=1.0, step=0.01, key=f"new_qty_{client_name}")

    if st.button(f"➕ Add {new_stock}", key=f"add_btn_{client_name}"):
        st.session_state.temp_holdings[new_stock] = float(qty)
        st.success(f"Added {qty} of {new_stock}")

    if st.session_state.temp_holdings:
        st.write("### Current Selections:")
        df_hold = pd.DataFrame([
            {"valeur": k, "quantité": v} for k, v in st.session_state.temp_holdings.items()
        ])
        st.dataframe(df_hold, use_container_width=True)

        if st.button(f"💾 Create Portfolio for {client_name}", key=f"create_pf_btn_{client_name}"):
            create_portfolio_rows(client_name, st.session_state.temp_holdings)
            del st.session_state.temp_holdings

# ===================== "Buy" Transaction =====================
def buy_shares(client_name, stock_name, transaction_price, quantity):
    cinfo = get_client_info(client_name)
    if not cinfo:
        st.error("Client info not found.")
        return
    cid = get_client_id(client_name)
    if cid is None:
        st.error("Client not found.")
        return

    exchange_rate = float(cinfo.get("exchange_commission_rate", 0.0))

    raw_cost = float(transaction_price) * float(quantity)
    commission = raw_cost * (exchange_rate / 100.0)
    total_cost = raw_cost + commission

    df = get_portfolio(client_name)
    match = df[df["valeur"] == stock_name]
    try:
        if match.empty:
            # Insert new row
            new_vwap = float(transaction_price)
            client.table("portfolios").upsert([{
                "client_id": cid,
                "valeur": str(stock_name),
                "quantité": float(quantity),
                "vwap": new_vwap,
                "cours": 0.0,
                "valorisation": 0.0
            }], on_conflict="client_id,valeur").execute()
        else:
            old_qty = float(match["quantité"].values[0])
            old_vwap = float(match["vwap"].values[0])
            old_cost_total = old_qty * old_vwap
            new_cost_total = old_cost_total + total_cost
            new_qty = old_qty + float(quantity)
            new_vwap = (new_cost_total / new_qty) if new_qty else 0.0
            client.table("portfolios").update({
                "quantité": new_qty,
                "vwap": new_vwap
            }).eq("client_id", cid).eq("valeur", str(stock_name)).execute()
    except Exception as e:
        st.error(f"Error updating stock for buy: {e}")
        return

    # Update "Cash"
    cash_match = df[df["valeur"] == "Cash"]
    try:
        if cash_match.empty:
            client.table("portfolios").upsert([{
                "client_id": cid,
                "valeur": "Cash",
                "quantité": -total_cost,
                "vwap": 1.0,
                "cours": 0.0,
                "valorisation": 0.0
            }], on_conflict="client_id,valeur").execute()
        else:
            old_cq = float(cash_match["quantité"].values[0])
            new_cq = old_cq - total_cost
            client.table("portfolios").update({
                "quantité": new_cq,
                "vwap": 1.0
            }).eq("client_id", cid).eq("valeur", "Cash").execute()
    except Exception as e:
        st.error(f"Error updating cash after buy: {e}")
        return

    st.success(f"Bought {quantity} of {stock_name} at {transaction_price:.2f}, total cost {total_cost:.2f}")
    st.experimental_rerun()

# ===================== "Sell" Transaction =====================
def sell_shares(client_name, stock_name, transaction_price, quantity):
    cinfo = get_client_info(client_name)
    if not cinfo:
        st.error("Client info not found.")
        return
    cid = get_client_id(client_name)
    if cid is None:
        st.error("Client not found.")
        return

    exchange_rate = float(cinfo.get("exchange_commission_rate", 0.0))
    tax_rate = float(cinfo.get("tax_on_gains_rate", 15.0))

    df = get_portfolio(client_name)
    match = df[df["valeur"] == stock_name]
    if match.empty:
        st.error(f"Client does not hold {stock_name}.")
        return
    old_qty = float(match["quantité"].values[0])
    if quantity > old_qty:
        st.error(f"Cannot sell {quantity}, only {old_qty} available.")
        return

    old_vwap = float(match["vwap"].values[0])
    raw_proceeds = float(transaction_price) * float(quantity)
    commission = raw_proceeds * (exchange_rate/100.0)
    net_proceeds = raw_proceeds - commission
    cost_shares = old_vwap * float(quantity)
    profit = net_proceeds - cost_shares

    if profit > 0:
        tax = profit * (tax_rate / 100.0)
        net_proceeds -= tax

    new_qty = old_qty - float(quantity)
    try:
        if new_qty <= 0.0:
            client.table("portfolios").delete().eq("client_id", cid).eq("valeur", str(stock_name)).execute()
        else:
            client.table("portfolios").update({
                "quantité": new_qty
            }).eq("client_id", cid).eq("valeur", str(stock_name)).execute()
    except Exception as e:
        st.error(f"Error updating stock after sell: {e}")
        return

    # Update Cash
    cash_match = df[df["valeur"] == "Cash"]
    try:
        if cash_match.empty:
            client.table("portfolios").upsert([{
                "client_id": cid,
                "valeur": "Cash",
                "quantité": net_proceeds,
                "vwap": 1.0,
                "cours": 0.0,
                "valorisation": 0.0
            }], on_conflict="client_id,valeur").execute()
        else:
            old_cq = float(cash_match["quantité"].values[0])
            new_cq = old_cq + net_proceeds
            client.table("portfolios").update({
                "quantité": new_cq,
                "vwap": 1.0
            }).eq("client_id", cid).eq("valeur", "Cash").execute()
    except Exception as e:
        st.error(f"Error updating cash after sell: {e}")
        return

    st.success(f"Sold {quantity} of {stock_name} at {transaction_price:.2f}, net {net_proceeds:.2f}.")
    st.experimental_rerun()

# ===================== Show Single Portfolio =====================
def show_portfolio(client_name, read_only=False):
    cid = get_client_id(client_name)
    if cid is None:
        st.warning("Client not found.")
        return
    df = get_portfolio(client_name)
    if df.empty:
        st.warning(f"No portfolio found for '{client_name}'")
        return

    # Recompute from 'stocks'
    df = df.copy()  # to avoid SettingWithCopyWarning
    df["quantité"] = df["quantité"].astype(float)

    # For each row, compute cours, valorisation, cost_total, performance_latente
    for i, row in df.iterrows():
        val = str(row["valeur"])
        match = stocks[stocks["valeur"] == val]
        live_price = float(match["cours"].values[0]) if not match.empty else 0.0
        df.at[i, "cours"] = live_price
        qty = float(row["quantité"])
        vwap_ = float(row.get("vwap", 0.0))
        df.at[i, "valorisation"] = round(qty * live_price, 2)
        cost_total = round(qty * vwap_, 2)
        df.at[i, "cost_total"] = cost_total
        df.at[i, "performance_latente"] = round(df.at[i, "valorisation"] - cost_total, 2)

    total_value = df["valorisation"].sum()
    if total_value > 0:
        df["poids"] = ((df["valorisation"] / total_value)*100).round(2)
    else:
        df["poids"] = 0.0

    st.subheader(f"📜 Portfolio for {client_name}")
    st.write(f"**Valorisation totale du portefeuille:** {total_value:.2f}")

    if read_only:
        # read-only => skip editing forms
        # color performance
        df_styled = df.style.format(
            subset=["quantité","vwap","cours","cost_total","valorisation","performance_latente","poids"],
            formatter="{:.2f}"
        ).applymap(
            lambda x: "color:red;" if (isinstance(x, (int,float)) and x<0) else ("color:green;" if isinstance(x,(int,float)) and x>0 else ""),
            subset=["performance_latente"]
        )
        st.dataframe(df_styled, use_container_width=True)
        return

    # If not read_only => do everything
    # Commission Editor
    with st.expander(f"Edit Commission/Taxes/Fees for {client_name}", expanded=False):
        cinfo = get_client_info(client_name)
        if cinfo:
            exch = float(cinfo.get("exchange_commission_rate", 0.0))
            mgf = float(cinfo.get("management_fee_rate", 0.0))
            pea = bool(cinfo.get("is_pea", False))
            tax = float(cinfo.get("tax_on_gains_rate", 15.0))
            new_exch = st.number_input(f"Exchange Commission Rate (%) - {client_name}", min_value=0.0, value=exch, step=0.01, key=f"exch_{client_name}")
            new_mgmt = st.number_input(f"Management Fee Rate (%) - {client_name}", min_value=0.0, value=mgf, step=0.01, key=f"mgf_{client_name}")
            new_pea = st.checkbox(f"Is {client_name} PEA (Tax Exempt)?", value=pea, key=f"pea_{client_name}")
            new_tax = st.number_input(f"Tax on Gains (%) - {client_name}", min_value=0.0, value=tax, step=0.01, key=f"tax_{client_name}")
            if st.button(f"Update Client Rates - {client_name}", key=f"update_rates_{client_name}"):
                update_client_rates(client_name, new_exch, new_pea, new_tax, new_mgmt)
        else:
            st.write("Client not found to set rates")

    # Show with color
    columns_display = ["valeur","quantité","vwap","cours","cost_total","valorisation","performance_latente","poids"]
    df_display = df[columns_display].copy()

    def color_perf(x):
        if isinstance(x, (float,int)) and x>0:
            return "color:green;"
        elif isinstance(x, (float,int)) and x<0:
            return "color:red;"
        return ""

    df_styled = df_display.style.format("{:.2f}", subset=["quantité","vwap","cours","cost_total","valorisation","performance_latente","poids"]) \
                                 .applymap(color_perf, subset=["performance_latente"])

    st.write("#### Current Holdings (read-only table with color styling)")
    st.dataframe(df_styled, use_container_width=True)

    # Manual Edits
    with st.expander("Manual Edits (Quantity / VWAP)", expanded=False):
        edit_cols = ["valeur","quantité","vwap"]
        edf = df[edit_cols].copy()
        updated_df = st.data_editor(
            edf,
            use_container_width=True,
            key=f"portfolio_editor_{client_name}",
            column_config={
                "quantité": st.column_config.NumberColumn("Quantité", format="%.2f", step=0.01),
                "vwap": st.column_config.NumberColumn("VWAP", format="%.4f", step=0.001),
            }
        )
        if st.button(f"💾 Save Edits (Quantity / VWAP) for {client_name}", key=f"save_edits_btn_{client_name}"):
            for idx, row2 in updated_df.iterrows():
                st_name = str(row2["valeur"])
                st_q = float(row2["quantité"])
                st_vw = float(row2["vwap"])
                try:
                    client.table("portfolios").update({
                        "quantité": st_q,
                        "vwap": st_vw
                    }).eq("client_id", get_client_id(client_name)).eq("valeur", st_name).execute()
                except Exception as e:
                    st.error(f"Error saving manual edits for {st_name}: {e}")
            st.success(f"Portfolio updated for '{client_name}'!")
            st.experimental_rerun()

    # Buy
    st.write("### Buy Transaction")
    buy_stock = st.selectbox(f"Stock to BUY for {client_name}", stocks["valeur"].tolist(), key=f"buy_s_{client_name}")
    buy_price = st.number_input(f"Buy Price for {buy_stock}", min_value=0.0, value=0.0, step=0.01, key=f"buy_price_{client_name}")
    buy_qty = st.number_input(f"Buy Quantity for {buy_stock}", min_value=1.0, value=1.0, step=0.01, key=f"buy_qty_{client_name}")
    if st.button(f"BUY {buy_stock}", key=f"buy_btn_{client_name}"):
        buy_shares(client_name, buy_stock, buy_price, buy_qty)

    # Sell
    st.write("### Sell Transaction")
    existing_stocks = df[df["valeur"] != "Cash"]["valeur"].unique().tolist()
    sell_stock = st.selectbox(f"Stock to SELL for {client_name}", existing_stocks, key=f"sell_s_{client_name}")
    sell_price = st.number_input(f"Sell Price for {sell_stock}", min_value=0.0, value=0.0, step=0.01, key=f"sell_price_{client_name}")
    sell_qty = st.number_input(f"Sell Quantity for {sell_stock}", min_value=1.0, value=1.0, step=0.01, key=f"sell_qty_{client_name}")
    if st.button(f"SELL {sell_stock}", key=f"sell_btn_{client_name}"):
        sell_shares(client_name, sell_stock, sell_price, sell_qty)

# ===================== Show All Portfolios =====================
def show_all_portfolios():
    clients = get_all_clients()
    if not clients:
        st.warning("No clients found.")
        return

    # We'll show each client in read-only mode
    for cname in clients:
        st.write(f"### Client: {cname}")
        show_portfolio(cname, read_only=True)
        st.write("---")

# ===================== Inventory Page =====================
def show_inventory():
    clients = get_all_clients()
    if not clients:
        st.warning("No clients found. Please create a client first.")
        return

    master_data = defaultdict(lambda: {"quantity": 0, "clients": set()})
    overall_portfolio_sum = 0.0

    for c in clients:
        df = get_portfolio(c)
        if not df.empty:
            portfolio_val = 0.0
            for _, row in df.iterrows():
                val = str(row["valeur"])
                qty = float(row["quantité"])
                match = stocks[stocks["valeur"] == val]
                price = float(match["cours"].values[0]) if not match.empty else 0.0
                val_agg = qty * price
                portfolio_val += val_agg
                master_data[val]["quantity"] += qty
                master_data[val]["clients"].add(c)
            overall_portfolio_sum += portfolio_val

    if not master_data:
        st.title("🗃️ Global Inventory")
        st.write("No stocks or cash found in any portfolio.")
        return

    rows_data = []
    sum_of_all_stocks_val = 0.0

    for val, info in master_data.items():
        match = stocks[stocks["valeur"] == val]
        price = float(match["cours"].values[0]) if not match.empty else 0.0
        agg_val = info["quantity"] * price
        sum_of_all_stocks_val += agg_val
        rows_data.append({
            "valeur": val,
            "quantité total": info["quantity"],
            "valorisation": agg_val,
            "portefeuille": ", ".join(sorted(info["clients"]))
        })

    for row in rows_data:
        if sum_of_all_stocks_val > 0:
            row["poids"] = round((row["valorisation"] / sum_of_all_stocks_val) * 100, 2)
        else:
            row["poids"] = 0.0

    st.title("🗃️ Global Inventory")
    inv_df = pd.DataFrame(rows_data)
    st.dataframe(
        inv_df[["valeur", "quantité total", "valorisation", "poids", "portefeuille"]],
        use_container_width=True
    )

    st.write(f"### Actif sous gestion: {overall_portfolio_sum:.2f}")

# ===================== Market Page =====================
def show_market():
    """
    Displays real-time stock/cash data from IDBourse API as read-only.
    Refreshes every minute.
    """
    st.title("📈 Market")
    st.write("Below are the current stocks/cash with real-time prices (refreshed every minute).")
    market_df = get_stock_list()
    st.dataframe(market_df, use_container_width=True)

# ===================== Pages =====================
page = st.sidebar.selectbox(
    "📂 Navigation",
    [
        "Manage Clients",
        "Create Portfolio",
        "View Client Portfolio",
        "View All Portfolios",
        "Inventory",
        "Market"
    ]
)

if page == "Manage Clients":
    st.title("👤 Manage Clients")
    existing = get_all_clients()

    with st.form("add_client_form", clear_on_submit=True):
        new_client_name = st.text_input("New Client Name", key="new_client_input")
        if st.form_submit_button("➕ Add Client"):
            create_client(new_client_name)

    if existing:
        # Rename
        with st.form("rename_client_form", clear_on_submit=True):
            rename_choice = st.selectbox("Select Client to Rename", options=existing, key="rename_choice")
            rename_new = st.text_input("New Client Name", key="rename_text")
            if st.form_submit_button("✏️ Rename Client"):
                rename_client(rename_choice, rename_new)

        # Delete
        with st.form("delete_client_form", clear_on_submit=True):
            delete_choice = st.selectbox("Select Client to Delete", options=existing, key="delete_choice")
            if st.form_submit_button("🗑️ Delete Client"):
                delete_client(delete_choice)

elif page == "Create Portfolio":
    st.title("📊 Create Client Portfolio")
    clist = get_all_clients()
    if not clist:
        st.warning("No clients found. Please create a client first.")
    else:
        cselect = st.selectbox("Select Client", clist, key="create_pf_select")
        if cselect:
            if client_has_portfolio(cselect):
                st.warning(f"Client '{cselect}' already has a portfolio. Go to 'View Client Portfolio' to edit.")
            else:
                new_portfolio_creation_ui(cselect)

elif page == "View Client Portfolio":
    st.title("📊 View Client Portfolio")
    c2 = get_all_clients()
    if not c2:
        st.warning("No clients found. Please create a client first.")
    else:
        client_selected = st.selectbox("Select Client", c2, key="view_portfolio_select")
        if client_selected:
            show_portfolio(client_selected, read_only=False)

elif page == "View All Portfolios":
    st.title("📊 All Clients' Portfolios")
    show_all_portfolios()

elif page == "Inventory":
    show_inventory()

elif page == "Market":
    show_market()
