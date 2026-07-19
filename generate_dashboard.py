"""
TNM Buy Box Dashboard Generator (v4) - World-class edition
-------------------------------------------------------------
Pulls your ENTIRE active listing catalog directly from Amazon (Reports API,
GET_MERCHANT_LISTINGS_DATA), filters to quantity > 0, classifies each listing
by Brand / Category / Listing Type, fetches live Buy Box data per unique ASIN,
and renders a multi-tab dashboard:

    Overview | Fitment/Installation | Only Delivery | Latched On | Top 50 | Trends

Each product-type tab shows: summary cards, brand split, category split, and
a full per-product detail table. The Trends tab shows day-on-day listing
counts and Buy Box win rate using accumulated history.

Listing type classification:
    - Fitment/Installation: detected from title/shipping-group text
    - Only Delivery vs Latched On: determined by whether any OTHER seller has
      an offer on the ASIN (num_offers <= 1 = Only Delivery, we're alone;
      num_offers > 1 = Latched On, someone else is also listed on this ASIN)

Required environment variables (GitHub Actions secrets):
    SP_API_REFRESH_TOKEN, SP_API_LWA_APP_ID, SP_API_LWA_CLIENT_SECRET
    CALLMEBOT_PHONE, CALLMEBOT_APIKEY (optional, for WhatsApp alerts)
"""

import os
import sys
import re
import time
import json
from datetime import datetime, timezone

import pandas as pd
import numpy as np
import requests
from sp_api.api import Products, Reports
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


# ---- Config -----------------------------------------------------------

TOP50_FILE = "data/top50_asins.csv"  # optional manual list of high-traffic ASINs to flag; refresh periodically
HISTORY_FILE = "data/history.csv"
DASHBOARD_FILE = "docs/index.html"
DETAIL_FILE = "docs/details.csv"
RAW_LISTINGS_CACHE = "data/listings_raw_cache.csv"  # saved each run for auditing/debugging

MARKETPLACE = Marketplaces.IN
ITEM_CONDITION = "New"
DELAY_BETWEEN_CALLS = 3
MAX_RETRIES = 4

BUY_BOX_ALERT_THRESHOLD_PCT = 20.0
CLOSE_GAP_RS = 50
MEDIUM_GAP_RS = 100
HEADROOM_RS = 100

KNOWN_BRANDS = {
    "CEAT": "CEAT", "JK": "JK Tyre", "GOODYEAR": "Goodyear", "APOLLO": "Apollo",
    "KELLY": "Kelly", "CONTINENTAL": "Continental", "YOKOHAMA": "Yokohama", "MRF": "MRF",
    "BOSCH": "Bosch",
}
MODEL_LINE_2W = ["Secura Zoom", "Zoom Cruz", "Zoom Rad", "Zoom Plus", "Zoom XL", "Zoom X3",
                 "Zoom X1", "Zoom", "Gripp XL", "Gripp X5", "Gripp", "Alpha H1", "actiGRIP"]
MODEL_LINE_4W = ["Milaze", "SecuraDrive", "Secura Drive", "SportDrive", "Sport Drive",
                  "CrossDrive", "Cross Drive", "Amazer", "Alnac", "Apterra", "UX Royale",
                  "UX Touring", "Ultima Neo", "Ultima Hi", "Ultima LXT", "Ultima Sports",
                  "Ultima", "Ranger H/T", "Ranger A/T", "Ranger X-A/T", "Ranger BRT", "Ranger",
                  "Brute", "Assurance TripleMax", "Assurance Duraplus", "Assurance MaxGuard",
                  "Assurance", "EfficientGrip", "Wrangler", "Geolander", "Blue Earth", "S.Drive",
                  "Earth-1", "UltraContact", "ComfortContact", "VFM", "F1 Steel", "DP-M1",
                  "Ducaro", "Taximax", "EAG F1", "Sport Rad"]

# Known ASIN-level overrides for edge cases where title text alone is ambiguous
ASIN_BRAND_OVERRIDES = {"B09442VGVR": "CEAT"}


# ---- Step 1: Pull live listings from Amazon ----------------------------

def fetch_active_listings_report(reports_client):
    print("Requesting active listings report from Amazon...")
    create_res = reports_client.create_report(
        reportType="GET_MERCHANT_LISTINGS_DATA",
        marketplaceIds=[MARKETPLACE.marketplace_id],
    )
    report_id = create_res.payload["reportId"]
    print(f"Report requested, ID: {report_id}")

    status = None
    for attempt in range(30):
        time.sleep(20)
        status_res = reports_client.get_report(report_id)
        status = status_res.payload["processingStatus"]
        print(f"  [{attempt+1}] Status: {status}")
        if status in ("DONE", "FATAL", "CANCELLED"):
            break

    if status != "DONE":
        raise Exception(f"Report did not complete successfully: {status}")

    document_id = status_res.payload["reportDocumentId"]
    reports_client.get_report_document(document_id, download=True, file="active_listings_raw.tsv")
    raw = pd.read_csv("active_listings_raw.tsv", sep="\t", encoding="utf-8-sig", dtype=str)
    return raw


