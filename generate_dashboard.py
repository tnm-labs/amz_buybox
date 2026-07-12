"""
TNM Buy Box Dashboard Generator (v3)
--------------------------------------
Reads data/listings.csv (ASIN, Title, Brand, Category, SKU, Your_Price,
Listing_Type, Is_Top_50), pulls live Buy Box / offer data from Amazon
SP-API for each ASIN, computes team-facing metrics, and writes
docs/index.html with two tabs: "Top 50" and "All listings" - each with
its own summary cards, clickable drill-down, and brand/category
breakdowns. Appends a row to data/history.csv for trend tracking, and
sends a WhatsApp alert if the Top 50 Buy Box win rate < 20%.

Winner status is determined by comparing your known listed price
(Your_Price) against the Buy Box price returned by Amazon - NOT by
looking for a "MyOffer" flag in the response, which is unreliable
when your own listing isn't among the top offers Amazon returns.

Required environment variables (set as GitHub Actions secrets):
    SP_API_REFRESH_TOKEN
    SP_API_LWA_APP_ID
    SP_API_LWA_CLIENT_SECRET
    CALLMEBOT_PHONE
    CALLMEBOT_APIKEY
"""

import os
import sys
import time
import json
from datetime import datetime, timezone

import pandas as pd
import numpy as np
import requests
from sp_api.api import Products
from sp_api.base import Marketplaces


class NumpySafeEncoder(json.JSONEncoder):
    def default(self, obj):
        if isinstance(obj, np.bool_):
            return bool(obj)
        if isinstance(obj, (np.integer,)):
            return int(obj)
        if isinstance(obj, (np.floating,)):
            return float(obj)
        return super().default(obj)


LISTINGS_FILE = "data/listings.csv"
HISTORY_FILE = "data/history.csv"
DASHBOARD_FILE = "docs/index.html"
DETAIL_FILE = "docs/details.csv"

MARKETPLACE = Marketplaces.IN
ITEM_CONDITION = "New"
DELAY_BETWEEN_CALLS = 3
MAX_RETRIES = 4

BUY_BOX_ALERT_THRESHOLD_PCT = 20.0
CLOSE_GAP_RS = 50
MEDIUM_GAP_RS = 100
HEADROOM_RS = 100


def load_credentials():
    creds = {
        "refresh_token": os.environ.get("SP_API_REFRESH_TOKEN"),
        "lwa_app_id": os.environ.get("SP_API_LWA_APP_ID"),
        "lwa_client_secret": os.environ.get("SP_API_LWA_CLIENT_SECRET"),
    }
    missing = [k for k, v in creds.items() if not v]
    if missing:
        print(f"Missing environment variables: {', '.join(missing)}")
        sys.exit(1)
    return creds


def fetch_offer_data(client, asin, your_price):
    for attempt in range(MAX_RETRIES):
        try:
            response = client.get_item_offers(asin, item_condition=ITEM_CONDITION)
            payload = response.payload
            break
        except Exception as e:
            if "QuotaExceeded" in str(e) and attempt < MAX_RETRIES - 1:
                wait = 5 * (attempt + 1)
                print(f"  Quota hit, waiting {wait}s before retry...")
                time.sleep(wait)
                continue
            return {"status": f"error: {e}"}

    summary = payload.get("Summary", {})
    offers = payload.get("Offers", [])

    lowest_prices = summary.get("LowestPrices", [])
    lowest_price = lowest_prices[0].get("LandedPrice", {}).get("Amount") if lowest_prices else None

    buy_box_prices = summary.get("BuyBoxPrices", [])
    buy_box_price = buy_box_prices[0].get("LandedPrice", {}).get("Amount") if buy_box_prices else None

    num_offers = sum(o.get("OfferCount", 0) for o in summary.get("NumberOfOffers", []))

    is_winner = None
    gap_to_buybox = None
    headroom = None

    if buy_box_price is not None and your_price is not None:
        is_winner = your_price <= (buy_box_price + 1)
        if not is_winner:
            gap_to_buybox = round(your_price - buy_box_price, 2)
        else:
            all_prices = sorted([
                o.get("ListingPrice", {}).get("Amount")
                for o in offers
                if o.get("ListingPrice", {}).get("Amount") is not None
            ])
            if len(all_prices) > 1:
                headroom = round(all_prices[1] - your_price, 2)

    return {
        "status": "ok",
        "buy_box_price": buy_box_price,
        "lowest_price": lowest_price,
        "num_offers": num_offers,
        "is_buy_box_winner": is_winner,
        "gap_to_buybox": gap_to_buybox,
        "headroom": headroom,
    }


