// glean-pg-tracker-automation.js
//
// Reads your PG Tracker CSV, and for each row with input data filled in,
// sends 2 SEPARATE prompts to Glean per person (instead of 1 bundled message
// running all 3 skills at once, which was diluting personalization):
//
//   1. Email skill only:
//        "/First Email Outreach" (or "/Japan Email Outreach" for Japan patch)
//        -> writes Subject Line + Messaging columns
//
//   2. WhatsApp + Cold Call skills together:
//        "/Whatsapp Outreach /MongoDB Cold Call Script Generator"
//        -> writes Whatsapp Message + Reference Script columns
//
// Each prompt is sent as its own fresh Glean chat (independent tabId /
// session token), so there's no cross-contamination between the 2 calls for
// the same person - they just both get that person's same company/contact
// details inserted.
//
// Why WhatsApp + Cold Call are still paired (and email is split out):
// the cold call script is effectively the reference script you'd glance at
// right before/while sending the WhatsApp message, so keeping those 2 in one
// chat turn is intentional. The email has a different tone/length and
// benefits from running on its own.
//
// Additional Context: if the "Additional Context" column in the input
// staging table is filled in for a row, it gets appended to BOTH prompts
// for that row (see buildAdditionalContextSuffix below). Leave it blank and
// the prompts are exactly the old generic ones, unchanged.
//
// IMPORTANT - read before running on your real data:
//   - ROW_LIMIT defaults to 5. Run a small batch first, check the console
//     debug output (artifact types + content segment lengths) and the
//     actual CSV output, THEN raise the limit once you've confirmed it's
//     mapping correctly. Don't run this against your whole sheet blind.
//   - Each person now costs 2 Glean calls instead of 1, so a full run will
//     take roughly 2x as long as before.
//
// Setup:
//   npm install playwright csv-parse csv-stringify
//
// Usage:
//   node glean-pg-tracker-automation.js path/to/your.csv

const fs = require('fs');
const path = require('path');
const { chromium } = require('playwright');
const { parse: parseCsv } = require('csv-parse/sync');
const { stringify: stringifyCsv } = require('csv-stringify/sync');

const USER_DATA_DIR = './glean-profile';

// Safety valve - bump this up once you've checked the first row's output.
// Optionally overridden by a 4th CLI argument.
const ROW_LIMIT = process.argv[4] ? parseInt(process.argv[4], 10) : 5;

const CSV_PATH = process.argv[2];
if (!CSV_PATH) {
  console.error('Usage: node glean-pg-tracker-automation.js path/to/your.csv [path/to/output.csv]');
  process.exit(1);
}

// Writes to a new file by default so the original input is never touched.
// Pass a third argument to choose the output path explicitly.
function deriveOutputPath(inputPath) {
  const dir = path.dirname(inputPath);
  const ext = path.extname(inputPath);
  const base = path.basename(inputPath, ext);
  return path.join(dir, `${base}-output${ext}`);
}
const OUTPUT_CSV_PATH = process.argv[3] || deriveOutputPath(CSV_PATH);

// Shared block of person/company details inserted into BOTH prompts below.
// Pulling this out so the 2 prompt builders below can't drift apart on
// which fields they include.
function buildPersonDetailsBlock(row) {
  return [
    `Company Name: ${row['Company Name'] || ''}`,
    `Contact Name: ${row['Contact Name'] || ''}`,
    `Title / Role: ${row['Title / Role'] || ''}`,
    `LinkedIn Profile: ${row['LinkedIn Profile'] || ''}`,
    `Interest / Role Research: ${row['Interest / Role Research'] || ''}`,
  ].join('\n');
}

// Optional per-row extra context (e.g. "this person is part of the IDC
// campaign, use that as the hook"), set via the "Additional Context" column
// in the input staging table. Blank/missing for a row is the common case -
// in that case this returns '' and both prompts below stay exactly as they
// were, unchanged. When it IS filled in, it's just concatenated onto the
// end of the prompt so it reads as one more instruction alongside the
// person details, not a separate message.
function buildAdditionalContextSuffix(row) {
  const extra = (row['Additional Context'] || '').trim();
  if (!extra) return '';
  return `\n\nAdditional context: ${extra}`;
}