def get_brand(title, sku):
    if pd.isna(sku):
        sku = ""
    if str(sku).startswith("CE"):
        return "CEAT"
    if pd.isna(title):
        return "Unknown"
    tu = title.upper()
    for kw, display in KNOWN_BRANDS.items():
        if re.search(rf'\b{kw}\b', tu):
            return display
    m = re.match(r'^\s*(?:With Doorstep Fitment\s*-\s*)?([A-Za-z]+)', str(title))
    return m.group(1).title() if m else "Unknown"


def classify_category(title):
    if pd.isna(title):
        return "Other"
    t = str(title)
    if re.search(r'\bbosch\b|\bbattery\b', t, re.I):
        return "Battery"
    if re.search(r'\bbike tyre\b|\bscooter tyre\b|\b2 wheeler\b', t, re.I):
        return "Tyre - 2W"
    for ml in MODEL_LINE_2W:
        if ml.lower() in t.lower():
            return "Tyre - 2W"
    for ml in MODEL_LINE_4W:
        if ml.lower() in t.lower():
            return "Tyre - 4W"
    if re.search(r'\bcar tyre\b|\b4 wheeler\b|\bsuv\b', t, re.I):
        return "Tyre - 4W"
    if re.search(r'\d{2,3}/\d{2,3}\s*-\s*\d{1,2}\b', t):
        return "Tyre - 2W"
    if re.search(r'\d{2,3}/\d{2,3}\s*%?\s*R\s*\d{2}', t, re.I) or re.search(r'\bTL\b|\bTubeless\b|\bTube-Type\b|\bTT\b', t, re.I):
        return "Tyre - 4W"
    return "Other"


def is_fitment_from_title(title, shipping_group):
    t = str(title).lower()
    g = str(shipping_group).lower() if pd.notna(shipping_group) else ""
    return bool(re.search(r'fitment|installation', t) or "fitment" in g)


def process_raw_listings(raw):
    raw = raw.copy()
    raw["quantity"] = pd.to_numeric(raw["quantity"], errors="coerce").fillna(0)
    raw = raw[raw["quantity"] > 0].copy()

    raw["Brand"] = raw.apply(lambda r: get_brand(r["item-name"], r.get("seller-sku")), axis=1)
    for asin, brand in ASIN_BRAND_OVERRIDES.items():
        raw.loc[raw["asin1"] == asin, "Brand"] = brand

    raw["Category"] = raw["item-name"].apply(classify_category)
    raw["is_fitment"] = raw.apply(lambda r: is_fitment_from_title(r["item-name"], r.get("merchant-shipping-group")), axis=1)

    raw["Product_URL"] = "https://www.amazon.in/dp/" + raw["asin1"] + "?th=1"
    raw = raw.rename(columns={"asin1": "ASIN", "seller-sku": "SKU", "item-name": "Title", "price": "Your_Price"})
    raw["Your_Price"] = pd.to_numeric(raw["Your_Price"], errors="coerce")

    return raw[["ASIN", "Title", "Brand", "Category", "SKU", "Your_Price", "is_fitment", "quantity", "Product_URL"]]


# ---- Step 2: Fetch live Buy Box data ------------------------------------

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


# ---- Step 3: Metrics and breakdowns -------------------------------------

def compute_metrics(df):
    total = len(df)
    ok_df = df[df["status"] == "ok"]
    errors = total - len(ok_df)
    won = ok_df[ok_df["is_buy_box_winner"] == True]
    lost = ok_df[ok_df["is_buy_box_winner"] == False]
    no_bb = ok_df[ok_df["is_buy_box_winner"].isna()]
    determined = len(won) + len(lost)
    win_pct = round(100 * len(won) / determined, 1) if determined else 0.0

    close_50 = lost[(lost["gap_to_buybox"].notna()) & (lost["gap_to_buybox"] <= CLOSE_GAP_RS)]
    close_100 = lost[(lost["gap_to_buybox"].notna()) & (lost["gap_to_buybox"] > CLOSE_GAP_RS) & (lost["gap_to_buybox"] <= MEDIUM_GAP_RS)]
    far_100 = lost[(lost["gap_to_buybox"].notna()) & (lost["gap_to_buybox"] > MEDIUM_GAP_RS)]
    headroom = won[(won["headroom"].notna()) & (won["headroom"] > HEADROOM_RS)]
    single_offer = ok_df[ok_df["num_offers"] <= 1]

    return {
        "total_tracked": total, "errors": errors, "no_buybox_data": len(no_bb),
        "buy_box_won": len(won), "buy_box_lost": len(lost), "buy_box_win_pct": win_pct,
        "close_to_winning_under_50": len(close_50), "close_to_winning_50_to_100": len(close_100),
        "far_from_winning_over_100": len(far_100), "won_with_headroom_over_100": len(headroom),
        "single_offer_listings": len(single_offer),
    }