def compute_metrics(df):
    total = len(df)
    ok_df = df[df["status"] == "ok"]
    errors = total - len(ok_df)
    won = ok_df[ok_df["is_buy_box_winner"] == True]
    lost = ok_df[ok_df["is_buy_box_winner"] == False]
    win_pct = round(100 * len(won) / len(ok_df), 1) if len(ok_df) else 0.0

    close_50 = lost[(lost["gap_to_buybox"].notna()) & (lost["gap_to_buybox"] <= CLOSE_GAP_RS)]
    close_100 = lost[(lost["gap_to_buybox"].notna()) & (lost["gap_to_buybox"] > CLOSE_GAP_RS) & (lost["gap_to_buybox"] <= MEDIUM_GAP_RS)]
    far_100 = lost[(lost["gap_to_buybox"].notna()) & (lost["gap_to_buybox"] > MEDIUM_GAP_RS)]
    headroom = won[(won["headroom"].notna()) & (won["headroom"] > HEADROOM_RS)]
    single_offer = ok_df[ok_df["num_offers"] <= 1]

    return {
        "total_tracked": total,
        "errors": errors,
        "buy_box_won": len(won),
        "buy_box_lost": len(lost),
        "buy_box_win_pct": win_pct,
        "close_to_winning_under_50": len(close_50),
        "close_to_winning_50_to_100": len(close_100),
        "far_from_winning_over_100": len(far_100),
        "won_with_headroom_over_100": len(headroom),
        "single_offer_listings": len(single_offer),
    }


def compute_group_breakdown(df, group_col):
    ok_df = df[df["status"] == "ok"]
    rows = []
    for name, g in ok_df.groupby(group_col):
        won = (g["is_buy_box_winner"] == True).sum()
        total = len(g)
        win_pct = round(100 * won / total, 1) if total else 0.0
        rows.append({"name": name, "total": total, "won": int(won), "win_pct": win_pct})
    rows.sort(key=lambda r: r["total"], reverse=True)
    return rows


def build_drilldowns(df):
    ok = df[df["status"] == "ok"]
    lost = ok[ok["is_buy_box_winner"] == False]
    won = ok[ok["is_buy_box_winner"] == True]

    def rows(sub_df):
        cols = ["ASIN", "Title", "Brand", "Category", "Your_Price", "buy_box_price", "gap_to_buybox", "num_offers"]
        return sub_df[cols].fillna("").to_dict(orient="records")

    return {
        "total": rows(df),
        "won": rows(won),
        "lost": rows(lost),
        "close_50": rows(lost[(lost["gap_to_buybox"].notna()) & (lost["gap_to_buybox"] <= CLOSE_GAP_RS)]),
        "close_100": rows(lost[(lost["gap_to_buybox"].notna()) & (lost["gap_to_buybox"] > CLOSE_GAP_RS) & (lost["gap_to_buybox"] <= MEDIUM_GAP_RS)]),
        "far_100": rows(lost[(lost["gap_to_buybox"].notna()) & (lost["gap_to_buybox"] > MEDIUM_GAP_RS)]),
        "headroom": rows(won[(won["headroom"].notna()) & (won["headroom"] > HEADROOM_RS)]),
        "single_offer": rows(ok[ok["num_offers"] <= 1]),
        "errors": rows(df[df["status"] != "ok"]),
    }


def send_whatsapp_alert(message):
    phone = os.environ.get("CALLMEBOT_PHONE")
    apikey = os.environ.get("CALLMEBOT_APIKEY")
    if not phone or not apikey:
        print("CallMeBot credentials not set - skipping WhatsApp alert.")
        return
    url = "https://api.callmebot.com/whatsapp.php"
    params = {"phone": phone, "text": message, "apikey": apikey}
    try:
        r = requests.get(url, params=params, timeout=15)
        print("WhatsApp alert response:", r.status_code, r.text[:200])
    except Exception as e:
        print("Failed to send WhatsApp alert:", e)


