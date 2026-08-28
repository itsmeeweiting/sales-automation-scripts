/**
 * zoominfo-lib.js
 *
 * Shared helpers for scraping ZoomInfo's People Search via its internal
 * GraphQL endpoint (POST /profiles/graphql/personSearch) instead of the DOM.
 *
 * How it works:
 *   - You log in and apply filters manually in a real browser tab.
 *   - We listen for the requests your browser already makes:
 *       1. The "list" request (no personIds)      -> search results page
 *       2. The "detail/reveal" request (personIds  -> clicking a contact
 *          + unmaskEmailAndPhone: true)               card to reveal them
 *   - We capture the query/variables/headers from those two real requests
 *     and replay them ourselves (with `page` and `personIds` swapped) to
 *     page through all results and reveal each contact, without touching
 *     the DOM at all.
 *
 * Requires Node 18+ (uses the global `fetch`).
 */

const fs = require('fs');
const readline = require('readline');

// ─── Persistent browser profile launcher ───────────────────────────────────
//
// Two separate scripts/options can each open their own browser, and the
// Flask UI lets both be started from the same page -- so a profile-folder
// collision (one already in use by another still-running job) is a real
// possibility, not just a theoretical one. Wrap launchPersistentContext so
// that specific, recoverable case prints one clear line and exits cleanly,
// instead of Playwright's full verbose launch log crashing as an uncaught
// exception.

async function launchProfile(chromium, profileDir, options, label) {
  try {
    return await chromium.launchPersistentContext(profileDir, options);
  } catch (err) {
    const msg = String(err && err.message || err);
    if (msg.includes('Opening in existing browser session') || msg.includes('already in use')) {
      console.error(
        '\nERROR: The ' + (label || profileDir) + ' browser profile (' + profileDir + ') is already ' +
        'open in another window or job.\n' +
        'Close that browser window / stop the other job first, then try again.\n'
      );
      process.exit(1);
    }
    throw err; // anything else: let it surface normally
  }
}

// ─── Master CSV schema ────────────────────────────────────────────────────

const MASTER_HEADERS = [
  'Source / Signal', 'Patch', 'Account Executive', 'Company Name', 'Contact Name',
  'Title / Role', 'LinkedIn Profile', 'Salesloft Link', 'Interest / Role Research',
  'Subject Line', 'Messaging', 'Phone', 'Email', 'First Contact', 'Last Contacted',
  'Status', 'Touch Method', 'Sentiments', 'First Contact (Days)', 'Last Contact (Days)',
  'Nurturing Days', 'Potential Coffee Chats', 'Reference Script', 'Whatsapp Message',
  'About', 'Experience Description', 'Latest Post', 'Tenure', 'LinkedIn Phone',
  'LinkedIn Email', 'Cognism Phone', 'Cognism Email', 'LeadIQ Email', 'LeadIQ Phone',
  'ZoomInfo Phone', 'ZoomInfo Email',
];