def compute_group_breakdown(df, group_col):
    ok_df = df[df["status"] == "ok"]
    rows = []
    for name, g in ok_df.groupby(group_col):
        won = (g["is_buy_box_winner"] == True).sum()
        determined = ((g["is_buy_box_winner"] == True) | (g["is_buy_box_winner"] == False)).sum()
        win_pct = round(100 * won / determined, 1) if determined else 0.0
        rows.append({"name": name, "total": len(g), "won": int(won), "win_pct": win_pct})
    rows.sort(key=lambda r: r["total"], reverse=True)
    return rows


def build_drilldowns(df):
    ok = df[df["status"] == "ok"]
    lost = ok[ok["is_buy_box_winner"] == False]
    won = ok[ok["is_buy_box_winner"] == True]

    def rows(sub_df):
        cols = ["ASIN", "Title", "Brand", "Category", "Your_Price", "buy_box_price",
                "gap_to_buybox", "num_offers", "Product_URL"]
        return sub_df[cols].fillna("").to_dict(orient="records")

    return {
        "total": rows(df), "won": rows(won), "lost": rows(lost),
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
    try:
        r = requests.get("https://api.callmebot.com/whatsapp.php",
                          params={"phone": phone, "text": message, "apikey": apikey}, timeout=15)
        print("WhatsApp alert response:", r.status_code, r.text[:200])
    except Exception as e:
        print("Failed to send WhatsApp alert:", e)


# ---- Rendering -----------------------------------------------------------

def th():
    return "text-align:left; padding:9px 12px; background:#2F5496; color:white; font-size:13px;"

def td():
    return "padding:9px 12px; border-bottom:1px solid #eee; font-size:13px;"

CARD_CSS = """
  body { font-family: Arial, Helvetica, sans-serif; background: #f4f6f8; margin: 0; padding: 24px; color: #222; }
  h1 { margin-bottom: 4px; } h2 { margin-top: 28px; margin-bottom: 10px; }
  .updated { color: #666; margin-bottom: 16px; font-size: 14px; }
  .tabs { display: flex; gap: 4px; margin-bottom: 22px; border-bottom: 2px solid #ddd; flex-wrap: wrap; }
  .tab-button { padding: 9px 16px; border: none; background: none; font-size: 13px; cursor: pointer; color: #666; border-bottom: 3px solid transparent; margin-bottom: -2px; }
  .tab-button.active { color: #2F5496; border-bottom-color: #2F5496; font-weight: bold; }
  .tab-content { display: none; } .tab-content.active { display: block; }
  .cards { display: grid; grid-template-columns: repeat(auto-fit, minmax(150px, 1fr)); gap: 14px; margin-bottom: 8px; }
  .card { background: white; border-radius: 8px; padding: 14px; box-shadow: 0 1px 3px rgba(0,0,0,0.1); text-align: center; cursor: pointer; }
  .card:hover { box-shadow: 0 2px 8px rgba(0,0,0,0.18); }
  .card-value { font-size: 22px; font-weight: bold; color: #2F5496; }
  .card-label { font-size: 11px; color: #444; margin-top: 4px; }
  .card-sub { font-size: 9px; color: #888; margin-top: 2px; }
  table { width: 100%; border-collapse: collapse; background: white; border-radius: 8px; overflow: hidden; box-shadow: 0 1px 3px rgba(0,0,0,0.1); margin-bottom: 20px;}
  .views { display: grid; grid-template-columns: 1fr 1fr; gap: 20px; }
  @media (max-width: 700px) { .views { grid-template-columns: 1fr; } }
  .drilldown-panel { display: none; background: white; border-radius: 8px; padding: 14px; box-shadow: 0 1px 3px rgba(0,0,0,0.15); margin-bottom: 20px; }
  .drilldown-header { margin: 0 0 10px 0; display: flex; justify-content: space-between; font-weight: bold; }
  .drilldown-close { cursor: pointer; color: #2F5496; font-size: 13px; font-weight: normal; }
  a { color: #2F5496; text-decoration: none; } a:hover { text-decoration: underline; }
"""


def render_product_tab(tab_id, df, priority_title, flag_if_under_100=False):
    metrics = compute_metrics(df)
    brand_bd = compute_group_breakdown(df, "Brand")
    cat_bd = compute_group_breakdown(df, "Category")
    drilldowns = build_drilldowns(df)

    def card(label, value, sub, key):
        return f"""<div class="card" onclick="showDrilldown('{tab_id}', '{key}', '{label}')">
            <div class="card-value">{value}</div><div class="card-label">{label}</div>
            <div class="card-sub">{sub}</div></div>"""

    win_pct_color = ""
    if flag_if_under_100 and metrics["buy_box_win_pct"] < 100.0:
        win_pct_color = "color:#c0392b;"

    cards_html = "".join([
        card("Total tracked", metrics["total_tracked"], "", "total"),
        f'<div class="card" onclick="showDrilldown(\'{tab_id}\', \'won\', \'Buy box win rate\')">'
        f'<div class="card-value" style="{win_pct_color}">{metrics["buy_box_win_pct"]}%</div>'
        f'<div class="card-label">Buy box win rate</div>'
        f'<div class="card-sub">{metrics["buy_box_won"]} won / {metrics["buy_box_lost"]} lost</div></div>',
        card("Close (under Rs 50)", metrics["close_to_winning_under_50"], "gap under Rs 50", "close_50"),
        card("Close (Rs 50-100)", metrics["close_to_winning_50_to_100"], "gap Rs 50-100", "close_100"),
        card("Far (over Rs 100)", metrics["far_from_winning_over_100"], "gap over Rs 100", "far_100"),
        card("Room to raise price", metrics["won_with_headroom_over_100"], "winning by over Rs 100", "headroom"),
        card("Single-offer", metrics["single_offer_listings"], "no real competition", "single_offer"),
        card("No buy box data", metrics["no_buybox_data"], "Amazon returned none", "no_buybox_data" if "no_buybox_data" in drilldowns else "errors"),
        card("Fetch errors", metrics["errors"], "failed to fetch", "errors"),
    ])

    brand_rows = "".join(f"<tr><td style='{td()}'>{b['name']}</td><td style='{td()}'>{b['total']}</td><td style='{td()}'>{b['won']}</td><td style='{td()}'>{b['win_pct']}%</td></tr>" for b in brand_bd)
    cat_rows = "".join(f"<tr><td style='{td()}'>{c['name']}</td><td style='{td()}'>{c['total']}</td><td style='{td()}'>{c['won']}</td><td style='{td()}'>{c['win_pct']}%</td></tr>" for c in cat_bd)

    ok_df = df[df["status"] == "ok"].copy()
    ok_df["sort_key"] = ok_df["is_buy_box_winner"] != True
    product_rows_df = ok_df.sort_values("sort_key", ascending=False)
    product_rows = ""
    for _, r in product_rows_df.iterrows():
        won = r["is_buy_box_winner"] == True
        row_style = "" if won else "background:#fdeaea;"
        status_label = "Won" if won else "Lost"
        url = r.get("Product_URL", "")
        asin_cell = f'<a href="{url}" target="_blank" rel="noopener">{r["ASIN"]}</a>' if url else r["ASIN"]
        product_rows += (f"<tr style='{row_style}'><td style='{td()}'>{str(r['Title'])[:55]}</td>"
                          f"<td style='{td()}'>{asin_cell}</td><td style='{td()}'>{r['Brand']}</td>"
                          f"<td style='{td()}'>{r['Category']}</td><td style='{td()}'>{r['Your_Price']}</td>"
                          f"<td style='{td()}'>{r['buy_box_price']}</td><td style='{td()}'>{status_label}</td></tr>")

    drilldowns_json = json.dumps(drilldowns, cls=NumpySafeEncoder)

    return f"""
  <div class="cards">{cards_html}</div>
  <p class="hint" style="font-size:12px; color:#999; margin-bottom:20px;">Click any number to see the underlying ASINs.</p>

  <div id="drilldown-panel-{tab_id}" class="drilldown-panel">
    <div class="drilldown-header"><span id="drilldown-title-{tab_id}"></span>
      <span class="drilldown-close" onclick="closeDrilldown('{tab_id}')">Close</span></div>
    <div id="drilldown-body-{tab_id}"></div>
  </div>

  <div class="views">
    <div><h2>By brand</h2><table><tr><th style='{th()}'>Brand</th><th style='{th()}'>Total</th><th style='{th()}'>Won</th><th style='{th()}'>Win %</th></tr>{brand_rows}</table></div>
    <div><h2>By category</h2><table><tr><th style='{th()}'>Category</th><th style='{th()}'>Total</th><th style='{th()}'>Won</th><th style='{th()}'>Win %</th></tr>{cat_rows}</table></div>
  </div>

  <h2>{priority_title}</h2>
  <table><tr><th style='{th()}'>Title</th><th style='{th()}'>ASIN</th><th style='{th()}'>Brand</th>
    <th style='{th()}'>Category</th><th style='{th()}'>Your price</th><th style='{th()}'>Buy box price</th><th style='{th()}'>Status</th></tr>
    {product_rows}
  </table>

  <script>
    window.drilldownData = window.drilldownData || {{}};
    window.drilldownData['{tab_id}'] = {drilldowns_json};
  </script>
"""


def render_top50_tab(top50_df):
    """Special version of the Top 50 tab that also breaks down by listing type."""
    metrics = compute_metrics(top50_df)
    brand_bd = compute_group_breakdown(top50_df, "Brand")
    cat_bd = compute_group_breakdown(top50_df, "Category")
    type_bd = compute_group_breakdown(top50_df, "Listing_Type")
    drilldowns = build_drilldowns(top50_df)

    def card(label, value, sub, key):
        return f"""<div class="card" onclick="showDrilldown('top50', '{key}', '{label}')">
            <div class="card-value">{value}</div><div class="card-label">{label}</div>
            <div class="card-sub">{sub}</div></div>"""

    cards_html = "".join([
        card("Total tracked", metrics["total_tracked"], "", "total"),
        card("Buy box win rate", f'{metrics["buy_box_win_pct"]}%', f'{metrics["buy_box_won"]} won / {metrics["buy_box_lost"]} lost', "won"),
        card("Far (over Rs 100)", metrics["far_from_winning_over_100"], "gap over Rs 100", "far_100"),
        card("Room to raise price", metrics["won_with_headroom_over_100"], "winning by over Rs 100", "headroom"),
    ])

    type_rows = "".join(f"<tr><td style='{td()}'>{t['name']}</td><td style='{td()}'>{t['total']}</td><td style='{td()}'>{t['won']}</td><td style='{td()}'>{t['win_pct']}%</td></tr>" for t in type_bd)
    brand_rows = "".join(f"<tr><td style='{td()}'>{b['name']}</td><td style='{td()}'>{b['total']}</td><td style='{td()}'>{b['won']}</td><td style='{td()}'>{b['win_pct']}%</td></tr>" for b in brand_bd)
    cat_rows = "".join(f"<tr><td style='{td()}'>{c['name']}</td><td style='{td()}'>{c['total']}</td><td style='{td()}'>{c['won']}</td><td style='{td()}'>{c['win_pct']}%</td></tr>" for c in cat_bd)

    ok_df = top50_df[top50_df["status"] == "ok"].copy()
    ok_df["sort_key"] = ok_df["is_buy_box_winner"] != True
    product_rows_df = ok_df.sort_values("sort_key", ascending=False)
    product_rows = ""
    for _, r in product_rows_df.iterrows():
        won = r["is_buy_box_winner"] == True
        row_style = "" if won else "background:#fdeaea;"
        status_label = "Won" if won else "Lost"
        url = r.get("Product_URL", "")
        asin_cell = f'<a href="{url}" target="_blank" rel="noopener">{r["ASIN"]}</a>' if url else r["ASIN"]
        product_rows += (f"<tr style='{row_style}'><td style='{td()}'>{str(r['Title'])[:55]}</td>"
                          f"<td style='{td()}'>{asin_cell}</td><td style='{td()}'>{r['Brand']}</td>"
                          f"<td style='{td()}'>{r['Listing_Type']}</td><td style='{td()}'>{r['Your_Price']}</td>"
                          f"<td style='{td()}'>{r['buy_box_price']}</td><td style='{td()}'>{status_label}</td></tr>")

    drilldowns_json = json.dumps(drilldowns, cls=NumpySafeEncoder)

    return f"""
  <div class="cards">{cards_html}</div>
  <p class="hint" style="font-size:12px; color:#999; margin-bottom:20px;">Click any number to see the underlying ASINs.</p>

  <div id="drilldown-panel-top50" class="drilldown-panel">
    <div class="drilldown-header"><span id="drilldown-title-top50"></span>
      <span class="drilldown-close" onclick="closeDrilldown('top50')">Close</span></div>
    <div id="drilldown-body-top50"></div>
  </div>

  <h2>By listing type</h2>
  <table><tr><th style='{th()}'>Type</th><th style='{th()}'>Total</th><th style='{th()}'>Won</th><th style='{th()}'>Win %</th></tr>{type_rows}</table>

  <div class="views">
    <div><h2>By brand</h2><table><tr><th style='{th()}'>Brand</th><th style='{th()}'>Total</th><th style='{th()}'>Won</th><th style='{th()}'>Win %</th></tr>{brand_rows}</table></div>
    <div><h2>By category</h2><table><tr><th style='{th()}'>Category</th><th style='{th()}'>Total</th><th style='{th()}'>Won</th><th style='{th()}'>Win %</th></tr>{cat_rows}</table></div>
  </div>

  <h2>Top 50 priority listings</h2>
  <table><tr><th style='{th()}'>Title</th><th style='{th()}'>ASIN</th><th style='{th()}'>Brand</th>
    <th style='{th()}'>Type</th><th style='{th()}'>Your price</th><th style='{th()}'>Buy box price</th><th style='{th()}'>Status</th></tr>
    {product_rows}
  </table>

  <script>
    window.drilldownData = window.drilldownData || {{}};
    window.drilldownData['top50'] = {drilldowns_json};
  </script>
"""


def render_overview_tab(all_df, fitment_df, od_df, latched_df, top50_df):
    fitment_m = compute_metrics(fitment_df)
    od_m = compute_metrics(od_df)
    latched_m = compute_metrics(latched_df)
    top50_m = compute_metrics(top50_df)

    def summary_card(label, m, flag_if_under_100=False):
        color = "color:#c0392b;" if (flag_if_under_100 and m["buy_box_win_pct"] < 100.0) else "color:#2F5496;"
        return f"""<div class="card" style="cursor:default;">
            <div class="card-value" style="{color}">{m['buy_box_win_pct']}%</div>
            <div class="card-label">{label}</div>
            <div class="card-sub">{m['total_tracked']} tracked - {m['buy_box_won']} won / {m['buy_box_lost']} lost</div></div>"""

    cards_html = "".join([
        summary_card("Fitment/Installation win rate", fitment_m, flag_if_under_100=True),
        summary_card("Only Delivery win rate", od_m, flag_if_under_100=True),
        summary_card("Latched On win rate", latched_m),
        summary_card("Top 50 win rate", top50_m),
    ])

    total_listings = len(all_df)
    type_counts = all_df["Listing_Type"].value_counts()
    type_rows = "".join(f"<tr><td style='{td()}'>{t}</td><td style='{td()}'>{c}</td></tr>" for t, c in type_counts.items())

    return f"""
  <h2>At a glance</h2>
  <div class="cards">{cards_html}</div>

  <h2>Catalog composition</h2>
  <table><tr><th style='{th()}'>Listing type</th><th style='{th()}'>Count</th></tr>{type_rows}</table>
  <p style="font-size:12px; color:#999;">Total live listings (quantity &gt; 0): {total_listings}</p>
"""


def render_trends_tab(history_df):
    if history_df is None or len(history_df) == 0:
        return "<p>No trend history yet - this populates after the first few scheduled runs.</p>"

    dates = history_df["date_label"].tolist()
    all_pct = history_df["all_win_pct"].tolist()
    fitment_pct = history_df.get("fitment_win_pct", pd.Series([None]*len(history_df))).tolist()
    od_pct = history_df.get("od_win_pct", pd.Series([None]*len(history_df))).tolist()
    latched_pct = history_df.get("latched_win_pct", pd.Series([None]*len(history_df))).tolist()
    top50_pct = history_df["top50_win_pct"].tolist()
    totals = history_df["total_listings"].tolist()

    table_rows = ""
    for i in range(len(history_df)):
        table_rows += (f"<tr><td style='{td()}'>{dates[i]}</td><td style='{td()}'>{totals[i]}</td>"
                        f"<td style='{td()}'>{all_pct[i]}%</td><td style='{td()}'>{top50_pct[i]}%</td></tr>")

    return f"""
  <h2>Day-on-day trend</h2>
  <div style="position:relative; width:100%; height:280px; margin-bottom:20px;">
    <canvas id="trendChart"></canvas>
  </div>
  <table>
    <tr><th style='{th()}'>Date</th><th style='{th()}'>Total listings</th><th style='{th()}'>Overall win %</th><th style='{th()}'>Top 50 win %</th></tr>
    {table_rows}
  </table>
  <script src="https://cdnjs.cloudflare.com/ajax/libs/Chart.js/4.4.1/chart.umd.js"></script>
  <script>
    new Chart(document.getElementById('trendChart'), {{
      type: 'line',
      data: {{
        labels: {json.dumps(dates)},
        datasets: [
          {{ label: 'All listings', data: {json.dumps(all_pct)}, borderColor: '#2a78d6', backgroundColor: 'rgba(42,120,214,0.08)', borderWidth: 2, pointRadius: 3, tension: 0.2, fill: true }},
          {{ label: 'Top 50', data: {json.dumps(top50_pct)}, borderColor: '#eb6834', borderWidth: 2, pointRadius: 3, tension: 0.2 }}
        ]
      }},
      options: {{ responsive: true, maintainAspectRatio: false,
        scales: {{ y: {{ min: 0, max: 100, ticks: {{ callback: v => v + '%' }} }} }} }}
    }});
  </script>
"""


def render_dashboard(all_df, fitment_df, od_df, latched_df, top50_df, history_df):
    generated_at = datetime.now(timezone.utc).astimezone().strftime("%Y-%m-%d %H:%M %Z")

    overview_content = render_overview_tab(all_df, fitment_df, od_df, latched_df, top50_df)
    fitment_content = render_product_tab("fitment", fitment_df, "Fitment/Installation listings", flag_if_under_100=True)
    od_content = render_product_tab("od", od_df, "Only Delivery listings", flag_if_under_100=True)
    latched_content = render_product_tab("latched", latched_df, "Latched On listings", flag_if_under_100=False)
    top50_content = render_top50_tab(top50_df)
    trends_content = render_trends_tab(history_df)

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8"><title>TNM Buy Box Dashboard</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<style>{CARD_CSS}</style>
</head>
<body>
  <h1>TNM Buy Box Dashboard</h1>
  <div class="updated">Last updated: {generated_at}</div>

  <div class="tabs">
    <button class="tab-button active" onclick="switchTab('overview')">Overview</button>
    <button class="tab-button" onclick="switchTab('fitment')">Fitment/Installation</button>
    <button class="tab-button" onclick="switchTab('od')">Only Delivery</button>
    <button class="tab-button" onclick="switchTab('latched')">Latched On</button>
    <button class="tab-button" onclick="switchTab('top50')">Top 50</button>
    <button class="tab-button" onclick="switchTab('trends')">Trends</button>
  </div>

  <div id="tab-overview" class="tab-content active">{overview_content}</div>
  <div id="tab-fitment" class="tab-content">{fitment_content}</div>
  <div id="tab-od" class="tab-content">{od_content}</div>
  <div id="tab-latched" class="tab-content">{latched_content}</div>
  <div id="tab-top50" class="tab-content">{top50_content}</div>
  <div id="tab-trends" class="tab-content">{trends_content}</div>

  <p style="color:#999; font-size:12px; margin-top:20px;">
    Auto-generated daily at 9 AM IST. Listing type: Fitment/Installation is exclusive to us
    (no other seller can offer it) so its win rate should ideally be 100% - flagged red if not,
    which usually means a hijacker or unauthorized reseller. Only Delivery listings are ones
    where we're currently the only seller with an offer - also expected near 100%. Latched On
    listings share the ASIN with other sellers, so real competition is expected there.
  </p>

<script>
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
  const rows = (window.drilldownData[tabId] || {{}})[key] || [];
  title.textContent = label + ' (' + rows.length + ')';
  if (rows.length === 0) {{
    body.innerHTML = '<p style="color:#888; font-size:13px;">No listings in this group.</p>';
  }} else {{
    let html = '<table><tr><th>Title</th><th>ASIN</th><th>Brand</th><th>Category</th><th>Your price</th><th>Buy box price</th><th>Gap</th></tr>';
    rows.forEach(r => {{
      const asinCell = r.Product_URL ? '<a href="' + r.Product_URL + '" target="_blank" rel="noopener">' + r.ASIN + '</a>' : r.ASIN;
      html += '<tr><td>' + String(r.Title || '').slice(0, 55) + '</td><td>' + asinCell +
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


# ---- Main ----------------------------------------------------------------

def main():
    creds = load_credentials()
    client = Products(marketplace=MARKETPLACE, credentials=creds)
    reports_client = Reports(marketplace=MARKETPLACE, credentials=creds)

    raw = fetch_active_listings_report(reports_client)
    raw.to_csv(RAW_LISTINGS_CACHE, index=False)
    listings = process_raw_listings(raw)
    print(f"Loaded {len(listings)} live listings (quantity > 0), {listings['ASIN'].nunique()} unique ASINs")

    # Optional Top 50 flag from a manually maintained file (Amazon doesn't expose
    # traffic/session data via a simple free API - refresh this file periodically
    # from a Seller Central traffic report if you want it to stay current)
    if os.path.isfile(TOP50_FILE):
        top50_asins = set(pd.read_csv(TOP50_FILE, dtype=str)["ASIN"])
    else:
        top50_asins = set()
        print(f"No {TOP50_FILE} found - Top 50 tab will be empty until you add one.")
    listings["Is_Top_50"] = listings["ASIN"].isin(top50_asins)

    # Fetch Buy Box data once per unique ASIN
    unique_asins = listings.drop_duplicates(subset="ASIN")[["ASIN", "Your_Price"]].reset_index(drop=True)
    asin_results = {}
    for i, row in unique_asins.iterrows():
        asin = row["ASIN"]
        print(f"[{i+1}/{len(unique_asins)}] Fetching {asin}...")
        asin_results[asin] = fetch_offer_data(client, asin, row["Your_Price"])
        if i < len(unique_asins) - 1:
            time.sleep(DELAY_BETWEEN_CALLS)

    records = []
    for _, row in listings.iterrows():
        data = dict(asin_results[row["ASIN"]])
        data.update({
            "ASIN": row["ASIN"], "SKU": row["SKU"], "Title": row["Title"], "Brand": row["Brand"],
            "Category": row["Category"], "Your_Price": row["Your_Price"], "is_fitment": row["is_fitment"],
            "Is_Top_50": bool(row["Is_Top_50"]), "Product_URL": row["Product_URL"],
        })
        # Listing type: Fitment from title/shipping-group; else Only Delivery vs
        # Latched On based on whether any other seller has an offer (num_offers)
        if data["is_fitment"]:
            data["Listing_Type"] = "Fitment/Installation"
        elif data.get("num_offers", 0) <= 1:
            data["Listing_Type"] = "Only Delivery"
        else:
            data["Listing_Type"] = "Latched On"
        records.append(data)

    df = pd.DataFrame(records)
    df = df.drop_duplicates(subset="ASIN", keep="first").copy()  # one row per ASIN for all downstream views

    fitment_df = df[df["Listing_Type"] == "Fitment/Installation"].copy()
    od_df = df[df["Listing_Type"] == "Only Delivery"].copy()
    latched_df = df[df["Listing_Type"] == "Latched On"].copy()
    top50_df = df[df["Is_Top_50"] == True].copy()

    os.makedirs(os.path.dirname(DETAIL_FILE), exist_ok=True)
    df.to_csv(DETAIL_FILE, index=False)

    # Append today's summary to history for the Trends tab
    os.makedirs(os.path.dirname(HISTORY_FILE), exist_ok=True)
    all_competitive = pd.concat([od_df, latched_df])
    history_row = pd.DataFrame([{
        "date_label": datetime.now(timezone.utc).strftime("%b %d"),
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "total_listings": len(df),
        "all_win_pct": compute_metrics(all_competitive)["buy_box_win_pct"],
        "fitment_win_pct": compute_metrics(fitment_df)["buy_box_win_pct"],
        "od_win_pct": compute_metrics(od_df)["buy_box_win_pct"],
        "latched_win_pct": compute_metrics(latched_df)["buy_box_win_pct"],
        "top50_win_pct": compute_metrics(top50_df)["buy_box_win_pct"],
    }])
    if os.path.isfile(HISTORY_FILE):
        try:
            existing = pd.read_csv(HISTORY_FILE)
            if "date_label" in existing.columns:
                history_row.to_csv(HISTORY_FILE, mode="a", header=False, index=False)
            else:
                history_row.to_csv(HISTORY_FILE, index=False)  # schema changed, start fresh
        except Exception:
            history_row.to_csv(HISTORY_FILE, index=False)
    else:
        history_row.to_csv(HISTORY_FILE, index=False)

    history_df = pd.read_csv(HISTORY_FILE) if os.path.isfile(HISTORY_FILE) else None

    html = render_dashboard(df, fitment_df, od_df, latched_df, top50_df, history_df)
    os.makedirs(os.path.dirname(DASHBOARD_FILE), exist_ok=True)
    with open(DASHBOARD_FILE, "w") as f:
        f.write(html)

    all_metrics = compute_metrics(all_competitive)
    top50_metrics = compute_metrics(top50_df)
    print("\nOverall competitive win rate:", all_metrics["buy_box_win_pct"], "%")
    print("Top 50 win rate:", top50_metrics["buy_box_win_pct"], "%")

    if top50_metrics["buy_box_win_pct"] < BUY_BOX_ALERT_THRESHOLD_PCT and len(top50_df) > 0:
        send_whatsapp_alert(
            f"TNM ALERT: Top 50 Buy Box win rate is {top50_metrics['buy_box_win_pct']}% "
            f"(below {BUY_BOX_ALERT_THRESHOLD_PCT}% threshold). Check the dashboard."
        )


if __name__ == "__main__":
    main()
