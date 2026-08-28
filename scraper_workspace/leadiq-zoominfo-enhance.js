/**
 * leadiq-zoominfo-enhance.js
 *
 * ZoomInfo + LeadIQ enrichment, ported from the Python prototypes into JS
 * so it can be called directly inside sales_nav_scraper.js's
 * scrapeOneContact(), right after the Cognism step -- one CSV per run,
 * no separate pipeline / contacts.csv step needed.
 *
 * Populates the existing 'ZoomInfo Email' / 'ZoomInfo Phone' columns
 * already reserved in the scraper's CSV header, and expects two new
 * columns added to that header: 'LeadIQ Email', 'LeadIQ Phone'.
 *
 * AUTH:
 *  - ZoomInfo: short-lived session cookie + JS-runtime tokens
 *    (x-ziaccesstoken/x-ziid/x-zisession/user), copied manually from a
 *    live DevTools capture. Not retrievable from the Playwright browser
 *    context automatically -- context.cookies() can't see these.
 *  - LeadIQ: a bearer token (Authorization: Bearer <JWT>), NOT a cookie.
 *    Confirmed from real lookup/person/bulk and UnlockData calls, which
 *    carry only an authorization header and no Cookie header at all.
 *    The token is itself a JWT with a ~24h expiry (decode the exp claim
 *    to check) -- re-capture via "Copy as cURL" on a live
 *    app.leadiq.com/router.leadiq.com request when calls start 401'ing.
 *  - Both: refresh auth.json whenever calls start failing.
 *
 * LeadIQ numeric member ID -- RESOLVED and now wired through: the id used
 * as both the dict key, "id", and "linkedinId" in real lookup/person/bulk
 * payloads (e.g. "32814131") IS the actual LinkedIn member numeric id,
 * confirmed against a live capture. Callers now pass this in as
 * `linkedinMemberId` on enhanceContact()'s options -- it's the same
 * `objectUrn`-derived id already fetched for the new Cognism flow in
 * cognism-playwright.js's fetchLinkedInProfileBlob(), so both callers
 * reuse a single fetch rather than hitting LinkedIn's API twice per
 * contact. matchLeadIQ() still falls back to the old URL-slug workaround
 * when no numeric id is supplied.
 */

const fs = require('fs');

// THIRD CORRECTION, 2026-07-20 (same day, better data): the first
// zoominfo-ext.har/sales-nav-page.har pair only captured the Sales Nav
// SEARCH RESULTS page (list context), which is why the note below wrongly
// concluded v4 was dead and switched this file to v3 peopleMatchBulk. A
// second, larger capture of the actual /sales/lead/... CONTACT page --
// the flow our scraper actually uses, one contact at a time -- shows v4
// person-match/peopleMatch (singular) is alive and well there, still
// wrapped in `personBasic` exactly like the 2026-07-07 version assumed.
// So: v4 singular is back as the default for matchZoomInfo(). The v3
// bulk endpoint is real too, but it's what fires on the search-results
// LIST page specifically -- kept below as matchZoomInfoBulk() for if this
// scraper ever grows a "scrape a whole search results page at once" mode.
//
// Confirmed structural details from the fresh single-lead-page capture:
//   - Payload version bumped to "12.9.3" (was "11.44.1").
//   - Response is still `{ personBasic: {...} }`, with `company` nested
//     inside personBasic as before -- no change needed to enrichZoomInfo().
//   - The real request carries NO `cookie`, `user`, `session-token`, or
//     `x-requested-with` header at all -- just the three x-zi* tokens.
//   - origin is `chrome-extension://fofjcndophjadilglgimelemjkjblgpf` and
//     x-sourceid is `ZI_CHROME_EXTENSION`, not the `https://ro.zoominfo.com`
//     / `RO` pair the headers below previously sent (that combination
//     never once appears in either fresh capture -- it was likely from
//     browsing app.zoominfo.com directly rather than the extension itself,
//     a different auth surface). Updated ziHeaders() below to match what's
//     actually observed.
//   - Confirmed working end-to-end: two of three contacts in the capture
//     came back already `masked:false` with a real mobile number straight
//     in the match response -- no separate viewContacts reveal call fired
//     for those, since there was nothing left to unmask.
const ZI_MATCH_URL = 'https://app.zoominfo.com/ziapi/reachout-api-zios/api/v4/person-match/peopleMatch';
const ZI_MATCH_BULK_URL = 'https://app.zoominfo.com/ziapi/reachout-api-zios/api/v3/person-match/peopleMatchBulk';
const ZI_VIEW_CONTACTS_URL = 'https://app.zoominfo.com/profiles/viewContacts';
const LEADIQ_LOOKUP_URL = 'https://app.leadiq.com/api/v1/lookup/person/bulk';
const LEADIQ_GRAPHQL_URL = 'https://router.leadiq.com/graphql?operation=UnlockData';