// Prompt 1: email skill ONLY. Clean, plain-text - any invisible/zero-width
// characters from copy-pasting out of Glean's rich text editor are stripped.
function buildEmailPromptText(row) {
  const patch = (row['Patch'] || '').trim().toLowerCase();
  const emailSkill = patch === 'japan' ? '/Japan Email Outreach' : '/First Email Outreach';

  return `Using my skill ${emailSkill}, help me generate email for ${buildPersonDetailsBlock(row)}${buildAdditionalContextSuffix(row)}`;
}

// Prompt 2: WhatsApp + cold call skills together.
function buildWhatsappColdCallPromptText(row) {
  return `Using my skill /Whatsapp Outreach, help me generate whatsapp message and using my skill /MongoDB Cold Call Script Generator help me generate cold call script, both targeted for ${buildPersonDetailsBlock(row)}${buildAdditionalContextSuffix(row)}`;
}

function randomId(length = 16) {
  const chars = 'ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789';
  let out = '';
  for (let i = 0; i < length; i++) out += chars[Math.floor(Math.random() * chars.length)];
  return out;
}

// Runs inside the actual page - cookies/Origin/CORS are handled automatically
// by the browser. credentials: 'include' is required for the cross-origin
// cookie to actually get sent (this is what was causing 401s before).
// Unchanged from the bundled version - it just sends whatever prompt text
// it's given, so it works the same whether that's 1 skill or 3.
async function sendGleanPrompt(page, promptText) {
  return await page.evaluate(async ({ text, tabId, sessionTrackingToken }) => {
    const agentConfig = {
      agent: 'AUTO',
      toolSets: { enableCompanyTools: true, enableWebSearch: true },
      useCanvas: false,
      useImageGeneration: false,
      clientCapabilities: {
        artifacts: { allowedArtifactTypes: ['PAPER', 'HTML_CODE', 'SKILL', 'SLIDE', 'IMAGE', 'SPREADSHEET'] },
        canRenderImages: true,
        canRenderVariants: true,
        hasBrowserOperator: false,
      },
    };
    const nowIso = new Date().toISOString();
    const payload = {
      agentConfig,
      background: false,
      clientTools: [],
      messages: [{ agentConfig, author: 'USER', fragments: [{ text }], messageType: 'CONTENT', ts: nowIso }],
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
    // fetch() can reject before any response ever comes back - expired
    // session redirecting to a login page with no CORS headers, a CSP
    // connect-src block, no network, etc. That used to throw straight out
    // of page.evaluate and crash the whole Node process. Now it's caught
    // and turned into the same shape as a normal failed response, so the
    // existing `status !== 200` handling at each call site logs it and
    // moves on to the next row instead of killing the run.
    try {
      const res = await fetch(
        'https://mongodb-be.glean.com/api/v1/chat?timezoneOffset=-480&clientVersion=fe-release-2026-06-16-ed9db59&locale=en',
        {
          method: 'POST',
          headers: { 'Content-Type': 'text/plain' },
          credentials: 'include',
          body: JSON.stringify(payload),
        }
      );
      const bodyText = await res.text();
      return { status: res.status, statusText: res.statusText, body: bodyText };
    } catch (err) {
      return {
        status: 0,
        statusText: 'FETCH_ERROR',
        body: JSON.stringify({ errorName: err.name, errorMessage: err.message }),
      };
    }
  }, { text: promptText, tabId: randomId(), sessionTrackingToken: randomId() });
}

// Pulls apart the NDJSON response into:
//   - contentSegments: plain text "bubbles" the agent said, grouped by
//     messageId (since CONTENT streams char-by-char but shares one
//     messageId per bubble), in the order they appeared
//   - artifacts: any messageType starting with ARTIFACT_ (e.g.
//     ARTIFACT_EMAIL), with whatever name/subject/recipient/body info
//     each one carries, in the order they appeared
// Unchanged - works the same regardless of how many skills were invoked.
function extractDeliverables(rawBody) {
  const lines = rawBody.split('\n').map((l) => l.trim()).filter(Boolean);
  let chatId = null;
  let order = 0;
  const contentByMsgId = new Map();
  const artifactByMsgId = new Map();
  const artifacts = [];
  const shellCommandTexts = [];

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
          entry = { order: order++, type: msg.messageType, name: null, subject: null, toRecipients: null, body: '' };
          artifactByMsgId.set(msg.messageId, entry);
          artifacts.push(entry);
        }
        for (const frag of msg.fragments || []) {
          if (frag.artifact) {
            entry.name = frag.artifact.name;
            entry.subject = frag.artifact.annotations?.subject ?? entry.subject;
            entry.toRecipients = frag.artifact.annotations?.to_recipients ?? entry.toRecipients;
          }
          if (typeof frag.text === 'string') entry.body += frag.text;
        }
      }
      // UPDATE / CONTROL / TOOL_RESULT messages are usually progress noise, but
      // UPDATE messages can carry codeExecutionOutput.shellCommands[].command -
      // the raw shell script the agent ran to write an artifact file. Glean
      // doesn't reliably echo the final artifact body back into the
      // ARTIFACT_* message's own fragments (confirmed via a captured chat
      // where ARTIFACT_EMAIL had no fragments key at all), but the shell
      // command that wrote the file always has the full content embedded in
      // it, so it's collected here as a fallback source.
      for (const frag of msg.fragments || []) {
        const cmds = frag.codeExecutionOutput?.shellCommands;
        if (Array.isArray(cmds)) {
          for (const sc of cmds) {
            if (typeof sc.command === 'string') shellCommandTexts.push(sc.command);
          }
        }
      }
    }
  }

  const contentSegments = [...contentByMsgId.values()]
    .sort((a, b) => a.order - b.order)
    .map((e) => e.text);
  artifacts.sort((a, b) => a.order - b.order);

  return { chatId, contentSegments, artifacts, shellCommandTexts };
}

