/**
 * sales_nav_scraper_paginated.js
 *
 * Same scraping logic as the original sales_nav_scraper.js, extended with:
 *
 *   1. PAGINATION — Sales Nav search URLs encode the page number right in the
 *      URL hash: page 1 is `#query=...&sessionId=...` and page 2+ is
 *      `#page=N&query=...&sessionId=...` (everything else identical). So
 *      instead of clicking a "Next" button, we capture that query+session
 *      string once (after you apply filters on page 1) and just rewrite the
 *      URL for each subsequent page. We stop automatically the first time a
 *      page comes back with zero contacts.
 *
 *   2. PAUSE / STOP — type `pause` + Enter to pause, `resume` + Enter to
 *      resume, or `stop` + Enter to stop. Stopping finishes whatever contact
 *      is currently being scraped, then exits cleanly.
 *
 *      This is deliberately typed-command-based rather than single-keypress
 *      (P/Q), because this script is run two ways: directly in a terminal
 *      (a real TTY), and as a subprocess from app.py with stdin wired to a
 *      pipe (no TTY at all -- raw keypress detection is impossible there,
 *      and silently does nothing). A typed command read from one persistent
 *      readline interface works identically in both cases: in a terminal
 *      you type it yourself, and from app.py the existing
 *      `/account-scraper/input/<job_id>` route can send "pause" / "resume" /
 *      "stop" as the `text` field -- no new plumbing needed on the Python
 *      side beyond what already exists for the login/filter ENTER prompts.
 *
 *   3. RESUMABLE PROGRESS — after every contact is appended to the CSV, a
 *      `progress_<AccountName>.json` checkpoint file is written with the
 *      current page number and the full list of LinkedIn URLs already
 *      completed. Run the script again with the same Account Name and it
 *      will resume from exactly where it left off, skipping anything
 *      already saved.
 *
 *      When stdin is NOT a real TTY (i.e. launched by app.py), the
 *      "Resume this run? (Y/n)" and "Max pages" prompts are skipped
 *      entirely -- it auto-resumes if a checkpoint exists and scrapes with
 *      no page limit. This matters: app.py's existing UI sends a fixed
 *      sequence of inputs for the login/filter ENTER steps, and inserting
 *      new interactive questions ahead of those would throw that sequence
 *      off by one. Set FORCE_FRESH=1 in the environment to skip resuming
 *      even if a checkpoint is found; set MAX_PAGES=<n> to cap pages when
 *      running non-interactively.
 *
 * Caveat: the saved search session is tied to your LinkedIn Sales Nav
 * session. If a lot of time has passed since the last run and LinkedIn
 * has invalidated the search/session token, delete the matching
 * progress_<AccountName>.json (or set FORCE_FRESH=1) and reapply your
 * filters fresh.
 */

const { chromium } = require('playwright');
const fs = require('fs');
const readline = require('readline');
const { enrichLead, fetchLinkedInProfileBlob } = require('./cognism-playwright');
const { enhanceContact } = require('./leadiq-zoominfo-enhance');

// ----------------------------------------------------------------------
// SHARED STDIN INTERFACE — one persistent readline interface for the
// whole run. prompt() queues a question and resolves it with the next
// line that ISN'T a recognized control command. Control commands (pause /
// resume / stop) are intercepted immediately, regardless of whether a
// prompt() is currently pending, so they can arrive at any time during
// the scrape loop, not just at the fixed points where we ask something.
// ----------------------------------------------------------------------

const isInteractiveTTY = !!process.stdin.isTTY;
const pendingPrompts = [];
let paused = false;
let stopRequested = false;
let rl = null;

function initInput() {
  rl = readline.createInterface({ input: process.stdin, terminal: isInteractiveTTY });

  rl.on('line', (rawLine) => {
    const cmd = rawLine.trim().toLowerCase();
    if (cmd === 'pause') {
      paused = true;
      console.log('\n⏸  PAUSED — type "resume" to continue, or "stop" to stop and save.');
      return;
    }
    if (cmd === 'resume') {
      paused = false;
      console.log('\n▶️  Resumed.');
      return;
    }
    if (cmd === 'stop') {
      stopRequested = true;
      console.log('\n🛑 Stop requested — finishing the current contact, then saving progress...');
      return;
    }
    const next = pendingPrompts.shift();
    if (next) next.resolve(rawLine);
    // If nothing is pending, the line is silently ignored (e.g. a stray
    // ENTER press with no question currently outstanding).
  });

  // Ctrl+C in a real terminal -- treat the same as typing "stop".
  rl.on('SIGINT', () => {
    stopRequested = true;
    console.log('\n🛑 Stop requested (Ctrl+C) — finishing the current contact, then saving progress...');
  });
}