// Captured persisted-query hash for LeadIQ's UnlockData operation.
// Re-capture from a live UnlockData call if requests start failing with
// a persisted-query-not-found error (LeadIQ frontend updates can change this).
// CONFIRMED via live HAR capture (2026-08-04, manual unlock of a test
// contact): LeadIQ rotated their persisted
// UnlockData query at some point -- the previous hash below started
// returning PERSISTED_QUERY_NOT_FOUND on every call, which silently
// looked identical to "genuinely nothing to unlock" since the response
// comes back as a 200 with `data: null` rather than an HTTP error.
// const UNLOCK_DATA_SHA256 = 'c225516b5571e3893e6b45470742d61b202ba7ba795b844dd3e3a7a9d3c325dd'; // STALE, do not use
const UNLOCK_DATA_SHA256 = 'd33a2a2c20460898ca2804686cc84ae2df513ce2f04b5f5f35975b5cd6a1e46b';

let authCache = null;

function loadAuth(authPath = './auth.json') {
  if (authCache) return authCache;
  if (!fs.existsSync(authPath)) {
    throw new Error(
      `${authPath} not found. Copy auth.example.json to auth.json and fill in ` +
      `real session tokens from a live DevTools capture before running enrichment.`
    );
  }
  authCache = JSON.parse(fs.readFileSync(authPath, 'utf8'));
  return authCache;
}

// ----------------------------------------------------------------------
// ZOOMINFO
// ----------------------------------------------------------------------

function ziHeaders(auth) {
  // Matches the real chrome-extension traffic in both fresh captures
  // (single-lead-page v4 call and search-results v3 bulk call alike) --
  // no cookie, no `user`, no session-token, no x-requested-with. If a
  // call ever starts getting rejected, re-check a live capture before
  // adding headers back rather than assuming the old RO/ro.zoominfo.com
  // set was right; that combination hasn't appeared in any capture so far.
  return {
    'content-type': 'application/json',
    'accept': 'application/json, text/plain, */*',
    'x-ziaccesstoken': auth.zoominfo['x-ziaccesstoken'],
    'x-ziid': auth.zoominfo['x-ziid'],
    'x-zisession': auth.zoominfo['x-zisession'],
    'origin': 'chrome-extension://fofjcndophjadilglgimelemjkjblgpf',
    'x-sourceid': 'ZI_CHROME_EXTENSION',
  };
}

/**
 * row/cardInfo here is the same shape scrapeOneContact already builds:
 * { 'Contact Name', 'Title / Role', 'Company Name', 'LinkedIn Profile' }
 *
 * Payload shape confirmed against a live capture of the ZoomInfo
 * extension's own peopleMatch call. A few fields the real extension sends
 * (imageUrl, companyLinkedinUrl, a full resumes[] employment history with
 * per-job companyLinkedinUrl/dates, additionalJobs) aren't available from
 * our `row` object and are left out -- they likely help match confidence
 * on ambiguous names but don't appear to be required for the endpoint to
 * respond. If matches stay unexpectedly sparse, the next thing worth
 * trying is threading the LinkedIn "Experience Description" scrape
 * through into a resumes[] array here.
 */