// Fallback/primary source for email artifact subject+body: parses the
// <artifact title="....email">...<field key="subject" value="..."/>...
// <content>...</content>...</artifact> XML that the agent's shell script
// writes to disk. This is present regardless of whether Glean also streamed
// the same content into the ARTIFACT_EMAIL message's fragments, so it's a
// more reliable source than relying on the artifact message alone.
function extractEmailArtifactFromShellCommands(shellCommandTexts) {
  for (const cmd of shellCommandTexts || []) {
    if (!cmd.includes('<artifact') || !cmd.includes('.email')) continue;
    const subjectMatch = cmd.match(/<field\s+key="subject"\s+value="([^"]*)"\s*\/>/);
    const contentMatch = cmd.match(/<content>\n([\s\S]*?)\n<\/content>/);
    if (contentMatch && contentMatch[1].trim()) {
      return {
        subject: subjectMatch ? subjectMatch[1] : '',
        body: contentMatch[1],
      };
    }
  }
  return null;
}

// Matches "phrase" as whole word(s) in haystack, not as a substring -
// "voicemail" contains the literal substring "email", so a plain
// .includes() check would wrongly match a "Voicemail version" heading as
// the email section. Word-boundary matching avoids that.
function containsWord(haystack, phrase) {
  const pattern = phrase.split(' ').join('\\s+');
  return new RegExp(`\\b${pattern}\\b`, 'i').test(haystack);
}

