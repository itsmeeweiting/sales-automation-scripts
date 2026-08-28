/**
 * linkedin-salesnav-lib.js
 *
 * Extracted, unmodified, from your existing zoominfo_scrap.js — reused as
 * a module so zoominfo-linkedin-scrap.js can call the same tested logic
 * for resolving a LinkedIn URL to its Sales Navigator profile, falling
 * back to a name+company search, and scraping the Sales Nav profile.
 */

const readline = require('readline');

// ─── Utilities ────────────────────────────────────────────────────────────────

function prompt(question) {
  const rl = readline.createInterface({ input: process.stdin, output: process.stdout });
  return new Promise(resolve => rl.question(question, ans => { rl.close(); resolve(ans); }));
}

function sleep(ms) {
  return new Promise(resolve => setTimeout(resolve, ms));
}

function sanitize(text) {
  return (text || '').trim().replace(/\s+/g, ' ');
}

// ─── Resolve LinkedIn URL → Sales Nav URL via "View in Sales Navigator" ──────
//
// Sales Nav profile URLs need a numeric member ID (e.g. /sales/people/ACoAA...).
// The vanity slug alone doesn't work. The most reliable way to get the real URL
// is to load the regular linkedin.com/in/<slug> page and click the button.

async function resolveToSalesNavUrl(browser, linkedinUrl) {
  if (!linkedinUrl) return null;
  if (linkedinUrl.includes('linkedin.com/sales/')) return linkedinUrl;

  const tab = await browser.newPage();
  try {
    // Attempt up to 2 times — LinkedIn occasionally aborts requests due to
    // automation detection; a brief pause + retry usually succeeds
    let lastErr = null;
    for (let attempt = 1; attempt <= 2; attempt++) {
      try {
        if (attempt > 1) {
          console.log('  [retry] Attempt ' + attempt + ' for: ' + linkedinUrl);
          await sleep(3000 + Math.random() * 2000);
        }
        await tab.goto(linkedinUrl, { waitUntil: 'domcontentloaded', timeout: 20000 });
        await sleep(3000);

        // The "View in Sales Navigator" button is an <a> with the full Sales Nav
        // URL (including numeric member ID) directly in its href — no click needed.
        const salesNavHref = await tab.$$eval('a[href*="/sales/people/"]', anchors => {
          const match = anchors.find(a => a.href.includes('/sales/people/'));
          return match ? match.href : null;
        }).catch(() => null);

        if (salesNavHref) {
          console.log('  [route] Sales Nav href found: ' + salesNavHref);
          return salesNavHref;
        }

        console.warn('  [warn] No Sales Nav people link found on: ' + linkedinUrl);
        return null;

      } catch (err) {
        lastErr = err;
        console.warn('  [warn] Attempt ' + attempt + ' failed: ' + err.message.split('\n')[0]);
      }
    }

    console.warn('  [warn] All attempts failed for: ' + linkedinUrl);
    return null;
  } finally {
    await tab.close().catch(() => {});
  }
}

// ─── Sales Nav keyword search fallback ───────────────────────────────────────

async function searchSalesNav(page, fullName, companyName) {
  console.log('  [search] "' + fullName + '" at "' + companyName + '"');
  const query = encodeURIComponent(fullName + (companyName ? ' ' + companyName : ''));
  const searchUrl = 'https://www.linkedin.com/sales/search/people?query=(keywords:' + query + ')';
  await page.goto(searchUrl, { waitUntil: 'domcontentloaded' });
  await sleep(3000);

  const firstLink = await page.$('.artdeco-entity-lockup__title a');
  if (!firstLink) {
    console.warn('  [search] No results found.');
    return null;
  }
  const href = await firstLink.getAttribute('href').catch(() => null);
  if (!href) return null;
  return href.startsWith('http') ? href : 'https://www.linkedin.com' + href;
}

// ─── Sales Nav profile scraper ────────────────────────────────────────────────

