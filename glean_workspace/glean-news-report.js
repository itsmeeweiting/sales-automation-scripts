// glean-news-report.js
//
// DIFFERENT USE CASE from glean-pg-tracker-automation.js -- that script
// sends one prompt PER CONTACT ROW to fill in specific tracker columns.
// This script attaches your whole news-scrape CSV as a FILE and sends
// ONE prompt asking Glean to assess signal strength across it.
//
// Rebuilt based on a real HAR capture of a manual Glean chat with a CSV
// attached (2026-06-28). That capture showed:
//   - file attachment is a real, working mechanism (upload -> poll until
//     processed -> reference by ID in the chat message)
//   - Glean's AUTO agent, given an attached CSV + a "give me a report"
//     style prompt, chose to run a "spreadsheet analysis workflow" and
//     returned a genuine multi-tab Excel workbook artifact, not just
//     plain text -- so this script downloads that workbook directly
//     rather than trying to reconstruct a report from chat bubbles.
//
// Because the file goes up as an attachment (not pasted into the prompt
// as text), there's no batching/chunking needed -- a 300-account CSV
// uploads the same way as a 6-row one.
//
// What's reused as-is from the contact script (proven, unchanged):
//   - the persistent Playwright login session (./glean-profile)
//   - randomId(), looksLikeMetaAcknowledgement(), stripTrailingRule()
//   - the core shape of extractDeliverables() (extended below to also
//     capture artifact entityId/mimeType, not just frag.text, since a
//     spreadsheet artifact carries its content as a download reference,
//     not as streamed text)
//
// What's new:
//   - uploadFileToGlean() / pollUntilProcessed() -- the file attachment
//     flow, not present in the contact script at all
//   - sendGleanPrompt() takes an optional uploadedFileIds list
//   - downloadChatFile() -- pulls the resulting workbook's bytes back
//     through the same authenticated browser session
//
// Fix (2026-07): Glean's AUTO agent doesn't always return a downloadable
// workbook -- sometimes it writes the report as a PAPER/canvas artifact
// instead, whose content streams back as inline text fragments (no
// entityId to download). The old artifact loop only handled the
// has-an-entityId case and skipped anything else, which silently dropped
// the entire canvas write-up and left the .md with only narration bubbles
// ("I'm loading...", "I've turned this into..."). Now both artifact
// shapes are captured: entityId -> download as before; inline body with
// no entityId -> written directly into the .md as the deliverable.
//
// Setup: same as the contact script, but csv-parse is NOT needed here
// (the file goes up as raw bytes, never parsed in Node) --
//   npm install playwright
//   (run from glean_workspace/ so it shares the same logged-in browser
//   profile as glean-pg-tracker-automation.js)
//
// Usage:
//   node glean-news-report.js path/to/news.csv [output_basename] ["custom prompt"]

const fs = require('fs');
const path = require('path');
const { chromium } = require('playwright');

const USER_DATA_DIR = './glean-profile'; // shared with glean-pg-tracker-automation.js
const CLIENT_VERSION = 'fe-release-2026-06-23-6cefe13'; // matches the proven HAR capture
const POLL_TIMEOUT_MS = 60000;
const POLL_INTERVAL_MS = 1500;

const DEFAULT_PROMPT = 'Based on this news report, help me analyse and assess which is high, medium, low signal. For each of the key signals, help me with why anything, why MongoDB, why now, key use case and persona.';

const CSV_PATH = process.argv[2];
if (!CSV_PATH) {
  console.error('Usage: node glean-news-report.js path/to/news.csv [output_basename] ["custom prompt"]');
  process.exit(1);
}

function deriveOutputBase(inputPath) {
  return path.join(path.dirname(inputPath), path.basename(inputPath, path.extname(inputPath)) + '-glean-report');
}
// Two fixed-ish output paths: a .md summary (always written) and a
// workbook (written only if Glean's response included one). Using a
// caller-specified basename keeps this predictable for app.py to find,
// rather than relying on whatever filename Glean's agent happened to
// generate this run.
const OUTPUT_BASE = process.argv[3] || deriveOutputBase(CSV_PATH);
const OUTPUT_MD_PATH = OUTPUT_BASE + '.md';
const OUTPUT_WORKBOOK_PATH = OUTPUT_BASE + '.xlsx';
const PROMPT_TEXT = process.argv[4] || DEFAULT_PROMPT;

