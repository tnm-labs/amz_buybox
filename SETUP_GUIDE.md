# TNM Buy Box Dashboard - Setup Guide

This gives you a live webpage showing Buy Box performance across your ASINs,
refreshed automatically twice a day, with a WhatsApp alert if your Buy Box
win rate drops below 20%. It runs entirely on GitHub's free infrastructure -
no server of your own to maintain.

## How it works

- `data/listings.csv` = the master list of ASINs you're tracking. **Edit this
  file anytime (add/remove ASINs, update your price) directly on GitHub's
  website - no code changes needed.**
- `generate_dashboard.py` = pulls live pricing from Amazon SP-API for every
  ASIN in that file, computes the metrics, and writes `docs/index.html`.
- `.github/workflows/dashboard.yml` = tells GitHub to run the script
  automatically twice a day (and lets you trigger it manually anytime).
- GitHub Pages serves `docs/index.html` as a public webpage link you can
  share with your team.
- `data/history.csv` builds up a row per run, so you can later chart trends
  over time if you want.

## One-time setup

### 1. Create a GitHub account (if you don't have one)
Go to github.com and sign up - free.

### 2. Create a new repository
- Click "+" (top right) -> "New repository"
- Name it something like `tnm-buybox-dashboard`
- Set it to **Private** (recommended, since it'll reference your ASINs/pricing)
- Click "Create repository"

### 3. Upload these files
On the repo's main page, click "Add file" -> "Upload files", and upload
this whole folder structure (keeping the folders intact):
```
data/listings.csv
generate_dashboard.py
.github/workflows/dashboard.yml
```
(The `docs/` folder will be created automatically the first time the workflow runs.)

### 4. Add your secrets
These are your credentials, stored securely by GitHub - never visible in
the code or to anyone browsing the repo.

Go to: repo -> **Settings** -> **Secrets and variables** -> **Actions** ->
**New repository secret**. Add each of these one at a time:

| Secret name | Value |
|---|---|
| `SP_API_REFRESH_TOKEN` | your SP-API refresh token |
| `SP_API_LWA_APP_ID` | `amzn1.application-oa2-client.c41ce7d9f29f4765a056bb812cdfc7a5` |
| `SP_API_LWA_CLIENT_SECRET` | your SP-API client secret |
| `CALLMEBOT_PHONE` | WhatsApp number to alert, country code no `+` (e.g. `919000994483`) |
| `CALLMEBOT_APIKEY` | see step 5 below |

### 5. Set up the free WhatsApp alert (CallMeBot)
This lets a script send you a WhatsApp message for free, no business
account needed - good for personal/team alerts like this.

1. Save this contact in your phone: **+34 644 59 71 65** (CallMeBot's number)
2. Send it this exact WhatsApp message: `I allow callmebot to send me messages`
3. You'll get a reply with your personal API key
4. Use your phone number (with country code, no `+` or spaces) as
   `CALLMEBOT_PHONE`, and the key you received as `CALLMEBOT_APIKEY`

If you want to alert **multiple people**, each person needs to do steps 1-3
themselves to get their own API key, and you'd extend the script to loop
over a list of (phone, apikey) pairs - let me know if you want this and
I'll adjust the script.

### 6. Enable GitHub Pages
- Repo -> **Settings** -> **Pages**
- Under "Build and deployment" -> Source: **Deploy from a branch**
- Branch: **main**, folder: **/docs**
- Save

GitHub will give you a URL like:
`https://YOUR_USERNAME.github.io/tnm-buybox-dashboard/`

This is the link you share with your team. It updates automatically after
each scheduled run.

### 7. Run it once manually to test
- Repo -> **Actions** tab -> click "Update TNM Buy Box Dashboard" (left
  sidebar) -> **Run workflow** -> **Run workflow** (green button)
- Watch it run (takes a few minutes depending on how many ASINs you have)
- Once it finishes, check your GitHub Pages link - it should show live data

## Updating your listings later (no code changes)

To add, remove, or re-price ASINs:
1. Go to `data/listings.csv` in the repo
2. Click the pencil (edit) icon
3. Make your changes (must keep the same column headers: `ASIN,Title,SKU,Your_Price,Listing_Type`)
4. Commit the change directly to `main`

The next scheduled run (or a manual "Run workflow" click) will pick up
your changes automatically.

## Adjusting the schedule or thresholds

- **Schedule:** edit the two `cron:` lines in `.github/workflows/dashboard.yml`.
  Times are in UTC. IST is UTC+5:30.
- **Alert threshold (currently 20%):** edit `BUY_BOX_ALERT_THRESHOLD_PCT`
  near the top of `generate_dashboard.py`.
- **"Close/far" gap thresholds (currently Rs 50 / Rs 100):** edit
  `CLOSE_GAP_RS`, `MEDIUM_GAP_RS`, `HEADROOM_RS` in the same file.

## Metrics explained

- **Buy Box Win Rate** - % of tracked ASINs where you currently hold the Buy Box.
- **Gap to Buy Box** - for listings you're NOT winning, how much higher your
  price is than the current Buy Box price. Smaller = closer to winning if
  you dropped your price slightly.
- **Room to Raise Price** - for listings you ARE winning, where the next
  competitor is priced more than Rs 100 above you - meaning you may be
  leaving margin on the table.
- **Single-Offer Listings** - ASINs where you're the only seller (or only
  one with a real offer) - no real Buy Box competition to track.
- **Fetch Errors** - ASINs that failed to return data (suspended listing,
  invalid ASIN, temporary API issue, etc.) - worth checking manually.

## A note on assumptions

"Gap to Buy Box" and "Room to Raise Price" are both derived from *your price
vs. the current Buy Box / next competitor price* - not an absolute price
level. If your team meant something different by "under Rs 50/100", let me
know and I'll adjust the calculation.