// Looks for a line like "Subject: ..." or "Subject Line: ..." (with or
// without surrounding markdown bold). Handles two formats seen from Glean:
//   - subject text right after the colon, on the same line
//   - the label alone on its own line, with the actual subject text
//     starting on the next non-blank line (this is what Glean actually
//     does most of the time -- confirmed from real samples)
// Returns null if no label line is found at all, in which case the caller
// falls back to treating the whole text as the body, and a debug snippet
// gets logged so a still-missed format shows up directly in the console.
function extractSubject(text, contextLabel) {
  const lines = text.split('\n');
  let offset = 0;

  for (let i = 0; i < lines.length; i++) {
    const line = lines[i];
    const labelMatch = line.match(/^\*{0,2}\s*Subject(?:\s*Line)?\s*:\s*\*{0,2}\s*(.*)$/i);

    if (labelMatch) {
      const sameLine = labelMatch[1].trim();
      if (sameLine) {
        const restOffset = offset + line.length + 1;
        return { subject: sameLine, rest: text.slice(restOffset).trim() };
      }

      let innerOffset = offset + line.length + 1;
      for (let j = i + 1; j < lines.length; j++) {
        if (lines[j].trim()) {
          const restOffset = innerOffset + lines[j].length + 1;
          return { subject: lines[j].trim(), rest: text.slice(restOffset).trim() };
        }
        innerOffset += lines[j].length + 1;
      }
    }

    offset += line.length + 1;
  }

  console.warn(`  [debug] No "Subject:" line found in ${contextLabel}. First 150 chars: ${text.slice(0, 150).replace(/\n/g, ' \\n ')}`);
  return null;
}

// Shared helper: splits a markdown doc into sections by "#" heading lines at
// the given depth (level=1 -> "# Heading", level=2 -> "## Heading"). Used by
// the WhatsApp+ColdCall mapper below to tell the 2 sections apart when Glean
// bundles them into one combined response.
// The regex requires exactly `level` hashes (not more), so level=1 will NOT
// match a "## Sub-heading" line - this matters because the cold call script
// itself contains nested "## Opening" / "## Objection handling" etc.
// sub-sections, and matching those too would chop the cold call body into
// fragments instead of capturing it as one block.
function parseHeaderSections(text, level) {
  const headerRegex = new RegExp(`^#{${level}}(?!#)\\s+(.+)$`, 'gm');
  const matches = [...text.matchAll(headerRegex)];
  if (matches.length === 0) return null;

  return matches.map((m, i) => {
    const start = m.index + m[0].length;
    const end = i + 1 < matches.length ? matches[i + 1].index : text.length;
    return { heading: m[1].trim().toLowerCase(), body: text.slice(start, end).trim() };
  });
}

// Strips a trailing markdown divider (e.g. "---") that's sometimes left
// over from the separator before a "## Sources" section.
function stripTrailingRule(s) {
  return s.replace(/\n?-{3,}\s*$/, '').trim();
}

// Glean sometimes streams a short first-person "working on it" bubble ahead
// of the real deliverable, e.g. "I've drafted both the WhatsApp message
// and the cold call script for Eileen Tan..." or "Using your WhatsApp
// Outreach and MongoDB Cold Call Script Generator skills, I'm pulling the
// LinkedIn context first...". These can literally namedrop "cold call" or
// "whatsapp" while describing what's about to be generated, which is
// exactly the kind of text a keyword scan would otherwise mistake for the
// real content. Detected by the text opening with first-person process
// narration instead of actual outreach content (a real WhatsApp message or
// cold call script opens by greeting the prospect, not narrating the task).
function looksLikeMetaAcknowledgement(text) {
  const opening = text.trim().slice(0, 60).toLowerCase();
  return /^(i['’]ve|i['’]m|i have|i am|i drafted|i pulled|i tailored|i grounded|i prepared|i kept|i based|using (your|my))\b/.test(opening);
}