function writeCSVRow(filepath, rowObj) {
  const fileExists = fs.existsSync(filepath);
  if (!fileExists) {
    fs.writeFileSync(filepath, MASTER_HEADERS.join(',') + '\n');
  }
  const values = MASTER_HEADERS.map(h => '"' + String(rowObj[h] || '').replace(/"/g, '""') + '"');
  fs.appendFileSync(filepath, values.join(',') + '\n');
}

// ─── Small utilities ────────────────────────────────────────────────────────

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

// ─── Fallback reveal query (no click required) ─────────────────────────────
//
// This is the exact GraphQL document ZoomInfo's own "reveal" button sends
// (captured from a real reveal click earlier). The query shape is part of
// their API contract, not their DOM — far more stable than a CSS selector —
// so we can ship it as a built-in fallback instead of requiring you to
// click a contact card just to teach the script what it looks like.
//
// The auth headers are session-level, not call-specific — the same
// x-ziaccesstoken/x-ziid/x-zisession from your results page works for the
// reveal call too, we just swap the `apollographql-client-name` label to
// match what a real reveal call sends.

const DEFAULT_DETAIL_QUERY = `query personSearch($searchFacadeParams: PersonArgs, $includeIsEmailUnsubscribed: Boolean!) {
  personSearch(searchFacadeParams: $searchFacadeParams) {
    primary: data {
      entityId: personID
      doziIndustry {
        displayName
        name
        isPrimary
        score
      }
      topLevelIndustry
      title
      jobFunction
      orgChartTier
      orgChartJobFunction {
        department
        departmentId
        jobFunction
        jobFunctionId
      }
      employmentHistory {
        companyName
        from
        to
        jobFunction
        title
        level
        companyID
        companyWebsite
      }
      education {
        school
        degree {
          areaOfStudy
          degree
        }
      }
      webReference {
        description
        title
        url
        date
      }
      boardMember {
        from
        to
        jobFunction
        title
        level
        company {
          id
          name
          tagged
          masked
          subscribed
          exported
          description
          domain
          logo
          fax
          phone
          ticker
          crmEntityId
          address {
            Street
            City
            State
            Zip
            street
            city
            state
            zip
            country
            latitude
            longitude
            CountryCode
          }
          displayAddress
          employeeCount
          revenue
          doziIndustry {
            displayName
            name
            isPrimary
            score
          }
          doziIndustryString
          revenueRange
          isDefunct
          uniqueCompanyNumContacts
          companyHref
          isInPreview
          funding {
            amountIn000s
            date
            type
            investors {
              companyName
            }
          }
          departmentBudgets {
            departmentType
            budgetAmount
          }
          certified
          certificationDate
          icpScore
          locationsCount
          ultimateParent: basicUltimateParent
          orgImport
        }
      }
      personBiography
      socialUrls {
        socialMedia {
          socialNetworkType
          socialNetworkUrl
        }
      }
      socialUrlsParsed {
        linkedin
        facebook
        twitter
        youtube
      }
      followerCountParsed {
        linkedin
        facebook
        twitter
        youtube
      }
      foundedYear
      alexaRank
      directPhoneIsDoNotCall
      mobilePhoneIsDoNotCall
      emailBlockedReason
      directPhoneBlockedReason
      mobilePhoneBlockedReason
      personalEmailBlockedReason
      company {
        id
      }
      importedData {
        date
        owners {
          key
          value {
            date
            crmEntityId
            ownerId
            ownerName
          }
        }
      }
      personHashtags
      isEmailUnsubscribed @include(if: $includeIsEmailUnsubscribed)
    }
    basic: data {
      name
      id: personID
      image: profileImageURL
      firstName
      lastName
      email
      phone
      personalEmail
      timezone
      mobilePhone
      businessEmailBlocked: emailBlocked
      personalEmailBlocked
      mobilePhoneBlocked
      directPhoneBlocked
      emailBlockedReason
      directPhoneBlockedReason
      mobilePhoneBlockedReason
      personalEmailBlockedReason
      masked: isMasked
      tagged: isTagged
      hasLeadIndicator
      leadStatus
      isTracked
      title
      lastUpdateDate: lastUpdatedDate
      lastMentioned
      confidence: confidenceScore
      orgUniversalTagged {
        tagName
        value
      }
      universalTagged {
        tagName
        value
      }
      noticeProvidedInfo {
        emailNoticeProvidedDate
      }
      buyingCommittee
      socialUrls {
        socialMedia {
          socialNetworkType
          socialNetworkUrl
        }
      }
      socialUrlsParsed {
        linkedin
        facebook
        twitter
        youtube
      }
      isUnEmployed: isPast
      companyID
      companyLogo
      companyName
      companyAddress {
        Street
        City
        State
        Zip
        CountryCode
        street
        city
        state
        zip
        country
        latitude
        longitude
      }
      companyRevenue
      companyRevenueRange
      companyEmployees
      companyEmployeeCountRange
      companyDomain
      companyPhone
      companyPhoneBlocked
      companyPhoneBlockedReason
      companyDescription
      companyFax
      companyRevenueIn000s
      companySIC
      companyNAICS
      companyTicker
      topLevelIndustry
      industry
      doziIndustry {
        displayName
        name
        isPrimary
        score
      }
      creationDate
      positionStartDate
      hasOnlinePresence
      publicSourcedData {
        dataType
        urls
      }
      directPhoneIsDoNotCall
      mobilePhoneIsDoNotCall
      personHashtags
      address: location {
        city: City
        country: CountryCode
        state: State
        street: Street
        zip: Zip
        metroArea: metroArea
      }
      isEmailUnsubscribed @include(if: $includeIsEmailUnsubscribed)
      alternativeAttributes {
        alternativeEmails {
          address
          sources
          score
        }
        alternativeDirectPhones {
          number
          sources
          score
        }
        alternativeMobilePhones {
          number
          sources
          score
        }
        attributeDefaultSort {
          Emails
          MobilePhones
        }
      }
    }
  }
}
`;

function buildDefaultDetailTemplate(listCapture) {
  const headers = Object.assign({}, listCapture.headers, { 'apollographql-client-name': 'HierarchyClient' });
  return {
    query: DEFAULT_DETAIL_QUERY,
    headers,
    variables: {
      searchFacadeParams: {
        personIds: '',
        page: 1,
        rpp: 1,
        excludeBoardMembers: false,
        excludeNoCompany: false,
        useUnifiedSearch: true,
        outputFieldOptions: 'd_address_street,d_address_city,d_address_country,d_address_metroarea,d_address_region,d_address_postal,d_resume,timezone,org_chart_tier,d_education,d_primary_title,job_function,d_reference-other,d_reference-news,d_reference-corp,social_urls,d_external_url,founding_year,d_primary_title,alexa_rank,person_automated_bio,person_biography',
        fetchLeadIndicator: false,
        fetchLeadStatus: false,
        unmaskEmailAndPhone: true,
      },
      includeIsEmailUnsubscribed: false,
    },
  };
}

// ─── Request capture ────────────────────────────────────────────────────────
//
// Attach this to a live Playwright `page` BEFORE the person logs in /
// navigates. It passively records the most recent "list" request and the
// most recent "detail/reveal" request it sees — last-write-wins, so by the
// time the person presses ENTER it reflects whatever filters/contact they
// actually used, with no hardcoded filter values on our side.

function summarizeFilters(params) {
  const bits = [];
  if (params.companyIdQuery) {
    try {
      const values = (params.companyIdQuery.longTermList && params.companyIdQuery.longTermList.values) || [];
      const names = values.map(v => v.displayName || v.value).join(', ');
      if (names) bits.push('company=' + names);
    } catch (e) { /* ignore */ }
  }
  if (params.rpp !== undefined) bits.push('rpp=' + params.rpp);
  if (params.hasEmail) bits.push('hasEmail=' + params.hasEmail);
  if (params.hasMobilePhone) bits.push('hasMobilePhone=' + params.hasMobilePhone);
  return bits.length ? bits.join(', ') : '(no company/contact filters detected on this request — probably not your results page)';
}

function attachCapture(page, captured) {
  page.on('request', (request) => {
    if (request.method() !== 'POST') return;
    if (!request.url().includes('/profiles/graphql/personSearch')) return;

    let body;
    try { body = JSON.parse(request.postData() || '{}'); } catch (e) { return; }
    if (body.operationName !== 'personSearch') return;

    const params = (body.variables && body.variables.searchFacadeParams) || {};
    const entry = { query: body.query, variables: body.variables, headers: request.headers() };

    if (params.personIds) {
      captured.detail = entry;
      console.log('  [capture] reveal request seen → personIds=' + params.personIds);
      return;
    }

    // ZoomInfo's frontend hits this same endpoint from other UI surfaces too
    // (typeahead suggestions, "recommended contacts" widgets, etc.) — those
    // come back with no `rpp` set. Only treat it as your actual results
    // page when `rpp` is present, so an unrelated widget firing AFTER your
    // real search can't silently overwrite the capture we actually want.
    if (params.rpp === undefined) {
      console.log('  [capture] ignored a personSearch request with no rpp (likely a suggestions/widget call, not your results page)');
      return;
    }

    captured.list = entry;
    console.log('  [capture] search request seen → ' + summarizeFilters(params));
  });
}

function missingCaptures(captured) {
  const missing = [];
  if (!captured.list) missing.push('search-results request (reload the results page once)');
  return missing;
}

// ─── Header allow-list ──────────────────────────────────────────────────────
//
// Captured headers include HTTP/2 pseudo-headers (":method", ":path", ...)
// and browser-managed ones (content-length, accept-encoding, sec-fetch-*)
// that must NOT be set manually on a fresh fetch() call. We keep only the
// ones that are actually part of ZoomInfo's auth/app contract.

const HEADER_ALLOWLIST = [
  'accept', 'apollographql-client-name', 'content-type',
  'origin', 'referer', 'session-token', 'user', 'user-agent',
  'x-requested-with', 'x-sourceid', 'x-ziaccesstoken', 'x-ziid', 'x-zisession',
];

function buildSearchHeaders(rawHeaders) {
  const out = {};
  for (const key of HEADER_ALLOWLIST) {
    if (rawHeaders[key] !== undefined) out[key] = rawHeaders[key];
  }
  return out;
}

// ─── Variable builders ──────────────────────────────────────────────────────
//
// Deep-clone the captured template and only override what needs to change.
// Everything else (filters, outputFieldOptions, etc.) is whatever your
// browser actually sent — so it always matches your real filter setup.

function buildListVariables(template, pageNum) {
  const v = JSON.parse(JSON.stringify(template));
  v.searchFacadeParams.page = pageNum;
  return v;
}

function buildDetailVariables(template, personId) {
  const v = JSON.parse(JSON.stringify(template));
  v.searchFacadeParams.personIds = String(personId);
  v.searchFacadeParams.page = 1;
  v.searchFacadeParams.rpp = 1;
  v.searchFacadeParams.unmaskEmailAndPhone = true;
  return v;
}

// ─── Raw API call ───────────────────────────────────────────────────────────

async function fetchPersonSearch(headers, query, variables) {
  const res = await fetch('https://app.zoominfo.com/profiles/graphql/personSearch', {
    method: 'POST',
    headers,
    body: JSON.stringify({ operationName: 'personSearch', variables, query }),
  });

  if (res.status === 401 || res.status === 403) {
    throw new Error('ZoomInfo session expired/unauthorized (HTTP ' + res.status + '). Re-run the script and log in again.');
  }
  if (!res.ok) {
    const text = await res.text().catch(() => '');
    throw new Error('HTTP ' + res.status + (text ? ' — ' + text.slice(0, 200) : ''));
  }

  const json = await res.json();
  if (json.errors) {
    throw new Error('GraphQL error: ' + JSON.stringify(json.errors).slice(0, 300));
  }
  return json;
}

// ─── Row mapping ─────────────────────────────────────────────────────────────

function mapListRowToPartial(d) {
  const country = (d.companyAddress && d.companyAddress.CountryCode) || '';
  const linkedin = (d.socialUrlsParsed && d.socialUrlsParsed.linkedin) || '';
  const hasMaskedData = Boolean(d.email || d.mobilePhone || d.phone);

  return {
    'Patch': country,
    'Company Name': d.companyName || '',
    'Contact Name': d.name || [d.firstName, d.lastName].filter(Boolean).join(' '),
    'Title / Role': d.title || d.jobTitle || '',
    'LinkedIn Profile': linkedin,
    _personID: d.personID,
    _needsUnmask: hasMaskedData,
  };
}

function mapDetailToPartial(basic) {
  return {
    'ZoomInfo Phone': basic.mobilePhone || basic.phone || '',
    'ZoomInfo Email': basic.email || basic.personalEmail || '',
  };
}

// ─── Orchestration ───────────────────────────────────────────────────────────
//
// 1. Pages through the "list" call until all results are collected.
// 2. Confirms with you (onConfirm) before spending any reveal credits.
// 3. For each contact that has *something* to reveal, calls the "detail"
//    query with unmaskEmailAndPhone: true.
// 4. Calls onRow(row) for every contact (revealed or not) and also returns
//    the full array, so a caller can chain further enrichment (e.g. LinkedIn).
//
// Skips the reveal call entirely for contacts with no phone/email on file
// at all (list call already shows `null` for both) — saves credits.

async function scrapeZoomInfo({ captured, onConfirm, onRow }) {
  const listHeaders = buildSearchHeaders(captured.list.headers);
  const listQuery = captured.list.query;
  const listTemplate = captured.list.variables;

  // Prefer a live-captured reveal request if you happened to click a card;
  // otherwise fall back to the built-in one — no click required either way.
  const detailSource = captured.detail || buildDefaultDetailTemplate(captured.list);
  const detailHeaders = buildSearchHeaders(detailSource.headers);
  const detailQuery = detailSource.query;
  const detailTemplate = detailSource.variables;
  console.log(captured.detail
    ? 'Using the reveal request captured from your click.\n'
    : 'No contact card was clicked — using the built-in reveal request.\n');

  const rpp = (listTemplate.searchFacadeParams && listTemplate.searchFacadeParams.rpp) || 25;

  console.log('\nUsing captured filters → ' + summarizeFilters(listTemplate.searchFacadeParams || {}));
  console.log('(If that doesn\'t match what you filtered for, Ctrl+C now and re-run — reload your results page right before pressing ENTER.)\n');

  // ── Step 1: page through the list call ──
  console.log('Fetching contact list...\n');
  let pageNum = 1;
  let totalResults = null;
  const allRows = [];

  while (true) {
    const vars = buildListVariables(listTemplate, pageNum);
    const json = await fetchPersonSearch(listHeaders, listQuery, vars);
    const result = json && json.data && json.data.personSearch;

    if (!result) {
      console.warn('  [warn] Unexpected response shape on page ' + pageNum + ' — stopping.');
      break;
    }
    if (totalResults === null) totalResults = result.totalResults || 0;

    const data = result.data || [];
    allRows.push(...data);
    console.log('  [page ' + pageNum + '] +' + data.length + ' (running total: ' + allRows.length + ' / ' + totalResults + ')');

    if (data.length === 0 || data.length < rpp || allRows.length >= totalResults) break;
    pageNum += 1;
    await sleep(800 + Math.random() * 700);
  }

  console.log('\nFound ' + allRows.length + ' contacts.\n');

  // ── Step 2: decide who needs revealing, confirm before spending credits ──
  const partials = allRows.map(mapListRowToPartial);
  const needUnmask = partials.filter(p => p._needsUnmask);

  console.log(needUnmask.length + ' of ' + partials.length + ' contacts have phone/email on file to reveal.');
  console.log('Each reveal uses 1 ZoomInfo credit.');
  await onConfirm('\nPress ENTER to start revealing, or Ctrl+C to cancel...');

  // ── Step 3: reveal + emit rows ──
  let revealed = 0;
  const completedRows = [];

  for (let i = 0; i < partials.length; i++) {
    const p = partials[i];
    console.log('\n[' + (i + 1) + '/' + partials.length + '] ' + p['Contact Name'] + ' | ' + p['Title / Role']);

    let detailPartial = {};
    if (p._needsUnmask) {
      try {
        const vars = buildDetailVariables(detailTemplate, p._personID);
        const json = await fetchPersonSearch(detailHeaders, detailQuery, vars);
        const basic = json && json.data && json.data.personSearch &&
                      json.data.personSearch.basic && json.data.personSearch.basic[0];
        if (basic) {
          detailPartial = mapDetailToPartial(basic);
          revealed += 1;
        }
      } catch (err) {
        console.warn('  [warn] Reveal failed: ' + err.message);
      }
      await sleep(1200 + Math.random() * 1200);
    } else {
      console.log('  [skip] No phone/email on file — not revealing.');
    }

    const row = { 'Source / Signal': 'ZoomInfo', ...p, ...detailPartial };
    delete row._personID;
    delete row._needsUnmask;

    completedRows.push(row);
    if (onRow) await onRow(row);
  }

  console.log('\n──────────────────────────────────────');
  console.log('ZoomInfo phase done. ' + completedRows.length + ' contacts, ' + revealed + ' revealed.');
  console.log('──────────────────────────────────────\n');

  return completedRows;
}

module.exports = {
  MASTER_HEADERS,
  writeCSVRow,
  prompt,
  sleep,
  sanitize,
  launchProfile,
  attachCapture,
  missingCaptures,
  summarizeFilters,
  buildSearchHeaders,
  buildListVariables,
  buildDetailVariables,
  fetchPersonSearch,
  mapListRowToPartial,
  mapDetailToPartial,
  scrapeZoomInfo,
};