async function matchZoomInfo(row, salesNavUrl, auth) {
  const [firstName, ...rest] = (row['Contact Name'] || '').split(' ');
  const lastName = rest.join(' ');

  const payload = {
    firstName: firstName || '',
    middleName: '',
    lastName: lastName || '',
    fullName: row['Contact Name'] || '',
    jobTitle: row['Title / Role'] || '',
    companyName: row['Company Name'] || '',
    companyLinkedinUrl: '',
    fullAddress: '',
    externalURL: salesNavUrl || '',
    originalURL: salesNavUrl || '',
    resumes: [{
      fromDate: null,
      toDate: null,
      isPresent: true,
      jobTitle: row['Title / Role'] || '',
      companyName: row['Company Name'] || '',
      companyLinkedinUrl: '',
    }],
    getAdditionalInfo: true,
    excludeNoCompany: true,
    feature: 'linkedinSalesnavPerson',
    version: '12.9.3',
    cancelCachingForLoadTest: false,
    location: '',
    generatedConfig: false,
    isAutoRevealEnabled: true,
    buyingCommittee: { personas: [] },
    isAlternateContactDataEnabled: true,
    matchboxLatest: true,
  };

  const resp = await fetch(ZI_MATCH_URL, {
    method: 'POST',
    headers: ziHeaders(auth),
    body: JSON.stringify(payload),
  });
  if (!resp.ok) {
    const errBody = await resp.text().catch(() => '<no body>');
    throw new Error(`ZoomInfo peopleMatch failed: ${resp.status} — ${errBody.slice(0, 500)}`);
  }
  const body = await resp.json();
  if (!body.personBasic) {
    console.warn('  [warn] ZoomInfo peopleMatch returned 200 with no personBasic -- raw response keys: '
      + (Object.keys(body).length ? Object.keys(body).join(', ') : '(empty object)'));
  }
  return body.personBasic || null;
}

// ----------------------------------------------------------------------
// v3 BULK variant -- NOT currently called by enrichZoomInfo()/enhanceContact().
// This is what the extension actually uses on the Sales Nav SEARCH
// RESULTS (list) page, confirmed from a live capture: 25 contacts sent in
// one array, 12 matched back. Kept here in case this scraper ever adds a
// "scrape a whole search results page in one shot" mode -- for the
// current one-contact-at-a-time flow, matchZoomInfo() (v4 singular) above
// is the one that's actually confirmed against the real contact-page
// traffic and is what enrichZoomInfo() calls.
// ----------------------------------------------------------------------
async function matchZoomInfoBulk(rows, auth) {
  // rows: array of { row, salesNavUrl } pairs, same row shape as matchZoomInfo.
  const contacts = rows.map(({ row, salesNavUrl }) => {
    const [firstName, ...rest] = (row['Contact Name'] || '').split(' ');
    return {
      firstName: firstName || '',
      middleName: '',
      lastName: rest.join(' '),
      fullName: row['Contact Name'] || '',
      jobTitle: row['Title / Role'] || '',
      companyName: row['Company Name'] || '',
      fullAddress: '',
      externalURL: salesNavUrl || '',
      originalURL: salesNavUrl || '',
      resumes: [{
        fromDate: null,
        toDate: null,
        isPresent: true,
        jobTitle: row['Title / Role'] || '',
        companyName: row['Company Name'] || '',
        companyLinkedinUrl: '',
      }],
      getAdditionalInfo: true,
      excludeNoCompany: true,
      feature: 'linkedinSalesnavPersonSearch',
      version: '12.9.3',
      cancelCachingForLoadTest: false,
      location: '',
      generatedConfig: false,
      isAutoRevealEnabled: true,
      buyingCommittee: { personas: [] },
      isAlternateContactDataEnabled: true,
      matchboxLatest: true,
    };
  });

  const resp = await fetch(ZI_MATCH_BULK_URL, {
    method: 'POST',
    headers: ziHeaders(auth),
    body: JSON.stringify(contacts),
  });
  if (!resp.ok) {
    const errBody = await resp.text().catch(() => '<no body>');
    throw new Error(`ZoomInfo peopleMatchBulk failed: ${resp.status} — ${errBody.slice(0, 500)}`);
  }
  const body = await resp.json();
  if (!Array.isArray(body)) return [];

  // Response can be shorter than the request (unmatched contacts just get
  // dropped) -- correlate each item back to its contact by matching
  // person.socialLinks[].url against the externalURL sent, not by index.
  return contacts.map(contact => {
    const match = body.find(item =>
      (item.person?.socialLinks || []).some(l => l.url === contact.externalURL)
    );
    if (!match || !match.person) return null;
    return { ...match.person, company: match.company || {} };
  });
}