// ---------------------------------------------------------------------------
// Prompt 1 response ("/First Email Outreach" or "/Japan Email Outreach")
// -> Subject Line / Messaging columns.
// ---------------------------------------------------------------------------
//
// With only 1 skill invoked, Glean usually returns either:
//   - a standalone ARTIFACT_EMAIL (cleanest - subject/body already split), or
//   - a single content/document block with a "Subject: ..." line, plus
//     sometimes a short throwaway acknowledgement bubble ("Here's your
//     email:") alongside it - the longest candidate is taken as the real one.
// ---------------------------------------------------------------------------
// Markdown -> HTML for the email body (Messaging column), which now feeds
// PG Tracker's rich Salesloft editor instead of a plain-text field. Glean's
// email skill inconsistently uses **bold**/bullet markdown depending on the
// run (verified against real captured responses: some emails have none of
// it at all, others use "* **Bold lead-in.** rest of sentence" bullets with
// each bullet as its own blank-line-separated block, plus inline
// "[text](url)" links) -- this converts whatever's actually there into the
// same p/b/ul/li/a allowlist that salesloft_pipeline.py's sanitizer and the
// define-stage editor already expect, instead of the old behaviour of just
// deleting "**" and leaving bullet/link markdown as literal characters.
function convertInlineMarkdown(text) {
  let out = text
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;');
  // Links: [text](url) -- http(s) only, so a malformed/typo'd URL doesn't
  // become a stray "javascript:" or bare-word href downstream.
  out = out.replace(/\[([^\]]+)\]\((https?:\/\/[^\s)]+)\)/g, '<a href="$2">$1</a>');
  // Bold: **text**
  out = out.replace(/\*\*([^*]+)\*\*/g, '<b>$1</b>');
  return out;
}

const _LIST_MARKER_RE = /^(\*|-|\d+\.)\s+(.*)$/;

function convertMessagingMarkdownToHtml(text) {
  if (!text || !text.trim()) return '';

  // Glean separates each bullet with a full blank line rather than
  // stacking them on consecutive lines like typical markdown, so list
  // items are detected per-block (not just per-line) and grouped across
  // consecutive list-only blocks into one <ul>/<ol>.
  const blocks = text.split(/\n{2,}/).map((b) => b.trim()).filter(Boolean);
  const htmlParts = [];
  let currentListItems = null;
  let currentListTag = null;

  function flushList() {
    if (currentListItems && currentListItems.length) {
      htmlParts.push(`<${currentListTag}>${currentListItems.join('')}</${currentListTag}>`);
    }
    currentListItems = null;
    currentListTag = null;
  }

  for (const block of blocks) {
    const lines = block.split('\n').map((l) => l.trim()).filter(Boolean);
    const allListLines = lines.length > 0 && lines.every((l) => _LIST_MARKER_RE.test(l));

    if (allListLines) {
      lines.forEach((line) => {
        const m = line.match(_LIST_MARKER_RE);
        const marker = m[1];
        const itemText = m[2];
        const tag = /^\d+\.$/.test(marker) ? 'ol' : 'ul';
        if (currentListTag && currentListTag !== tag) flushList();
        if (!currentListTag) currentListTag = tag;
        currentListItems = currentListItems || [];
        currentListItems.push(`<li>${convertInlineMarkdown(itemText)}</li>`);
      });
      continue; // don't flush -- next block may continue the same list
    }

    flushList();
    const withBreaks = block.split('\n').map((l) => convertInlineMarkdown(l.trim())).join('<br>');
    htmlParts.push(`<p>${withBreaks}</p>`);
  }
  flushList();

  return htmlParts.join('');
}

