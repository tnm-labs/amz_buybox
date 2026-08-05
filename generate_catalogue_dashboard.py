"""
TNM Catalogue Coverage Dashboard Generator (live)
----------------------------------------------------
Reuses data/details.csv (produced daily by the Buy Box dashboard workflow -
real live Amazon data, no separate Amazon fetch needed here) and combines it
with:
    - data/Marketplace_Catalogue_Master.xlsx (your master SKU/brand/category
      mapping - update this file directly on GitHub whenever your catalogue
      changes; rarely needs to change)
    - data/fk_listing_latest.xls (the most recent Flipkart listing export -
      ANY team member can upload a fresh one directly on GitHub any time;
      this script and its workflow automatically pick up the latest version)

Outputs docs/catalogue.html - a second page alongside your main Buy Box
dashboard, both served from the same GitHub Pages site.

Excludes brands: Radar, Maxxis, Roadcruza, BFGoodrich, Comforser, Timsum,
Firestone (per business decision - edit REMOVE_BRANDS below to change).
"""

import os
import re
import sys
import pandas as pd


DETAILS_FILE = "data/details.csv"
MASTER_FILE = "data/Marketplace_Catalogue_Master.xlsx"
FK_FILE = "data/fk_listing_latest.xls"
OUTPUT_FILE = "docs/catalogue.html"

REMOVE_BRANDS = ["Radar", "Maxxis", "Roadcruza", "BFGoodrich", "Comforser", "Timsum", "Firestone"]

BRAND_NORMALIZE = {
    "PIRELLI": "Pirelli", "Pirelli": "Pirelli", "MRF": "MRF", "CEAT": "CEAT", "Ceat": "CEAT",
    "Apollo": "Apollo", "APOLLO": "Apollo", "Goodyear": "Goodyear", "Reise": "Reise",
    "BRIDGESTONE": "Bridgestone", "Bridgestone": "Bridgestone", "Vredestein": "Vredestein",
    "CONTINENTAL": "Continental", "Metzeler": "Metzeler", "JK": "JK Tyre", "Jk": "JK Tyre",
    "Amaron": "Amaron", "Exide": "Exide", "EXIDE": "Exide", "SF Battery": "SF Battery",
    "MICHELIN": "Michelin", "Kelly": "Kelly", "Radar": "Radar", "Maxxis": "Maxxis",
    "ROADCRUZA": "Roadcruza", "BFGOODRICH": "BFGoodrich", "Comforser": "Comforser",
    "Bosch": "Bosch", "Timsum": "Timsum", "Firestone": "Firestone",
}


def normalize_sku(s):
    s = str(s)
    s = re.sub(r'_DEL$', '', s)
    s = re.sub(r'-\d+$', '', s)
    return s


def strip_amazon_suffix(sku):
    s = str(sku)
    return re.sub(r'(ODLONSO[24]|ODLON|LONSO[24]|LON|OD|WF)$', '', s)


def classify_category(row):
    cat = row["Category"]
    title = str(row["name"]).lower()
    sku = str(row["TNM SKU Code"]).upper()
    if cat == "Migration_Car Tyre":
        return "Tyre - 4W"
    if cat == "Migration_Motorcycle Tyre":
        return "Tyre - 2W"
    is_battery_like = (cat == "Battery") or ("battery" in title) or sku.startswith(("2W", "4W"))
    if is_battery_like:
        if sku.startswith("2W"):
            return "Battery - 2W"
        if sku.startswith("4W"):
            return "Battery - 4W"
        if re.search(r'\bcar battery\b', title):
            return "Battery - 4W"
        if re.search(r'\b2w battery\b|\bbike battery\b|\bmotorcycle battery\b|\b2\s*wheeler battery\b', title):
            return "Battery - 2W"
        m = re.search(r'\((\d+(?:\.\d+)?)\s*ah\)', title)
        if m:
            return "Battery - 2W" if float(m.group(1)) <= 20 else "Battery - 4W"
        return "Battery - UNCLEAR"
    return "UNCLEAR"


def classify_fk_type_fallback(title, sku):
    title_l = str(title).lower()
    sku_u = str(sku).upper()
    if "doorstep installation" in title_l:
        return "Fitment/Installation"
    if re.search(r'LONSO2', sku_u):
        return "Latched On (Set of 2)"
    if re.search(r'LONSO4|(?<!AR)SO4', sku_u):
        return "Latched On (Set of 4)"
    if "LON" in sku_u:
        return "Latched On"
    if sku_u.endswith("OD"):
        return "Only Delivery"
    return "UNRESOLVED"