async function revealZoomInfo(personId, auth) {
  // creditSource CHANGED per the 2026-07-20 capture: the real extension's
  // viewContacts call now sends "GROW", not "REACH_OUT_20". Worth
  // double-checking this is a value tied to your ZoomInfo license/seat
  // type rather than a hardcoded extension constant, in case it turns out
  // to be account-specific.
  const payload = {
    personIds: personId,
    creditSource: 'GROW',
    rpp: 1,
    page: 1,
    unmaskEmailAndPhone: true,
    useUnifiedSearch: true,
  };
  const resp = await fetch(ZI_VIEW_CONTACTS_URL, {
    method: 'POST',
    headers: ziHeaders(auth),
    body: JSON.stringify(payload),
  });
  if (!resp.ok) {
    const errBody = await resp.text().catch(() => '<no body>');
    throw new Error(`ZoomInfo viewContacts failed: ${resp.status} — ${errBody.slice(0, 500)}`);
  }
  const body = await resp.json();
  return (body.data && body.data[0]) || null;
}

/**
 * Full ZoomInfo enrichment for one contact. Returns
 * { email, mobilePhone, directPhone, confidence } or null on no match.
 * Pass reveal=false to dry-run matching only (no credit spend).
 *
 * The live capture showed email/phone can come back already unmasked
 * directly in the match response ("masked": false) -- when that happens
 * we skip the separate reveal call entirely, since there's nothing left
 * to unmask and no reason to spend a credit on it. The reveal call only
 * fires as a fallback when the match itself came back masked.
 */
async function enrichZoomInfo(row, salesNavUrl, auth, reveal = true) {
  const person = await matchZoomInfo(row, salesNavUrl, auth);
  if (!person) return null;

  const result = {
    confidence: person.confidenceScore || null,
    personId: person.id || null,
    email: person.email || '',
    mobilePhone: person.mobilePhone || '',
    directPhone: person.company?.phone || '',
  };

  const alreadyUnmasked = person.masked === false || person.isMasked === false;

  if (!alreadyUnmasked && reveal && person.id) {
    try {
      const revealed = await revealZoomInfo(person.id, auth);
      if (revealed) {
        result.email = revealed.email || result.email;
        result.mobilePhone = revealed.mobilePhone || result.mobilePhone;
        result.directPhone = revealed.companyPhone || result.directPhone;
      }
    } catch (revealErr) {
      console.warn('  ZoomInfo reveal failed: ' + revealErr.message);
    }
  }

  return result;
}

// ----------------------------------------------------------------------
// LEADIQ
// ----------------------------------------------------------------------

function leadiqHeaders(auth) {
  return {
    'content-type': 'application/json',
    'accept': 'application/json, text/plain, */*',
    'authorization': 'Bearer ' + auth.leadiq.bearerToken,
    'origin': 'https://account.leadiq.com',
    'referer': 'https://account.leadiq.com/',
  };
}

/**
 * CONFIRMED against a live capture (2026-07-07): the request dict key /
 * `id` / `linkedinId` fields are the target's real LinkedIn numeric
 * member id (e.g. "76541094"), not a URL slug -- the file header's old
 * caveat about this being unresolved is now resolved. Pass it as
 * `linkedinMemberId` (the same `objectUrn`-derived id already fetched
 * for the Cognism flow in cognism-playwright.js -- reuse that value
 * rather than fetching it twice). Falls back to the old URL-slug
 * workaround only when the numeric id isn't available, since LeadIQ can
 * sometimes still match on `linkedinUrls` alone.
 */
