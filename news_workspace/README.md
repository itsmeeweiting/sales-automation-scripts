[README.md](https://github.com/user-attachments/files/31541551/README.md)
# News Scraper — now wired into your Flask app

This folder (`news_workspace/`) drops into your main app's `BASE_DIR`, the
same way `scraper_workspace/`, `salesloft_workspace/`, and `glean_workspace/`
already do.

## What changed from the standalone version

- Reads your **real** `accounts.csv` (Company Name, Alternative Name, Patch,
  Industry) instead of a separate file — no duplicate account list to keep
  in sync. It's invoked via `--accounts-csv`, pointed at the same
  `ACCOUNTS_PATH` your `/library/accounts` page already uses.
- Output CSV uses `company_name` and `country` column names specifically
  because your `app.py` already has a **"Google News Scrape" import
  profile** defined (`DEFAULT_PROFILES["google_news"]`) that maps those
  exact names to Company Name / Patch. This was apparently set up for
  exactly this purpose and never connected — it now is.
- Added `--limit N` so you can test on a handful of accounts before running
  the full list (it's 300+ rows — see "First run" below).

## What's new in this update

- **Per-account region/language** — `main.py` now maps each account's
  `Patch` (Malaysia/Indonesia/Japan) to the right Google News locale via
  `PATCH_LOCALE` instead of using one fixed region for everyone. Add more
  entries to that dict if your patch list grows. `keywords.csv` also
  supports an optional `patch` column per row to scope a specific query to
  one market — leave it blank to use the global default.
- **Account selection on `/news`** — the page now shows your accounts
  library as a checkbox table (search by name/patch, quick filter chips
  for Malaysia/Indonesia/Japan, select-all-visible). Leaving nothing
  checked runs the full list, same as before; checking specific accounts
  runs only those (Flask stages a filtered CSV automatically, no change
  needed on the `main.py` side).
- **Keywords are now editable in-app** — registered as a library file
  (`/library/news_keywords`), same add/edit/delete/import UI as your
  Accounts and Proof Points libraries. The `/news` page shows a read-only
  preview with a link through to the editor.

## Setup


1. Copy this whole `news_workspace/` folder into your Flask app's root
   directory (next to `scraper_workspace/`, `glean_workspace/`, etc).
2. Copy `news.html` and `news_preview.html` (provided separately) into your
   `templates/` folder.
3. Apply the `app.py` changes (provided as a full replacement file —
   diff it against your current one before overwriting, in case you've
   made other edits since you uploaded it to me).
4. Install dependencies — same venv pattern as before:
   ```bash
   cd news_workspace
   python3 -m venv venv
   source venv/bin/activate
   pip install -r requirements.txt
   ```
   Your Flask app calls this script via `sys.executable`, which means it
   runs with **whatever Python is running Flask itself** — so if Flask
   runs outside this venv, install these same packages into Flask's own
   environment instead (or wherever `python3 app.py` is normally run from).

## Using it

Visit `/news` in your running app. Click **Run scrape** (use the "test with
first N accounts" field for a small first run — see below). Progress
streams live, same as your other scrapers. When it finishes, **Preview**
shows a sortable-by-relevance table; **Download CSV** gets you the raw file.

To get results into the tracker: download the CSV, go to **Import**, pick
**"Google News Scrape"** from the profile list, and commit. Company name
and Patch land automatically; the rest of the columns (headline, url,
relevance_score, body, etc.) stay in the CSV for your own reference even
though the import only pulls the two mapped fields.

## First run — test small first

You have 300+ accounts in the library. A first run with no dedup history
will hit Google News + decode + extract for every fresh article it finds,
which adds up. Use the limit field on `/news` (e.g. `5`) for your first
test, confirm the output looks right (real article bodies, not
`[fetch_failed]` rows), then run without a limit for the full list.

## Known limitation from testing

I verified the scoring, dedup, and CSV-writing logic directly, and confirmed
your real `accounts.csv` parses correctly (303 rows, Company Name /
Alternative Name / Patch / Industry read as expected). I could not test the
live Google News fetch, URL decode, or article extraction from my sandbox —
no network path to `news.google.com` there — so that part needs verifying
on your machine via the `/news` page (or `python3 main.py --limit 3`
directly) before trusting it at full volume.

## Extending later

- Swap `scorer.py`'s heuristic for a Glean call, same pattern as
  `glean-pg-tracker-automation.js`, if you want sharper relevance
  classification than the keyword/mention heuristic.
- The `google_news` profile currently only maps 2 fields. If you want
  headline/url to land in the tracker too, you'd need to know what an
  existing tracker column like "About" is actually used for elsewhere
  before repurposing it — I didn't have visibility into tracker.csv's full
  schema, so I left that profile untouched rather than guess.
- Add a Japan-locale pass (`hl=ja&gl=JP` in `CONFIG` inside `main.py`) —
  English coverage of Japanese enterprise news is thin.