def th():
    return "text-align:left; padding:9px 12px; background:#2F5496; color:white; font-size:13px;"

def td():
    return "padding:9px 12px; border-bottom:1px solid #eee; font-size:13px;"

def td_white():
    return "padding:9px 12px; border-bottom:1px solid #eee; font-size:13px; color:white;"


def build_dashboard():
    if not os.path.isfile(DETAILS_FILE):
        print(f"ERROR: {DETAILS_FILE} not found. This dashboard depends on the Buy Box "
              f"workflow having run at least once to produce this file.")
        sys.exit(1)
    if not os.path.isfile(MASTER_FILE):
        print(f"ERROR: {MASTER_FILE} not found. Upload your master catalogue file to this path.")
        sys.exit(1)

    details = pd.read_csv(DETAILS_FILE, dtype={"SKU": str})
    mapping = pd.read_excel(MASTER_FILE, sheet_name="SKU Mapping")
    mapping = mapping[mapping["TNM SKU Code"].notna()].copy()

    mapping["Brand_Clean"] = mapping["Brand"].map(BRAND_NORMALIZE).fillna(mapping["Brand"])
    mapping["Category_Clean"] = mapping.apply(classify_category, axis=1)
    mapping = mapping[~mapping["Brand_Clean"].isin(REMOVE_BRANDS)].copy()

    details["base_sku"] = details["SKU"].apply(strip_amazon_suffix)
    amz_valid = details[["base_sku", "ASIN", "Listing_Type"]].rename(columns={"base_sku": "TNM SKU Code"})
    amz_pivot = amz_valid.pivot_table(index="TNM SKU Code", columns="Listing_Type", values="ASIN", aggfunc="first")
    for col in ["Only Delivery", "Fitment/Installation", "Latched On"]:
        if col not in amz_pivot.columns:
            amz_pivot[col] = None
    amz_pivot = amz_pivot.rename(columns={
        "Only Delivery": "AMZ_OD", "Fitment/Installation": "AMZ_WF", "Latched On": "AMZ_Latched"
    })[["AMZ_OD", "AMZ_WF", "AMZ_Latched"]].reset_index()

    master = mapping.merge(amz_pivot, on="TNM SKU Code", how="left")
    master["AMZ_OD_Present"] = master["AMZ_OD"].notna()
    master["AMZ_WF_Present"] = master["AMZ_WF"].notna()
    master["AMZ_Latched_Present"] = master["AMZ_Latched"].notna()

    unmatched_amz = details[~details["base_sku"].isin(mapping["TNM SKU Code"])].drop_duplicates(subset="base_sku")

    fk_available = os.path.isfile(FK_FILE)
    if fk_available:
        fk = pd.read_excel(FK_FILE, skiprows=1)
        fk = fk.rename(columns={fk.columns[1]: "Seller SKU Id", fk.columns[4]: "FSN",
                                  fk.columns[6]: "Listing Status", fk.columns[0]: "Product Title"})
        melted = []
        for col, label in [("FK Latched On", "Latched On"), ("FK OD", "Only Delivery"),
                            ("FK WF", "Fitment/Installation")]:
            sub = mapping[mapping[col].notna()][["TNM SKU Code", col]].rename(columns={col: "FSN"})
            sub["FK_Type"] = label
            melted.append(sub)
        fsn_truth = pd.concat(melted, ignore_index=True)
        fk_matched = fk.merge(fsn_truth, on="FSN", how="left")
        needs_fallback = fk_matched["FK_Type"].isna()
        fk_matched.loc[needs_fallback, "FK_Type"] = fk_matched[needs_fallback].apply(
            lambda r: classify_fk_type_fallback(r["Product Title"], r["Seller SKU Id"]), axis=1
        )
        fk_active_fsns = set(fk_matched["FSN"])
        master["FK_OD_Active"] = master["FK OD"].isin(fk_active_fsns)
        master["FK_WF_Active"] = master["FK WF"].isin(fk_active_fsns)
        master["FK_Latched_Active"] = master["FK Latched On"].isin(fk_active_fsns)
    else:
        print(f"NOTE: {FK_FILE} not found - Flipkart columns will show as not-present until "
              f"someone uploads a file to that path.")
        master["FK_OD_Active"] = False
        master["FK_WF_Active"] = False
        master["FK_Latched_Active"] = False
        fk_matched = pd.DataFrame(columns=["FK_Type"])

    master["On_Amazon"] = master["AMZ_OD_Present"] | master["AMZ_WF_Present"] | master["AMZ_Latched_Present"]
    master["On_FK"] = master["FK_OD_Active"] | master["FK_WF_Active"] | master["FK_Latched_Active"]

    def slicer_class(cat):
        if cat == "Tyre - 2W":
            return "cat-tyre2w"
        if cat == "Tyre - 4W":
            return "cat-tyre4w"
        return "cat-battery"
    master["slicer_class"] = master["Category_Clean"].apply(slicer_class)

    total = len(master)
    on_amz, on_fk = master["On_Amazon"].sum(), master["On_FK"].sum()
    both = (master["On_Amazon"] & master["On_FK"]).sum()
    neither = (~master["On_Amazon"] & ~master["On_FK"]).sum()

    cards = [
        ("Total TNM SKUs (filtered)", total, "excludes removed brands"),
        ("On Amazon", f"{on_amz} ({round(100*on_amz/total,1)}%)", "any listing type"),
        ("On Flipkart", f"{on_fk} ({round(100*on_fk/total,1)}%)", "any listing type" if fk_available else "NO FK FILE UPLOADED YET"),
        ("On both marketplaces", f"{both} ({round(100*both/total,1)}%)", "the real moat"),
        ("On neither", f"{neither} ({round(100*neither/total,1)}%)", "biggest opportunity"),
    ]
    cards_html = "".join(f"""<div style="background:white;border-radius:8px;padding:16px;box-shadow:0 1px 3px rgba(0,0,0,0.1);text-align:center;">
        <div style="font-size:22px;font-weight:bold;color:#2F5496;">{v}</div>
        <div style="font-size:12px;color:#444;margin-top:4px;">{l}</div>
        <div style="font-size:10px;color:#888;margin-top:2px;">{s}</div></div>""" for l, v, s in cards)

    amz_lt_counts = details["Listing_Type"].value_counts()
    if fk_available:
        fk_lt_counts = fk_matched["FK_Type"].value_counts()
        fk_latched_total = (fk_lt_counts.get("Latched On", 0) + fk_lt_counts.get("Latched On (Set of 2)", 0)
                              + fk_lt_counts.get("Latched On (Set of 4)", 0))
        fk_total_listings = len(fk_matched)
    else:
        fk_lt_counts = {}
        fk_latched_total = 0
        fk_total_listings = 0

    listing_count_rows = f"""
    <tr><td style='{td()}'>Fitment/Installation</td><td style='{td()}'>{amz_lt_counts.get('Fitment/Installation',0)}</td>
        <td style='{td()}'>{fk_lt_counts.get('Fitment/Installation',0) if fk_available else 'N/A'}</td></tr>
    <tr><td style='{td()}'>Only Delivery</td><td style='{td()}'>{amz_lt_counts.get('Only Delivery',0)}</td>
        <td style='{td()}'>{fk_lt_counts.get('Only Delivery',0) if fk_available else 'N/A'}</td></tr>
    <tr><td style='{td()}'>Latched On (all variants)</td><td style='{td()}'>{amz_lt_counts.get('Latched On',0)}</td>
        <td style='{td()}'>{fk_latched_total if fk_available else 'N/A'}</td></tr>
    <tr style="font-weight:bold; background:#f5f5f5;"><td style='{td()}'>Total listings</td>
        <td style='{td()}'>{len(details)}</td><td style='{td()}'>{fk_total_listings if fk_available else 'N/A'}</td></tr>
    """

    tyre_brands = sorted(master[master["Category_Clean"].str.startswith("Tyre")]["Brand_Clean"].unique())
    battery_brands = sorted(master[master["Category_Clean"].str.startswith("Battery")]["Brand_Clean"].unique())

    def build_brand_rows(brands):
        rows = ""
        totals = {"n": 0, "amz_wf": 0, "amz_od": 0, "amz_lat": 0, "fk_wf": 0, "fk_od": 0, "fk_lat": 0}
        for brand in brands:
            g = master[master["Brand_Clean"] == brand]
            n = len(g)
            amz_wf, amz_od, amz_lat = int(g["AMZ_WF_Present"].sum()), int(g["AMZ_OD_Present"].sum()), int(g["AMZ_Latched_Present"].sum())
            fk_wf, fk_od, fk_lat = int(g["FK_WF_Active"].sum()), int(g["FK_OD_Active"].sum()), int(g["FK_Latched_Active"].sum())
            brand_classes = " ".join(g["slicer_class"].unique())
            rows += f"""<tr class="{brand_classes}"><td style='{td()}'>{brand}</td><td style='{td()}'>{n}</td>
                <td style='{td()}'>{amz_wf}</td><td style='{td()}'>{amz_od}</td><td style='{td()}'>{amz_lat}</td>
                <td style='{td()}'>{fk_wf}</td><td style='{td()}'>{fk_od}</td><td style='{td()}'>{fk_lat}</td></tr>"""
            totals["n"] += n; totals["amz_wf"] += amz_wf; totals["amz_od"] += amz_od; totals["amz_lat"] += amz_lat
            totals["fk_wf"] += fk_wf; totals["fk_od"] += fk_od; totals["fk_lat"] += fk_lat
        return rows, totals

    tyre_rows, tyre_totals = build_brand_rows(tyre_brands)
    battery_rows, battery_totals = build_brand_rows(battery_brands)

    def subtotal_row(label, t, classes):
        return f"""<tr class="{classes}" style="background:#f5f5f5; font-weight:bold;">
            <td style='{td()}'>{label} subtotal</td><td style='{td()}'>{t['n']}</td>
            <td style='{td()}'>{t['amz_wf']}</td><td style='{td()}'>{t['amz_od']}</td><td style='{td()}'>{t['amz_lat']}</td>
            <td style='{td()}'>{t['fk_wf']}</td><td style='{td()}'>{t['fk_od']}</td><td style='{td()}'>{t['fk_lat']}</td></tr>"""

    grand_totals = {k: tyre_totals[k] + battery_totals[k] for k in tyre_totals}
    grand_row = f"""<tr style="background:#2F5496;">
        <td style='{td_white()}'><b>GRAND TOTAL</b></td><td style='{td_white()}'>{grand_totals['n']}</td>
        <td style='{td_white()}'>{grand_totals['amz_wf']}</td><td style='{td_white()}'>{grand_totals['amz_od']}</td><td style='{td_white()}'>{grand_totals['amz_lat']}</td>
        <td style='{td_white()}'>{grand_totals['fk_wf']}</td><td style='{td_white()}'>{grand_totals['fk_od']}</td><td style='{td_white()}'>{grand_totals['fk_lat']}</td></tr>"""

    all_tyre_classes = " ".join(sorted(set(master[master["Category_Clean"].str.startswith("Tyre")]["slicer_class"])))
    all_battery_classes = " ".join(sorted(set(master[master["Category_Clean"].str.startswith("Battery")]["slicer_class"])))

    brand_type_table = f"""
    <div style="margin-bottom:12px;">
      <label style="font-size:13px; font-weight:500; margin-right:8px;">Filter by category:</label>
      <select id="categoryFilter" onchange="filterBrandTable()" style="padding:6px 10px; border-radius:6px; border:1px solid #ccc;">
        <option value="all">All (2W Tyres + 4W Tyres + Batteries)</option>
        <option value="cat-tyre2w">2W Tyres only</option>
        <option value="cat-tyre4w">4W Tyres only</option>
        <option value="cat-battery">Batteries only</option>
      </select>
    </div>
    <table id="brandTypeTable">
    <tr><th style='{th()}' rowspan="2">Brand</th><th style='{th()}' rowspan="2">Total SKUs</th>
        <th style='{th()}' colspan="3">Amazon</th><th style='{th()}' colspan="3">Flipkart</th></tr>
    <tr><th style='{th()}'>Fitment</th><th style='{th()}'>Only Delivery</th><th style='{th()}'>Latched On</th>
        <th style='{th()}'>Fitment</th><th style='{th()}'>Only Delivery</th><th style='{th()}'>Latched On</th></tr>
    <tr><td colspan="8" style="background:#2F5496; color:white; font-weight:bold; padding:8px 12px; font-size:13px;">TYRES</td></tr>
    {tyre_rows}
    {subtotal_row("Tyres", tyre_totals, all_tyre_classes)}
    <tr><td colspan="8" style="background:#2F5496; color:white; font-weight:bold; padding:8px 12px; font-size:13px;">BATTERIES</td></tr>
    {battery_rows}
    {subtotal_row("Batteries", battery_totals, all_battery_classes)}
    {grand_row}
    </table>
    <script>
    function filterBrandTable() {{
      const val = document.getElementById('categoryFilter').value;
      document.querySelectorAll('#brandTypeTable tr[class]').forEach(row => {{
        if (val === 'all') {{ row.style.display = ''; return; }}
        row.style.display = row.className.includes(val) ? '' : 'none';
      }});
    }}
    </script>
    """

    cat_rows, cat_totals = "", {"n": 0, "amz": 0, "fk": 0, "both": 0}
    for cat, g in master.groupby("Category_Clean"):
        n = len(g)
        amz_n, fk_n, both_n = int(g["On_Amazon"].sum()), int(g["On_FK"].sum()), int((g["On_Amazon"] & g["On_FK"]).sum())
        cat_rows += f"""<tr><td style='{td()}'>{cat}</td><td style='{td()}'>{n}</td>
            <td style='{td()}'>{amz_n} ({round(100*amz_n/n,1)}%)</td>
            <td style='{td()}'>{fk_n} ({round(100*fk_n/n,1)}%)</td>
            <td style='{td()}'>{both_n} ({round(100*both_n/n,1)}%)</td></tr>"""
        cat_totals["n"] += n; cat_totals["amz"] += amz_n; cat_totals["fk"] += fk_n; cat_totals["both"] += both_n

    cat_total_row = f"""<tr style="background:#2F5496;">
        <td style='{td_white()}'><b>GRAND TOTAL</b></td><td style='{td_white()}'>{cat_totals['n']}</td>
        <td style='{td_white()}'>{cat_totals['amz']} ({round(100*cat_totals['amz']/cat_totals['n'],1)}%)</td>
        <td style='{td_white()}'>{cat_totals['fk']} ({round(100*cat_totals['fk']/cat_totals['n'],1)}%)</td>
        <td style='{td_white()}'>{cat_totals['both']} ({round(100*cat_totals['both']/cat_totals['n'],1)}%)</td></tr>"""

    unmatched_rows = "".join(
        f"<tr><td style='{td()}'>{r['SKU']}</td><td style='{td()}'>{r['Listing_Type']}</td>"
        f"<td style='{td()}'>{r['ASIN']}</td><td style='{td()}'>{str(r['Title'])[:70]}</td></tr>"
        for _, r in unmatched_amz.iterrows()
    )

    from datetime import datetime, timezone
    generated_at = datetime.now(timezone.utc).astimezone().strftime("%Y-%m-%d %H:%M %Z")
    fk_status_note = ("Flipkart data current as of the last uploaded fk_listing_latest.xls."
                        if fk_available else
                        "<b style='color:#c0392b;'>No Flipkart file has been uploaded yet - "
                        "upload one to data/fk_listing_latest.xls to populate Flipkart columns.</b>")

    html = f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="UTF-8"><title>TNM Catalogue Coverage Dashboard</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<style>
