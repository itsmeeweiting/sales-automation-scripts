/**
 * cognism-playwright.js
 *
 * Enrich LinkedIn Sales Navigator leads via Cognism, run directly inside
 * Playwright pages that are logged into app.cognism.com and
 * linkedin.com/sales. No extension, no manual cookie copy/paste.
 *
 * ---- 2026-07-07 REWRITE ----
 * The old match+redeem flow (`/api/graph/ce/person/match?bulk=true` +
 * `/api/anansi/person/{id}/redeem`) does not appear ANYWHERE in two full
 * HAR captures of real Cognism extension usage against live LinkedIn
 * profiles -- it's very likely dead/replaced. The real flow captured is:
 *
 *   1. Fetch the target's own LinkedIn profile JSON via LinkedIn's own
 *      internal Sales Nav API (salesApiProfiles), which needs a page
 *      that's on linkedin.com (for cookies/csrf), NOT the Cognism tab.
 *   2. gzip-compress + base64-encode that JSON blob.
 *   3. POST it to Cognism's `/api/anansi/person/view`, along with the
 *      target's LinkedIn numeric member id (`liurn`, pulled out of the
 *      profile blob's own `objectUrn`) and their canonical linkedin.com/in/
 *      profile URL.
 *   4. The response comes back with real, already-unmasked contact data
 *      in `emails[].handle` / `phone_numbers_filtered.officePhoneNumber.number`
 *      -- no separate reveal/redeem step observed in the captures.
 *
 * Confirmed structurally against a live capture (works end-to-end,
 * returned two real verified emails for a real contact) but NOT yet
 * verified against more than one contact, and it's UNVERIFIED whether
 * `force:true` in the request always spends a Cognism credit or whether
 * there's a cheaper preview mode -- every capture we have used
 * `force:true`, so that's what this always sends. If that turns out to
 * burn credits on every single call, the `reveal` option below currently
 * has no way to avoid that cost (there's no known free-preview call for
 * this new flow yet) -- flag this if credit usage looks off.
 *
 * The old v1 matchPerson()/redeemPerson() functions are kept below for
 * reference/rollback but are no longer called by enrichLead().
 *
 * ---- ONE-TIME SETUP ----
 * Run this once to log in and save your session:
 *   node cognism-playwright.js --login
 * A browser window opens. Log into Cognism (Okta SSO included) as you
 * normally would, then come back to the terminal and press Enter.
 * This saves a session file to ./cognism-state.json.
 *
 * Note: your Cognism session cookie has an expiry of a few days (it's a
 * normal login session, not a permanent token), so you'll need to rerun
 * --login periodically — if openCognismTab() throws "Not logged in",
 * that's your sign to redo this step.
 *
 * ---- USAGE IN YOUR SCRAPER ----
 *
 *   const { chromium } = require('playwright');
 *   const { openCognismTab, enrichLead } = require('./cognism-playwright');
 *
 *   const browser = await chromium.launch();
 *   const cognismContext = await browser.newContext({ storageState: './cognism-state.json' });
 *   const cognismPage = await openCognismTab(cognismContext);
 *
 *   for (const lead of leads) {
 *     // `page` must be a Playwright page currently on linkedin.com, logged
 *     // into Sales Navigator -- needed to fetch the target's own profile
 *     // blob before it gets forwarded to Cognism.
 *     const result = await enrichLead(cognismPage, page, lead, { redeem: true });
 *     // merge result.fullData.email / result.fullData.officePhone into your record
 *   }
 *
 *   await cognismPage.close();
 *   await cognismContext.close();
 */

const zlib = require('zlib');

const COGNISM_URL = "https://app.cognism.com";

/**
 * Opens a tab on app.cognism.com within an existing browser context and
 * confirms the session is actually logged in before handing it back.
 */
async function openCognismTab(browserContext) {
  const page = await browserContext.newPage();
  await page.goto(COGNISM_URL, { waitUntil: "domcontentloaded" });

  const loggedIn = await page.evaluate(async () => {
    const res = await fetch("/api/users?force=true", { headers: { accept: "application/json" } });
    return res.ok;
  });

  if (!loggedIn) {
    throw new Error(
      "Not logged into Cognism in this browser context. Run `node cognism-playwright.js --login` first."
    );
  }

  return page;
}

/**
 * One-time interactive login helper. Launches a visible browser, lets you
 * log in manually, then saves the session to disk for reuse.
 */
async function loginAndSaveState(statePath = "./cognism-state.json") {
  const { chromium } = require("playwright");
  const browser = await chromium.launch({ headless: false });
  const context = await browser.newContext();
  const page = await context.newPage();
  await page.goto(COGNISM_URL);

  console.log("Log into Cognism in the opened browser window (Okta SSO included if prompted).");
  console.log("Once you see your normal Cognism dashboard, come back here and press Enter.");
  await new Promise((resolve) => process.stdin.once("data", resolve));

  await context.storageState({ path: statePath });
  console.log(`Saved login session to ${statePath}`);
  await browser.close();
}

