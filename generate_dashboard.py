"""
TNM Buy Box Dashboard Generator
--------------------------------
Reads data/listings.csv (ASIN, Title, SKU, Your_Price, Listing_Type),
pulls live Buy Box / offer data from Amazon SP-API for each ASIN,
computes team-facing metrics, writes docs/index.html (a static dashboard
page served by GitHub Pages), appends a row to data/history.csv for
trend tracking, and sends a WhatsApp alert if Buy Box win rate < 20%.

Required environment variables (set as GitHub Actions secrets):
    SP_API_REFRESH_TOKEN
    SP_API_LWA_APP_ID
    SP_API_LWA_CLIENT_SECRET
    CALLMEBOT_PHONE        (WhatsApp number with country code, no +, e.g. 91900xxxxxxx)
    CALLMEBOT_APIKEY       (from callmebot.com WhatsApp API signup)
"""

import os
import sys
import csv
import time
import json
from datetime import datetime, timezone

import pandas as pd
import requests
from sp_api.api import Products
from sp_api.base import Marketplaces

# ---- Config -----------------------------------------------------------

LISTINGS_FILE = "data/listings.csv"
HISTORY_FILE = "data/history.csv"
DASHBOARD_FILE = "docs/index.html"
DETAIL_FILE = "docs/details.csv"

MARKETPLACE = Marketplaces.IN
ITEM_CONDITION = "New"
DELAY_BETWEEN_CALLS = 1.1

# Thresholds - adjust anytime, no other code changes needed
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


def fetch_offer_data(client, asin):
    """Call getItemOffers for one ASIN, return parsed dict or an error dict."""
    try:
        response = client.get_item_offers(asin, item_condition=ITEM_CONDITION)
        payload = response.payload
    except Exception as e:
        return {"status": f"error: {e}"}

    summary = payload.get("Summary", {})
    offers = payload.get("Offers", [])

    lowest_prices = summary.get("LowestPrices", [])
    lowest_price = lowest_prices[0].get("LandedPrice", {}).get("Amount") if lowest_prices else None

    buy_box_prices = summary.get("BuyBoxPrices", [])
    buy_box_price = buy_box_prices[0].get("LandedPrice", {}).get("Amount") if buy_box_prices else None

    num_offers = sum(o.get("OfferCount", 0) for o in summary.get("NumberOfOffers", []))

    my_offer = next((o for o in offers if o.get("MyOffer")), None)
    my_price = None
    if my_offer:
        my_price = my_offer.get("ListingPrice", {}).get("Amount")

    is_winner = bool(my_offer and my_offer.get("IsBuyBoxWinner"))

    competitor_prices = [
        o.get("ListingPrice", {}).get("Amount")
        for o in offers
        if not o.get("MyOffer") and o.get("ListingPrice", {}).get("Amount") is not None
    ]
    next_lowest_competitor = min(competitor_prices) if competitor_prices else None

    return {
        "status": "ok",
        "buy_box_price": buy_box_price,
        "lowest_price": lowest_price,
        "num_offers": num_offers,
        "my_price": my_price,
        "is_buy_box_winner": is_winner,
        "next_lowest_competitor": next_lowest_competitor,
    }


def classify_row(row):
    """
    Add derived fields used for the dashboard metrics.

    gap_to_buybox: for listings you do NOT win, how far (Rs) your price is
        above the current Buy Box price. Smaller = closer to winning.
    headroom: for listings you DO win, how much higher the next competitor
        is priced above you. Larger = more room to raise price safely.
    """
    if row["status"] != "ok":
        row["gap_to_buybox"] = None
        row["headroom"] = None
        return row

    my_price = row.get("my_price")
    buy_box_price = row.get("buy_box_price")
    next_lowest = row.get("next_lowest_competitor")

    if row["is_buy_box_winner"]:
        row["gap_to_buybox"] = 0
        if my_price is not None and next_lowest is not None:
            row["headroom"] = round(next_lowest - my_price, 2)
        else:
            row["headroom"] = None
    else:
        row["headroom"] = None
        if my_price is not None and buy_box_price is not None:
            row["gap_to_buybox"] = round(my_price - buy_box_price, 2)
        else:
            row["gap_to_buybox"] = None

    return row