function randomId(length = 16) {
  const chars = 'ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789';
  let out = '';
  for (let i = 0; i < length; i++) out += chars[Math.floor(Math.random() * chars.length)];
  return out;
}

function sleep(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

// Reads the CSV's raw bytes (no parsing needed -- it goes up as-is) and
// posts it as multipart/form-data, matching the exact field name ("files")
// and per-part Content-Type ("text/csv") seen in the HAR capture.
async function uploadFileToGlean(page, filePath) {
  const fileBuffer = fs.readFileSync(filePath);
  const base64 = fileBuffer.toString('base64');
  const filename = path.basename(filePath);

  return await page.evaluate(async ({ base64, filename, clientVersion }) => {
    function base64ToBlob(b64, mime) {
      const byteChars = atob(b64);
      const byteNumbers = new Array(byteChars.length);
      for (let i = 0; i < byteChars.length; i++) byteNumbers[i] = byteChars.charCodeAt(i);
      return new Blob([new Uint8Array(byteNumbers)], { type: mime });
    }
    try {
      const blob = base64ToBlob(base64, 'text/csv');
      const formData = new FormData();
      formData.append('files', blob, filename);

      const res = await fetch(
        `https://mongodb-be.glean.com/api/v1/uploadchatfiles?clientVersion=${clientVersion}&locale=en`,
        { method: 'POST', credentials: 'include', body: formData }
      );
      const bodyText = await res.text();
      return { status: res.status, statusText: res.statusText, body: bodyText };
    } catch (err) {
      return { status: 0, statusText: 'FETCH_ERROR', body: JSON.stringify({ errorName: err.name, errorMessage: err.message }) };
    }
  }, { base64, filename, clientVersion: CLIENT_VERSION });
}

// Polls getchatfiles until the upload's status flips from PROCESSING to
// PROCESSED (seen taking ~1 second for a small CSV in the HAR capture;
// a larger file may take longer, hence the poll loop rather than a fixed
// single check).
async function pollUntilProcessed(page, fileId) {
  const start = Date.now();
  while (Date.now() - start < POLL_TIMEOUT_MS) {
    const result = await page.evaluate(async ({ fileId, clientVersion }) => {
      try {
        const res = await fetch(
          `https://mongodb-be.glean.com/api/v1/getchatfiles?clientVersion=${clientVersion}&locale=en`,
          {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            credentials: 'include',
            body: JSON.stringify({ fileIds: [fileId] }),
          }
        );
        return { status: res.status, body: await res.text() };
      } catch (err) {
        return { status: 0, body: JSON.stringify({ errorName: err.name, errorMessage: err.message }) };
      }
    }, { fileId, clientVersion: CLIENT_VERSION });

    if (result.status === 200) {
      try {
        const parsed = JSON.parse(result.body);
        const status = parsed.files?.[fileId]?.metadata?.status;
        if (status === 'PROCESSED') return { ok: true };
        if (status === 'FAILED' || status === 'ERROR') return { ok: false, error: `file processing status: ${status}` };
        // else still PROCESSING -- keep polling
      } catch {
        // malformed response -- keep polling rather than failing outright
      }
    }
    await sleep(POLL_INTERVAL_MS);
  }
  return { ok: false, error: 'timed out waiting for file to finish processing' };
}

// Same call shape as glean-pg-tracker-automation.js, extended with an
// optional uploadedFileIds list on the message when a file is attached.
async function sendGleanPrompt(page, promptText, uploadedFileIds, clientVersion) {
  return await page.evaluate(async ({ text, tabId, sessionTrackingToken, uploadedFileIds, clientVersion }) => {
    const agentConfig = {
      agent: 'AUTO',
      toolSets: { enableCompanyTools: true, enableWebSearch: true },
      useCanvas: false, // NOTE: AUTO can still choose a PAPER/canvas-style
                        // artifact even with this off -- see the capture
                        // fix in the artifact loop below, which no longer
                        // depends on this flag to find the content.
      useImageGeneration: false,
      clientCapabilities: {
        artifacts: { allowedArtifactTypes: ['PAPER', 'HTML_CODE', 'SKILL', 'SLIDE', 'IMAGE', 'SPREADSHEET'] },
        canRenderImages: true,
        canRenderVariants: true,
        hasBrowserOperator: false,
      },
    };
    const nowIso = new Date().toISOString();
    const message = { agentConfig, author: 'USER', fragments: [{ text }], messageType: 'CONTENT', ts: nowIso };
    if (uploadedFileIds && uploadedFileIds.length > 0) {
      message.uploadedFileIds = uploadedFileIds;
    }
    const payload = {
      agentConfig,
      background: true, // matches the proven HAR capture for file-attached chats
      clientTools: [],
      messages: [message],
      saveChat: true,
      sourceInfo: { feature: 'CHAT', initiator: 'USER', platform: 'WEB', hasCopyPaste: false, isDebug: false },
      stream: true,
      sc: '',
      sessionInfo: {
        lastSeen: nowIso,
        sessionTrackingToken,
        tabId,
        lastQuery: text,
        clickedInJsSession: true,
        firstEngageTsSec: Math.floor(Date.now() / 1000),
      },
    };
    try {
      const res = await fetch(
        `https://mongodb-be.glean.com/api/v1/chat?timezoneOffset=-480&clientVersion=${clientVersion}&locale=en`,
        { method: 'POST', headers: { 'Content-Type': 'text/plain' }, credentials: 'include', body: JSON.stringify(payload) }
      );
      const bodyText = await res.text();
      return { status: res.status, statusText: res.statusText, body: bodyText };
    } catch (err) {
      return { status: 0, statusText: 'FETCH_ERROR', body: JSON.stringify({ errorName: err.name, errorMessage: err.message }) };
    }
  }, { text: promptText, tabId: randomId(), sessionTrackingToken: randomId(), uploadedFileIds: uploadedFileIds || [], clientVersion });
}

// Extended from glean-pg-tracker-automation.js's version: a spreadsheet
// artifact carries its content as a download reference (entityId +
// mimeType + name across separate fragments), not as frag.text, so those
// fields are captured too, merged across all fragments sharing the same
// messageId -- the same merge pattern the original already used for
// frag.text, just with more fields.
function extractDeliverables(rawBody) {
  const lines = rawBody.split('\n').map((l) => l.trim()).filter(Boolean);
  let chatId = null;
  let order = 0;
  const contentByMsgId = new Map();
  const artifactByMsgId = new Map();
  const artifacts = [];

  for (const line of lines) {
    let obj;
    try {
      obj = JSON.parse(line);
    } catch {
      continue;
    }
    if (obj.chatId) chatId = obj.chatId;

    for (const msg of obj.messages || []) {
      if (msg.author !== 'GLEAN_AI') continue;

      if (msg.messageType === 'CONTENT') {
        let entry = contentByMsgId.get(msg.messageId);
        if (!entry) {
          entry = { order: order++, text: '' };
          contentByMsgId.set(msg.messageId, entry);
        }
        for (const frag of msg.fragments || []) {
          if (typeof frag.text === 'string') entry.text += frag.text;
        }
      } else if (typeof msg.messageType === 'string' && msg.messageType.startsWith('ARTIFACT_')) {
        let entry = artifactByMsgId.get(msg.messageId);
        if (!entry) {
          entry = { order: order++, type: msg.messageType, name: null, entityId: null, mimeType: null, body: '' };
          artifactByMsgId.set(msg.messageId, entry);
          artifacts.push(entry);
        }
        for (const frag of msg.fragments || []) {
          if (frag.artifact) {
            if (frag.artifact.name) entry.name = frag.artifact.name;
            if (frag.artifact.entityId) entry.entityId = frag.artifact.entityId;
            if (frag.artifact.mimeType) entry.mimeType = frag.artifact.mimeType;
          }
          if (typeof frag.text === 'string') entry.body += frag.text;
        }
      }
    }
  }

  const contentSegments = [...contentByMsgId.values()]
    .sort((a, b) => a.order - b.order)
    .map((e) => e.text);
  artifacts.sort((a, b) => a.order - b.order);

  return { chatId, contentSegments, artifacts };
}

function looksLikeMetaAcknowledgement(text) {
  const opening = text.trim().slice(0, 60).toLowerCase();
  return /^(i['’]ve|i['’]m|i have|i am|i drafted|i pulled|i tailored|i grounded|i prepared|i kept|i based|using (your|my))\b/.test(opening);
}

function stripTrailingRule(s) {
  return s.replace(/\n?-{3,}\s*$/, '').trim();
}

// Plain-text portion of the response -- usually short narration bubbles
// when the real deliverable is a downloaded workbook (see main loop
// below), but kept as a companion summary either way.
function extractReportText(extracted) {
  const candidates = [
    ...extracted.contentSegments,
  ]
    .filter((t) => t && t.trim().length > 20)
    .map((t) => stripTrailingRule(t.trim()));
  return candidates.join('\n\n');
}

// Pulls an artifact's bytes back through the same authenticated session
// -- this is exactly the /downloadchatfile/{entityId} endpoint referenced
// in the artifact's own metadata.
async function downloadChatFile(page, entityId, clientVersion) {
  return await page.evaluate(async ({ entityId, clientVersion }) => {
    try {
      const res = await fetch(
        `https://mongodb-be.glean.com/api/v1/downloadchatfile/${entityId}?clientVersion=${clientVersion}&locale=en`,
        { method: 'GET', credentials: 'include' }
      );
      if (res.status !== 200) return { status: res.status, base64: null };
      const buf = await res.arrayBuffer();
      const bytes = new Uint8Array(buf);
      let binary = '';
      for (let i = 0; i < bytes.length; i++) binary += String.fromCharCode(bytes[i]);
      return { status: res.status, base64: btoa(binary) };
    } catch (err) {
      return { status: 0, base64: null, error: err.message };
    }
  }, { entityId, clientVersion });
}

(async () => {
  // Clear any stale workbook from a previous run so a run that doesn't
  // produce one this time doesn't leave a misleadingly old file behind.
  if (fs.existsSync(OUTPUT_WORKBOOK_PATH)) fs.unlinkSync(OUTPUT_WORKBOOK_PATH);

  const context = await chromium.launchPersistentContext(USER_DATA_DIR, { headless: false });
  const page = await context.newPage();
  await page.goto('https://app.glean.com');

  console.log('Waiting for Glean to finish loading (log in by hand now if prompted)...');
  await page.waitForSelector('.ql-editor', { timeout: 120000 });
  console.log('Glean loaded, proceeding.\n');

  const roughRowCount = Math.max(fs.readFileSync(CSV_PATH, 'utf8').split('\n').filter(Boolean).length - 1, 0);
  console.log(`Uploading ${path.basename(CSV_PATH)} (~${roughRowCount} rows) to Glean...`);

  const uploadResult = await uploadFileToGlean(page, CSV_PATH);
  if (uploadResult.status !== 200) {
    console.error('Upload FAILED, status:', uploadResult.status, uploadResult.statusText);
    console.error(uploadResult.body.slice(0, 500));
    await context.close();
    process.exit(1);
  }

  let fileId;
  try {
    fileId = JSON.parse(uploadResult.body).files[0].id;
  } catch (e) {
    console.error('Could not parse upload response:', e.message);
    console.error(uploadResult.body.slice(0, 500));
    await context.close();
    process.exit(1);
  }
  console.log(`Uploaded, file id: ${fileId} -- waiting for processing...`);

  const processed = await pollUntilProcessed(page, fileId);
  if (!processed.ok) {
    console.error('File processing FAILED:', processed.error);
    await context.close();
    process.exit(1);
  }
  console.log('File processed. Sending prompt...\n');

  const result = await sendGleanPrompt(page, PROMPT_TEXT, [fileId], CLIENT_VERSION);
  if (result.status !== 200) {
    console.error('Chat FAILED, status:', result.status, result.statusText);
    console.error(result.body.slice(0, 500));
    await context.close();
    process.exit(1);
  }

  const extracted = extractDeliverables(result.body);
  console.log('Artifacts found:', extracted.artifacts.map((a) => `${a.type} (${a.name || a.entityId || 'no ref'})`));
  console.log('Content segment lengths:', extracted.contentSegments.map((s) => s.length));

  let workbookSaved = false;
  // Canvas/paper-style artifacts (Glean's AUTO agent sometimes writes the
  // report as a PAPER/CANVAS artifact instead of a downloadable file) ship
  // their actual content as inline text fragments -- entry.body -- rather
  // than a downloadable entityId. The old code only handled the
  // has-an-entityId case (workbooks) and `continue`d past anything else,
  // which silently threw away the entire canvas write-up: the .md ended up
  // with only the narration bubbles ("I'm loading...", "I've turned this
  // into...") and none of the actual assessment. Handle both shapes.
  const canvasSections = [];
  for (const artifact of extracted.artifacts) {
    if (artifact.entityId) {
      console.log(`Downloading artifact: ${artifact.name || artifact.entityId} ...`);
      const dl = await downloadChatFile(page, artifact.entityId, CLIENT_VERSION);
      if (dl.base64) {
        fs.writeFileSync(OUTPUT_WORKBOOK_PATH, Buffer.from(dl.base64, 'base64'));
        console.log(`Saved workbook to: ${OUTPUT_WORKBOOK_PATH} (original name: ${artifact.name || 'unknown'})`);
        workbookSaved = true;
      } else {
        console.log(`  Failed to download (status ${dl.status}) -- the text summary below is all that's available for this artifact.`);
      }
    } else if (artifact.body && artifact.body.trim()) {
      console.log(`Captured canvas artifact: ${artifact.name || artifact.type} (${artifact.body.length} chars)`);
      canvasSections.push({
        name: artifact.name || 'Canvas',
        body: stripTrailingRule(artifact.body.trim()),
      });
    } else {
      console.log(`  Artifact ${artifact.name || artifact.type} had no downloadable file and no inline content -- skipped.`);
    }
  }

  const canvasSaved = canvasSections.length > 0;
  const canvasText = canvasSections
    .map((c) => `## ${c.name}\n\n${c.body}`)
    .join('\n\n---\n\n');
  const reportText = extractReportText(extracted);

  let deliverableNote;
  if (workbookSaved) {
    deliverableNote = `Workbook: ${path.basename(OUTPUT_WORKBOOK_PATH)}`;
  } else if (canvasSaved) {
    deliverableNote = 'Glean returned this as a canvas document -- captured below.';
  } else {
    deliverableNote = 'No workbook or canvas artifact this run -- see text below.';
  }

  const header = [
    '# News Signal Report',
    '',
    `Generated: ${new Date().toISOString()}`,
    `Source file: ${path.basename(CSV_PATH)} (~${roughRowCount} rows)`,
    deliverableNote,
    '',
    '---',
    '',
  ].join('\n');

  // Canvas content is the actual deliverable when present, so it goes
  // first; the narration bubbles (reportText) follow as a companion
  // summary, same as before.
  const bodyParts = [canvasText, reportText].filter((t) => t && t.trim().length > 0);
  const fullBody = bodyParts.length > 0 ? bodyParts.join('\n\n---\n\n') : '(No content returned.)';

  fs.writeFileSync(OUTPUT_MD_PATH, header + fullBody);

  console.log(`\nOutput saved to: ${OUTPUT_MD_PATH}`);
  if (workbookSaved) console.log(`Workbook saved to: ${OUTPUT_WORKBOOK_PATH}`);
  if (canvasSaved) console.log(`Canvas content captured (${canvasSections.length} section(s)) into ${OUTPUT_MD_PATH}`);

  await context.close();
})();
