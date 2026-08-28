#!/usr/bin/env node
/**
 * zoominfo-linkedin-scrap.js
 *
 * Phase 1 (ZoomInfo)  — same as zoominfo-contact.js: scrape a filtered
 *                       People Search via ZoomInfo's internal GraphQL API.
 *                       No need to click into any contact — reveal is
 *                       handled automatically for every contact found.
 * Phase 2 (LinkedIn)  — for every contact found, resolve their LinkedIn URL
 *                       to a Sales Navigator profile (falling back to a
 *                       name+company search) and scrape About, Experience,
 *                       Tenure, Latest Post, LinkedIn Phone, LinkedIn Email
 *                       — same logic as your existing zoominfo_scrap.js.
 *
 * You log in to BOTH ZoomInfo and Sales Navigator up front. Each phase has
 * its own "press ENTER to start" confirmation before it spends anything
 * (ZoomInfo credits in phase 1, time/requests in phase 2).
 *
 * A checkpoint CSV is written after phase 1 in case phase 2 fails partway —
 * you won't lose the ZoomInfo data.
 *
 * Requires Node 18+ (uses the global fetch).
 *
 * Usage:
 *   node zoominfo-linkedin-scrap.js [output.csv]
 */

const { chromium } = require('playwright');
const zi = require('./zoominfo-lib');
const li = require('./linkedin-salesnav-lib');

(async () => {
  const outputFile = process.argv[2] || ('zoominfo_linkedin_' + Date.now() + '.csv');
  const phase1File = outputFile.replace(/\.csv$/i, '') + '_zoominfo_phase.csv';
  console.log('Output CSV: ' + outputFile);
  console.log('ZoomInfo checkpoint CSV: ' + phase1File + '\n');

  // ── Phase 1: ZoomInfo ──────────────────────────────────────────────────

  const ziBrowser = await zi.launchProfile(chromium, './browser_profile_zoominfo_for_linkedin', {
    headless: false,
    viewport: { width: 1400, height: 900 },
  }, 'ZoomInfo');
  const ziPage = await ziBrowser.newPage();

  const captured = { list: null, detail: null };
  zi.attachCapture(ziPage, captured);

  await ziPage.goto('https://app.zoominfo.com/');

  console.log('──────────────────────────────────────');
  console.log('STEP 1 — ZOOMINFO LOGIN');
  console.log('1. Log in to ZoomInfo if needed.');
  console.log('2. Go to People Search and apply all your filters as usual,');
  console.log('   so the results page shows exactly the contacts you want.');
  console.log('──────────────────────────────────────\n');

  while (true) {
    await zi.prompt('Press ENTER once both steps above are done...');
    const missing = zi.missingCaptures(captured);
    if (missing.length === 0) break;
    console.log('\nStill missing: ' + missing.join('; ') + '\n');
  }

  console.log('\nStarting ZoomInfo scrape...\n');

  const ziRows = await zi.scrapeZoomInfo({
    captured,
    onConfirm: zi.prompt,
    onRow: async (row) => { zi.writeCSVRow(phase1File, row); },
  });

  await ziBrowser.close();
  console.log('ZoomInfo phase complete: ' + ziRows.length + ' contacts. Checkpoint saved to ' + phase1File + '\n');

  // ── Phase 2: LinkedIn Sales Navigator ───────────────────────────────────

  const liBrowser = await zi.launchProfile(chromium, './browser_profile_linkedin', {
    headless: false,
    viewport: { width: 1400, height: 900 },
    args: ['--disable-blink-features=AutomationControlled'],
  }, 'LinkedIn Sales Navigator');
  const liPage = await liBrowser.newPage();
  await liPage.goto('https://www.linkedin.com/sales/home');

  console.log('──────────────────────────────────────');
  console.log('STEP 2 — LINKEDIN SALES NAVIGATOR LOGIN');
  console.log('Log in to Sales Navigator if needed.');
  console.log('──────────────────────────────────────\n');
  await li.prompt('Press ENTER once logged in...');

  console.log('\n' + ziRows.length + ' contacts ready for LinkedIn enrichment.');
  await li.prompt('Press ENTER to start, or Ctrl+C to cancel...');
  console.log('\nStarting LinkedIn Sales Navigator enrichment...\n');

  let success = 0;
  let failed = 0;

  for (let i = 0; i < ziRows.length; i++) {
    const row = ziRows[i];
    console.log('\n[' + (i + 1) + '/' + ziRows.length + '] ' + row['Contact Name'] + ' | ' + row['Title / Role'] + ' | ' + row['Company Name']);

    // ── Resolve a Sales Nav profile URL ──
    let profileUrl = null;
    if (row['LinkedIn Profile'] && row['LinkedIn Profile'].includes('linkedin.com/in/')) {
      console.log('  [route] LinkedIn URL found → clicking "View in Sales Navigator"');
      try {
        profileUrl = await li.resolveToSalesNavUrl(liBrowser, row['LinkedIn Profile']);
      } catch (err) {
        console.warn('  [warn] Could not load LinkedIn profile (' + err.message.split('\n')[0] + ') — trying search fallback');
      }
    }
    if (!profileUrl) {
      console.log('  [route] Falling back to Sales Nav keyword search');
      profileUrl = await li.searchSalesNav(liPage, row['Contact Name'], row['Company Name']);
    }

    if (!profileUrl) {
      console.warn('  [skip] Could not resolve a Sales Nav profile — saving ZoomInfo data only.');
      failed += 1;
      zi.writeCSVRow(outputFile, row);
      await li.sleep(3000 + Math.random() * 3000);
      continue;
    }

    // ── Scrape the Sales Nav profile ──
    try {
      const scraped = await li.scrapeProfile(liBrowser, profileUrl);
      success += 1;
      zi.writeCSVRow(outputFile, {
        ...row,
        'LinkedIn Profile': row['LinkedIn Profile'] || profileUrl,
        'About': scraped['About'] || '',
        'Experience Description': scraped['Experience Description'] || '',
        'Latest Post': scraped['Latest Post'] || '',
        'Tenure': scraped['Tenure'] || '',
        'LinkedIn Phone': scraped['LinkedIn Phone'] || '',
        'LinkedIn Email': scraped['LinkedIn Email'] || '',
      });
      console.log('  Saved with LinkedIn enrichment.');
    } catch (err) {
      console.warn('  [warn] Sales Nav scrape failed: ' + err.message);
      failed += 1;
      zi.writeCSVRow(outputFile, row);
    }

    await li.sleep(3000 + Math.random() * 3000); // 3-6s random delay, same as your existing scraper
  }

  await liBrowser.close();

  console.log('\n──────────────────────────────────────');
  console.log('Done. ' + success + ' enriched, ' + failed + ' skipped/failed.');
  console.log('Output CSV: ' + outputFile);
  console.log('ZoomInfo-only checkpoint: ' + phase1File);
  console.log('──────────────────────────────────────');
})();