function prompt(question) {
  if (question) process.stdout.write(question);
  return new Promise(resolve => { pendingPrompts.push({ resolve }); });
}

function closeInput() {
  if (rl) rl.close();
}

async function waitWhilePaused() {
  while (paused && !stopRequested) {
    await sleep(400);
  }
}

function sleep(ms) {
  return new Promise(resolve => setTimeout(resolve, ms));
}

function appendToCSV(filepath, row) {
  const headers = ['Source / Signal', 'Patch', 'Account Executive', 'Company Name', 'Contact Name', 'Title / Role', 'LinkedIn Profile', 'Salesloft Link', 'Interest / Role Research', 'Subject Line', 'Messaging', 'Phone', 'Email', 'First Contact', 'Last Contacted', 'Status', 'Touch Method', 'Sentiments', 'First Contact (Days)', 'Last Contact (Days)', 'Nurturing Days', 'Potential Coffee Chats', 'Reference Script', 'Whatsapp Message', 'About', 'Experience Description', 'Latest Post', 'Tenure', 'LinkedIn Phone', 'LinkedIn Email', 'Cognism Phone', 'Cognism Email', 'LeadIQ Email', 'LeadIQ Phone', 'ZoomInfo Phone', 'ZoomInfo Email'];
  const fileExists = fs.existsSync(filepath);
  if (!fileExists) {
    fs.writeFileSync(filepath, headers.join(',') + '\n');
  }
  // Coerce every cell to a string before quoting/escaping it -- see the
  // matching comment in zoominfo_scrap.js's appendToCSV for why.
  const values = headers.map(h => {
    const v = row[h];
    const s = (v === null || v === undefined) ? '' : String(v);
    return '"' + s.replace(/"/g, '""') + '"';
  });
  fs.appendFileSync(filepath, values.join(',') + '\n');
}

function sanitize(text) {
  return (text || '').trim().replace(/\s+/g, ' ');
}

function splitName(fullName) {
  const parts = sanitize(fullName).split(' ').filter(p => p);
  if (parts.length === 0) return { firstName: '', lastName: '' };
  if (parts.length === 1) return { firstName: parts[0], lastName: '' };
  return { firstName: parts[0], lastName: parts.slice(1).join(' ') };
}

async function scrollToLoadAll(page) {
  console.log('Scrolling to load all contacts...');
  let prevCount = 0;
  let stableRounds = 0;
  for (let attempt = 0; attempt < 20; attempt++) {
    await page.evaluate(() => {
      const container = document.querySelector('.overflow-x-hidden.overflow-y-auto.p4');
      if (container) container.scrollBy(0, 600);
      else window.scrollBy(0, 600);
    });
    await sleep(1500);
    const count = await page.$$eval('.ember-view .artdeco-entity-lockup__title a', els => els.length).catch(() => 0);
    console.log('  Loaded ' + count + ' items so far...');
    if (count === prevCount && count > 0) {
      stableRounds++;
      if (stableRounds >= 3) break;
    } else {
      stableRounds = 0;
    }
    prevCount = count;
  }
  await page.evaluate(() => {
    const container = document.querySelector('.overflow-x-hidden.overflow-y-auto.p4');
    if (container) container.scrollTo(0, 0);
    window.scrollTo(0, 0);
  });
  await sleep(1000);
}

/**
 * Opens a Cognism tab in the same persistent browser profile and makes
 * sure we're actually logged in before the scrape starts. Because this
 * reuses the same persistent profile dir as Sales Navigator, you only
 * need to log into Cognism manually on the very first run — it'll stay
 * logged in (until the session naturally expires after a few days) on
 * every run after that, same as Sales Navigator already does.
 */
async function ensureCognismLogin(browser) {
  console.log('Opening Cognism tab...');
  const cognismPage = await browser.newPage();
  await cognismPage.goto('https://app.cognism.com', { waitUntil: 'domcontentloaded' });

  const loggedIn = await cognismPage.evaluate(async () => {
    const res = await fetch('/api/users?force=true', { headers: { accept: 'application/json' } });
    return res.ok;
  }).catch(() => false);

  if (!loggedIn) {
    console.log('Log in to Cognism in the opened tab if needed, then press ENTER...');
    await prompt('');
  }

  return cognismPage;
}

// ----------------------------------------------------------------------
// PAGINATION HELPERS
// ----------------------------------------------------------------------

/**
 * Pulls {page, recentSearchId} out of a Sales Nav search URL.
 *
 *   page 1: https://...sales/search/people?recentSearchId=12345&sessionId=...
 *   page N: https://...sales/search/people?page=N&recentSearchId=12345&sessionId=...
 *
 * NOTE: this used to be hash-based (#query=...&sessionId=...) — LinkedIn has
 * since moved search state into regular query-string params referencing a
 * saved/recent search by ID instead of inlining the full query. `sessionId`
 * is minted fresh by LinkedIn per page load (confirmed: pasting a URL with
 * page+recentSearchId but NO sessionId still works — LinkedIn just appends
 * its own), so we deliberately do NOT capture or carry it forward. Only
 * `recentSearchId` is stable across pages and needs to be remembered.
 */
function parseSalesNavUrl(url) {
  const u = new URL(url);

  // Prefer real query-string params (newer LinkedIn format).
  let recentSearchId = u.searchParams.get('recentSearchId');
  let pageParam = u.searchParams.get('page');

  // Fall back to parsing them out of the hash fragment, e.g.
  // "#query=(...)&recentSearchId=4411362409&sessionId=..." — LinkedIn
  // still serves this format for some filter combinations, and
  // u.searchParams can't see anything after the '#'.
  if (!recentSearchId && u.hash) {
    const hashParams = new URLSearchParams(u.hash.replace(/^#/, ''));
    recentSearchId = recentSearchId || hashParams.get('recentSearchId');
    pageParam = pageParam || hashParams.get('page');

    // Third variant: the id is nested INSIDE the query param's own value
    // as recentSearchParam:(id:5826220572,doLogHistory:true), not as a
    // top-level recentSearchId key -- URLSearchParams can't see into it.
    // Match against the raw (still-encoded) hash so we don't have to
    // worry about decode edge cases in the surrounding filter blob.
    if (!recentSearchId) {
      const nested = u.hash.match(/recentSearchParam%3A%28id%3A(\d+)/);
      if (nested) recentSearchId = nested[1];
    }
  }

  return {
    page: pageParam ? parseInt(pageParam, 10) : 1,
    recentSearchId,
  };
}

function buildPageUrl(pageNum, recentSearchId) {
  const base = 'https://www.linkedin.com/sales/search/people?';
  if (pageNum <= 1) return base + 'recentSearchId=' + recentSearchId;
  return base + 'page=' + pageNum + '&recentSearchId=' + recentSearchId;
}

// ----------------------------------------------------------------------
// PROGRESS CHECKPOINT HELPERS
// ----------------------------------------------------------------------

function progressFilePath(accountName) {
  return 'progress_' + accountName.replace(/\s+/g, '_') + '.json';
}

function loadProgress(accountName) {
  const fp = progressFilePath(accountName);
  if (!fs.existsSync(fp)) return null;
  try {
    return JSON.parse(fs.readFileSync(fp, 'utf8'));
  } catch (e) {
    return null;
  }
}

function saveProgress(accountName, state) {
  fs.writeFileSync(progressFilePath(accountName), JSON.stringify(state, null, 2));
}

function deleteProgress(accountName) {
  const fp = progressFilePath(accountName);
  if (fs.existsSync(fp)) fs.unlinkSync(fp);
}

// ----------------------------------------------------------------------
// PER-PAGE CONTACT LIST EXTRACTION
// ----------------------------------------------------------------------

async function getPageContacts(page) {
  const allLinks = await page.$$('.ember-view .artdeco-entity-lockup__title a');
  const contactLinks = allLinks.slice(1);

  const salesNavUrls = [];
  for (const link of contactLinks) {
    const href = await link.getAttribute('href');
    if (href) salesNavUrls.push(href.startsWith('http') ? href : 'https://www.linkedin.com' + href);
  }

  const cardData = [];
  const cards = await page.$$('.ember-view .artdeco-entity-lockup');
  for (let i = 1; i < cards.length; i++) {
    const card = cards[i];
    const name = await card.$eval('.artdeco-entity-lockup__title a', el => el.innerText).catch(() => '');
    const role = await card.$eval('.artdeco-entity-lockup__subtitle span', el => el.innerText).catch(() => '');
    const company = await card.$eval('.artdeco-entity-lockup__caption span', el => el.innerText).catch(() => '');
    cardData.push({ name: sanitize(name), role: sanitize(role), company: sanitize(company) });
  }

  return { salesNavUrls, cardData };
}

// ----------------------------------------------------------------------
// PER-CONTACT SCRAPE (unchanged logic from the original)
// ----------------------------------------------------------------------

async function scrapeOneContact(profileTab, cognismPage, salesNavUrl, cardInfo, accountName, unlockMobile) {
  const row = {
    'Company Name': cardInfo && cardInfo.company ? cardInfo.company : accountName,
    'Contact Name': cardInfo ? cardInfo.name : '',
    'Title / Role': cardInfo ? cardInfo.role : '',
    'Source / Signal': '',
    'Patch': '',
    'Account Executive': '',

    'Experience Description': '',

    'LinkedIn Phone': '',
    'LinkedIn Email': '',
    'Cognism Phone': '',
    'Cognism Email': '',
    'ZoomInfo Phone': '',
    'ZoomInfo Email': '',
    'LinkedIn Profile': salesNavUrl || '',
    'Salesloft Link': '',
    'Interest / Role Research': '',
    'Subject Line': '',
    'Messaging': '',
    'Phone': '',
    'Email': '',
    'First Contact': '',
    'Last Contacted': '',
    'Status': '',
    'Touch Method': '',
    'Sentiments': '',
    'First Contact (Days)': '',
    'Last Contact (Days)': '',
    'Nurturing Days': '',
    'Potential Coffee Chats': '',
    'Reference Script': '',
    'Whatsapp Message': '',
    'About': '',
    'Latest Post': '',
  };

  let cognismProfileBlob = null;
  try {
    await profileTab.goto(salesNavUrl);
    await profileTab.waitForLoadState('domcontentloaded');
    await sleep(4000);

    // Fetch the Cognism profile blob now, while profileTab is still sitting
    // on the Sales Nav lead URL (fetchLinkedInProfileBlob needs that exact
    // URL shape to parse profileId/authType/authToken out of it). Cached
    // here and used later in the Cognism enrichment step below, since by
    // then profileTab has already navigated away (to the canonical
    // linkedin.com/in/ URL) and been closed.
    try {
      cognismProfileBlob = await fetchLinkedInProfileBlob(profileTab, salesNavUrl);
    } catch (blobErr) {
      console.warn('  Could not fetch LinkedIn profile blob for Cognism: ' + blobErr.message);
    }

    // Name
    const name = await profileTab.$eval('._headingText_e3b563', el => el.innerText).catch(() => '');
    if (name) row['Contact Name'] = sanitize(name);

    // Role + Company from "Current role" section
    const currentRole = await profileTab.evaluate(() => {
      const section = Array.from(document.querySelectorAll('section'))
        .find(s => s.innerText.trim().startsWith('Current role'));
      if (!section) return null;
      // First line after "Current role" is "Role at Company"
      const lines = section.innerText.split('\n').map(l => l.trim()).filter(l => l);
      const roleAtLine = lines.find(l => l.includes(' at ') && !l.includes('Also worked'));
      return roleAtLine || null;
    }).catch(() => null);

    if (currentRole) {
      const atIndex = currentRole.indexOf(' at ');
      row['Title / Role'] = sanitize(currentRole.substring(0, atIndex));
      row['Company Name'] = sanitize(currentRole.substring(atIndex + 4));
    }

    // Click Experience tab first to ensure experience section is loaded
    await profileTab.evaluate(() => {
      const expTab = document.querySelector('[id*="tab-experience-section"]');
      if (expTab) expTab.click();
    }).catch(() => {});
    await profileTab.waitForTimeout(2000);

    // Click all Show more buttons in experience section to expand descriptions
    await profileTab.evaluate(() => {
      const expSection = Array.from(document.querySelectorAll('section'))
        .find(s => (s.innerText.includes('’s experience') || s.innerText.includes('’ experience')));
      if (!expSection) return;
      Array.from(expSection.querySelectorAll('button, span'))
        .filter(el => el.innerText.trim() === 'Show more')
        .forEach(el => el.dispatchEvent(new MouseEvent('click', {bubbles: true, cancelable: true})));
    }).catch(() => {});
    await profileTab.waitForTimeout(3000);

    // Experience description — first job description from experience section
    const expDesc = await profileTab.evaluate(() => {
      const expSection = Array.from(document.querySelectorAll('section'))
        .find(s => (s.innerText.includes('’s experience') || s.innerText.includes('’ experience')));
      if (!expSection) return JSON.stringify({tenure: '', desc: ''});
      const lines = expSection.innerText.split('\n').map(l => l.trim()).filter(l => l);
      const presentIdx = lines.findIndex(l => l.includes('Present'));
      if (presentIdx === -1) return JSON.stringify({tenure: '', desc: ''});
      const tenureLine = lines[presentIdx];
      // start 3 lines back to capture role title and company regardless of layout
      const startIdx = Math.max(0, presentIdx - 3);
      const descLines = [];
      let dateCount = 0;
      for (let j = startIdx; j < lines.length; j++) {
        const line = lines[j];
        if (line.includes('Also worked')) break;
        if (line.includes('has worked for')) continue;
        if (line.includes('Summarized by AI') || line.includes('Was this helpful') || line.includes('Account insights') || line.includes('Strategic priorities') || line.includes('Business challenges') || line.includes('Competitive landscape') || line.includes('Headcount insights') || line.includes('View Relationship Map') || line.includes('Sources:') || line.includes('Share your') || line.includes('makes money') || line.includes('generates revenue') || line.includes('target market') || line.includes('Top solutions') || line.includes('[www.') || line.startsWith('http') || line.includes('Airlines and Aviation')) continue;
        if (line.includes('Show more') || line.includes('Show less')) continue;
        if (line.match(/^(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\s+\d{4}/)) {
          dateCount++;
          descLines.push(line);
          if (dateCount >= 2) break;
          continue;
        }
        descLines.push(line);
      }
      return JSON.stringify({tenure: tenureLine, desc: descLines.join(' ').trim()});
    }).catch(() => '');
    if (expDesc) {
      try {
        const parsed = JSON.parse(expDesc);
        if (parsed.tenure) row['Tenure'] = sanitize(parsed.tenure);
        if (parsed.desc) row['Experience Description'] = sanitize(parsed.desc);
      } catch(e) {
        row['Experience Description'] = sanitize(expDesc);
      }
    }

    // Email
    const email = await profileTab.$eval('[data-anonymize="email"]', el => el.innerText).catch(() => '');
    if (email) row['LinkedIn Email'] = sanitize(email);

    // Phone
    const phone = await profileTab.$eval('[data-anonymize="phone"]', el => el.innerText).catch(() => '');
    if (phone) row['LinkedIn Phone'] = sanitize(phone);

    // LinkedIn URL = Sales Nav profile URL (already set in row initialisation)

    // About section — click Show more first to expand
    await profileTab.evaluate(() => {
      const section = Array.from(document.querySelectorAll('section')).find(s => s.innerText.trim().startsWith('About'));
      if (!section) return;
      const showMore = Array.from(section.querySelectorAll('button, span')).find(el => el.innerText.trim() === 'Show more');
      if (showMore) showMore.dispatchEvent(new MouseEvent('click', {bubbles: true, cancelable: true}));
    }).catch(() => {});
    await profileTab.waitForTimeout(1000);

    const about = await profileTab.evaluate(() => {
      const section = Array.from(document.querySelectorAll('section')).find(s => s.innerText.trim().startsWith('About'));
      if (!section) return '';
      return section.innerText.replace(/^About\s*/i, '').replace(/Show less\s*$/, '').replace(/Show more\s*$/, '').trim();
    }).catch(() => '');
    if (about) row['About'] = sanitize(about);

    // Latest Post — from Recent activity section
    const latestPost = await profileTab.evaluate(() => {
      const activitySection = Array.from(document.querySelectorAll('section'))
        .find(s => s.innerText.includes('Recent activity on LinkedIn'));
      if (!activitySection) return '';
      // grab the activity section text, strip the header
      const text = activitySection.innerText
        .replace(/^.*?Recent activity on LinkedIn\s*/s, '')
        .replace(/What you share in common.*$/s, '')
        .trim();
      return text.substring(0, 1000);
    }).catch(() => '');
    if (latestPost) row['Latest Post'] = sanitize(latestPost);

    // Resolve the canonical linkedin.com/in/... URL. The Sales Nav lead
    // URL contains LinkedIn's permanent internal member ID (the string
    // between "/lead/" and the next comma) -- visiting
    // linkedin.com/in/<that ID> while logged in makes LinkedIn 302-redirect
    // to the real public profile URL. If it doesn't redirect (e.g. the ID
    // didn't parse, or LinkedIn just re-served the same URL with no
    // redirect), we keep the Sales Nav URL already sitting in the row as
    // the fallback.
    const leadIdMatch = (salesNavUrl || '').match(/\/lead\/([^,/?]+)/);
    if (leadIdMatch) {
      const requestedUrl = 'https://www.linkedin.com/in/' + leadIdMatch[1];
      try {
        await profileTab.goto(requestedUrl, { waitUntil: 'domcontentloaded' });
        await sleep(2000);
        const finalUrl = (profileTab.url() || '').split('?')[0].replace(/\/$/, '');
        if (finalUrl && finalUrl !== requestedUrl.replace(/\/$/, '')) {
          row['LinkedIn Profile'] = finalUrl;
        }
      } catch (resolveErr) {
        console.warn('  Could not resolve public LinkedIn URL: ' + resolveErr.message);
      }
    }

  } catch (err) {
    console.warn('  Warning: ' + err.message);
  }
  // Note: profileTab is intentionally NOT closed here -- it's a single
  // tab shared across every contact in the run (created once in the
  // main loop below) so LinkedIn just navigates in place instead of a
  // new tab popping up and stealing window focus for each contact.
  // It gets closed once, after the whole run finishes.

  // Cognism enrichment — runs after the LinkedIn scrape above, using
  // whatever name/company/title we ended up with (LinkedIn-derived
  // values take priority over the card-level ones, same as the row
  // already reflects at this point).
  try {
    const { firstName, lastName } = splitName(row['Contact Name']);
    if (!cognismProfileBlob) {
      console.log('  Cognism: skipped (could not fetch LinkedIn profile blob)');
    } else {
      const cognismResult = await enrichLead(cognismPage, null, {
        firstName,
        lastName,
        companyName: row['Company Name'],
        jobTitle: row['Title / Role'],
        profileUrl: salesNavUrl || '',
        canonicalLinkedinUrl: row['LinkedIn Profile'] || '',
        profileBlob: cognismProfileBlob,
      }, { redeem: true });

      if (cognismResult.matched) {
        if (cognismResult.fullData) {
          row['Cognism Email'] = cognismResult.fullData.email || '';
          row['Cognism Phone'] = cognismResult.fullData.officePhone || '';
        }
        console.log('  Cognism: ' + (row['Cognism Email'] || 'matched, no email on file'));
      } else {
        console.log('  Cognism: no match found');
      }
    }
  } catch (cognismErr) {
    console.warn('  Cognism enrichment failed: ' + cognismErr.message);
  }

  // ZoomInfo + LeadIQ enrichment — runs after Cognism, using the resolved
  // canonical LinkedIn URL set above. Set ENRICH_REVEAL=0 in the
  // environment to dry-run matching only on both providers without
  // spending unlock credits (useful for a first sanity-check run).
  try {
    const reveal = process.env.ENRICH_REVEAL !== '0';
    // Reuse the numeric LinkedIn member id out of the blob already fetched
    // for Cognism above (blob.objectUrn, e.g. "urn:li:member:76541094") --
    // LeadIQ needs this same id, no separate fetch required.
    const linkedinMemberId = (cognismProfileBlob?.objectUrn || '').replace('urn:li:member:', '');
    await enhanceContact(row, salesNavUrl, './auth.json', reveal, { unlockMobile, linkedinMemberId });
  } catch (enhanceErr) {
    console.warn('  ZoomInfo/LeadIQ enrichment failed: ' + enhanceErr.message);
  }

  return row;
}

// ----------------------------------------------------------------------
// MAIN
// ----------------------------------------------------------------------

(async () => {
  initInput();

  const accountName = await prompt('Account Name for this run: ');

  const savedProgress = loadProgress(accountName);
  let resuming = false;

  if (savedProgress && process.env.FORCE_FRESH === '1') {
    console.log('\nFound saved progress for "' + accountName + '", but FORCE_FRESH=1 is set -- starting fresh.');
  } else if (savedProgress) {
    console.log('\nFound saved progress for "' + accountName + '":');
    console.log('  Page reached: ' + savedProgress.currentPage);
    console.log('  Contacts already saved: ' + savedProgress.completedUrls.length);
    console.log('  Output CSV: ' + savedProgress.outputFile);

    if (isInteractiveTTY) {
      const ans = await prompt('Resume this run? (Y/n): ');
      resuming = ans.trim().toLowerCase() !== 'n';
    } else {
      // Non-interactive (e.g. launched by app.py) -- auto-resume rather
      // than inserting a new question into app.py's fixed input sequence.
      resuming = true;
      console.log('Running non-interactively -- auto-resuming.');
    }
  }

  let outputFile, recentSearchId, currentPage, completedUrls;

  if (resuming && savedProgress) {
    outputFile = savedProgress.outputFile;
    recentSearchId = savedProgress.recentSearchId;
    currentPage = savedProgress.currentPage;
    completedUrls = new Set(savedProgress.completedUrls);
  } else {
    outputFile = 'contacts_' + accountName.replace(/\s+/g, '_') + '_' + Date.now() + '.csv';
    recentSearchId = null; // captured after filters are applied, below
    currentPage = 1;
    completedUrls = new Set();
  }

  // Always printed as its own line, in both branches, so anything parsing
  // stdout (e.g. app.py's run_account_scraper_job) can find the output
  // file's name regardless of whether this was a fresh run or a resume.
  console.log('\nOutput CSV: ' + outputFile);
  if (resuming && savedProgress) {
    console.log('Resuming from page ' + currentPage + '...\n');
  }

  let maxPages = Infinity;
  if (isInteractiveTTY) {
    const maxPagesInput = await prompt('Max pages to scrape this run (ENTER for no limit): ');
    maxPages = maxPagesInput.trim() ? parseInt(maxPagesInput.trim(), 10) : Infinity;
  } else if (process.env.MAX_PAGES) {
    maxPages = parseInt(process.env.MAX_PAGES, 10) || Infinity;
  }

  // LeadIQ mobile-phone unlock is a separate credit spend from the email
  // unlock, so it's opt-in per run rather than always-on. In a real
  // terminal, ask directly; when launched non-interactively by app.py,
  // read it from LEADIQ_UNLOCK_MOBILE instead (set by the UI's checkbox --
  // same convention as MAX_PAGES/FORCE_FRESH above) so we don't insert a
  // new question into app.py's fixed input sequence.
  let unlockMobile = false;
  if (isInteractiveTTY) {
    const unlockAns = await prompt('Unlock LeadIQ mobile phone numbers? Uses extra LeadIQ credits (y/N): ');
    unlockMobile = unlockAns.trim().toLowerCase() === 'y';
  } else {
    unlockMobile = process.env.LEADIQ_UNLOCK_MOBILE === '1';
  }
  console.log('LeadIQ mobile phone unlock: ' + (unlockMobile ? 'ON' : 'off') + '\n');

  console.log('\nWhile running: type "pause" + Enter to pause, "resume" + Enter to continue, or "stop" + Enter (or Ctrl+C in a terminal) to stop and save.\n');
  console.log('Launching browser...\n');

  const browser = await chromium.launchPersistentContext(
    './browser_profile',
    {
      headless: false,
      viewport: { width: 1400, height: 900 },
      args: ['--disable-blink-features=AutomationControlled'],
    }
  );

  const page = await browser.newPage();
  await page.goto('https://www.linkedin.com/sales/home');

  console.log('Log in to Sales Navigator if needed, then press ENTER...');
  await prompt('');

  if (!recentSearchId) {
    console.log('Apply your Lead filters, then press ENTER...');
    await prompt('');
    const parsed = parseSalesNavUrl(page.url());
    recentSearchId = parsed.recentSearchId;
    currentPage = 1;
  } else {
    console.log('Navigating to the saved search (page ' + currentPage + ')...');
  }

  const cognismPage = await ensureCognismLogin(browser);

  // One shared tab reused for every contact, instead of opening a new
  // tab per contact -- new tabs steal window focus in headed mode,
  // which was interrupting other work happening on screen.
  const profileTab = await browser.newPage();

  console.log('Starting scrape...\n');

  let pagesScrapedThisRun = 0;

  pageLoop:
  while (true) {
    if (stopRequested) break;
    if (pagesScrapedThisRun >= maxPages) {
      console.log('\nReached the max-pages limit for this run (' + maxPages + '). Progress is saved — re-run to continue from page ' + currentPage + '.');
      break;
    }

    const pageUrl = buildPageUrl(currentPage, recentSearchId);
    console.log('\n=== Page ' + currentPage + ' ===');
    await page.goto(pageUrl, { waitUntil: 'domcontentloaded' });
    await page.waitForSelector('.ember-view .artdeco-entity-lockup__title a', { timeout: 15000 }).catch(() => {
      console.log('  (warning: no contact cards appeared within 15s — page may be empty or slow)');
    });
    await sleep(3000);
    await scrollToLoadAll(page);

    const { salesNavUrls, cardData } = await getPageContacts(page);

    if (salesNavUrls.length === 0) {
      console.log('No contacts found on page ' + currentPage + ' — reached the end of results.');
      break;
    }

    console.log('Found ' + salesNavUrls.length + ' contacts on page ' + currentPage + '.\n');

    saveProgress(accountName, {
      accountName, outputFile, recentSearchId, currentPage,
      completedUrls: Array.from(completedUrls), updatedAt: new Date().toISOString(),
    });

    for (let i = 0; i < salesNavUrls.length; i++) {
      if (stopRequested) break pageLoop;
      await waitWhilePaused();
      if (stopRequested) break pageLoop;

      const url = salesNavUrls[i];
      if (completedUrls.has(url)) {
        continue; // already saved before a previous pause/stop
      }

      console.log('Processing contact ' + (i + 1) + ' of ' + salesNavUrls.length + ' on page ' + currentPage + ' (total saved so far: ' + completedUrls.size + ')...');

      const row = await scrapeOneContact(profileTab, cognismPage, url, cardData[i], accountName, unlockMobile);
      appendToCSV(outputFile, row);
      completedUrls.add(url);

      console.log('  Saved: ' + (row['Contact Name'] || '(unknown)') + ' | ' + (row['Title / Role'] || '-') + ' | ' + (row['Company Name'] || '-'));

      saveProgress(accountName, {
        accountName, outputFile, recentSearchId, currentPage,
        completedUrls: Array.from(completedUrls), updatedAt: new Date().toISOString(),
      });

      await sleep(2500 + Math.random() * 1500);
    }

    currentPage++;
    pagesScrapedThisRun++;
    saveProgress(accountName, {
      accountName, outputFile, recentSearchId, currentPage,
      completedUrls: Array.from(completedUrls), updatedAt: new Date().toISOString(),
    });

    await sleep(3000 + Math.random() * 2000);
  }

  try {
    await profileTab.close();
  } catch (closeErr) {
    // Already closed or unreachable -- nothing more to do here.
  }

  closeInput();

  if (stopRequested) {
    console.log('\nStopped. ' + completedUrls.size + ' contacts saved to: ' + outputFile);
    console.log('Progress saved — re-run with the same Account Name ("' + accountName + '") to continue from page ' + currentPage + '.');
  } else {
    console.log('\nDone! ' + completedUrls.size + ' contacts saved to: ' + outputFile);
    deleteProgress(accountName); // run fully completed, no need to keep the checkpoint
  }

  await cognismPage.close();
  await browser.close();
  process.exit(0);
})();