async function matchLeadIQ(row, canonicalUrl, auth, linkedinMemberId) {
  const slugMatch = (canonicalUrl || '').match(/\/in\/([^/?]+)/);
  const key = linkedinMemberId || (slugMatch ? slugMatch[1] : (row['Contact Name'] || 'unknown').replace(/\s+/g, '_'));

  const payload = {
    data: {
      [key]: {
        id: key,
        name: row['Contact Name'] || '',
        linkedinId: key,
        linkedinUrls: [canonicalUrl, row['LinkedIn Profile']].filter(Boolean),
        currentCompanies: [{
          company: row['Company Name'] || '',
          title: row['Title / Role'] || '',
          link: '',
          linkedinId: '',
        }],
        source: 'LI_SALES_PROFILE',
      },
    },
  };

  const resp = await fetch(LEADIQ_LOOKUP_URL, {
    method: 'POST',
    headers: leadiqHeaders(auth),
    body: JSON.stringify(payload),
  });
  if (!resp.ok) throw new Error(`LeadIQ lookup/person/bulk failed: ${resp.status}`);
  const body = await resp.json();
  const profiles = body.profiles || {};
  return profiles[key] || null;
}

async function unlockLeadIQ(leadiqId, auth, unlockMobile = false) {
  const body = {
    operationName: 'UnlockData',
    variables: {
      input: {
        id: leadiqId,
        email: true,
        // CONFIRMED against a live phone-unlock capture: the real UI
        // requests workPhones and mobilePhones together, not mobilePhones
        // alone -- bundling them here rather than hardcoding workPhones
        // false.
        workPhones: !!unlockMobile,
        mobilePhones: !!unlockMobile,
        personalEmails: false,
      },
    },
    extensions: {
      persistedQuery: { version: 1, sha256Hash: UNLOCK_DATA_SHA256 },
    },
  };
  const resp = await fetch(LEADIQ_GRAPHQL_URL, {
    method: 'POST',
    headers: leadiqHeaders(auth),
    body: JSON.stringify(body),
  });
  if (!resp.ok) throw new Error(`LeadIQ UnlockData failed: ${resp.status}`);
  const json = await resp.json();
  if (json.errors && json.errors.length) {
    console.warn('  [debug] LeadIQ UnlockData returned GraphQL errors: ' + JSON.stringify(json.errors));
  }
  return (json.data && json.data.unlockSelectedFields) || null;
}

/**
 * Full LeadIQ enrichment for one contact. Returns
 * { email, emailStatus, mobilePhone } or null on no match.
 * Pass reveal=false to dry-run matching only (no credit spend).
 * Pass unlockMobile=true to also request/unlock the mobile number --
 * this is a separate credit spend from the email unlock, so it stays
 * opt-in.
 */
async function enrichLeadIQ(row, canonicalUrl, auth, reveal = true, unlockMobile = false, linkedinMemberId) {
  const profile = await matchLeadIQ(row, canonicalUrl, auth, linkedinMemberId);
  if (!profile) return null;

  const result = { email: '', emailStatus: '', mobilePhone: '' };

  // CONFIRMED against a live capture: "already unlocked" is a flat
  // profile.email string, not a nested profile.workEmail.type flag --
  // the old check here never matched anything real.
  if (profile.email) {
    result.email = profile.email;
    result.emailStatus = 'already_unlocked';
  }
  // Same idea for mobile, using the same field names the unlock response
  // uses (scoredMobilePhones[].value / mobilePhones[]) -- in the one
  // capture we have this was empty pre-unlock even for a contact that DID
  // have a number, so this is likely to rarely fire, but costs nothing to
  // check first.
  if (unlockMobile) {
    result.mobilePhone = profile.scoredMobilePhones?.[0]?.value || profile.mobilePhones?.[0] || '';
  }

  // NOTE (2026-08-04): previously gated the email unlock attempt on
  // `profile.availableCachedFields.includes('WORK_EMAIL')` to avoid
  // spending a credit when nothing was there. Turned out to produce false
  // negatives -- a contact skipped by this check was manually unlocked
  // seconds later in the LeadIQ UI, meaning `availableCachedFields` more
  // likely reflects what's already cached from a PRIOR lookup by someone
  // else, not whether a live unlock would succeed. LeadIQ appears to run
  // live enrichment on unlock regardless. So: always attempt the unlock
  // (same approach already used for mobile phone below, which has no
  // pre-signal at all) rather than trying to predict availability first.
  if (result.email && (!unlockMobile || result.mobilePhone)) return result;

  const leadiqId = profile.id;
  if (reveal && leadiqId) {
    try {
      const unlocked = await unlockLeadIQ(leadiqId, auth, unlockMobile);
      if (unlocked) {
        // CONFIRMED: email comes back as a flat top-level field on the
        // unlock response, same shape as the match response. companies[0]
        // duplicates it in the one capture we have, kept as a fallback.
        result.email = result.email || unlocked.email || unlocked.companies?.[0]?.email || '';
        if (result.email) result.emailStatus = result.emailStatus || 'unlocked';
        if (unlockMobile) {
          // CONFIRMED against a live phone-unlock capture:
          //   unlocked.mobilePhones      -> array of plain strings, e.g. ["+62-811-1367-257"]
          //   unlocked.scoredMobilePhones -> array of {value, score, suppression}, phone is .value
          // Prefer scoredMobilePhones since it's ranked; both are top-level
          // fields on the response, not nested under companies[] like the
          // earlier guess assumed.
          result.mobilePhone = result.mobilePhone
            || unlocked.scoredMobilePhones?.[0]?.value
            || unlocked.mobilePhones?.[0]
            || '';
          if (!result.mobilePhone) {
            console.warn('  [warn] LeadIQ unlock succeeded but no mobile phone found for this contact -- raw response:');
            console.warn(JSON.stringify(unlocked));
          }
        }
      }
    } catch (unlockErr) {
      console.warn('  LeadIQ unlock failed: ' + unlockErr.message);
    }
  }

  return result;
}