def render_tab_content(tab_id, metrics, brand_bd, category_bd, priority_rows, priority_title):
    def card(label, value, sub, key):
        return f"""<div class="card" onclick="showDrilldown('{tab_id}', '{key}', '{label}')">
            <div class="card-value">{value}</div>
            <div class="card-label">{label}</div>
            <div class="card-sub">{sub}</div>
        </div>"""

    cards_html = "".join([
        card("Total tracked", metrics["total_tracked"], "", "total"),
        card("Buy box win rate", f'{metrics["buy_box_win_pct"]}%', f'{metrics["buy_box_won"]} won / {metrics["buy_box_lost"]} lost', "won"),
        card("Close (under Rs 50)", metrics["close_to_winning_under_50"], "gap under Rs 50", "close_50"),
        card("Close (Rs 50-100)", metrics["close_to_winning_50_to_100"], "gap Rs 50-100", "close_100"),
        card("Far (over Rs 100)", metrics["far_from_winning_over_100"], "gap over Rs 100", "far_100"),
        card("Room to raise price", metrics["won_with_headroom_over_100"], "winning by over Rs 100", "headroom"),
        card("Single-offer", metrics["single_offer_listings"], "no real competition", "single_offer"),
        card("Fetch errors", metrics["errors"], "failed to fetch", "errors"),
    ])

    brand_rows = "".join(
        f"<tr><td>{b['name']}</td><td>{b['total']}</td><td>{b['won']}</td><td>{b['win_pct']}%</td></tr>"
        for b in brand_bd
    )
    category_rows = "".join(
        f"<tr><td>{c['name']}</td><td>{c['total']}</td><td>{c['won']}</td><td>{c['win_pct']}%</td></tr>"
        for c in category_bd
    )

    priority_rows_html = ""
    for r in priority_rows:
        won = r.get("is_buy_box_winner") == True
        status_label = "Won" if won else ("Error" if r.get("status") != "ok" else "Lost")
        row_style = "" if won else 'style="background: #fdeaea;"'
        priority_rows_html += f"""<tr {row_style}>
            <td>{str(r.get('Title',''))[:55]}</td>
            <td>{r.get('ASIN','')}</td>
            <td>{r.get('Brand','')}</td>
            <td>{r.get('Category','')}</td>
            <td>{r.get('Your_Price','')}</td>
            <td>{r.get('buy_box_price','')}</td>
            <td>{status_label}</td>
        </tr>"""

    return f"""
  <div class="cards">
    {cards_html}
  </div>
  <div class="hint">Click any number to see the underlying ASINs.</div>

  <div id="drilldown-panel-{tab_id}" class="drilldown-panel">
    <div class="drilldown-header">
      <span id="drilldown-title-{tab_id}"></span>
      <span class="drilldown-close" onclick="closeDrilldown('{tab_id}')">Close</span>
    </div>
    <div id="drilldown-body-{tab_id}"></div>
  </div>

  <h2>{priority_title}</h2>
  <table>
    <tr><th>Title</th><th>ASIN</th><th>Brand</th><th>Category</th><th>Your price</th><th>Buy box price</th><th>Status</th></tr>
    {priority_rows_html}
  </table>

  <div class="views">
    <div>
      <h2>By brand</h2>
      <table>
        <tr><th>Brand</th><th>Total</th><th>Won</th><th>Win %</th></tr>
        {brand_rows}
      </table>
    </div>
    <div>
      <h2>By category</h2>
      <table>
        <tr><th>Category</th><th>Total</th><th>Won</th><th>Win %</th></tr>
        {category_rows}
      </table>
    </div>
  </div>
"""