def compute_metrics(results_df):
    total = len(results_df)
    ok_df = results_df[results_df["status"] == "ok"]
    errors = total - len(ok_df)

    won = ok_df[ok_df["is_buy_box_winner"] == True]
    lost = ok_df[ok_df["is_buy_box_winner"] == False]

    won_count = len(won)
    lost_count = len(lost)
    win_pct = round(100 * won_count / len(ok_df), 1) if len(ok_df) else 0.0

    close_gap = lost[(lost["gap_to_buybox"].notna()) & (lost["gap_to_buybox"] <= CLOSE_GAP_RS)]
    medium_gap = lost[
        (lost["gap_to_buybox"].notna())
        & (lost["gap_to_buybox"] > CLOSE_GAP_RS)
        & (lost["gap_to_buybox"] <= MEDIUM_GAP_RS)
    ]
    far_gap = lost[(lost["gap_to_buybox"].notna()) & (lost["gap_to_buybox"] > MEDIUM_GAP_RS)]

    has_headroom = won[(won["headroom"].notna()) & (won["headroom"] > HEADROOM_RS)]

    single_offer = ok_df[ok_df["num_offers"] <= 1]

    metrics = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "total_tracked": total,
        "errors": errors,
        "buy_box_won": won_count,
        "buy_box_lost": lost_count,
        "buy_box_win_pct": win_pct,
        "close_to_winning_under_50": len(close_gap),
        "close_to_winning_50_to_100": len(medium_gap),
        "far_from_winning_over_100": len(far_gap),
        "won_with_headroom_over_100": len(has_headroom),
        "single_offer_listings": len(single_offer),
    }
    return metrics


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