body {{ font-family: Arial, Helvetica, sans-serif; background: #f4f6f8; margin: 0; padding: 24px; color: #222; }}
h1 {{ margin-bottom: 4px; }} h2 {{ margin-top: 28px; margin-bottom: 10px; }}
.cards {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(160px, 1fr)); gap: 14px; margin-bottom: 20px; }}
table {{ width: 100%; border-collapse: collapse; background: white; border-radius: 8px; overflow: hidden; box-shadow: 0 1px 3px rgba(0,0,0,0.1); margin-bottom: 24px;}}
.note {{ font-size: 12px; color: #999; margin: 4px 0 20px; }}
</style></head><body>
<h1>TNM Catalogue Coverage Dashboard</h1>
<div style="color:#666; margin-bottom:16px; font-size:14px;">Last updated: {generated_at}</div>
<p class="note">Amazon: live from the Buy Box dashboard's details.csv. {fk_status_note}
Excludes: {", ".join(REMOVE_BRANDS)}.</p>

<div class="cards">{cards_html}</div>

<h2>Listing counts by type (raw counts, both marketplaces)</h2>
<table><tr><th style='{th()}'>Listing Type</th><th style='{th()}'>Amazon</th><th style='{th()}'>Flipkart</th></tr>{listing_count_rows}</table>

<h2>Brand-level view: Fitment / Only Delivery / Latched On by marketplace</h2>
{brand_type_table}

<h2>Coverage by category</h2>
<table><tr><th style='{th()}'>Category</th><th style='{th()}'>Total SKUs</th><th style='{th()}'>On Amazon</th><th style='{th()}'>On Flipkart</th><th style='{th()}'>On both</th></tr>{cat_rows}{cat_total_row}</table>

<h2>Live Amazon SKUs with no master catalogue match ({len(unmatched_amz)})</h2>
<p class="note">Genuinely live Amazon listings whose base SKU code isn't in your master catalogue.</p>
<table><tr><th style='{th()}'>Amazon SKU</th><th style='{th()}'>Type</th><th style='{th()}'>ASIN</th><th style='{th()}'>Title</th></tr>{unmatched_rows}</table>
</body></html>"""

    os.makedirs(os.path.dirname(OUTPUT_FILE), exist_ok=True)
    with open(OUTPUT_FILE, "w") as f:
        f.write(html)
    print(f"Dashboard written to {OUTPUT_FILE}")
    print(f"Total SKUs: {total}, On Amazon: {on_amz}, On Flipkart: {on_fk} (FK file present: {fk_available})")


if __name__ == "__main__":
    build_dashboard()