async function scrapeProfile(browser, profileUrl) {
  const tab = await browser.newPage();
  const result = {};

  try {
    await tab.goto(profileUrl, { waitUntil: 'domcontentloaded' });
    await sleep(4000);

    // Name
    const name = await tab.$eval('._headingText_e3b563', el => el.innerText).catch(() => '');
    if (name) result['Contact Name'] = sanitize(name);

    // Current role + company
    const currentRole = await tab.evaluate(() => {
      const section = Array.from(document.querySelectorAll('section'))
        .find(s => s.innerText.trim().startsWith('Current role'));
      if (!section) return null;
      const lines = section.innerText.split('\n').map(l => l.trim()).filter(l => l);
      const roleAtLine = lines.find(l => l.includes(' at ') && !l.includes('Also worked'));
      return roleAtLine || null;
    }).catch(() => null);

    if (currentRole) {
      const atIndex = currentRole.indexOf(' at ');
      result['Title / Role'] = sanitize(currentRole.substring(0, atIndex));
      result['Company Name'] = sanitize(currentRole.substring(atIndex + 4));
    }

    // Click Experience tab
    await tab.evaluate(() => {
      const expTab = document.querySelector('[id*="tab-experience-section"]');
      if (expTab) expTab.click();
    }).catch(() => {});
    await tab.waitForTimeout(2000);

    // Expand Show more buttons in experience section
    await tab.evaluate(() => {
      const expSection = Array.from(document.querySelectorAll('section'))
        .find(s => s.innerText.trim().split('\n')[0].toLowerCase().endsWith("'s experience")
                || s.innerText.trim().split('\n')[0].toLowerCase().endsWith("\u2019s experience"));
      if (!expSection) return;
      Array.from(expSection.querySelectorAll('button, span'))
        .filter(el => el.innerText.trim() === 'Show more')
        .forEach(el => el.dispatchEvent(new MouseEvent('click', { bubbles: true, cancelable: true })));
    }).catch(() => {});
    await tab.waitForTimeout(3000);

    // Experience description + tenure
    const expDesc = await tab.evaluate(() => {
      // Section header is "<Name>'s experience" — match by end of first line
      const expSection = Array.from(document.querySelectorAll('section'))
        .find(s => {
          const first = s.innerText.trim().split('\n')[0].toLowerCase();
          return first.endsWith("'s experience") || first.endsWith("\u2019s experience");
        });
      if (!expSection) return JSON.stringify({ tenure: '', desc: '' });
      const lines = expSection.innerText.split('\n').map(l => l.trim()).filter(l => l);

      // Tenure line contains en-dash + Present: e.g. "Aug 2023\u2013Present  2 yrs 11 mos"
      // Also handle hyphen variant just in case
      const isPresent = l => /\u2013\s*Present|\u2014\s*Present|–\s*Present|—\s*Present|-\s*Present/.test(l);
      const presentIdx = lines.findIndex(isPresent);
      if (presentIdx === -1) return JSON.stringify({ tenure: '', desc: '' });

      const tenureLine = lines[presentIdx];

      // Walk BACK from tenure line to collect: role title, company, total tenure
      // These appear in 2-4 lines above depending on profile layout
      const noiseWords = [
        'has worked for', 'Summarized by AI', 'Was this helpful',
        'Account insights', 'Strategic priorities', 'Business challenges',
        'Competitive landscape', 'Headcount insights', 'View Relationship Map',
        'Sources:', 'Share your', 'makes money', 'generates revenue',
        'target market', 'Top solutions', 'Show more', 'Show less',
      ];
      const isNoise = l => noiseWords.some(w => l.includes(w)) || l.startsWith('www.') || l.startsWith('http');
      const isTenure = l => /\d{4}/.test(l) && /\u2013|–|—|-/.test(l);

      // Collect header lines (role, company, total tenure) above presentIdx
      const headerLines = [];
      for (let j = presentIdx - 1; j >= 1; j--) {
        const line = lines[j];
        if (isNoise(line)) continue;
        // Stop if we hit another tenure line (means we've gone too far back)
        if (isTenure(line) && !isPresent(line)) break;
        headerLines.unshift(line);
        if (headerLines.length >= 4) break;
      }

      // Collect description lines AFTER tenure line until next role starts
      const descLines = [];
      let hitLocation = false;
      for (let j = presentIdx + 1; j < lines.length; j++) {
        const line = lines[j];
        if (isNoise(line)) continue;
        // Skip location line (short, no digits, right after tenure)
        if (!hitLocation && !line.match(/\d/) && line.length < 60) {
          hitLocation = true;
          continue;
        }
        // Stop when we hit the NEXT role's tenure line (non-present date range)
        if (isTenure(line) && !isPresent(line)) {
          // Include the next role title + company + this date as the stop marker
          // Walk back to grab next role's title (1-2 lines before this date)
          const nextRoleLines = [];
          for (let k = j - 1; k >= presentIdx + 1; k--) {
            const prev = lines[k];
            if (isNoise(prev)) continue;
            if (!prev.match(/\d/) || prev.length > 10) nextRoleLines.unshift(prev);
            if (nextRoleLines.length >= 2) break;
          }
          descLines.push('');
          nextRoleLines.forEach(l => descLines.push(l));
          descLines.push(line);
          break;
        }
        descLines.push(line);
      }

      const fullDesc = [...headerLines, tenureLine, ...descLines].join('\n').trim();
      return JSON.stringify({ tenure: tenureLine, desc: fullDesc });
    }).catch(() => '');

    if (expDesc) {
      try {
        const parsed = JSON.parse(expDesc);
        if (parsed.tenure) result['Tenure'] = sanitize(parsed.tenure);
        if (parsed.desc)   result['Experience Description'] = sanitize(parsed.desc);
      } catch (e) {
        result['Experience Description'] = sanitize(expDesc);
      }
    }

    // LinkedIn email + phone (Sales Nav surfaces these)
    const liEmail = await tab.$eval('[data-anonymize="email"]', el => el.innerText).catch(() => '');
    if (liEmail) result['LinkedIn Email'] = sanitize(liEmail);
    const liPhone = await tab.$eval('[data-anonymize="phone"]', el => el.innerText).catch(() => '');
    if (liPhone) result['LinkedIn Phone'] = sanitize(liPhone);

    // About section
    await tab.evaluate(() => {
      const section = Array.from(document.querySelectorAll('section'))
        .find(s => s.innerText.trim().startsWith('About'));
      if (!section) return;
      const showMore = Array.from(section.querySelectorAll('button, span'))
        .find(el => el.innerText.trim() === 'Show more');
      if (showMore) showMore.dispatchEvent(new MouseEvent('click', { bubbles: true, cancelable: true }));
    }).catch(() => {});
    await tab.waitForTimeout(1000);

    const about = await tab.evaluate(() => {
      const section = Array.from(document.querySelectorAll('section'))
        .find(s => s.innerText.trim().startsWith('About'));
      if (!section) return '';
      return section.innerText
        .replace(/^About\s*/i, '')
        .replace(/Show less\s*$/, '')
        .replace(/Show more\s*$/, '')
        .trim();
    }).catch(() => '');
    if (about) result['About'] = sanitize(about);

    // Latest post from Recent activity section
    const latestPost = await tab.evaluate(() => {
      const activitySection = Array.from(document.querySelectorAll('section'))
        .find(s => s.innerText.includes('Recent activity on LinkedIn'));
      if (!activitySection) return '';
      const text = activitySection.innerText
        .replace(/^.*?Recent activity on LinkedIn\s*/s, '')
        .replace(/What you share in common.*$/s, '')
        .trim();
      return text.substring(0, 1000);
    }).catch(() => '');
    if (latestPost) result['Latest Post'] = sanitize(latestPost);

  } finally {
    await tab.close().catch(() => {});
  }

  return result;
}

module.exports = { prompt, sleep, sanitize, resolveToSalesNavUrl, searchSalesNav, scrapeProfile };
