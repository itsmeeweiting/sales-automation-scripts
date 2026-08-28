"""
Salesloft Pipeline -- consolidated import + cadence + send/schedule
=====================================================================
Replaces the manual "upload to Salesloft > map fields > add to
cadence" step + your two separate automation scripts with one run:

  PHASE 1 -- BULK IMPORT (pure API calls, reuses your logged-in
             browser session's cookies via page.request -- no
             clicking, no field-mapping screen)
    1. POST /api/imports/csv_setup.json         (upload CSV)
    2. POST /v2/import_previews/csvs             (submit field mapping)
    3. GET  /v2/import_previews?ids=X            (validated preview:
                                                    people / repeats /
                                                    rejects)
    4. POST /api/imports                         (commit the batch,
                                                    with cadence_id set
                                                    -- this both
                                                    creates/updates the
                                                    people AND enrolls
                                                    them in the cadence
                                                    in one call)
    5. GET  /v2/imports/{id}                      (poll for completion)

    Deliberately SKIPS  POST /api/people/active_cadences.  That call
    only powers a "some of these are already in an active cadence"
    confirmation popup in the UI -- confirmed via live testing that it
    does not gate step 4 in any way, so for automation we just always
    proceed (equivalent to always clicking "continue anyway").

  PHASE 2 -- PER-CONTACT COMPOSE (browser UI, same DOM interactions
             your original scripts already proved out)
    For each contact: search People > open profile > look up the
    target cadence's CURRENT name via GET /v2/cadences/{id} (never
    hardcoded) > hover that cadence's box > click its "Run" button >
    expand the compose pane > fill Subject + Messaging > then EITHER:
      --mode now      : pause for your manual review, you send, press
                         Enter (or type 'skip') for the next contact
      --mode schedule  : open "Expand Schedule Send Menu", force the
                         timezone, set the date/time, click Schedule
                         -- fully unattended after SSO login

SETUP (same as your existing scripts):
    pip3 install playwright pandas openpyxl
    python3 -m playwright install chromium

USAGE:
    python3 salesloft_pipeline.py --cadence-id 826963 --mode now
    python3 salesloft_pipeline.py --cadence-id 826963 --mode schedule --send-at "2026-07-10 09:00"
    python3 salesloft_pipeline.py --cadence-id 826963 --mode schedule --send-at "2026-07-10 09:00" --dry-run
    python3 salesloft_pipeline.py --csv contacts.csv --cadence-id 826963 --mode now --exclude-existing

    --cadence-id        required. Numeric Salesloft cadence ID (from
                         your Cadence settings page).
    --mode               required. "now" or "schedule".
    --send-at            required if --mode schedule. Singapore time,
                         "YYYY-MM-DD HH:MM". This is meant to be
                         chosen on the "Define stage" screen and
                         passed in here, not typed interactively.
    --exclude-existing    by default, contacts that already exist in
                         Salesloft (matched by email) are INCLUDED in
                         the import batch same as brand-new contacts
                         (mirrors "select all" in the UI). Pass this
                         flag to skip re-importing/re-enrolling people
                         who already exist in Salesloft.
    --dry-run            (schedule mode only) fills the date/time
                         fields for real but never clicks the final
                         "Schedule" button. No equivalent needed for
                         --mode now since that already pauses for your
                         manual review before anything sends.

⚠️ STILL UI-DEPENDENT FOR PHASE 2's schedule popover, same caveat as
   the script this was built from: selectors were read from one
   DevTools capture. Run --dry-run first on a small batch before
   trusting it unattended.

⚠️ PHASE 1's preview-ID lookup (see find_latest_preview_id) is an
   inferred workaround -- the browser's own POST to
   /v2/import_previews/csvs returns 204 with no body and no id in any
   response header we could find in the captured HARs, so the real
   frontend likely learns the new preview's ID via a websocket push
   (salesloft-pusher) that we don't reproduce here. Instead we poll
   GET /v2/import_previews (sorted newest-first, no ids filter) and
   match by filename + recency. This has worked in testing but is the
   one part of Phase 1 most likely to need adjustment if Salesloft's
   UI changes -- watch the log output on your first live run.
"""

import argparse
import base64
import html
import json
import re
import sys
import time
import uuid
from datetime import datetime, timedelta, timezone
from html.parser import HTMLParser
from pathlib import Path

import pandas as pd
from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeout

# ──────────────────────────────────────────────
# CONFIGURATION
# ──────────────────────────────────────────────

SALESLOFT_URL = "https://app.salesloft.com"
API_BASE = "https://api.salesloft.com"
MELODY_BASE = "https://melody.us4.salesloft.com"

COL_EMAIL = "Email"
COL_SUBJECT = "Subject Line"
COL_BODY = "Messaging"
COL_NAME = "Contact Name"
# Optional per-contact override for --mode schedule. When present and
# non-empty on a row, it wins over --send-at for that contact -- this is
# what makes a "split batch" (different rows going out at different
# times in the same run) possible. Same "YYYY-MM-DD HH:MM" format as
# --send-at, always Singapore time. Rows without it fall back to
# --send-at (the batch default), if one was given.
COL_SEND_AT = "Send At"

# Canonical field mapping WE control, sent on every import -- never
# trust whatever Salesloft's UI auto-suggests (confirmed unreliable:
# it's mis-mapped fields like Title -> twitter_handle in live testing).
# Keys are Salesloft's csv_fields target keys; values are your PG
# Tracker CSV's column headers. Add/adjust here, not on the fly.
CSV_FIELD_MAPPING = {
    "first_name": "First Name",
    "last_name": "Last Name",
    "company": "Company Name",
    "title": "Title / Role",
    "linkedin_url": "LinkedIn Profile",
    "phone": "Phone",
    "email_address": "Email",
    "country": "Patch",
    # Explicitly unmapped -- keeps Salesloft from guessing on it.
    "null": "ZoomInfo Phone",
}

DELAY_BETWEEN_CONTACTS = 3
PREVIEW_POLL_ATTEMPTS = 10
PREVIEW_POLL_DELAY = 1.5
IMPORT_STATUS_POLL_ATTEMPTS = 20
IMPORT_STATUS_POLL_DELAY = 1.5

SG_TZ = timezone(timedelta(hours=8))
FORCE_TIMEZONE_SEARCH_TEXT = "Hong_Kong"  # matches "(UTC+08:00) Asia/Hong_Kong"
FORCE_TIMEZONE_OFFSET = (8, 0)
TZ_OFFSET_PATTERN = re.compile(r"\(UTC([+-]\d{2}):(\d{2})\)")
PERSON_ID_PATTERN = re.compile(r"/app/people/(\d+)")


# ──────────────────────────────────────────────
# HELPERS
# ──────────────────────────────────────────────

