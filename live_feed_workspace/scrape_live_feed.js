/**
 * Salesloft Live Feed Scraper — Phase 1 Base
 * ------------------------------------------------------
 * Scrapes the Live Feed page (notifications-center/live-feed) and dumps
 * every event card into a CSV. No API lookups, no profile page scraping —
 * just what's visible in the feed itself.
 *
 * Usage: node scrape_live_feed.js <targetPage>
 *   e.g. node scrape_live_feed.js 3   -> navigates to ?page=3
 *
 * Pagination is URL-driven and cumulative -- ?page=3 already contains
 * everything from pages 1-2, so we navigate straight to the target page
 * and parse the DOM once. A dedupe pass is applied as a safety net.
 *
 * Runs headed (not headless) with a persistent browser profile at
 * ./browser-profile, same pattern as the other scrapers. A browser
 * window opens -- Salesloft requires login pretty much every run, so
 * this always pauses and prints "READY_FOR_LOGIN" after navigating.
 * Log into Salesloft in that window, then click "I've logged in,
 * continue" in the Flask UI -- same manual-confirm pattern your other
 * scraper/Salesloft automations already use.
 *
 * KNOWN UNCERTAINTY (verify via DevTools once running for real):
 *   - Only 3 event types seen so far (email-view-event, email-clicked-event,
 *     hot-lead-event). The parser reads fields generically, so other types
 *     (replied, bounced, meeting-booked) should still produce a row, just
 *     worth a spot-check on the CSV.
 */

const { chromium } = require('playwright');
const fs = require('fs');
const path = require('path');
const readline = require('readline');

const LIVE_FEED_BASE_URL = 'https://app.salesloft.com/app/notifications-center/live-feed';
const PROFILE_DIR = path.join(__dirname, 'browser-profile');
const OUTPUT_DIR = path.join(__dirname, 'output');

function timestampSlug() {
  const d = new Date();
  const pad = (n) => String(n).padStart(2, '0');
  return `${d.getFullYear()}${pad(d.getMonth() + 1)}${pad(d.getDate())}_${pad(d.getHours())}${pad(d.getMinutes())}${pad(d.getSeconds())}`;
}

// Waits for a single line on stdin. The Flask app relays your click on
// the "I've logged in, continue" button by writing "\n" to this
// process's stdin -- same mechanism your other scraper/Salesloft jobs
// use (SCRAPER_JOBS, SALESLOFT_JOBS, etc.), so this works the same way
// those already do for you.
function waitForContinueSignal() {
  return new Promise((resolve) => {
    const rl = readline.createInterface({ input: process.stdin });
    rl.once('line', () => {
      rl.close();
      resolve();
    });
  });
}

async function extractEvents(page) {
  return await page.evaluate(() => {
    // Matches both grouped ("+1 more activity") and single-event cards
    const cards = document.querySelectorAll(
      '[data-e2e-testid="grouped-events-wrapper"], .LiveFeedEventsList__BaseEventWrapper-ggljJe'
    );

    const results = [];

    cards.forEach((card) => {
      const row = {
        event_type: '',
        account_name: '',
        account_id: '',
        person_name: '',
        person_id: '',
        action: '',
        email_subject: '',
        email_id: '',
        cadence_name: '',
        timestamp_relative: '',
        click_count: '',
        additional_recipients: '',
        more_activities: '',
      };

      const eventEl = card.querySelector('[data-e2e-testid$="-event"]');
      if (eventEl) {
        row.event_type = eventEl.getAttribute('data-e2e-testid') || '';
      }

      const accountEl = card.querySelector('[class*="AccountLink"]');
      if (accountEl) {
        row.account_name = accountEl.textContent.trim();
        if (accountEl.tagName === 'A') {
          const href = accountEl.getAttribute('href') || '';
          const match = href.match(/\/app\/company\/(\d+)/);
          if (match) row.account_id = match[1];
        }
      }

      const personLink = card.querySelector('a[href*="/app/people/"]');
      if (personLink) {
        row.person_name = personLink.textContent.trim();
        const match = personLink.getAttribute('href').match(/\/app\/people\/(\d+)/);
        if (match) row.person_id = match[1];
      }

      // Action text (e.g. "opened", "clicked", "became a Hot Lead") and
      // click count (e.g. "5 times") share the same paragraph class as
      // the account name when the account isn't a clickable link (no
      // linked Account record for that contact) -- exclude whatever we
      // already matched as the account element so it doesn't get
      // mistaken for the action text.
      const actionParagraphs = Array.from(
        card.querySelectorAll('p.eventItemStyles__EventItemText-faViWQ')
      ).filter((p) => p !== accountEl);

      if (actionParagraphs.length > 0) {
        row.action = actionParagraphs[0].textContent.trim();
      }
      if (actionParagraphs.length > 1 && /time/i.test(actionParagraphs[1].textContent)) {
        row.click_count = actionParagraphs[1].textContent.trim();
      }

      const emailLink = card.querySelector('a[href*="/app/emails/detail/"]');
      if (emailLink) {
        row.email_subject = emailLink.textContent.trim();
        const match = emailLink.getAttribute('href').match(/\/app\/emails\/detail\/(\d+)/);
        if (match) row.email_id = match[1];
      }

      const cadenceEl = card.querySelector('[class*="CadenceNameText"]');
      if (cadenceEl) {
        row.cadence_name = cadenceEl.textContent.trim();
      }

      const timestampEl = card.querySelector('[class*="EventTimestampText"]');
      if (timestampEl) {
        row.timestamp_relative = timestampEl.textContent.trim();
      }

      const recipientsBtn = card.querySelector('[class*="RecipientsBox"] button');
      if (recipientsBtn) {
        row.additional_recipients = recipientsBtn.textContent.trim();
      }

      const moreActivitiesBtn = card.querySelector('[class*="MoreActivitiesButton"] p');
      if (moreActivitiesBtn) {
        row.more_activities = moreActivitiesBtn.textContent.trim();
      }

      results.push(row);
    });

    return results;
  });
}

