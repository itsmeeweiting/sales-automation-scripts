[README.md](https://github.com/user-attachments/files/31541986/README.md)
# Sales Automation Scripts — Setup Guide

A Flask app + a set of Playwright/Node automation scripts for running the sales
pipeline: LinkedIn/ZoomInfo scraping, contact enrichment (Cognism, LeadIQ,
ZoomInfo), Glean-generated outreach messaging, Salesloft import/send, a news
signal scraper, and a Salesloft Live Feed scraper — all writing into one
shared tracker CSV.

This README covers first-time setup end to end. Individual workspaces also
have their own `README.md` / `README-integration.md` with extra detail —
this file is the map that ties them together.

---

## 1. Prerequisites

- **Python 3** (for `app.py` and the `news_workspace/` scripts)
- **Node.js 18+** (for the Playwright scraper/automation scripts — global
  `fetch` is required)
- **Google Chrome/Chromium** (Playwright installs its own, see below)

---

## 2. Clone and install

```bash
git clone <this-repo-url>
cd sales-automation-scripts
```

### Python side (Flask app + news_workspace)

```bash
python3 -m venv venv
source venv/bin/activate          # Windows: venv\Scripts\activate
pip install -r requirements.txt   # if a top-level one exists
cd news_workspace
pip install -r requirements.txt
python3 -m playwright install chromium   # only needed if this venv also runs Playwright
cd ..
```

> `venv/` is intentionally **not** in the repo — see `.gitignore`. Anyone
> setting this up creates their own from `requirements.txt`.

### Node side (scraper_workspace, glean_workspace, live_feed_workspace, salesloft_pipeline_workspace)

Each workspace folder has its own dependencies. From inside each one:

```bash
npm install
npx playwright install chromium
```

For `salesloft_pipeline.py` specifically (Python + Playwright, not Node):

```bash
pip3 install playwright pandas openpyxl
python3 -m playwright install chromium
```

---

## 3. Set up credentials — read this before running anything

**None of the real credential files are in this repo.** You need to create
them locally, once, and they never get committed (they're in `.gitignore`).

### `auth.json` (ZoomInfo + LeadIQ)

Used by `leadiq-zoominfo-enhance.js` (called from `sales_nav_scraper.js` and
`zoominfo_scrap.js`).

```bash
cp auth.example.json auth.json
```

Then fill in real session tokens:

1. **ZoomInfo**: open DevTools on `app.zoominfo.com`, find any API request,
   copy the `x-ziaccesstoken`, `x-ziid`, `x-zisession` header values into
   `auth.json`.
2. **LeadIQ**: `chrome://extensions` → LeadIQ → "Inspect views" →
   `prospector/panel/side-panel.html` → trigger a lookup → right-click any
   `app.leadiq.com` / `router.leadiq.com` request → Copy → Copy as cURL →
   find `-H 'authorization: Bearer eyJ...'` and copy everything after
   `Bearer ` into `auth.json`'s `bearerToken` field.

Both expire (ZoomInfo ~a day, LeadIQ ~24h) — re-capture when calls start
401'ing. See `README-integration.md` for more detail.

### Browser session folders (Cognism, Glean, Salesloft, LinkedIn Sales Nav, ZoomInfo)

These scripts use **persistent Playwright browser profiles** instead of
stored tokens — you log in by hand once per script, in a real browser
window that opens, and the session is reused on future runs:

| Script | Profile folder | Logs into |
|---|---|---|
| `sales_nav_scraper.js` | `./browser_profile` | LinkedIn Sales Nav + Cognism |
| `zoominfo_scrap.js` | `./browser_profile` | LinkedIn Sales Nav + Cognism |
| `zoominfo-contact.js` | `./browser_profile_zoominfo` | ZoomInfo |
| `zoominfo-linkedin-scrap.js` | `./browser_profile_zoominfo_for_linkedin` + `./browser_profile_linkedin` | ZoomInfo, then LinkedIn |
| `scrape_live_feed.js` | `./browser-profile` (hyphenated — different from the above) | Salesloft |
| `glean-news-report.js` / `glean-pg-tracker-automation.js` | `./glean-profile` | Glean |
| `salesloft_pipeline.py` | `./browser_profile` (inside that workspace) | Salesloft (SSO) |

None of these folders exist until you run the corresponding script for the
first time — a browser window opens, you log in, and the folder is created
automatically. **Never commit these folders** — they contain live logged-in
session cookies, equivalent to a saved password.

### Cognism (standalone login helper)

```bash
cd scraper_workspace
node cognism-playwright.js --login
```

Saves a session to `./cognism-state.json` (also gitignored).

---

## 4. Running the Flask app

```bash
python3 app.py
```

Visit `http://localhost:5000` (or whatever port `app.py` binds to). The app
is the control panel for every workspace below — most scripts are meant to
be triggered from its UI (which streams live log output and prompts), not
run standalone, though every script also works from the command line for
testing.

> ⚠️ `app.py` currently runs with `debug=True`. That's fine for local use
> but should not be exposed on a network — Flask's debugger allows
> arbitrary code execution if reachable. Set this from an environment
> variable before deploying anywhere beyond your own machine.

---

## 5. Workspace-by-workspace overview

### `scraper_workspace/` — LinkedIn + ZoomInfo scraping
- `sales_nav_scraper.js` — paginated Sales Navigator scraper with
  pause/resume/stop and resumable progress checkpoints, enriches each
  contact via Cognism, LeadIQ, and ZoomInfo.
- `zoominfo_scrap.js` — ZoomInfo CSV → resolves each contact's Sales Nav
  profile → scrapes + enriches.
- `zoominfo-lib.js`, `linkedin-salesnav-lib.js`, `zoominfo-contact.js`,
  `zoominfo-linkedin-scrap.js` — a second, GraphQL-API-based ZoomInfo
  scraping path (captures your browser's own live session headers instead
  of using `auth.json`).
- `cognism-playwright.js`, `leadiq-zoominfo-enhance.js` — shared enrichment
  modules, imported by the scrapers above.

### `glean_workspace/`
- `glean-pg-tracker-automation.js` — per-contact prompts to Glean skills
  (email, WhatsApp, cold call script), writes results back to the tracker
  columns.
- `glean-news-report.js` — uploads a whole CSV to Glean as an attachment,
  gets back a relevance-scored report (workbook or canvas doc).

### `live_feed_workspace/`
- `scrape_live_feed.js` — scrapes Salesloft's Live Feed (opens, clicks,
  hot leads) into a CSV, page by page.

### `news_workspace/`
- `main.py` — Google News RSS scraper keyed off your real `accounts.csv` +
  `keywords.csv`, with per-account/keyword relevance scoring
  (`scorer.py`), redirect resolution (`decoder.py`), article extraction
  (`extractor.py`), and dedup tracking (`state.py`).

### `salesloft_pipeline_workspace/`
- `salesloft_pipeline.py` — consolidated bulk import + cadence enrollment
  (API calls) + per-contact compose/send-or-schedule (browser UI), replacing
  the older two-script flow.

---

## 6. Data files — what's real vs. sample

Several CSVs/JSON files in this repo are **10-row sanitized samples**, not
real data — they exist so the app has something to load and so the format
is documented, without exposing real prospects or employees:

- `accounts.csv`, `tracker.csv`, `contacts.csv`, `selected_accounts.csv`
- `cadences.json`, `validation_rules.json`, `auth.example.json`

The **real** versions of these files (with actual prospect/customer PII,
live Salesforce links, and real Salesloft cadence IDs) live only on the
machine actually running the pipeline, and are excluded via `.gitignore`.
If you're setting this up fresh, either keep using the sample data for
testing, or replace it with your own real export once you're ready to run
a live batch.

---

## 7. Before you commit anything new

Check `.gitignore` — it already excludes credential/session files and
generated output CSVs. If you add a new script that creates a new kind of
credential file or output CSV, add a pattern for it there too, rather than
relying on remembering not to `git add` it by hand.