def wait(seconds=1):
    time.sleep(seconds)


def log(msg):
    print(msg, flush=True)


def sanitize_phone(value):
    """Strips the leading '=' Excel/Sheets adds when a phone number
    looks like a formula (e.g. '=+81 6-6381-7323' -> '+81 6-6381-7323')."""
    value = str(value or "").strip()
    if value.startswith("="):
        value = value[1:].strip()
    return value


def split_contact_name(full_name):
    """'Caesario Dito' -> ('Caesario', 'Dito'). A single-word name keeps
    everything in First Name and leaves Last Name blank -- Salesloft is
    fine with a blank last name but not a blank first name."""
    full_name = str(full_name or "").strip()
    if not full_name:
        return "", ""
    parts = full_name.split(None, 1)
    if len(parts) == 1:
        return parts[0], ""
    return parts[0], parts[1]


def ensure_name_columns_for_import(csv_path: Path):
    """Salesloft's CSV import only understands First Name / Last Name as
    separate columns (see CSV_FIELD_MAPPING) -- it does not split a
    single Contact Name column itself. Without this, every brand-new
    contact comes back from the import preview with first_name=(blank),
    last_name=(blank), even when Contact Name is filled in. Derives
    First Name / Last Name from Contact Name for any row that doesn't
    already have them, and rewrites the CSV in place before it's
    uploaded. Rows that already carry a First Name (e.g. a manual edit)
    are left untouched."""
    df = pd.read_csv(csv_path, keep_default_na=False, dtype=str)

    if COL_NAME not in df.columns:
        return  # nothing to derive from

    if "First Name" not in df.columns:
        df["First Name"] = ""
    if "Last Name" not in df.columns:
        df["Last Name"] = ""

    filled = 0
    for idx, row in df.iterrows():
        if str(row.get("First Name", "")).strip():
            continue
        first, last = split_contact_name(row.get(COL_NAME, ""))
        if not first:
            continue
        df.at[idx, "First Name"] = first
        df.at[idx, "Last Name"] = last
        filled += 1

    if filled:
        df.to_csv(csv_path, index=False)
        log(f"  ✂️  Split Contact Name into First/Last Name for {filled} row(s) before upload.")


def load_contacts(csv_file):
    if csv_file.endswith(".csv"):
        df = pd.read_csv(csv_file, keep_default_na=False, dtype=str)
    else:
        df = pd.read_excel(csv_file, keep_default_na=False, dtype=str)

    required = [COL_EMAIL, COL_SUBJECT, COL_BODY]
    missing = [c for c in required if c not in df.columns]
    if missing:
        log(f"❌ Missing columns: {missing}")
        log(f"   Found columns: {list(df.columns)}")
        sys.exit(1)

    if "Phone" in df.columns:
        df["Phone"] = df["Phone"].apply(sanitize_phone)

    df = df[df[COL_EMAIL].str.strip() != ""]
    contacts = df.to_dict("records")
    log(f"📋 Loaded {len(contacts)} contacts\n")
    return contacts


_ALLOWED_TAGS = {
    "p": "p", "div": "p",
    "br": "br",
    "b": "b", "strong": "b",
    "ul": "ul", "ol": "ol", "li": "li",
    "a": "a",
}
_VOID_TAGS = {"br"}
_TAG_SNIFF_PATTERN = re.compile(
    r"</?(p|br|b|strong|ul|ol|li|a|div)\b", re.IGNORECASE
)