// ----------------------------------------------------------------------
// NEW FLOW (2026-07-07) -- confirmed via live HAR capture
// ----------------------------------------------------------------------

// Full field list captured from a live request. Requesting fields we
// don't strictly need (memberBadges, latestTouchPointActivity, etc.)
// since trimming it risks the response shape no longer matching what
// Cognism expects to receive back in the forwarded blob.
const SALES_PROFILE_DECORATION =
  '(entityUrn,objectUrn,firstName,lastName,fullName,headline,memberBadges,' +
  'latestTouchPointActivity,pronoun,degree,profileUnlockInfo,location,' +
  'listCount,summary,savedLead,defaultPosition,contactInfo,crmStatus,' +
  'pendingInvitation,unlocked,flagshipProfileUrl,fullNamePronunciationAudio,' +
  'memorialized,numOfConnections,numOfSharedConnections,showTotalConnectionsPage,' +
  'positions*(companyName,current,new,description,endedOn,posId,startedOn,title,' +
  'location,richMedia*,companyUrn~fs_salesCompany(entityUrn,name,companyPictureDisplayImage)),' +
  'crmManualMatched)';

/**
 * Fetches the target's own LinkedIn profile JSON via LinkedIn's internal
 * Sales Nav API -- this is the blob Cognism's extension gzips and
 * forwards to itself. Must run from a `page` that's on linkedin.com and
 * logged into Sales Navigator (NOT the Cognism tab -- this is a
 * same-origin call to linkedin.com).
 *
 * profileUrl needs to be a resolved Sales Nav lead/people URL in the
 * form https://www.linkedin.com/sales/(people|lead)/<profileId>,<authType>,<authToken>
 * -- exactly what resolveToSalesNavUrl()/searchSalesNav() already
 * produce elsewhere in this pipeline.
 */