def render_dashboard(metrics, results_df, listing_type_breakdown):
    generated_at_ist = datetime.now(timezone.utc).astimezone().strftime("%Y-%m-%d %H:%M %Z")

    def card(label, value, sub=""):
        return f"""
        <div class="card">
            <div class="card-value">{value}</div>
            <div class="card-label">{label}</div>
            <div class="card-sub">{sub}</div>
        </div>"""

    cards_html = "".join([
        card("Total Listings Tracked", metrics["total_tracked"]),
        card("Buy Box Win Rate", f'{metrics["buy_box_win_pct"]}%',
             f'{metrics["buy_box_won"]} won / {metrics["buy_box_lost"]} lost'),
        card("Close to Winning (&lt;Rs 50)", metrics["close_to_winning_under_50"],
             "gap to Buy Box under Rs 50"),
        card("Close to Winning (Rs 50-100)", metrics["close_to_winning_50_to_100"],
             "gap to Buy Box Rs 50-100"),
        card("Far from Winning (&gt;Rs 100)", metrics["far_from_winning_over_100"],
             "gap to Buy Box over Rs 100"),
        card("Room to Raise Price", metrics["won_with_headroom_over_100"],
             "winning by more than Rs 100"),
        card("Single-Offer Listings", metrics["single_offer_listings"],
             "no real competition"),
        card("Fetch Errors", metrics["errors"], "ASINs that failed to fetch"),
    ])

    type_rows = "".join(
        f"<tr><td>{t}</td><td>{c}</td></tr>" for t, c in listing_type_breakdown.items()
    )

    # Worst-gap table: top 20 listings furthest from winning, for the team to act on
    lost_df = results_df[
        (results_df["status"] == "ok") & (results_df["is_buy_box_winner"] == False)
    ].copy()
    lost_df = lost_df.sort_values("gap_to_buybox", ascending=False).head(20)

    table_rows = ""
    for _, r in lost_df.iterrows():
        table_rows += f"""<tr>
            <td>{r['Title'][:60]}</td>
            <td>{r['ASIN']}</td>
            <td>{r['Your_Price']}</td>
            <td>{r['buy_box_price']}</td>
            <td>{r['gap_to_buybox']}</td>
        </tr>"""

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<title>TNM Buy Box Dashboard</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<style>
  body {{ font-family: Arial, Helvetica, sans-serif; background: #f4f6f8; margin: 0; padding: 24px; color: #222; }}
  h1 {{ margin-bottom: 4px; }}
  .updated {{ color: #666; margin-bottom: 24px; font-size: 14px; }}
  .cards {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(200px, 1fr)); gap: 16px; margin-bottom: 32px; }}
  .card {{ background: white; border-radius: 8px; padding: 18px; box-shadow: 0 1px 3px rgba(0,0,0,0.1); text-align: center; }}
  .card-value {{ font-size: 28px; font-weight: bold; color: #2F5496; }}
  .card-label {{ font-size: 13px; color: #444; margin-top: 4px; }}
  .card-sub {{ font-size: 11px; color: #888; margin-top: 2px; }}
  table {{ width: 100%; border-collapse: collapse; background: white; border-radius: 8px; overflow: hidden; box-shadow: 0 1px 3px rgba(0,0,0,0.1); margin-bottom: 32px;}}
  th, td {{ padding: 10px 14px; text-align: left; border-bottom: 1px solid #eee; font-size: 13px; }}
  th {{ background: #2F5496; color: white; }}
  .section-title {{ margin-top: 32px; margin-bottom: 12px; }}
</style>
</head>
<body>
  <h1>TNM Buy Box Dashboard</h1>
  <div class="updated">Last updated: {generated_at_ist}</div>

  <div class="cards">
    {cards_html}
  </div>

  <h2 class="section-title">Listings by Shipping/Delivery Type</h2>
  <table>
    <tr><th>Listing Type</th><th>Count</th></tr>
    {type_rows}
  </table>

  <h2 class="section-title">Top 20 Listings Furthest From Winning the Buy Box</h2>
  <table>
    <tr><th>Title</th><th>ASIN</th><th>Your Price</th><th>Buy Box Price</th><th>Gap (Rs)</th></tr>
    {table_rows}
  </table>

  <p style="color:#999; font-size:12px;">
    Auto-generated twice daily. "Gap to Buy Box" = Your Price minus current Buy Box Price
    (smaller is closer to winning). "Room to Raise Price" = listings you're winning where
    the next competitor is priced more than Rs {HEADROOM_RS} above you.
  </p>
</body>
</html>"""

    return html


def main():
    creds = load_credentials()

    listings = pd.read_csv(LISTINGS_FILE, dtype={"ASIN": str})
    print(f"Loaded {len(listings)} listings from {LISTINGS_FILE}")

    client = Products(marketplace=MARKETPLACE, credentials=creds)

    records = []
    for i, row in listings.iterrows():
        asin = row["ASIN"]
        print(f"[{i+1}/{len(listings)}] Fetching {asin}...")
        data = fetch_offer_data(client, asin)
        data["ASIN"] = asin
        data["Title"] = row["Title"]
        data["Your_Price"] = row["Your_Price"]
        data["Listing_Type"] = row["Listing_Type"]
        records.append(classify_row(data))
        if i < len(listings) - 1:
            time.sleep(DELAY_BETWEEN_CALLS)

    results_df = pd.DataFrame(records)

    metrics = compute_metrics(results_df)
    listing_type_breakdown = listings["Listing_Type"].value_counts().to_dict()

    # Write detail CSV for anyone who wants the raw per-ASIN data
    os.makedirs(os.path.dirname(DETAIL_FILE), exist_ok=True)
    results_df.to_csv(DETAIL_FILE, index=False)

    # Append to history for trend tracking over time
    os.makedirs(os.path.dirname(HISTORY_FILE), exist_ok=True)
    history_row = pd.DataFrame([metrics])
    if os.path.isfile(HISTORY_FILE):
        history_row.to_csv(HISTORY_FILE, mode="a", header=False, index=False)
    else:
        history_row.to_csv(HISTORY_FILE, index=False)

    # Render and write the dashboard HTML
    html = render_dashboard(metrics, results_df, listing_type_breakdown)
    os.makedirs(os.path.dirname(DASHBOARD_FILE), exist_ok=True)
    with open(DASHBOARD_FILE, "w") as f:
        f.write(html)

    print("\nMetrics summary:")
    print(json.dumps(metrics, indent=2))

    if metrics["buy_box_win_pct"] < BUY_BOX_ALERT_THRESHOLD_PCT:
        msg = (
            f"TNM ALERT: Buy Box win rate is {metrics['buy_box_win_pct']}% "
            f"(below {BUY_BOX_ALERT_THRESHOLD_PCT}% threshold). "
            f"{metrics['buy_box_lost']} of {metrics['total_tracked']} listings "
            f"are not winning the Buy Box. Check the dashboard."
        )
        send_whatsapp_alert(msg)
    else:
        print(f"Buy Box win rate {metrics['buy_box_win_pct']}% is above alert threshold - no alert sent.")


if __name__ == "__main__":
    main()
