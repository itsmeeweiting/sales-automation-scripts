# LeadIQ + ZoomInfo Enrichment — integrated into sales_nav_scraper.js

This replaces the earlier standalone Python pipeline approach. Enrichment
now runs inline, per contact, as part of your existing scrape — one CSV
out, no separate contacts.csv/config.json/pipeline.py steps needed.

## What changed in sales_nav_scraper.js
1. Added `const { enhanceContact } = require('./leadiq-zoominfo-enhance');`
2. Added `'LeadIQ Email', 'LeadIQ Phone'` to the CSV header (ZoomInfo's
   columns already existed in your header, just unpopulated before).
3. Added a call to `enhanceContact(row, salesNavUrl, './auth.json', reveal)`
   at the end of `scrapeOneContact()`, right after the Cognism step.

## Files
- `leadiq-zoominfo-enhance.js` — the actual ZoomInfo + LeadIQ API calls
- `auth.example.json` — copy to `auth.json`, fill in real session tokens
- `sales_nav_scraper.js` — your original scraper with the 3 edits above

## Setup
1. `cp auth.example.json auth.json`
2. ZoomInfo tokens: DevTools on `app.zoominfo.com`, copy `cookie`,
   `x-ziaccesstoken`, `x-ziid`, `x-zisession`, `user` headers from any
   request into `auth.json`.
3. LeadIQ token: `chrome://extensions` → LeadIQ → Inspect views →
   `prospector/panel/side-panel.html` → trigger a lookup → right-click
   any `app.leadiq.com` or `router.leadiq.com` request → Copy → Copy as
   cURL → find `-H 'authorization: Bearer eyJ...'` and copy everything
   after `Bearer ` into `auth.json`'s `bearerToken` field. NOTE: this is
   a bearer token, not a cookie — confirmed from real captures.
4. These expire — re-grab when calls start 401'ing.
5. Run the scraper as usual. Set `ENRICH_REVEAL=0` as an environment
   variable for a dry-run first pass (matching only, no credits spent):
   ```
   ENRICH_REVEAL=0 node sales_nav_scraper.js
   ```
   Once match quality looks right, drop the env var (or set it to `1`)
   for a real run that actually unlocks emails.

## Known open items — confirm before trusting this on a full list
1. **LeadIQ numeric member ID — confirmed real, not yet scraped.**
   Real captures confirm `lookup/person/bulk` is keyed by LinkedIn's
   actual numeric member id (e.g. `32814131`), which your scraper
   doesn't currently extract anywhere. `matchLeadIQ()` currently
   substitutes the canonical URL's vanity slug
   instead — this has NOT been tested against LeadIQ's actual matching
   logic and may return no matches or wrong matches. First thing to check
   if LeadIQ results look empty: capture a fresh LeadIQ side-panel HAR
   and look for any request that resolves a vanity URL to a numeric id.
2. **`viewContacts` / lookup response key names** — same caveats as
   before: confirm `person.id` echoes correctly in `viewContacts`'s
   response, and confirm the right key for LeadIQ's internal lead id in
   the lookup response, against live responses.
3. **Credit spend** — both unlock steps cost credits per contact. Always
   dry-run (`ENRICH_REVEAL=0`) on a small batch first.
4. **Auth refresh** — still fully manual; no programmatic login wired in.