function mapEmailDeliverablesToRow(extracted) {
  const result = { subjectLine: '', messaging: '' };

  const emailArtifact = extracted.artifacts.find((a) => a.type === 'ARTIFACT_EMAIL');
  const fromShell = (!emailArtifact || !emailArtifact.body || !emailArtifact.body.trim())
    ? extractEmailArtifactFromShellCommands(extracted.shellCommandTexts)
    : null;

  if (emailArtifact && emailArtifact.body && emailArtifact.body.trim()) {
    result.subjectLine = emailArtifact.subject || '';
    result.messaging = emailArtifact.body;
  } else if (fromShell) {
    // ARTIFACT_EMAIL message existed (or didn't) but carried no usable body -
    // this is the common failure mode (Glean doesn't always echo the final
    // artifact content into the ARTIFACT_EMAIL message's fragments). Fall
    // back to the shell script that actually wrote the .email file, which
    // always has the real subject/content.
    result.subjectLine = emailArtifact?.subject || fromShell.subject || '';
    result.messaging = fromShell.body;
  } else {
    const candidateTexts = [
      ...extracted.artifacts.map((a) => a.body),
      ...extracted.contentSegments,
    ]
      .filter((t) => t && t.length > 100)
      .filter((t) => !looksLikeMetaAcknowledgement(t))
      .sort((a, b) => b.length - a.length); // longest = the real email, not an ack bubble

    const text = candidateTexts[0];
    if (text) {
      const extractedSubject = extractSubject(text, 'the email outreach response');
      if (extractedSubject) {
        result.subjectLine = extractedSubject.subject;
        result.messaging = extractedSubject.rest;
      } else {
        result.messaging = text.trim();
      }
    }
  }

  result.subjectLine = stripTrailingRule(result.subjectLine);
  // Used to just delete literal "**" here since the old Salesloft flow was
  // plain-text-only. Now converts markdown (bold/bullets/links) to the
  // p/b/ul/li/a HTML the rich editor and pipeline sanitizer expect -- a
  // Messaging value with no markdown in it at all still comes out fine,
  // just wrapped in plain <p> tags.
  result.messaging = convertMessagingMarkdownToHtml(stripTrailingRule(result.messaging));

  return result;
}

// ---------------------------------------------------------------------------
// Prompt 2 response ("/Whatsapp Outreach /MongoDB Cold Call Script
// Generator") -> Whatsapp Message / Reference Script columns.
// ---------------------------------------------------------------------------

// Sometimes the model bundles both outputs into one combined markdown
// document with section headers (e.g. "# WhatsApp message",
// "# Cold call script") instead of returning 2 separate deliverables. The
// exact heading wording AND heading depth vary run to run - so far the top
// level has been a single "#" with the cold call script's own internal
// sub-sections (Opening, Objection handling, etc.) nested one level deeper
// as "##" - but level 2 is tried as a fallback in case a run comes back
// with the 2 sections at "##" directly instead.
// Tries splitting at one specific heading depth, and only counts it as a
// real match if at least one of the 2 expected sections (cold call /
// whatsapp) is actually found there. Just finding SOME heading at a given
// depth isn't enough -- a single outer "# Company outreach" wrapper title
// would otherwise count as "found something" and block a deeper, correct
// split from ever being tried.
function trySplitAtLevel(text, level) {
  const sections = parseHeaderSections(text, level);
  if (!sections) return null;

  const coldCallSection = sections.find((s) => containsWord(s.heading, 'cold call'));
  const whatsappSection = sections.find((s) => containsWord(s.heading, 'whatsapp'));
  if (!coldCallSection && !whatsappSection) return null;

  return { coldCallSection, whatsappSection };
}

// Sometimes the model bundles both outputs into one combined markdown
// document with section headers (e.g. "# WhatsApp message" /
// "# Cold call script" at the top level) instead of returning 2 separate
// deliverables. Other times it wraps everything in an outer title first
// (e.g. "# CIMB Securities outreach for Eileen Tan") and pushes WhatsApp/Cold
// call down to "##" instead, with their own internal sub-sections at "###".
// Heading depth varies run to run, so each depth is tried in turn, moving
// deeper only when the shallower one didn't actually contain either
// expected section (not just whenever it found zero headings at all).
function splitWhatsappColdCallPack(text) {
  if (!text) return null;
  const found = trySplitAtLevel(text, 1) || trySplitAtLevel(text, 2) || trySplitAtLevel(text, 3);
  if (!found) return null;

  return {
    coldCallScript: found.coldCallSection ? stripTrailingRule(found.coldCallSection.body) : '',
    whatsappMessage: found.whatsappSection ? stripTrailingRule(found.whatsappSection.body) : '',
  };
}

