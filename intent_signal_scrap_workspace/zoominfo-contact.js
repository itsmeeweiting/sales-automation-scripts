#!/usr/bin/env node
/**
 * zoominfo-contact.js
 *
 * Scrapes a ZoomInfo People Search results page — filtered by you, by hand,
 * in the browser — via ZoomInfo's internal GraphQL API instead of the DOM.
 *
 * What it does:
 *   1. Opens ZoomInfo in a real (persistent) browser window.
 *   2. You log in and set up your filtered search as usual, then click on
 *      ONE contact card to reveal them (just like you'd normally do).
 *   3. Once you press ENTER, it replays that same search across every page
 *      of results, and reveals (unmasks) phone/email for every contact that
 *      has something to reveal.
 *   4. Writes one row per contact to the master CSV, populating only:
 *      Patch (country), Company Name, Contact Name, Title / Role,
 *      LinkedIn Profile, ZoomInfo Phone, ZoomInfo Email.
 *
 * Requires Node 18+ (uses the global fetch).
 *
 * Usage:
 *   node zoominfo-contact.js [output.csv]
 */

const { chromium } = require('playwright');
const zi = require('./zoominfo-lib');

(async () => {
  const outputFile = process.argv[2] || ('zoominfo_contacts_' + Date.now() + '.csv');
  console.log('Output CSV: ' + outputFile + '\n');

  const browser = await zi.launchProfile(chromium, './browser_profile_zoominfo', {
    headless: false,
    viewport: { width: 1400, height: 900 },
  }, 'ZoomInfo');
  const page = await browser.newPage();

  const captured = { list: null, detail: null };
  zi.attachCapture(page, captured);

  await page.goto('https://app.zoominfo.com/');

  console.log('──────────────────────────────────────');
  console.log('1. Log in to ZoomInfo if needed.');
  console.log('2. Go to People Search and apply all your filters as usual,');
  console.log('   so the results page shows exactly the contacts you want.');
  console.log('──────────────────────────────────────\n');

  // Keep prompting until we've actually seen a search-results request go by.
  while (true) {
    await zi.prompt('Press ENTER once both steps above are done...');
    const missing = zi.missingCaptures(captured);
    if (missing.length === 0) break;
    console.log('\nStill missing: ' + missing.join('; ') + '\n');
  }

  console.log('\nGot it. Starting scrape...\n');

  let rows = [];
  try {
    rows = await zi.scrapeZoomInfo({
      captured,
      onConfirm: zi.prompt,
      onRow: async (row) => {
        zi.writeCSVRow(outputFile, row);
        console.log('  Saved: ' + row['Contact Name']);
      },
    });
  } finally {
    await browser.close();
  }

  console.log('\n──────────────────────────────────────');
  console.log('Done. ' + rows.length + ' scraped.');
  console.log('Output CSV: ' + outputFile);
  console.log('──────────────────────────────────────');
})();