function dedupeRows(rows) {
  const seen = new Set();
  const deduped = [];
  for (const row of rows) {
    const key = [row.event_type, row.person_id, row.email_id, row.timestamp_relative, row.action].join('|');
    if (!seen.has(key)) {
      seen.add(key);
      deduped.push(row);
    }
  }
  return deduped;
}

function toCsv(rows) {
  const headers = [
    'event_type', 'account_name', 'account_id', 'person_name', 'person_id',
    'action', 'email_subject', 'email_id', 'cadence_name',
    'timestamp_relative', 'click_count', 'additional_recipients', 'more_activities',
  ];

  const escape = (val) => `"${(val || '').toString().replace(/"/g, '""')}"`;

  const lines = [headers.join(',')];
  for (const row of rows) {
    lines.push(headers.map((h) => escape(row[h])).join(','));
  }
  return lines.join('\n');
}

async function main() {
  const targetPage = parseInt(process.argv[2], 10);
  if (!targetPage || targetPage < 1) {
    console.error('Usage: node scrape_live_feed.js <targetPage>');
    process.exit(1);
  }

  if (!fs.existsSync(OUTPUT_DIR)) fs.mkdirSync(OUTPUT_DIR, { recursive: true });

  const targetUrl = `${LIVE_FEED_BASE_URL}?page=${targetPage}`;
  console.log(`Launching browser (persistent profile at ${PROFILE_DIR})...`);

  const context = await chromium.launchPersistentContext(PROFILE_DIR, { headless: false });
  const page = await context.newPage();

  console.log(`Navigating to page ${targetPage}...`);
  await page.goto(targetUrl, { waitUntil: 'networkidle' });

  console.log('READY_FOR_LOGIN');
  console.log('Log into Salesloft in the browser window that opened, then click "I\'ve logged in, continue" below.');
  await waitForContinueSignal();
  console.log('Continue signal received, proceeding...');

  // Re-navigate in case login redirected us elsewhere
  if (!page.url().includes('notifications-center/live-feed')) {
    await page.goto(targetUrl, { waitUntil: 'networkidle' });
  }

  console.log('Extracting events from the page...');
  const rawRows = await extractEvents(page);
  console.log(`Found ${rawRows.length} raw event cards.`);

  const rows = dedupeRows(rawRows);
  console.log(`${rows.length} unique events after dedupe.`);

  const outputFilename = `live_feed_page${targetPage}_${timestampSlug()}.csv`;
  const outputPath = path.join(OUTPUT_DIR, outputFilename);
  fs.writeFileSync(outputPath, toCsv(rows), 'utf-8');

  console.log(`Output CSV: ${outputFilename}`);
  console.log(`Done. ${rows.length} events scraped.`);

  await context.close();
}

main().catch((err) => {
  console.error('Scraper failed:', err.message || err);
  process.exit(1);
});