// Used when the model does NOT bundle both into one combined pack, and
// instead returns 2 separate standalone documents (each often titled with a
// "# ..." heading naming what it is). Classifies a single document by its
// title first, falling back to scanning its content for the same keywords -
// but only for texts long enough to plausibly BE a script/message, since a
// short Glean acknowledgement bubble (e.g. "Using your WhatsApp Outreach
// and MongoDB Cold Call Script Generator skills, I'm pulling...") can
// namedrop "Cold Call Script Generator" without being the actual script.
function classifyColdCallOrWhatsapp(text) {
  if (!text) return null;
  const firstLine = (text.match(/^#\s+(.+)$/m) || [])[1] || '';
  if (containsWord(firstLine, 'cold call')) return 'coldCall';
  if (containsWord(firstLine, 'whatsapp')) return 'whatsapp';

  if (text.length < 250) return null; // too short to trust a body-text keyword match
  const haystack = text.slice(0, 300).toLowerCase();
  if (containsWord(haystack, 'cold call')) return 'coldCall';
  if (containsWord(haystack, 'whatsapp')) return 'whatsapp';
  return null;
}

// Best-effort mapping from whatever came back to the 2 CSV output columns.
// Tries multiple strategies in order:
//   1. A combined pack with labeled "## ..." sections - most reliable.
//   2. Two separate standalone documents, classified by title/content.
//   3. Last-resort: whatever's left over, longest-first (cold call scripts
//      are consistently much longer than WhatsApp messages in every
//      example seen so far, so this is a reasonable tiebreaker, not just
//      arbitrary stream order).
// Generic counterpart to extractEmailArtifactFromShellCommands: pulls the
// raw string assigned to `content` right before the agent's `write(...)`
// call, whatever shape it's in. The .email skill wraps this in
// <artifact>...</artifact> XML (handled separately above); the WhatsApp +
// Cold Call skill writes a plain markdown file instead (headed by
// "# ... outreach for ..." with "## WhatsApp message" / "## Cold call
// script" sections) with no XML wrapper at all. Rather than write a second
// XML-specific parser, this just grabs the raw written text and lets the
// existing splitWhatsappColdCallPack/classifyColdCallOrWhatsapp logic (which
// already expects exactly this markdown shape) do the rest - same fallback
// this needs for ARTIFACT_PAPER messages that (like ARTIFACT_EMAIL) often
// carry no body in their own fragments.
// Returned most-recent-first, since the same write() call can appear
// duplicated across an UPDATE "call" message and its paired "result"
// message with identical content, or - less commonly - a genuine revision,
// in which case the later one in the transcript is the one actually kept.
function extractWrittenFileContentsFromShellCommands(shellCommandTexts) {
  const contents = [];
  for (const cmd of shellCommandTexts || []) {
    const re = /content\s*=\s*'''([\s\S]*?)'''\s*\n\s*async def main/g;
    let m;
    while ((m = re.exec(cmd)) !== null) {
      if (m[1].trim()) contents.push(m[1]);
    }
  }
  return contents.reverse();
}

function mapWhatsappColdCallDeliverablesToRow(extracted) {
  const result = { referenceScript: '', whatsappMessage: '' };

  const writtenContents = extractWrittenFileContentsFromShellCommands(extracted.shellCommandTexts);

  const candidateTexts = [
    ...writtenContents,
    ...extracted.artifacts.map((a) => a.body),
    ...extracted.contentSegments,
  ]
    .filter((t) => t && t.length > 100)
    .filter((t) => !looksLikeMetaAcknowledgement(t));

  let pack = null;
  for (const text of candidateTexts) {
    pack = splitWhatsappColdCallPack(text);
    if (pack) break;
  }

  if (pack) {
    result.referenceScript = pack.coldCallScript;
    result.whatsappMessage = pack.whatsappMessage;
  } else {
    for (const text of candidateTexts) {
      const kind = classifyColdCallOrWhatsapp(text);
      if (kind === 'coldCall' && !result.referenceScript) {
        result.referenceScript = text.trim();
      } else if (kind === 'whatsapp' && !result.whatsappMessage) {
        result.whatsappMessage = text.trim();
      }
    }
  }

  if (!result.referenceScript || !result.whatsappMessage) {
    const used = new Set([result.referenceScript, result.whatsappMessage].map((s) => s.trim()).filter(Boolean));
    const leftovers = candidateTexts.filter((t) => !used.has(t.trim()));
    leftovers.sort((a, b) => b.length - a.length); // scripts are consistently longer than WhatsApp messages
    let idx = 0;
    if (!result.referenceScript && leftovers[idx]) result.referenceScript = leftovers[idx++].trim();
    if (!result.whatsappMessage && leftovers[idx]) result.whatsappMessage = leftovers[idx++].trim();
  }

  return result;
}

(async () => {
  const context = await chromium.launchPersistentContext(USER_DATA_DIR, { headless: false });
  const page = await context.newPage();
  await page.goto('https://app.glean.com');

  console.log('Waiting for Glean to finish loading (log in by hand now if prompted)...');
  await page.waitForSelector('.ql-editor', { timeout: 120000 });
  console.log('Glean loaded, proceeding.\n');

  const csvRaw = fs.readFileSync(CSV_PATH, 'utf8');
  const records = parseCsv(csvRaw, { columns: true, skip_empty_lines: true, bom: true });
  const columnOrder = records.length > 0 ? Object.keys(records[0]) : [];

  let processed = 0;
  for (const row of records) {
    if (processed >= ROW_LIMIT) break;
    if (!row['Company Name']) continue; // no input data on this row - skip

    console.log(`=== Processing: ${row['Contact Name']} @ ${row['Company Name']} ===`);

    // ---- Prompt 1: email skill only ----
    console.log('  -> [1/2] Email prompt...');
    const emailPromptText = buildEmailPromptText(row);
    const emailResult = await sendGleanPrompt(page, emailPromptText);

    if (emailResult.status !== 200) {
      console.log('  FAILED (email), status:', emailResult.status, emailResult.statusText);
      console.log(emailResult.body.slice(0, 500));
    } else {
      const emailExtracted = extractDeliverables(emailResult.body);
      console.log('  Email artifacts found:', emailExtracted.artifacts.map((a) => a.type));
      console.log('  Email content segment lengths:', emailExtracted.contentSegments.map((s) => s.length));

      const mappedEmail = mapEmailDeliverablesToRow(emailExtracted);
      row['Subject Line'] = mappedEmail.subjectLine;
      row['Messaging'] = mappedEmail.messaging;

      console.log('  Subject Line ->', mappedEmail.subjectLine);
      console.log('  Messaging length ->', mappedEmail.messaging.length);
    }

    // ---- Prompt 2: WhatsApp + cold call skills ----
    console.log('  -> [2/2] Whatsapp + Cold Call prompt...');
    const waPromptText = buildWhatsappColdCallPromptText(row);
    const waResult = await sendGleanPrompt(page, waPromptText);

    if (waResult.status !== 200) {
      console.log('  FAILED (whatsapp/cold-call), status:', waResult.status, waResult.statusText);
      console.log(waResult.body.slice(0, 500));
    } else {
      const waExtracted = extractDeliverables(waResult.body);
      console.log('  WA/ColdCall artifacts found:', waExtracted.artifacts.map((a) => a.type));
      console.log('  WA/ColdCall content segment lengths:', waExtracted.contentSegments.map((s) => s.length));

      const mappedWa = mapWhatsappColdCallDeliverablesToRow(waExtracted);
      row['Reference Script'] = mappedWa.referenceScript;
      row['Whatsapp Message'] = mappedWa.whatsappMessage;

      console.log('  Reference Script length ->', mappedWa.referenceScript.length);
      console.log('  Whatsapp Message length ->', mappedWa.whatsappMessage.length);
    }

    console.log('');
    processed++;

    // Write progress after every row, not just once at the very end -- this
    // is what makes it safe to stop the script early and keep whatever's
    // already been processed, instead of losing the whole run.
    const progressCsv = stringifyCsv(records, { header: true, columns: columnOrder });
    fs.writeFileSync(OUTPUT_CSV_PATH, progressCsv);
  }

  console.log(`Done. Processed ${processed} row(s).`);
  console.log(`Output saved to: ${OUTPUT_CSV_PATH}`);
  console.log(`Original input untouched: ${CSV_PATH}`);

  await context.close();
})();