// JS's encodeURIComponent deliberately leaves ( ) * ! ' unescaped per spec
// (they're in the "unreserved" set) -- but a live capture confirmed
// LinkedIn's real request has these percent-encoded (%28 for '(' etc),
// and LinkedIn's parser 400s without it. This wraps encodeURIComponent to
// also escape those five characters, matching the real captured request
// byte-for-byte.
function strictEncodeURIComponent(str) {
  return encodeURIComponent(str).replace(/[!'()*]/g, c => '%' + c.charCodeAt(0).toString(16).toUpperCase());
}

async function fetchLinkedInProfileBlob(page, profileUrl) {
  const m = (profileUrl || '').match(/\/sales\/(?:people|lead)\/([^,]+),([^,]+),([^/?]+)/);
  if (!m) throw new Error(`profileUrl didn't match the expected /sales/people|lead/<id>,<type>,<token> shape: ${profileUrl}`);
  const [, profileId, authType, authToken] = m;
  const url = `https://www.linkedin.com/sales-api/salesApiProfiles/(profileId:${profileId},authType:${authType},authToken:${authToken})?decoration=${strictEncodeURIComponent(SALES_PROFILE_DECORATION)}`;

  return page.evaluate(async (url) => {
    // csrf-token is derived from LinkedIn's own JSESSIONID cookie --
    // standard Voyager-API pattern, no separate token to capture/store.
    const jsessionCookie = document.cookie.split('; ').find(c => c.trim().startsWith('JSESSIONID='));
    const csrfToken = jsessionCookie ? decodeURIComponent(jsessionCookie.split('=')[1]).replace(/"/g, '') : '';
    if (!csrfToken) {
      throw new Error('No JSESSIONID cookie found on this page (document.cookie had no JSESSIONID= entry) -- cannot build csrf-token');
    }
    const res = await fetch(url, {
      headers: {
        accept: 'application/json',
        'csrf-token': csrfToken,
        'x-restli-protocol-version': '2.0.0',
      },
    });
    if (!res.ok) {
      const bodyText = await res.text().catch(() => '<no body>');
      throw new Error(`salesApiProfiles failed: ${res.status} ${res.statusText} -- ${bodyText.slice(0, 300)}`);
    }
    return res.json();
  }, url);
}

/**
 * The real match+reveal call, confirmed against a live capture. Returns
 * Cognism's raw response object (profile_score, emails[], phone_numbers,
 * phone_numbers_filtered, id, redeemed, etc.) or null if the profile
 * blob couldn't be fetched.
 *
 * lead needs: profileUrl (resolved Sales Nav URL) and ideally
 * canonicalLinkedinUrl (the plain linkedin.com/in/<slug> URL) -- the
 * captured request sent the canonical URL in the `linkedin` field, not
 * the Sales Nav one. Falls back to profileUrl if canonicalLinkedinUrl
 * isn't available.
 */
async function matchPersonViaProfileBlob(cognismPage, page, lead) {
  const blob = lead.profileBlob || await fetchLinkedInProfileBlob(page, lead.profileUrl);
  if (!blob) return null;

  const liurn = (blob.objectUrn || '').replace('urn:li:member:', '');
  const jsonField = zlib.gzipSync(Buffer.from(JSON.stringify(blob))).toString('base64');
  const linkedinUrl = lead.canonicalLinkedinUrl || lead.profileUrl || '';

  return cognismPage.evaluate(async ({ liurn, linkedinUrl, jsonField }) => {
    const res = await fetch('/api/anansi/person/view', {
      method: 'POST',
      headers: {
        'content-type': 'application/json',
        'x-cognism-client': 'CE',
        'x-cognism-client-version': '4.0.26',
      },
      body: JSON.stringify({
        force: true,
        fullName: '',
        liurn,
        version: 'CE:4.0.26',
        source: 'SalesNavigator',
        linkedin: linkedinUrl,
        json: jsonField,
      }),
    });
    if (!res.ok) throw new Error(`person/view failed: ${res.status}`);
    return res.json();
  }, { liurn, linkedinUrl, jsonField });
}

// ----------------------------------------------------------------------
// OLD v1 FLOW -- kept for reference/rollback, no longer called by
// enrichLead() below. Neither endpoint appears in either of the two live
// HAR captures we have, so this is likely dead.
// ----------------------------------------------------------------------

/**
 * Step 1: free preview match — runs inside the Cognism tab's own JS context.
 * lead = { firstName, lastName, companyName, jobTitle, profileUrl, linkedinCompanyId?, country?, city? }
 */
async function matchPerson(cognismPage, lead) {
  return cognismPage.evaluate(async (lead) => {
    const res = await fetch("/api/graph/ce/person/match?bulk=true", {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({
        country: lead.country || "",
        city: lead.city || "",
        jobTitle: lead.jobTitle || "",
        firstName: lead.firstName || "",
        lastName: lead.lastName || "",
        companyName: lead.companyName || "",
        telephone: [],
        emails: [],
        profileUrl: lead.profileUrl || "",
        companyId: lead.linkedinCompanyId || "",
        source: "LinkedIn-SN",
      }),
    });
    if (res.status === 204) return null;
    if (!res.ok) throw new Error(`match failed: ${res.status}`);
    return res.json();
  }, lead);
}

/**
 * Step 2: the actual unlock — also runs inside the Cognism tab's context.
 */
async function redeemPerson(cognismPage, personId, comId, companyName, jobTitle) {
  return cognismPage.evaluate(
    async ({ personId, comId, companyName, jobTitle }) => {
      const params = new URLSearchParams({ id: personId, comId, companyName: companyName || "", jobTitle: jobTitle || "" });
      const res = await fetch(`/api/anansi/person/${personId}/redeem?${params}`);
      if (!res.ok) throw new Error(`redeem failed: ${res.status}`);
      return res.json();
    },
    { personId, comId, companyName, jobTitle }
  );
}

/**
 * Convenience wrapper: fetches the target's LinkedIn profile blob and
 * forwards it to Cognism's person/view endpoint, then normalizes the
 * response. `page` must be a Playwright page on linkedin.com/sales
 * (logged into Sales Navigator) -- see file header for why.
 *
 * There's no confirmed free-preview call for this new flow (every
 * capture we have used force:true), so `redeem: false` currently just
 * skips the Cognism call entirely rather than running a cheaper
 * dry-run -- there's nothing cheaper to run yet.
 */
async function enrichLead(cognismPage, page, lead, { redeem = false } = {}) {
  if (!redeem) return { matched: null, skipped: 'no free-preview call confirmed for this flow yet' };

  const result = await matchPersonViaProfileBlob(cognismPage, page, lead);
  if (!result) return { matched: false };

  const summary = {
    matched: true,
    cognismPersonId: result.id,
    alreadyRedeemed: result.redeemed,
    profileScore: result.profile_score,
  };

  return {
    ...summary,
    fullData: {
      email: result.emails?.[0]?.handle || null,
      allEmails: (result.emails || []).map(e => e.handle).filter(Boolean),
      officePhone: result.phone_numbers_filtered?.officePhoneNumber?.number || null,
      allPhones: result.phone_numbers || [],
    },
  };
}

module.exports = {
  openCognismTab,
  loginAndSaveState,
  enrichLead,
  // new flow
  fetchLinkedInProfileBlob,
  matchPersonViaProfileBlob,
  // old v1 flow, kept for reference
  matchPerson,
  redeemPerson,
};

// ---- CLI entry point ----
if (require.main === module) {
  if (process.argv.includes("--login")) {
    loginAndSaveState().then(() => process.exit(0));
  } else {
    console.log("Usage:");
    console.log("  node cognism-playwright.js --login    (one-time login setup)");
    console.log("Otherwise, require this module from your scraper script — see the");
    console.log("usage example in the comment block at the top of this file.");
  }
}