// ----------------------------------------------------------------------
// COMBINED ENTRY POINT -- call this from scrapeOneContact()
// ----------------------------------------------------------------------

/**
 * Mutates `row` in place, adding:
 *   row['ZoomInfo Email'], row['ZoomInfo Phone']
 *   row['LeadIQ Email'], row['LeadIQ Phone']   <- add these two columns
 *                                                  to the CSV header
 * Call AFTER the canonical 'LinkedIn Profile' URL has already been
 * resolved on the row (i.e. after the Cognism step in scrapeOneContact).
 *
 * reveal=false runs matching only on both providers, no credits spent --
 * use this first to sanity-check match quality before a full paid run.
 *
 * options.unlockMobile (default false) controls whether LeadIQ's mobile
 * number gets unlocked alongside the email -- this is a separate credit
 * spend on LeadIQ's side, so it's opt-in per run. Does not affect
 * ZoomInfo's reveal call, which already unmasks both email and phone
 * together (ZoomInfo doesn't meter them separately).
 */
async function enhanceContact(row, salesNavUrl, authPath = './auth.json', reveal = true, options = {}) {
  const unlockMobile = !!options.unlockMobile;
  const linkedinMemberId = options.linkedinMemberId || '';
  const auth = loadAuth(authPath);
  const canonicalUrl = row['LinkedIn Profile'] || salesNavUrl;

  try {
    const zi = await enrichZoomInfo(row, salesNavUrl, auth, reveal);
    if (zi) {
      row['ZoomInfo Email'] = zi.email || '';
      row['ZoomInfo Phone'] = zi.mobilePhone || zi.directPhone || '';
      console.log('  ZoomInfo: ' + (zi.email || `matched (confidence ${zi.confidence}), no email`));
    } else {
      console.log('  ZoomInfo: no match found');
    }
  } catch (ziErr) {
    console.warn('  ZoomInfo enrichment failed: ' + ziErr.message);
  }

  try {
    const li = await enrichLeadIQ(row, canonicalUrl, auth, reveal, unlockMobile, linkedinMemberId);
    if (li) {
      row['LeadIQ Email'] = li.email || '';
      row['LeadIQ Phone'] = li.mobilePhone || '';
      console.log('  LeadIQ: ' + (li.email || 'matched, no email available to unlock')
        + (unlockMobile ? ' | ' + (li.mobilePhone || 'no mobile available to unlock') : ''));
    } else {
      console.log('  LeadIQ: no match found');
    }
  } catch (liErr) {
    console.warn('  LeadIQ enrichment failed: ' + liErr.message);
  }

  return row;
}

module.exports = { enhanceContact, enrichZoomInfo, enrichLeadIQ, matchZoomInfoBulk };