class _BodyHTMLSanitizer(HTMLParser):
    """Strips everything outside _ALLOWED_TAGS, unwrapping (not
    deleting) disallowed tags so their text content survives. Balances
    tags itself rather than trusting the input, since contenteditable
    output and old CSV data are not guaranteed well-formed."""

    _DROP_CONTENT_TAGS = {"script", "style"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.out = []
        self.open_stack = []
        self._drop_depth = 0  # >0 while inside a script/style tag -- swallow everything

    def handle_starttag(self, tag, attrs):
        if self._drop_depth or tag.lower() in self._DROP_CONTENT_TAGS:
            if tag.lower() in self._DROP_CONTENT_TAGS:
                self._drop_depth += 1
            return
        self._handle_open(tag, attrs)

    def handle_startendtag(self, tag, attrs):
        if self._drop_depth or tag.lower() in self._DROP_CONTENT_TAGS:
            return
        self._handle_open(tag, attrs)
        mapped = _ALLOWED_TAGS.get(tag.lower())
        if mapped and mapped not in _VOID_TAGS:
            self._handle_close(tag)

    def _handle_open(self, tag, attrs):
        mapped = _ALLOWED_TAGS.get(tag.lower())
        if not mapped:
            return
        if mapped == "br":
            self.out.append("<br>")
            return
        if mapped == "a":
            href = dict(attrs).get("href") or ""
            if not re.match(r"^https?://", href.strip(), re.IGNORECASE):
                return
            self.out.append(f'<a href="{html.escape(href.strip(), quote=True)}" target="_blank" rel="noopener">')
            self.open_stack.append("a")
            return
        self.out.append(f"<{mapped}>")
        self.open_stack.append(mapped)

    def handle_endtag(self, tag):
        if tag.lower() in self._DROP_CONTENT_TAGS:
            if self._drop_depth:
                self._drop_depth -= 1
            return
        if self._drop_depth:
            return
        self._handle_close(tag)

    def _handle_close(self, tag):
        mapped = _ALLOWED_TAGS.get(tag.lower())
        if not mapped or mapped in _VOID_TAGS:
            return
        if self.open_stack and self.open_stack[-1] == mapped:
            self.out.append(f"</{mapped}>")
            self.open_stack.pop()

    def handle_data(self, data):
        if self._drop_depth:
            return
        self.out.append(html.escape(data))

    def get_html(self):
        while self.open_stack:
            self.out.append(f"</{self.open_stack.pop()}>")
        return "".join(self.out)


def sanitize_body_html(raw):
    """Allowlists raw (potentially rich) body HTML down to
    p/br/b/ul/ol/li/a[href^=http]. Disallowed tags are unwrapped, not
    deleted, so stray formatting never eats the message text."""
    parser = _BodyHTMLSanitizer()
    parser.feed(str(raw))
    return parser.get_html()


def build_paragraphs_html(body):
    """Sanitizes the staged body down to the allowlist above before
    injecting into Salesloft (verified against a real send,
    Test_Email.eml, 2026-07-10: Salesloft's compose editor and outbound
    pipeline preserve p/br/b/ul/ol/li/a[href] untouched). Legacy
    plain-text bodies (no recognizable tags -- e.g. older CSV imports
    made before the rich editor existed) fall back to the original
    \\n\\n -> paragraph break, \\n -> <br> conversion so nothing already
    staged breaks. The editor's own signature/footer is already part of
    the loaded cadence template, so this is NOT wrapped in anything
    extra."""
    text = str(body)
    if _TAG_SNIFF_PATTERN.search(text):
        return sanitize_body_html(text)
    body_html = (
        text
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace("\n\n", "</p><p>")
        .replace("\n", "<br>")
    )
    return "<p>" + body_html + "</p>"


# ──────────────────────────────────────────────
# AUTH -- Salesloft's frontend is token-based (see POST
# accounts.salesloft.com/tokens in any HAR), not pure cookie auth.
# page.request shares cookies with the browser automatically but does
# NOT know about that bearer token, which is why a naive page.request
# call 401s even right after a successful login. Fix: listen for the
# app's OWN traffic and copy the Authorization header it's already
# using, rather than trying to reproduce the token exchange ourselves.
# ──────────────────────────────────────────────

SESSION_HEADERS = {}


def capture_auth_headers(page, timeout_seconds=25):
    """Attaches a request listener, forces some of the app's own API
    traffic by visiting /app/people, and grabs the Authorization header
    off the first matching api.salesloft.com/melody.us4.salesloft.com
    request. That header is then reused on every manual page.request
    call for the rest of the run."""
    captured = {}

    def on_request(req):
        if "Authorization" in captured:
            return
        if "api.salesloft.com" in req.url or "melody.us4.salesloft.com" in req.url:
            headers = req.headers
            auth = headers.get("authorization")
            if auth:
                captured["Authorization"] = auth

    page.on("request", on_request)
    try:
        page.goto(f"{SALESLOFT_URL}/app/people")
        page.wait_for_load_state("networkidle")
        deadline = time.time() + timeout_seconds
        while time.time() < deadline and "Authorization" not in captured:
            page.wait_for_timeout(250)
    finally:
        page.remove_listener("request", on_request)

    if "Authorization" not in captured:
        raise RuntimeError(
            "Could not capture an Authorization header from the app's own traffic. "
            "Make sure you're actually logged in (check the browser window) and try again -- "
            "if this keeps happening, Salesloft may have changed its auth flow and the API "
            "calls in this script will need a fresh look."
        )

    SESSION_HEADERS.clear()
    SESSION_HEADERS.update(captured)
    log("  🔑 Captured auth header from the app's own session.")


# ──────────────────────────────────────────────
# PHASE 1 -- BULK IMPORT (API calls via page.request, reusing your
# logged-in browser session's cookies -- no UI clicking)
# ──────────────────────────────────────────────

def api_get_me(page):
    resp = page.request.get(f"{API_BASE}/v2/me?include_private_fields=true", headers=SESSION_HEADERS)
    if not resp.ok:
        raise RuntimeError(f"GET /v2/me failed: {resp.status}")
    return resp.json()["data"]


def api_csv_setup(page, csv_path: Path):
    resp = page.request.post(
        f"{MELODY_BASE}/api/imports/csv_setup.json",
        multipart={
            "file": {
                "name": csv_path.name,
                "mimeType": "text/csv",
                "buffer": csv_path.read_bytes(),
            },
            "name": csv_path.name,
        },
        headers=SESSION_HEADERS,
    )
    if not resp.ok:
        raise RuntimeError(f"csv_setup.json failed: {resp.status} {resp.text()[:300]}")
    raw = resp.text()
    try:
        # Sometimes base64-wrapped, sometimes plain JSON -- handle both.
        return json.loads(raw)
    except json.JSONDecodeError:
        return json.loads(base64.b64decode(raw).decode("utf-8"))


def api_submit_field_mapping(page, csv_path: Path, field_mapping: dict):
    qs = f"field_mapping={json.dumps(field_mapping)}&overwrite_owner=false"
    resp = page.request.post(
        f"{API_BASE}/v2/import_previews/csvs?{qs}",
        multipart={
            "file": {
                "name": csv_path.name,
                "mimeType": "text/csv",
                "buffer": csv_path.read_bytes(),
            },
            "name": csv_path.name,
        },
        headers=SESSION_HEADERS,
    )
    if resp.status not in (200, 204):
        raise RuntimeError(f"import_previews/csvs failed: {resp.status} {resp.text()[:300]}")


def find_latest_preview_id(page, csv_filename, submitted_after):
    """The frontend seems to learn the new preview's ID via a
    websocket push we don't reproduce here (see module docstring).
    Workaround: poll the most recent import_previews and match by
    filename.

    BUG FIXED: matching by filename alone isn't enough when every
    export is named 'contacts.csv' (confirmed live -- a fresh 13-row
    upload matched preview id 326619 on the very first poll attempt,
    but that preview actually held 19 people from an EARLIER run's
    leftover 'contacts.csv' preview object, since the brand-new
    preview hadn't finished being created server-side yet). Now also
    requires the matched entry's created_at to be after submitted_after
    (the moment we actually posted this CSV), so a same-named stale
    preview from a prior run can never be picked up again."""
    for attempt in range(PREVIEW_POLL_ATTEMPTS):
        resp = page.request.get(
            f"{API_BASE}/v2/import_previews?per_page=5&sort_by=created_at&sort_direction=desc",
            headers=SESSION_HEADERS,
        )
        if resp.ok:
            data = resp.json().get("data", [])
            for entry in data:
                name = entry.get("mapped_data", {}).get("name")
                if name != csv_filename:
                    continue
                created_at = entry.get("created_at")
                try:
                    entry_dt = datetime.fromisoformat(created_at)
                except (TypeError, ValueError):
                    entry_dt = None
                if entry_dt is not None and entry_dt <= submitted_after:
                    log(f"  … attempt {attempt + 1}: found a preview named '{csv_filename}' (id {entry['id']}) "
                        f"but it's from before this submission ({created_at}) -- stale, skipping")
                    continue
                log(f"  ✓ Found preview id {entry['id']} (attempt {attempt + 1}) for '{csv_filename}'")
                return entry["id"]
            if data:
                log(f"  … attempt {attempt + 1}: most recent preview is '{data[0].get('mapped_data', {}).get('name')}', not ours yet")
        wait(PREVIEW_POLL_DELAY)
    raise RuntimeError(f"Could not find a fresh preview matching '{csv_filename}' after {PREVIEW_POLL_ATTEMPTS} attempts")


def api_get_preview(page, preview_id):
    resp = page.request.get(f"{API_BASE}/v2/import_previews?ids={preview_id}", headers=SESSION_HEADERS)
    if not resp.ok:
        raise RuntimeError(f"GET import_previews failed: {resp.status}")
    data = resp.json()["data"]
    if not data:
        raise RuntimeError(f"No preview found for id {preview_id}")
    return data[0]["mapped_data"]


def build_finalize_people(preview_data, exclude_existing=False):
    """preview_data['people'] contains BOTH brand-new rows (id: null)
    and rows that matched an existing Salesloft person (real id) --
    confirmed from live captures. Selection-checkbox behavior in the
    UI is purely client-side, so "select all" is just: include
    everyone from this list. Set exclude_existing=True to only import
    brand-new people (id is null) and skip re-touching existing ones."""
    people = preview_data.get("people", [])
    rejects = preview_data.get("rejects", [])
    repeats = preview_data.get("repeats", [])

    if rejects:
        log(f"  ⚠️  {len(rejects)} row(s) rejected by Salesloft (won't be imported):")
        for r in rejects:
            reason = "; ".join(r.get("errors", []) or ["unknown reason"])
            who = r.get("email_address") or f"{r.get('first_name', '')} {r.get('last_name', '')}".strip()
            log(f"     • {who}: {reason}")

    if repeats:
        log(f"  ⚠️  {len(repeats)} duplicate row(s) within the CSV itself (skipped)")

    if people:
        log(f"  🔎 Diagnostic — what Salesloft's own preview returned per person (before commit):")
        for p in people:
            crm_id = p.get("crm_id")
            crm_tag = f"crm_id={crm_id}" if crm_id else "crm_id=null (not CRM-synced)"
            existing_tag = f"MATCHED EXISTING id={p.get('id')}" if p.get("id") else "NEW"
            fname = p.get("first_name") or "(blank)"
            lname = p.get("last_name") or "(blank)"
            log(f"     • {p.get('email_address')} — {existing_tag}, {crm_tag}, first_name={fname}, last_name={lname}")

    if exclude_existing:
        included = [p for p in people if p.get("id") is None]
        skipped = len(people) - len(included)
        if skipped:
            log(f"  ℹ️  Excluding {skipped} row(s) that already exist in Salesloft (--exclude-existing)")
    else:
        included = people

    for person in included:
        person["row_id"] = str(uuid.uuid4())

    return included, rejects


def api_finalize_import(page, people, cadence_id, csv_filename, assigned_account_id):
    body = {
        "assigned_account_id": assigned_account_id,
        "people": people,
        "cadence_id": cadence_id,
        "name": csv_filename,
        "step_id": None,
        "import_type": "csv",
    }
    resp = page.request.post(
        f"{MELODY_BASE}/api/imports",
        data=json.dumps(body),
        headers={**SESSION_HEADERS, "Content-Type": "application/json"},
    )
    if not resp.ok:
        raise RuntimeError(f"POST /api/imports failed: {resp.status} {resp.text()[:500]}")
    return resp.json()


def poll_import_status(page, import_id):
    """Salesloft processes imports asynchronously. current_people_count
    and imported_people_count read as 0 immediately after commit and
    only reflect real numbers once processing actually finishes --
    confirmed live: a poll reporting '0 of 0' still resulted in most
    contacts being successfully created + enrolled by the time Phase 2
    ran a few seconds later. So instead of trusting the first non-None
    read (the old, wrong behavior), we wait for two consecutive polls
    to agree on a non-(0,0) reading before calling it done."""
    last_counts = None
    stable_streak = 0
    latest = None
    for attempt in range(IMPORT_STATUS_POLL_ATTEMPTS):
        resp = page.request.get(f"{API_BASE}/v2/imports/{import_id}", headers=SESSION_HEADERS)
        if resp.ok:
            data = resp.json()["data"]
            latest = data
            counts = (data.get("imported_people_count"), data.get("current_people_count"))
            if counts[0] is not None:
                if counts == last_counts and counts != (0, 0):
                    stable_streak += 1
                    if stable_streak >= 2:
                        return data
                else:
                    stable_streak = 0
                last_counts = counts
        wait(IMPORT_STATUS_POLL_DELAY)
    if latest is None:
        log("  ⚠️  Import status didn't confirm completion in time -- check Salesloft's Import History manually.")
    else:
        log("  ⚠️  Import status never stabilized on a non-zero reading in the time we waited -- returning "
            "the last poll seen. Salesloft may still be finishing up; check Import History if numbers look off.")
    return latest


def extract_already_taken_emails(errors):
    """Pulls out emails Salesloft rejected as 'already been taken' --
    i.e. the import preview mismatched an existing person as NEW.
    NOTE: these errors show up inconsistently -- sometimes in the
    synchronous POST /api/imports response's 'errors' field, sometimes
    only in the polled GET /v2/imports/{id} status's 'errors_list'
    (confirmed live, different runs showed them in different places).
    Callers should pass in errors merged from both sources."""
    failed = set()
    for err in errors or []:
        params = err.get("params", {}) or {}
        email = (params.get("email_address") or "").strip().lower()
        reasons = (err.get("errors", {}) or {}).get("email_address", [])
        if email and any("already" in r.lower() and "taken" in r.lower() for r in reasons):
            failed.add(email)
    return failed


def resolve_person_id_by_email(page, email):
    """Authoritative existing-person lookup for emails the import
    preview mismatched. Reuses the exact same People-search flow as
    open_contact_profile (Owner > All Users > search bar > '1 People'
    > read id from the row's href) since that flow is already proven
    to work -- just stops short of clicking into the profile, since
    all we need here is the numeric id."""
    page.goto(f"{SALESLOFT_URL}/app/people")
    wait(2.5)
    page.wait_for_load_state("networkidle")
    wait(2)

    owner_btn = page.locator('button:has-text("Owner")').first
    try:
        owner_btn.wait_for(state="visible", timeout=8000)
        owner_btn.click()
        wait(1)
        all_users = page.locator("text=All Users").first
        all_users.wait_for(state="visible", timeout=5000)
        all_users.click()
        wait(1.5)
    except PlaywrightTimeout:
        log(f"     ⚠️  {email}: couldn't open the Owner filter, skipping lookup")
        return None

    search_bar = page.locator('input[placeholder*="Search Person"]').first
    try:
        search_bar.wait_for(state="visible", timeout=10000)
        search_bar.click()
        wait(0.5)
        search_bar.fill("")
        search_bar.type(email, delay=50)
        page.keyboard.press("Enter")
        wait(3)
    except PlaywrightTimeout:
        log(f"     ⚠️  {email}: search bar never appeared, skipping lookup")
        return None

    wait(2)
    people_count = page.locator("text=/^1 People$/").first
    try:
        people_count.wait_for(state="visible", timeout=5000)
    except PlaywrightTimeout:
        log(f"     ⚠️  {email}: search didn't return exactly 1 match, skipping lookup")
        return None

    person_name_link = page.locator('table a[href*="/app/people/"]').first
    try:
        person_name_link.wait_for(state="visible", timeout=8000)
        href = person_name_link.get_attribute("href") or ""
        id_match = PERSON_ID_PATTERN.search(href)
        return id_match.group(1) if id_match else None
    except PlaywrightTimeout:
        log(f"     ⚠️  {email}: found the row but couldn't read its profile link")
        return None


def run_bulk_import(page, csv_path: Path, cadence_id: int, exclude_existing: bool):
    log("\n── PHASE 1: bulk import + cadence enrollment ──\n")

    me = api_get_me(page)
    assigned_account_id = me["id"]

    log("🌐 Uploading CSV...")
    api_csv_setup(page, csv_path)

    log("🗺️  Submitting field mapping...")
    submitted_at = datetime.now(timezone.utc) - timedelta(seconds=5)  # buffer for clock skew
    api_submit_field_mapping(page, csv_path, CSV_FIELD_MAPPING)

    log("🔎 Locating the new import preview...")
    preview_id = find_latest_preview_id(page, csv_path.name, submitted_at)

    log("📋 Fetching validated preview...")
    preview_data = api_get_preview(page, preview_id)

    people, rejects = build_finalize_people(preview_data, exclude_existing=exclude_existing)
    log(f"  → {len(people)} contact(s) will be imported + enrolled in cadence {cadence_id}")

    if not people:
        log("  ❌ Nothing to import after filtering -- stopping Phase 1.")
        return [], rejects

    # NOTE: current_people_count / imported_people_count from the
    # polled status are NOT a reliable success signal -- confirmed
    # live, people showed up actually enrolled in the cadence (visible
    # directly in the Salesloft UI) even when this endpoint reported
    # 0/0. So we submit everyone (new AND matched-existing) together
    # in one commit and treat the counts as informational only, not
    # as pass/fail. (A separate browser-driven 'Add to Cadence' step
    # was tried here and removed -- it was unreliable and redundant
    # once it was confirmed people are enrolled by the import itself.)
    log("🚀 Committing import (create/update people + enroll in cadence)...")
    result = api_finalize_import(page, people, cadence_id, csv_path.name, assigned_account_id)
    import_id = result.get("import", {}).get("id")
    commit_errors = result.get("errors") or []

    status = None
    if import_id:
        log(f"⏳ Waiting for import {import_id} to finish processing...")
        status = poll_import_status(page, import_id)
        if status:
            log(f"  ℹ️  Salesloft reports {status.get('imported_people_count')} of "
                f"{status.get('current_people_count')} contact(s) (this count is unreliable -- "
                f"spot check in the Salesloft UI if in doubt, don't treat 0/0 as failure)")

    status_errors = (status or {}).get("errors_list") or []
    all_errors = commit_errors + status_errors

    already_taken = extract_already_taken_emails(all_errors)
    unrecoverable_errors = [
        e for e in all_errors
        if (e.get("params", {}) or {}).get("email_address", "").strip().lower() not in already_taken
    ]
    if unrecoverable_errors:
        log(f"  ⚠️  Import reported errors we can't auto-resolve: {unrecoverable_errors}")

    if already_taken:
        log(f"  ⚠️  {len(already_taken)} contact(s) already exist in Salesloft under a match the preview "
            f"missed. Resolving their real id and re-submitting just those:")
        for e in sorted(already_taken):
            log(f"     • {e}")

        conflict_people = [
            p for p in people
            if (p.get("email_address") or "").strip().lower() in already_taken and p.get("id") is None
        ]
        resolved_people = []
        for p in conflict_people:
            email = p["email_address"].strip().lower()
            resolved_id = resolve_person_id_by_email(page, email)
            if resolved_id:
                p["id"] = resolved_id
                resolved_people.append(p)
                log(f"     ✓ {email} resolved to id={resolved_id}")
            else:
                log(f"     ❌ could not resolve {email} -- check/add to cadence manually in Salesloft")

        if resolved_people:
            log(f"  🚀 Re-submitting {len(resolved_people)} resolved contact(s)...")
            retry_result = api_finalize_import(page, resolved_people, cadence_id, csv_path.name, assigned_account_id)
            retry_import_id = retry_result.get("import", {}).get("id")
            if retry_import_id:
                log(f"⏳ Waiting for follow-up import {retry_import_id} to finish processing...")
                retry_status = poll_import_status(page, retry_import_id)
                if retry_status:
                    leftover_errors = retry_status.get("errors_list") or []
                    if leftover_errors:
                        log(f"  ⚠️  Follow-up still reported errors: {leftover_errors}")

    return people, rejects


# ──────────────────────────────────────────────
# PHASE 2 -- PER-CONTACT COMPOSE (browser UI, unchanged mechanics)
# ──────────────────────────────────────────────

def get_cadence_name(page, cadence_id):
    resp = page.request.get(f"{API_BASE}/v2/cadences/{cadence_id}", headers=SESSION_HEADERS)
    if not resp.ok:
        raise RuntimeError(f"Could not fetch cadence {cadence_id}: {resp.status}")
    return resp.json()["data"]["name"]


def open_contact_profile(page, email):
    page.goto(f"{SALESLOFT_URL}/app/people")
    wait(2.5)
    page.wait_for_load_state("networkidle")
    wait(2)

    owner_btn = page.locator('button:has-text("Owner")').first
    try:
        owner_btn.wait_for(state="visible", timeout=8000)
        owner_btn.click()
        wait(1)
        all_users = page.locator("text=All Users").first
        all_users.wait_for(state="visible", timeout=5000)
        all_users.click()
        wait(1.5)
    except PlaywrightTimeout:
        return "error_owner_filter", None

    search_bar = page.locator('input[placeholder*="Search Person"]').first
    try:
        search_bar.wait_for(state="visible", timeout=10000)
        search_bar.click()
        wait(0.5)
        search_bar.type(email, delay=50)
        page.keyboard.press("Enter")
        wait(3)
    except PlaywrightTimeout:
        return "error_search_bar", None

    wait(2)
    people_count = page.locator("text=/^1 People$/").first
    try:
        people_count.wait_for(state="visible", timeout=5000)
    except PlaywrightTimeout:
        return "skipped_not_found", None

    person_name_link = page.locator('table a[href*="/app/people/"]').first
    try:
        person_name_link.wait_for(state="visible", timeout=8000)
        href = person_name_link.get_attribute("href") or ""
        id_match = PERSON_ID_PATTERN.search(href)
        person_id = id_match.group(1) if id_match else None
        person_name_link.click()
        page.wait_for_load_state("networkidle")
        wait(2)
    except PlaywrightTimeout:
        return "error_profile_link", None

    return "found", person_id


def open_compose_and_fill(page, cadence_name, subject, body):
    """Same DOM interaction as your original scripts, but the cadence
    box is now matched by a NAME fetched live from Salesloft (see
    get_cadence_name) instead of a hardcoded regex constant. Uses a
    literal-text match (re.escape) rather than a loose pattern, since
    the name itself is always authoritative and current."""
    try:
        cadence_box = page.locator(f"text=/{re.escape(cadence_name)}/").first
        cadence_box.wait_for(state="visible", timeout=6000)
    except PlaywrightTimeout:
        return "skipped_no_cadence"

    try:
        cadence_box.hover()
        wait(1)
        run_btn = page.locator('button:has-text("Run")').first
        run_btn.wait_for(state="visible", timeout=5000)
        run_btn.click()
        wait(1)

        expand_btn = page.locator('button[aria-label="Expand Email Pane"]').first
        expand_btn.wait_for(state="visible", timeout=20000)
        expand_btn.click()
        wait(2)
    except PlaywrightTimeout:
        return "error_compose_pane_not_opened"

    wait(4)
    editor_frame = None
    for _ in range(15):
        for frame in page.frames:
            try:
                count = frame.evaluate('document.querySelectorAll("[contenteditable=true]").length')
                if count > 0:
                    editor_frame = frame
                    break
            except Exception:
                continue
        if editor_frame:
            break
        wait(1)

    if editor_frame is None:
        return "error_editor_frame_not_found"

    body_html = build_paragraphs_html(body)
    editor_frame.evaluate(
        """([htmlContent]) => {
            var editor = document.querySelector('[contenteditable=true]');
            if (!editor) return;
            var walker = document.createTreeWalker(editor, NodeFilter.SHOW_TEXT);
            var node;
            while (node = walker.nextNode()) {
                if (node.textContent.trim() === 'Body') {
                    var span = document.createElement('span');
                    span.innerHTML = htmlContent;
                    node.parentNode.replaceChild(span, node);
                    break;
                }
            }
        }""",
        [body_html],
    )
    wait(1)

    subject_input = page.locator('input[placeholder="Subject"]').first
    try:
        subject_input.wait_for(state="visible", timeout=5000)
        subject_input.click()
        wait(0.2)
        subject_input.fill(subject)
        wait(0.5)
    except PlaywrightTimeout:
        return "error_subject_field_not_found"

    return "staged"


def send_now(page, name):
    log(f"  👀 Review + send email for {name} in the browser.")
    log("     Once sent, press ENTER for next contact (or type 'skip' to skip): ")
    choice = input().strip().lower()
    if choice == "skip":
        log("  ⏭️  Skipped")
        return "skipped_manual"
    log("  ✅ Done")
    return "sent"


def set_timezone_to_fixed_zone(page):
    tz_input = None
    inputs = page.locator('input[id^="downshift-"][id$="-input"]')
    for i in range(inputs.count()):
        try:
            value = inputs.nth(i).input_value(timeout=1000)
        except Exception:
            continue
        if TZ_OFFSET_PATTERN.search(value or ""):
            tz_input = inputs.nth(i)
            break

    if tz_input is None:
        return False

    try:
        tz_input.click(click_count=3)
        tz_input.type(FORCE_TIMEZONE_SEARCH_TEXT, delay=50)
        wait(0.5)
        option = page.locator('[role="option"]').filter(
            has_text=re.compile(re.escape(FORCE_TIMEZONE_SEARCH_TEXT))
        ).first
        option.wait_for(state="visible", timeout=3000)
        option.click()
        wait(0.3)
        return True
    except Exception:
        return False


def navigate_calendar_to_month(page, target_year, target_month):
    calendar = page.locator('[data-testid="date-picker-calendar"]').first
    calendar.wait_for(state="visible", timeout=3000)

    header_row = page.locator('div:has(> button[title="Previous"])').first
    month_year_buttons = header_row.locator("button").filter(has_text=re.compile(r"\S"))
    prev_btn = page.locator('button[title="Previous"]').first
    next_btn = page.locator('button[title="Next"]').first

    for _ in range(24):
        month_text = month_year_buttons.nth(0).inner_text().strip()
        year_text = month_year_buttons.nth(1).inner_text().strip()
        try:
            current_month = datetime.strptime(month_text, "%B").month
        except ValueError:
            current_month = datetime.strptime(month_text, "%b").month
        current_year = int(year_text)

        if (current_year, current_month) == (target_year, target_month):
            return True
        if (current_year, current_month) < (target_year, target_month):
            next_btn.click()
        else:
            prev_btn.click()
        wait(0.3)

    return False


def click_calendar_day(page, day):
    calendar = page.locator('[data-testid="date-picker-calendar"]').first
    day_btn = calendar.locator("button:not([disabled])").filter(
        has_text=re.compile(rf"^{day}$")
    ).first
    day_btn.wait_for(state="visible", timeout=3000)
    day_btn.click()


def resolve_target_dt(row, fallback_dt):
    """Per-contact send time for --mode schedule. A non-empty Send At
    cell on the row wins over the batch's --send-at fallback, so
    different rows in the same run can go out at different times."""
    raw = row.get(COL_SEND_AT, "")
    if raw is None or pd.isna(raw):
        raw = ""
    raw = str(raw).strip()
    if not raw:
        if fallback_dt is None:
            return None, "no Send At value on this row and no batch --send-at fallback"
        return fallback_dt, None
    try:
        naive = datetime.strptime(raw, "%Y-%m-%d %H:%M")
    except ValueError:
        return None, f'unreadable Send At value "{raw}" (expected "YYYY-MM-DD HH:MM")'
    dt = naive.replace(tzinfo=SG_TZ)
    if dt <= datetime.now(SG_TZ):
        return None, f'Send At "{raw}" is in the past'
    return dt, None


def schedule_send(page, target_dt, dry_run):
    try:
        expand_btn = page.locator('button[aria-label="Expand Schedule Send Menu"]').first
        expand_btn.wait_for(state="visible", timeout=5000)
        expand_btn.click()
        wait(1)
    except PlaywrightTimeout:
        return "error_schedule_menu_not_found", None

    if not set_timezone_to_fixed_zone(page):
        return "error_could_not_set_timezone", None

    hours, minutes = FORCE_TIMEZONE_OFFSET
    tz = timezone(timedelta(hours=hours, minutes=minutes))
    local_dt = target_dt.astimezone(tz)

    try:
        date_input = page.locator('input[aria-label="Date Picker Input"]').first
        date_input.click()
        wait(0.3)

        if not navigate_calendar_to_month(page, local_dt.year, local_dt.month):
            return "error_calendar_month_navigation_failed", None
        click_calendar_day(page, local_dt.day)
        wait(0.8)

        time_input = page.locator('input[aria-label="Time Input"]').first
        time_input.fill(local_dt.strftime("%H:%M"))
        wait(0.3)
    except Exception as e:
        return "error_setting_date_time", str(e)

    tz_label = f"UTC{hours:+03d}:{abs(minutes):02d}"
    if dry_run:
        log(f"  🧪 [dry-run] date/time set to {local_dt.strftime('%Y-%m-%d %H:%M')} ({tz_label}) -- not clicking Schedule")
        return "dry_run_staged", None

    try:
        schedule_btn = page.locator('span:has-text("Schedule")').first
        schedule_btn.wait_for(state="visible", timeout=3000)
        schedule_btn.click()
        wait(2)
    except PlaywrightTimeout:
        return "error_schedule_button_not_found", None

    log(f"  → scheduled for {local_dt.strftime('%Y-%m-%d %H:%M')} ({tz_label})")
    return "scheduled", None


def run_compose_loop(page, contacts, cadence_id, mode, target_dt, dry_run):
    log("\n── PHASE 2: per-contact compose + send/schedule ──\n")

    cadence_name = get_cadence_name(page, cadence_id)
    log(f"  Target cadence: \"{cadence_name}\" (id {cadence_id})\n")

    results = []
    for i, row in enumerate(contacts):
        email = str(row[COL_EMAIL]).strip()
        subject = str(row[COL_SUBJECT]).strip()
        body = str(row[COL_BODY]).strip()
        name = str(row.get(COL_NAME, email)).strip()

        log(f"[{i + 1}/{len(contacts)}] {name} <{email}>")

        status, person_id = open_contact_profile(page, email)
        if status != "found":
            log(f"  ⚠️  {status}")
            results.append({"name": name, "email": email, "status": status, "salesloft_person_id": ""})
            wait(DELAY_BETWEEN_CONTACTS)
            continue
        log(f"  🆔 Salesloft person id: {person_id}")

        stage_status = open_compose_and_fill(page, cadence_name, subject, body)
        if stage_status != "staged":
            log(f"  ⚠️  {stage_status}")
            results.append({"name": name, "email": email, "status": stage_status, "salesloft_person_id": person_id or ""})
            wait(DELAY_BETWEEN_CONTACTS)
            continue

        if mode == "now":
            final_status = send_now(page, name)
            entry = {"name": name, "email": email, "status": final_status, "salesloft_person_id": person_id or ""}
            if final_status == "sent":
                entry["scheduled_for"] = datetime.now(SG_TZ).strftime("%Y-%m-%d %H:%M")
            results.append(entry)
        else:
            contact_dt, dt_error = resolve_target_dt(row, target_dt)
            if dt_error:
                log(f"  ❌ error_send_at — {dt_error}")
                results.append({"name": name, "email": email, "status": "error_send_at", "detail": dt_error, "salesloft_person_id": person_id or ""})
                wait(DELAY_BETWEEN_CONTACTS)
                continue

            sched_status, detail = schedule_send(page, contact_dt, dry_run)
            entry = {
                "name": name, "email": email, "status": sched_status,
                "scheduled_for": contact_dt.strftime("%Y-%m-%d %H:%M") if sched_status in ("scheduled", "dry_run_staged") else "",
                "salesloft_person_id": person_id or "",
            }
            if detail:
                entry["detail"] = str(detail)[:300]
            if sched_status not in ("scheduled", "dry_run_staged"):
                log(f"  ❌ {sched_status}" + (f" — {detail}" if detail else ""))
            results.append(entry)

        wait(DELAY_BETWEEN_CONTACTS)

    return results


# ──────────────────────────────────────────────
# MAIN
# ──────────────────────────────────────────────

def parse_args():
    p = argparse.ArgumentParser(description="Consolidated Salesloft import + cadence + send/schedule pipeline")
    p.add_argument("--csv", default="contacts.csv")
    p.add_argument("--cadence-id", type=int, default=None)
    p.add_argument("--mode", choices=["now", "schedule"], default=None)
    p.add_argument("--send-at", default=None, help='Singapore time, "YYYY-MM-DD HH:MM". Batch-wide default for --mode schedule -- '
                   "any row with its own Send At column value overrides this for that row (split-batch scheduling). "
                   "Optional if every row has its own Send At value.")
    p.add_argument("--exclude-existing", action="store_true")
    p.add_argument("--dry-run", action="store_true", help="schedule mode only: fill fields but don't click Schedule")
    p.add_argument("--merge-only", action="store_true",
                    help="Skip the browser entirely -- just merge an existing automation_results.xlsx/.json "
                         "back into --csv to (re)generate the final master-tracker-aligned output.")
    p.add_argument("--results", default="automation_results.xlsx",
                    help="(--merge-only only) path to automation_results.xlsx or .json to merge from.")
    p.add_argument("--out", default=None,
                    help="(--merge-only only) output path. Defaults to <csv_stem>_salesloft_final.csv.")
    args = p.parse_args()

    if args.merge_only:
        return args

    if args.cadence_id is None:
        p.error("--cadence-id is required (unless using --merge-only)")
    if args.mode is None:
        p.error("--mode is required (unless using --merge-only)")

    if args.mode == "schedule" and args.send_at:
        try:
            naive = datetime.strptime(args.send_at, "%Y-%m-%d %H:%M")
        except ValueError:
            p.error('--send-at must look like "2026-07-10 09:00"')
        target = naive.replace(tzinfo=SG_TZ)
        if target <= datetime.now(SG_TZ):
            p.error("--send-at is in the past")
        args.target_dt = target
    else:
        # No batch-wide fallback given -- fine, as long as every row
        # supplies its own Send At value (checked per-contact in
        # run_compose_loop via resolve_target_dt).
        args.target_dt = None

    return args


def format_tracker_date(scheduled_for_str):
    """'2026-07-06 08:38' -> '6 Jul 26' -- date-only, matching the
    tracker's existing 'D Mon YY' style (no leading zero on the day)."""
    try:
        dt = datetime.strptime(str(scheduled_for_str or "").strip(), "%Y-%m-%d %H:%M")
    except ValueError:
        return ""
    return f"{dt.day} {dt.strftime('%b %y')}"


def update_input_csv_with_results(csv_path: Path, results):
    """Writes Phase 2's results straight back into the same CSV that was
    uploaded -- it's already in tracker column shape (exported from the
    tracker in the first place), so there's no need for a separate slim
    results table. Per contact, matched on Email (case-insensitive):
      - Salesloft Link gets the bare numeric person id (same format as
        everywhere else -- see build_tracker_lookup_by_salesloft_id in
        app.py, which joins the Live Feed scraper against this column
        expecting exactly this bare-id shape, not a URL)
      - First Contact is set from this run's schedule/send date, but
        only if it was blank -- a real first-touch date already on the
        row is never overwritten
      - Last Contacted is set from this run's schedule/send date
        unconditionally
      - Status becomes "Pending Response" for contacts that were
        actually scheduled or sent
    Leaves rows with no matching result (e.g. Phase 1 rejects) untouched."""
    df = pd.read_csv(csv_path, keep_default_na=False, dtype=str)
    if COL_EMAIL not in df.columns:
        log("  ⚠️  Input CSV has no Email column -- couldn't write results back into it.")
        return

    for col in ("Salesloft Link", "First Contact", "Last Contacted", "Status"):
        if col not in df.columns:
            df[col] = ""

    by_email = {}
    for r in results:
        email = (r.get("email") or "").strip().lower()
        if email:
            by_email[email] = r

    updated = 0
    for idx, row in df.iterrows():
        email = str(row.get(COL_EMAIL, "")).strip().lower()
        result = by_email.get(email)
        if not result:
            continue

        person_id = str(result.get("salesloft_person_id") or "").strip()
        if person_id:
            df.at[idx, "Salesloft Link"] = person_id

        touch_date = format_tracker_date(result.get("scheduled_for"))
        if touch_date:
            if not str(row.get("First Contact", "")).strip():
                df.at[idx, "First Contact"] = touch_date
            df.at[idx, "Last Contacted"] = touch_date

        if result.get("status") in ("scheduled", "sent"):
            df.at[idx, "Status"] = "Pending Response"

        updated += 1

    if updated:
        df.to_csv(csv_path, index=False)
        log(f"  📌 Updated {updated} row(s) in {csv_path.name}: Salesloft Link (id), First/Last Contact, Status.")


def write_final_salesloft_output(csv_path: Path, results, out_path: Path = None):
    """Builds a clean, master-tracker-aligned copy of the input CSV with
    salesloft_person_id merged into the Salesloft Link column -- WITHOUT
    touching the original csv_path (update_input_csv_with_results already
    overwrites that file in place; this is a separate, non-destructive
    deliverable meant for re-importing / diffing against the master
    tracker). Matched on Email, case-insensitive, same as the in-place
    update. All other original columns are preserved untouched.

    Defaults to <csv_stem>_salesloft_final.csv next to the input file."""
    df = pd.read_csv(csv_path, keep_default_na=False, dtype=str)
    if COL_EMAIL not in df.columns:
        log("  ⚠️  Input CSV has no Email column -- couldn't build final output.")
        return None

    if "Salesloft Link" not in df.columns:
        df["Salesloft Link"] = ""

    by_email = {}
    for r in results:
        email = (r.get("email") or "").strip().lower()
        if email:
            by_email[email] = r

    matched = 0
    for idx, row in df.iterrows():
        email = str(row.get(COL_EMAIL, "")).strip().lower()
        result = by_email.get(email)
        if not result:
            continue
        person_id = str(result.get("salesloft_person_id") or "").strip()
        if person_id:
            df.at[idx, "Salesloft Link"] = person_id
            matched += 1

    out_path = out_path or csv_path.with_name(f"{csv_path.stem}_salesloft_final.csv")
    df.to_csv(out_path, index=False)
    log(f"📁 Final Salesloft-aligned output saved to {out_path.name} "
        f"({matched} row(s) got a Salesloft Link/person ID)")
    return out_path


def run_merge_only(csv_path: Path, results_path: Path, out_path: Path = None):
    """Standalone path (--merge-only): skip the browser entirely and just
    merge an existing automation_results.xlsx/.json back into a CSV. Use
    this if a run already produced results and you just need to
    (re)generate the final aligned output, e.g. after tweaking the input
    CSV or if the automatic in-place write-back was skipped/failed."""
    if not csv_path.exists():
        log(f"❌ CSV not found: {csv_path}")
        sys.exit(1)
    if not results_path.exists():
        log(f"❌ Results file not found: {results_path}")
        sys.exit(1)

    if results_path.suffix.lower() == ".json":
        with open(results_path, "r", encoding="utf-8") as f:
            results = json.load(f)
    else:
        results = pd.read_excel(results_path).to_dict("records")

    write_final_salesloft_output(csv_path, results, out_path)


def run():
    args = parse_args()

    if args.merge_only:
        run_merge_only(
            Path(args.csv).resolve(),
            Path(args.results).resolve(),
            Path(args.out).resolve() if args.out else None,
        )
        return

    csv_path = Path(args.csv).resolve()
    if not csv_path.exists():
        log(f"❌ CSV not found: {csv_path}")
        sys.exit(1)

    contacts = load_contacts(str(csv_path))
    ensure_name_columns_for_import(csv_path)

    with sync_playwright() as p:
        context = p.chromium.launch_persistent_context(
            "./browser_profile",
            headless=False,
            slow_mo=80,
            viewport={"width": 1440, "height": 900},
        )
        page = context.new_page()

        log("🌐 Opening Salesloft...")
        page.goto(SALESLOFT_URL)
        log("\n" + "=" * 55)
        log("👉 Log in via SSO in the browser window, then press ENTER.")
        log("=" * 55 + "\n")
        input("Press ENTER when logged in > ")
        page.goto(SALESLOFT_URL)
        wait(3)

        capture_auth_headers(page)

        imported_people, rejects = run_bulk_import(
            page, csv_path, args.cadence_id, args.exclude_existing
        )

        rejected_emails = {r.get("email_address") for r in rejects if r.get("email_address")}
        remaining_contacts = [c for c in contacts if str(c.get(COL_EMAIL, "")).strip() not in rejected_emails]

        if not remaining_contacts:
            log("\n🛑 No contacts left to compose for after Phase 1 -- stopping.")
            context.close()
            return

        results = run_compose_loop(
            page, remaining_contacts, args.cadence_id, args.mode, args.target_dt, args.dry_run
        )

        context.close()

    success_key = "sent" if args.mode == "now" else "scheduled"
    succeeded = sum(1 for r in results if r["status"] == success_key)
    log("\n── SUMMARY ──────────────────────────────")
    log(f"✅ {succeeded}/{len(remaining_contacts)} contact(s) {success_key}")
    others = [r for r in results if r["status"] != success_key]
    if others:
        log(f"⚠️  {len(others)} need manual attention:")
        for r in others:
            log(f"   • {r['name']} ({r['email']}) — {r['status']}")

    update_input_csv_with_results(csv_path, results)

    pd.DataFrame(results).to_excel("automation_results.xlsx", index=False)
    # Plain-JSON twin of the same results, next to the xlsx -- the Flask
    # app reads this (no pandas dependency needed there) to know which
    # contacts to mark as contacted in the tracker CSV once the job ends.
    with open("automation_results.json", "w", encoding="utf-8") as f:
        json.dump(results, f, ensure_ascii=False, indent=2)
    log("\n📁 Results saved to automation_results.xlsx")

    write_final_salesloft_output(csv_path, results)

    if args.mode == "schedule":
        log("📁 You can review/cancel scheduled sends in Salesloft's Tasks view before send time.")


if __name__ == "__main__":
    run()