def render_dashboard(top50_metrics, top50_brand_bd, top50_cat_bd, top50_drilldowns, top50_priority_rows,
                      all_metrics, all_brand_bd, all_cat_bd, all_drilldowns, all_priority_rows):
    generated_at = datetime.now(timezone.utc).astimezone().strftime("%Y-%m-%d %H:%M %Z")

    top50_content = render_tab_content("top50", top50_metrics, top50_brand_bd, top50_cat_bd,
                                        top50_priority_rows, "Top 50 priority listings")
    all_content = render_tab_content("all", all_metrics, all_brand_bd, all_cat_bd,
                                      all_priority_rows[:30],
                                      "Top 30 listings furthest from winning (all listings)")

    drilldowns_json = json.dumps({"top50": top50_drilldowns, "all": all_drilldowns}, cls=NumpySafeEncoder)

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<title>TNM Buy Box Dashboard</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<style>
  body {{ font-family: Arial, Helvetica, sans-serif; background: #f4f6f8; margin: 0; padding: 24px; color: #222; }}
  h1 {{ margin-bottom: 4px; }}
  h2 {{ margin-top: 32px; margin-bottom: 12px; }}
  .updated {{ color: #666; margin-bottom: 16px; font-size: 14px; }}
  .tabs {{ display: flex; gap: 8px; margin-bottom: 24px; border-bottom: 2px solid #ddd; }}
  .tab-button {{ padding: 10px 20px; border: none; background: none; font-size: 14px; cursor: pointer; color: #666; border-bottom: 3px solid transparent; margin-bottom: -2px; }}
  .tab-button.active {{ color: #2F5496; border-bottom-color: #2F5496; font-weight: bold; }}
  .tab-content {{ display: none; }}
  .tab-content.active {{ display: block; }}
  .cards {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(160px, 1fr)); gap: 16px; margin-bottom: 8px; }}
  .card {{ background: white; border-radius: 8px; padding: 16px; box-shadow: 0 1px 3px rgba(0,0,0,0.1); text-align: center; cursor: pointer; }}
  .card:hover {{ box-shadow: 0 2px 8px rgba(0,0,0,0.18); }}
  .card-value {{ font-size: 24px; font-weight: bold; color: #2F5496; }}
  .card-label {{ font-size: 12px; color: #444; margin-top: 4px; }}
  .card-sub {{ font-size: 10px; color: #888; margin-top: 2px; }}
  .hint {{ font-size: 12px; color: #999; margin-bottom: 20px; }}
  table {{ width: 100%; border-collapse: collapse; background: white; border-radius: 8px; overflow: hidden; box-shadow: 0 1px 3px rgba(0,0,0,0.1); margin-bottom: 24px;}}
  th, td {{ padding: 9px 12px; text-align: left; border-bottom: 1px solid #eee; font-size: 13px; }}
  th {{ background: #2F5496; color: white; }}
  .views {{ display: grid; grid-template-columns: 1fr 1fr; gap: 24px; }}
  @media (max-width: 700px) {{ .views {{ grid-template-columns: 1fr; }} }}
  .drilldown-panel {{ display: none; background: white; border-radius: 8px; padding: 16px; box-shadow: 0 1px 3px rgba(0,0,0,0.15); margin-bottom: 24px; }}
  .drilldown-header {{ margin: 0 0 12px 0; display: flex; justify-content: space-between; align-items: center; font-weight: bold; }}
  .drilldown-close {{ cursor: pointer; color: #2F5496; font-size: 13px; font-weight: normal; }}
</style>
</head>
<body>
  <h1>TNM Buy Box Dashboard</h1>
  <div class="updated">Last updated: {generated_at}</div>

  <div class="tabs">
    <button class="tab-button active" onclick="switchTab('top50')">Top 50</button>
    <button class="tab-button" onclick="switchTab('all')">All listings</button>
  </div>

  <div id="tab-top50" class="tab-content active">
    {top50_content}
  </div>
  <div id="tab-all" class="tab-content">
    {all_content}
  </div>

  <p style="color:#999; font-size:12px;">
    Auto-generated twice daily. "Gap to buy box" = your price minus current buy box price
    (smaller is closer to winning). "Room to raise price" = listings you're winning where
    the next cheapest offer is priced more than Rs {HEADROOM_RS} above you. Buy box winner
    status is determined by comparing your known listed price to the buy box price Amazon
    returns, since Amazon does not reliably identify your own offer in the response.
  </p>

<script>
const drilldowns = {drilldowns_json};

function switchTab(tab) {{
  document.querySelectorAll('.tab-content').forEach(el => el.classList.remove('active'));
  document.querySelectorAll('.tab-button').forEach(el => el.classList.remove('active'));
  document.getElementById('tab-' + tab).classList.add('active');
  event.target.classList.add('active');
}}

function showDrilldown(tabId, key, label) {{
  const panel = document.getElementById('drilldown-panel-' + tabId);
  const title = document.getElementById('drilldown-title-' + tabId);
  const body = document.getElementById('drilldown-body-' + tabId);
  const rows = drilldowns[tabId][key] || [];

  title.textContent = label + ' (' + rows.length + ')';

  if (rows.length === 0) {{
    body.innerHTML = '<p style="color:#888; font-size:13px;">No listings in this group.</p>';
  }} else {{
    let html = '<table><tr><th>Title</th><th>ASIN</th><th>Brand</th><th>Category</th>' +
               '<th>Your price</th><th>Buy box price</th><th>Gap</th></tr>';
    rows.forEach(r => {{
      html += '<tr><td>' + String(r.Title || '').slice(0, 55) + '</td><td>' + r.ASIN +
              '</td><td>' + (r.Brand || '') + '</td><td>' + (r.Category || '') +
              '</td><td>' + (r.Your_Price || '') + '</td><td>' + (r.buy_box_price || '') +
              '</td><td>' + (r.gap_to_buybox !== undefined ? r.gap_to_buybox : '') + '</td></tr>';
    }});
    html += '</table>';
    body.innerHTML = html;
  }}

  panel.style.display = 'block';
  panel.scrollIntoView({{ behavior: 'smooth', block: 'start' }});
}}

function closeDrilldown(tabId) {{
  document.getElementById('drilldown-panel-' + tabId).style.display = 'none';
}}
</script>
</body>
</html>"""

    return html


def main():
    creds = load_credentials()

    listings = pd.read_csv(LISTINGS_FILE, dtype={"ASIN": str})
    listings["Is_Top_50"] = listings["Is_Top_50"].astype(str).str.lower().isin(["true", "1", "yes"])
    print(f"Loaded {len(listings)} listings from {LISTINGS_FILE}")

    client = Products(marketplace=MARKETPLACE, credentials=creds)

    records = []
    for i, row in listings.iterrows():
        asin = row["ASIN"]
        print(f"[{i+1}/{len(listings)}] Fetching {asin}...")
        data = fetch_offer_data(client, asin, row["Your_Price"])
        data.update({
            "ASIN": asin,
            "Title": row["Title"],
            "Brand": row.get("Brand", "Unknown"),
            "Category": row.get("Category", "Unknown"),
            "Your_Price": row["Your_Price"],
            "Listing_Type": row["Listing_Type"],
            "Is_Top_50": bool(row["Is_Top_50"]),
        })
        records.append(data)
        if i < len(listings) - 1:
            time.sleep(DELAY_BETWEEN_CALLS)

    df = pd.DataFrame(records)
    top50_df = df[df["Is_Top_50"] == True].copy()

    top50_metrics = compute_metrics(top50_df)
    all_metrics = compute_metrics(df)

    top50_brand_bd = compute_group_breakdown(top50_df, "Brand")
    top50_cat_bd = compute_group_breakdown(top50_df, "Category")
    all_brand_bd = compute_group_breakdown(df, "Brand")
    all_cat_bd = compute_group_breakdown(df, "Category")

    top50_drilldowns = build_drilldowns(top50_df)
    all_drilldowns = build_drilldowns(df)

    top50_priority = top50_df.copy()
    top50_priority["sort_key"] = top50_priority["is_buy_box_winner"] != True
    top50_priority_rows = top50_priority.sort_values("sort_key", ascending=False).fillna("").to_dict(orient="records")

    all_lost = df[(df["status"] == "ok") & (df["is_buy_box_winner"] == False)].copy()
    all_priority_rows = all_lost.sort_values("gap_to_buybox", ascending=False).fillna("").to_dict(orient="records")

    os.makedirs(os.path.dirname(DETAIL_FILE), exist_ok=True)
    df.to_csv(DETAIL_FILE, index=False)

    os.makedirs(os.path.dirname(HISTORY_FILE), exist_ok=True)
    history_row = pd.DataFrame([{**all_metrics, "generated_at": datetime.now(timezone.utc).isoformat(),
                                  "top50_win_pct": top50_metrics["buy_box_win_pct"]}])
    if os.path.isfile(HISTORY_FILE):
        history_row.to_csv(HISTORY_FILE, mode="a", header=False, index=False)
    else:
        history_row.to_csv(HISTORY_FILE, index=False)

    html = render_dashboard(top50_metrics, top50_brand_bd, top50_cat_bd, top50_drilldowns, top50_priority_rows,
                             all_metrics, all_brand_bd, all_cat_bd, all_drilldowns, all_priority_rows)
    os.makedirs(os.path.dirname(DASHBOARD_FILE), exist_ok=True)
    with open(DASHBOARD_FILE, "w") as f:
        f.write(html)

    print("\nTop 50 metrics:", json.dumps(top50_metrics, indent=2, cls=NumpySafeEncoder))
    print("\nAll listings metrics:", json.dumps(all_metrics, indent=2, cls=NumpySafeEncoder))

    if top50_metrics["buy_box_win_pct"] < BUY_BOX_ALERT_THRESHOLD_PCT:
        msg = (
            f"TNM ALERT: Top 50 Buy Box win rate is {top50_metrics['buy_box_win_pct']}% "
            f"(below {BUY_BOX_ALERT_THRESHOLD_PCT}% threshold). Check the dashboard."
        )
        send_whatsapp_alert(msg)
    else:
        print(f"\nTop 50 win rate {top50_metrics['buy_box_win_pct']}% is above alert threshold - no alert sent.")


if __name__ == "__main__":
    main()
