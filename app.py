import csv
import io
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import urllib.request
import uuid
from collections import Counter
from datetime import datetime
from html.parser import HTMLParser

from flask import Flask, render_template, request, redirect, url_for, send_file, jsonify, Response, abort

import cadence_store

app = Flask(__name__)


# ── Rich Messaging preview filter -- Messaging (and any future rich-HTML
# column) can now hold real p/b/ul/li/a markup produced by the Glean
# converter or the Salesloft rich editor, instead of plain text. The dense
# grid views (main tracker, Glean review) show every column as a compact
# single-line cell, which isn't a safe place to either (a) render the raw
# HTML source as visible text (shows literal "<p>...") or (b) let a
# contenteditable div's .textContent-based save touch it (collapses
# separate <p>/<li> blocks together with no separator, silently corrupting
# the structure on any edit). This produces a flattened, readable,
# read-only preview instead; real editing happens at the Salesloft staging
# step where the sanitizing rich editor already lives.
class _HtmlPreviewExtractor(HTMLParser):
    _BLOCK_TAGS = {"p", "li", "div", "br"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts = []

    def handle_starttag(self, tag, attrs):
        if tag.lower() in self._BLOCK_TAGS and self.parts:
            self.parts.append(" / ")

    def handle_data(self, data):
        if data.strip():
            self.parts.append(data.strip())

    def get_text(self):
        return " ".join(" ".join(self.parts).split())


def html_preview_text(value):
    """Jinja filter: flattens rich (or plain) content down to one readable
    preview line. Safe on plain text too -- it just passes through as a
    single segment with no ' / ' separators added."""
    if not value:
        return ""
    parser = _HtmlPreviewExtractor()
    parser.feed(str(value))
    return parser.get_text()


app.jinja_env.filters["html_preview_text"] = html_preview_text

BASE_DIR = os.path.dirname(__file__)
TRACKER_PATH = os.path.join(BASE_DIR, "tracker.csv")
UPLOADS_DIR = os.path.join(BASE_DIR, "uploads")
PROFILES_PATH = os.path.join(BASE_DIR, "profiles.json")
os.makedirs(UPLOADS_DIR, exist_ok=True)

# Shared landing spot for every automation's finished output file, so raw
# scrape/automation output doesn't pile up inside each script's own
# workspace/ folder. A file only ever gets moved here AFTER it has already
# been copied into its staging path (or otherwise consumed) -- see
# move_to_working_files() below and its call sites for the per-automation
# reasoning on exactly when that point is.
WORKING_FILES_DIR = os.path.join(BASE_DIR, "working_files")
os.makedirs(WORKING_FILES_DIR, exist_ok=True)

# Sensible starting widths for columns that tend to run long or short.
# Anything not listed here falls back to a 140px default.
DEFAULT_WIDTHS = {
    "Source / Signal": 110,
    "Patch": 90,
    "Account Executive": 130,
    "Company Name": 200,
    "Contact Name": 160,
    "Title / Role": 220,
    "LinkedIn Profile": 220,
    "Phone": 120,
    "Email": 190,
    "Status": 140,
    "Messaging": 260,
    "About": 260,
    "Experience Description": 260,
    "Reference Script": 260,
    "Sentiments": 220,
    "Total Touches": 100,
}

# Starting widths for the Email Live Feed preview table -- same idea as
# DEFAULT_WIDTHS above, just a separate dict since the column set is
# unrelated to the tracker's. Columns not listed fall back to 140px.
LIVE_FEED_DEFAULT_WIDTHS = {
    "event_type": 150,
    "account_name": 180,
    "account_id": 110,
    "person_name": 160,
    "person_id": 100,
    "action": 140,
    "email_subject": 260,
    "email_id": 100,
    "cadence_name": 200,
    "timestamp_relative": 100,
    "click_count": 90,
    "additional_recipients": 140,
    "more_activities": 140,
}


def get_headers():
    with open(TRACKER_PATH, newline="", encoding="utf-8") as f:
        return next(csv.reader(f), [])


# --- Source profiles: same 4 mapping types as the artifact ------------------
# column  -> straight copy from a raw column, with optional true/false invert
# concat  -> joins two or more raw columns with a separator
# static  -> the same fixed value on every row
# manual  -> you fill this in once per import, applied to every row

DEFAULT_PROFILES = {
    "sigma_report": {
        "display_name": "Sigma Sales Territory Report",
        "mappings": {
            "Company Name": {"type": "column", "raw": "Account Name"},
            "Contact Name": {"type": "column", "raw": "Person Name"},
            "Title / Role": {"type": "column", "raw": "Job Title"},
            "Email": {"type": "column", "raw": "Person Email"},
            "Account Executive": {"type": "column", "raw": "sales_rep_name__c"},
            "Source / Signal": {"type": "static", "value": "Intent Signals"},
            "Patch": {"type": "manual", "default": ""},
        },
    },
    "mql_campaign": {
        "display_name": "MQL / Campaign History",
        "mappings": {
            "Company Name": {"type": "column", "raw": "Account/Company"},
            "Contact Name": {"type": "concat", "raw": ["First Name", "Last Name"], "separator": " "},
            "Email": {"type": "column", "raw": "Email"},
            "Account Executive": {"type": "column", "raw": "Account Owner"},
            "Patch": {"type": "column", "raw": "Country"},
            "Source / Signal": {"type": "static", "value": "MQL"},
        },
    },
    "cloud_mpss": {
        "display_name": "Cloud MPSS Report",
        "mappings": {
            "Company Name": {"type": "column", "raw": "Account Name"},
            "Account Executive": {"type": "column", "raw": "Opportunity Owner"},
            "Source / Signal": {"type": "static", "value": "Sales Report"},
            "Patch": {"type": "manual", "default": ""},
        },
    },
    "community_list": {
        "display_name": "MongoDB Community List",
        "mappings": {
            "Company Name": {"type": "column", "raw": "Account_name"},
            "Account Executive": {"type": "column", "raw": "Account_owner"},
            "Patch": {"type": "column", "raw": "account_country__c"},
            "Contact Name": {"type": "column", "raw": "Contact_name"},
            "Title / Role": {"type": "column", "raw": "title"},
            "Email": {"type": "column", "raw": "email"},
            "Phone": {"type": "column", "raw": "phone"},
            "Source / Signal": {"type": "static", "value": "MDB Skills"},
        },
    },
    "google_news": {
        "display_name": "Google News Scrape",
        "mappings": {
            "Company Name": {"type": "column", "raw": "company_name"},
            "Patch": {"type": "column", "raw": "country"},
            "Source / Signal": {"type": "static", "value": "News"},
        },
    },
    "zoominfo_intent": {
        "display_name": "ZoomInfo Intent Signal",
        "mappings": {
            "Company Name": {"type": "column", "raw": "Company"},
            "Source / Signal": {"type": "static", "value": "Intent Signals"},
            "Patch": {"type": "manual", "default": ""},
        },
    },
    "key_accounts": {
        "display_name": "Key Accounts",
        "mappings": {
            "Company Name": {"type": "column", "raw": "Account Name"},
            "Account Executive": {"type": "manual", "default": ""},
            "Patch": {"type": "manual", "default": ""},
            "Source / Signal": {"type": "static", "value": "AE Contact"},
        },
    },
}


def load_profiles():
    """Profiles live in profiles.json so they're editable and persist across
    restarts. First run seeds the file with the 7 defaults."""
    if os.path.exists(PROFILES_PATH):
        try:
            with open(PROFILES_PATH, "r", encoding="utf-8") as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError):
            pass
    save_profiles(DEFAULT_PROFILES)
    return json.loads(json.dumps(DEFAULT_PROFILES))


def save_profiles(profiles):
    with open(PROFILES_PATH, "w", encoding="utf-8") as f:
        json.dump(profiles, f, indent=2)


def slugify(name):
    s = re.sub(r"[^a-z0-9]+", "_", name.strip().lower())
    return s.strip("_")


STATUS_OPTIONS = [
    "Pending Response",
    "Contacted - Working",
    "Meeting Pending TBC",
    "Meeting booked",
    "No Interest / No Response / Blocked",
    "Left Company",
    "For AE Contact",
]
STATUS_LOOKUP = {s.lower(): s for s in STATUS_OPTIONS}


def build_call_statistics(rows):
    """Per-account breakdown: count of contacts in each Status, how many
    have a phone number on file, the most recent Last Contacted date, and
    the most common Account Executive / Patch for that account (in case a
    handful of rows disagree). Note: 'most recent' is a plain string sort,
    so it's only reliable if your dates are consistently formatted (e.g.
    YYYY-MM-DD)."""
    accounts = {}
    for row in rows:
        company = (row.get("Company Name") or "").strip() or "(no company)"
        status = (row.get("Status") or "").strip()
        phone = (row.get("Phone") or "").strip()
        last_contacted = (row.get("Last Contacted") or "").strip()
        ae = (row.get("Account Executive") or "").strip()
        patch = (row.get("Patch") or "").strip()

        acc = accounts.setdefault(company, {
            "status_counts": {s: 0 for s in STATUS_OPTIONS},
            "blank_status": 0,
            "phone_count": 0,
            "total": 0,
            "last_contacted_dates": [],
            "ae_counts": Counter(),
            "patch_counts": Counter(),
        })

        acc["total"] += 1
        canonical_status = STATUS_LOOKUP.get(status.lower())
        if canonical_status:
            acc["status_counts"][canonical_status] += 1
        else:
            acc["blank_status"] += 1
        if phone:
            acc["phone_count"] += 1
        if last_contacted:
            acc["last_contacted_dates"].append(last_contacted)
        if ae:
            acc["ae_counts"][ae] += 1
        if patch:
            acc["patch_counts"][patch] += 1

    result = []
    for company, data in accounts.items():
        last_dates = sorted(data["last_contacted_dates"], reverse=True)
        top_ae = data["ae_counts"].most_common(1)
        top_patch = data["patch_counts"].most_common(1)
        result.append({
            "company": company,
            "total": data["total"],
            "status_counts": data["status_counts"],
            "blank_status": data["blank_status"],
            "phone_count": data["phone_count"],
            "last_contacted": last_dates[0] if last_dates else "",
            "account_executive": top_ae[0][0] if top_ae else "",
            "patch": top_patch[0][0] if top_patch else "",
        })

    result.sort(key=lambda r: r["company"].lower())
    return result


COUNTRY_CODES = {
    "Malaysia": "60",
    "Indonesia": "62",
    "Japan": "81",
}


def format_whatsapp_number(raw_phone, patch):
    """Cleans a raw phone number into the digits-only format wa.me needs,
    prepending the right country code based on Patch if it looks like a
    local number without one already."""
    if not raw_phone:
        return ""
    digits = re.sub(r"\D", "", raw_phone)
    if not digits:
        return ""

    cc = COUNTRY_CODES.get((patch or "").strip(), "")
    if not cc:
        # Unknown Patch -- return the cleaned digits as-is rather than
        # guessing at a reformat that could make it worse.
        return digits

    if digits.startswith(cc):
        return digits
    if digits.startswith("0"):
        digits = digits[1:]
    return cc + digits


def is_truthy(v):
    return str(v or "").strip().lower() in ("true", "yes", "y", "1")


def apply_mapping(raw_row, profile, canonical_columns, manual_values=None):
    """The same logic as buildRow() in the artifact: walk the profile's
    mappings and fill in only the fields it knows about. Everything else
    in canonical_columns stays blank, same convention as before."""
    out = {col: "" for col in canonical_columns}
    for field, m in (profile.get("mappings") or {}).items():
        if field not in out or not m:
            continue
        mtype = m.get("type")
        if mtype == "column":
            val = raw_row.get(m.get("raw"), "") or ""
            if m.get("invert"):
                val = "FALSE" if is_truthy(val) else "TRUE"
        elif mtype == "concat":
            parts = [raw_row.get(r, "") for r in m.get("raw", [])]
            parts = [p.strip() for p in parts if p and str(p).strip()]
            val = (m.get("separator", " ")).join(parts)
        elif mtype == "static":
            val = m.get("value", "")
        elif mtype == "manual":
            mv = (manual_values or {}).get(field, "")
            val = mv if mv else m.get("default", "")
        else:
            val = ""
        out[field] = val
    return out


def read_csv_dicts(path):
    # utf-8-sig quietly handles the BOM that Excel/Salesforce exports often add
    with open(path, newline="", encoding="utf-8-sig") as f:
        rows = []
        for row in csv.DictReader(f):
            row.pop(None, None)
            rows.append(row)
        return rows


def normalize_mapping_for_editor(profile_mappings, canonical_columns):
    """Flattens each field's mapping into a shape the editor template can
    read without fragile list-indexing in Jinja."""
    result = {}
    for col in canonical_columns:
        m = (profile_mappings or {}).get(col) or {}
        entry = {"type": m.get("type", "blank")}
        if entry["type"] == "column":
            entry["raw"] = m.get("raw", "")
            entry["invert"] = bool(m.get("invert"))
        elif entry["type"] == "concat":
            raw = m.get("raw") or ["", ""]
            entry["concat1"] = raw[0] if len(raw) > 0 else ""
            entry["concat2"] = raw[1] if len(raw) > 1 else ""
            entry["separator"] = m.get("separator", " ")
        elif entry["type"] == "static":
            entry["value"] = m.get("value", "")
        elif entry["type"] == "manual":
            entry["default"] = m.get("default", "")
        result[col] = entry
    return result


def render_preview(source_id, profile, token):
    temp_path = os.path.join(UPLOADS_DIR, f"{token}.csv")
    raw_rows = read_csv_dicts(temp_path)
    canonical_columns = get_headers()

    manual_fields = [
        {"field": field, "default": m.get("default", "")}
        for field, m in profile["mappings"].items()
        if m.get("type") == "manual"
    ]
    preview_rows = [apply_mapping(r, profile, canonical_columns) for r in raw_rows[:5]]

    return render_template(
        "import_preview.html",
        source_id=source_id,
        display_name=profile["display_name"],
        token=token,
        row_count=len(raw_rows),
        canonical_columns=canonical_columns,
        preview_rows=preview_rows,
        manual_fields=manual_fields,
    )


def load_tracker():
    """Reads tracker.csv fresh on every request. Returns (headers, rows)
    where each row is a dict keyed by header name. If a row has more
    columns than the header row -- usually a stray unescaped comma
    somewhere in the source data -- Python's csv module stuffs the
    leftover values under a None key instead of failing outright. We drop
    that here so the rest of the app never has to think about it; it just
    means that one row's extra value(s) get silently dropped rather than
    crashing every save.
    """
    if not os.path.exists(TRACKER_PATH):
        return [], []
    with open(TRACKER_PATH, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        headers = reader.fieldnames or []
        rows = []
        for row in reader:
            row.pop(None, None)
            rows.append(row)
    return headers, rows


def write_tracker(headers, rows):
    with open(TRACKER_PATH, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=headers)
        writer.writeheader()
        writer.writerows(rows)


def find_duplicate_contact_names(rows):
    names = [r.get("Contact Name", "").strip() for r in rows if r.get("Contact Name", "").strip()]
    counts = Counter(names)
    return sorted(name for name, c in counts.items() if c > 1)


@app.route("/")
def index():
    headers, rows = load_tracker()
    total = len(rows)

    def count_by(column):
        counts = {}
        for r in rows:
            val = (r.get(column) or "").strip() or "(blank)"
            counts[val] = counts.get(val, 0) + 1
        ordered = sorted(counts.items(), key=lambda kv: -kv[1])
        return [{"label": k, "count": v, "pct": round((v / total * 100) if total else 0, 1)} for k, v in ordered]

    def count_where(column, match_value):
        match_value = match_value.strip().lower()
        return sum(1 for r in rows if (r.get(column) or "").strip().lower() == match_value)

    def count_filled(column):
        return sum(1 for r in rows if (r.get(column) or "").strip())

    def is_real_phone(value):
        # Values containing parentheses are placeholders / non-numbers
        # (e.g. "(none)", "(N/A)") rather than an actual phone number.
        value = (value or "").strip()
        return bool(value) and "(" not in value and ")" not in value

    phone_count = sum(1 for r in rows if is_real_phone(r.get("Phone")))

    kpis = {
        "total": total,
        "meetings_booked": count_where("Status", "Meeting booked"),
        "pending_response": count_where("Status", "Pending Response"),
        "coffee_chats": count_where("Potential Coffee Chats", "TRUE"),
        "linkedin_pct": round((count_filled("LinkedIn Profile") / total * 100) if total else 0, 1),
        "phone_pct": round((phone_count / total * 100) if total else 0, 1),
    }

    # Contacts per account (Company Name), sorted highest first.
    account_counts = {}
    for r in rows:
        company = (r.get("Company Name") or "").strip() or "(blank)"
        account_counts[company] = account_counts.get(company, 0) + 1
    ordered_accounts = sorted(account_counts.items(), key=lambda kv: -kv[1])
    account_contact_counts = [
        {"label": k, "count": v, "pct": round((v / total * 100) if total else 0, 1)}
        for k, v in ordered_accounts
    ]

    # Contacts stuck at "Contacted - Working" or "Meeting Pending TBC" --
    # a quick worklist of who needs a next step.
    followup_statuses = {"contacted - working", "meeting pending tbc"}
    needs_followup = [
        r for r in rows
        if (r.get("Status") or "").strip().lower() in followup_statuses
    ]

    return render_template(
        "index.html",
        headers=headers,
        row_count=total,
        kpis=kpis,
        patch_counts=count_by("Patch"),
        status_counts=count_by("Status"),
        source_counts=count_by("Source / Signal"),
        ae_counts=count_by("Account Executive"),
        account_contact_counts=account_contact_counts,
        needs_followup=needs_followup,
    )


@app.route("/add", methods=["POST"])
def add_contact():
    headers = get_headers()
    new_row = {h: request.form.get(h, "").strip() for h in headers}

    with open(TRACKER_PATH, "a", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=headers)
        writer.writerow(new_row)

    return redirect(url_for("dashboard_page"))


@app.route("/update_cell", methods=["POST"])
def update_cell():
    data = request.get_json(silent=True) or {}
    try:
        row_index = int(data.get("row"))
    except (TypeError, ValueError):
        return jsonify({"error": "bad row index"}), 400
    column = data.get("column")
    value = data.get("value", "")

    headers, rows = load_tracker()
    if column not in headers:
        return jsonify({"error": "unknown column"}), 400
    if row_index < 0 or row_index >= len(rows):
        return jsonify({"error": "row out of range"}), 400

    rows[row_index][column] = value
    write_tracker(headers, rows)

    warning = None
    rules = load_validation_rules()
    allowed = resolve_validation_options(rules).get(column)
    if allowed and value.strip() and value.strip() not in allowed:
        warning = "\"" + value.strip() + "\" isn't in the expected list for " + column + ". Saved anyway -- expected one of: " + ", ".join(allowed)

    return jsonify({"ok": True, "warning": warning})


@app.route("/import")
def import_page():
    return render_template("import.html", profiles=load_profiles())


@app.route("/import/preview", methods=["POST"])
def import_preview():
    source_id = request.form.get("source_id")
    file = request.files.get("csv_file")

    if not source_id or not file or file.filename == "":
        return redirect(url_for("import_page"))

    token = uuid.uuid4().hex
    temp_path = os.path.join(UPLOADS_DIR, f"{token}.csv")
    file.save(temp_path)

    if source_id == "__new__":
        return redirect(url_for("import_edit", token=token))

    profile = load_profiles().get(source_id)
    if not profile:
        return redirect(url_for("import_page"))

    return render_preview(source_id, profile, token)


@app.route("/import/edit")
def import_edit():
    token = request.args.get("token", "")
    source_id = request.args.get("source_id", "")
    temp_path = os.path.join(UPLOADS_DIR, f"{token}.csv")

    if not token or not os.path.exists(temp_path):
        return redirect(url_for("import_page"))

    with open(temp_path, newline="", encoding="utf-8-sig") as f:
        headers = next(csv.reader(f), [])

    profiles = load_profiles()
    profile = profiles.get(source_id)
    canonical_columns = get_headers()

    return render_template(
        "import_edit.html",
        token=token,
        source_id=source_id if profile else "",
        display_name=profile["display_name"] if profile else "",
        headers=headers,
        canonical_columns=canonical_columns,
        mappings=normalize_mapping_for_editor(profile["mappings"] if profile else {}, canonical_columns),
    )


@app.route("/import/save_mapping", methods=["POST"])
def import_save_mapping():
    token = request.form.get("token", "")
    existing_source_id = request.form.get("source_id", "").strip()
    name = request.form.get("display_name", "").strip()
    canonical_columns = get_headers()

    mappings = {}
    for col in canonical_columns:
        ftype = request.form.get(f"type__{col}", "blank")
        if ftype == "column":
            raw = request.form.get(f"column__{col}", "")
            if raw:
                entry = {"type": "column", "raw": raw}
                if request.form.get(f"invert__{col}"):
                    entry["invert"] = True
                mappings[col] = entry
        elif ftype == "concat":
            c1 = request.form.get(f"concat1__{col}", "")
            c2 = request.form.get(f"concat2__{col}", "")
            sep = request.form.get(f"sep__{col}", " ")
            if c1 or c2:
                mappings[col] = {"type": "concat", "raw": [c1, c2], "separator": sep}
        elif ftype == "static":
            mappings[col] = {"type": "static", "value": request.form.get(f"static__{col}", "")}
        elif ftype == "manual":
            mappings[col] = {"type": "manual", "default": request.form.get(f"manual__{col}", "")}

    profiles = load_profiles()
    if existing_source_id and existing_source_id in profiles:
        source_id = existing_source_id
        if not name:
            name = profiles[source_id]["display_name"]
    else:
        if not name:
            name = "Untitled source"
        base = slugify(name) or "source"
        source_id = base
        n = 2
        while source_id in profiles:
            source_id = f"{base}_{n}"
            n += 1

    profile = {"display_name": name, "mappings": mappings}
    profiles[source_id] = profile
    save_profiles(profiles)

    return render_preview(source_id, profile, token)


@app.route("/import/commit", methods=["POST"])
def import_commit():
    source_id = request.form.get("source_id")
    token = request.form.get("token")
    profile = load_profiles().get(source_id)
    temp_path = os.path.join(UPLOADS_DIR, f"{token}.csv") if token else None

    if not profile or not temp_path or not os.path.exists(temp_path):
        return redirect(url_for("import_page"))

    manual_values = {
        field: request.form.get(field, "").strip()
        for field, m in profile["mappings"].items()
        if m.get("type") == "manual"
    }

    canonical_columns = get_headers()
    raw_rows = read_csv_dicts(temp_path)
    mapped_rows = [apply_mapping(r, profile, canonical_columns, manual_values) for r in raw_rows]

    with open(TRACKER_PATH, "a", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=canonical_columns)
        writer.writerows(mapped_rows)

    os.remove(temp_path)

    return redirect(url_for("dashboard_page"))


@app.route("/delete_row", methods=["POST"])
def delete_row():
    try:
        row_index = int(request.form.get("row"))
    except (TypeError, ValueError):
        return redirect(url_for("dashboard_page"))

    headers, rows = load_tracker()
    if 0 <= row_index < len(rows):
        rows.pop(row_index)
        write_tracker(headers, rows)

    return redirect(url_for("dashboard_page"))


@app.route("/delete_rows", methods=["POST"])
def delete_rows():
    raw_indices = request.form.getlist("rows")
    try:
        indices = sorted({int(i) for i in raw_indices}, reverse=True)
    except ValueError:
        return redirect(url_for("dashboard_page"))

    headers, rows = load_tracker()
    for idx in indices:
        if 0 <= idx < len(rows):
            rows.pop(idx)
    write_tracker(headers, rows)

    return redirect(url_for("dashboard_page"))


@app.route("/calls")
def call_dashboard():
    headers, rows = load_tracker()
    account_filter = (request.args.get("account") or "").strip()
    status_filter = (request.args.get("status") or "").strip()
    active_tab = request.args.get("tab", "list")

    contacts = []
    for i, row in enumerate(rows):
        phone = (row.get("Phone") or "").strip()
        patch = (row.get("Patch") or "").strip()
        wa_number = format_whatsapp_number(phone, patch)
        contacts.append({
            "row": i,
            "source": row.get("Source / Signal", ""),
            "company": row.get("Company Name", ""),
            "contact": row.get("Contact Name", ""),
            "title": row.get("Title / Role", ""),
            "linkedin": row.get("LinkedIn Profile", ""),
            "interest_research": row.get("Interest / Role Research", ""),
            "patch": patch,
            "phone": phone,
            "email": row.get("Email", ""),
            "first_contact": row.get("First Contact", ""),
            "last_contacted": row.get("Last Contacted", ""),
            "status": row.get("Status", ""),
            "sentiments": row.get("Sentiments", ""),
            "reference_script": row.get("Reference Script", ""),
            "whatsapp_message": row.get("Whatsapp Message", ""),
            "wa_link": f"https://wa.me/{wa_number}" if wa_number else "",
        })

    if account_filter:
        contacts = [c for c in contacts if c["company"] == account_filter]
    if status_filter:
        contacts = [c for c in contacts if c["status"].strip().lower() == status_filter.strip().lower()]

    return render_template(
        "call_dashboard.html",
        contacts=contacts,
        count=len(contacts),
        account_filter=account_filter,
        status_filter=status_filter,
        active_tab=active_tab,
        stats=build_call_statistics(rows),
        status_options=STATUS_OPTIONS,
    )


OLLAMA_URL = "http://localhost:11434/api/chat"
OLLAMA_MODEL = "qwen3:8b"

PROOF_POINTS_PATH = os.path.join(BASE_DIR, "proof_points.csv")
USE_CASES_PATH = os.path.join(BASE_DIR, "use_cases.csv")
ACCOUNTS_PATH = os.path.join(BASE_DIR, "accounts.csv")
GENERATION_SETTINGS_PATH = os.path.join(BASE_DIR, "generation_settings.json")

NEWS_WORKSPACE_DIR = os.path.join(BASE_DIR, "news_workspace")
os.makedirs(NEWS_WORKSPACE_DIR, exist_ok=True)
NEWS_SCRIPT_PATH = os.path.join(NEWS_WORKSPACE_DIR, "main.py")
NEWS_KEYWORDS_PATH = os.path.join(NEWS_WORKSPACE_DIR, "keywords.csv")
NEWS_OUTPUT_DIR = os.path.join(NEWS_WORKSPACE_DIR, "output")
NEWS_SELECTED_ACCOUNTS_PATH = os.path.join(NEWS_WORKSPACE_DIR, "selected_accounts.csv")
NEWS_SELECTED_KEYWORDS_PATH = os.path.join(NEWS_WORKSPACE_DIR, "selected_keywords.csv")

# Each action fills one or more PG Tracker columns from one Ollama call.
# "email" asks for structured JSON since it fills two fields at once;
# the other two are single plain-text fields.
GENERATION_ACTIONS = {
    "email": {"label": "Email", "target_fields": ["Subject Line", "Messaging"], "json_output": True},
    "whatsapp": {"label": "WhatsApp", "target_fields": ["Whatsapp Message"], "json_output": False},
    "call_script": {"label": "Call Script", "target_fields": ["Reference Script"], "json_output": False},
}

EMAIL_DEFAULT_PROMPT = """You write personalised cold outreach emails from Kayley Yeo, Account Development Representative at MongoDB, to a prospect. The email should feel human, warm, and confident, never salesy or try-hard.

Structure, in this exact order:
1. Greeting on its own line: "Hello [Name],"
2. Introduction: open with a brief, warm self-introduction along the lines of "Nice to meet you, this is Kayley from MongoDB." Never state a job title or role anywhere in the introduction (no "Account Development Representative", no "ADR", no title at all). Then lightly anchor MongoDB's relevance to their industry in 1-2 short sentences. Do not make it sound like a pitch.
3. Hook: if the context includes a specific trigger (recent role change, LinkedIn post, event, company news), lead with it lightly. If no specific trigger exists, frame around the kinds of conversations MongoDB is having with similar roles in that industry. 2-3 sentences max.
4. Bridge: one short transition sentence connecting the hook to why you are about to share relevant use cases.
5. Use cases: choose 2-3 from the provided use case list below, matching the contact's industry and role. Write each use case as a short label followed by a colon, then one short sentence on the same line, for example "Scaling Write Intensive Workloads: High growth platforms see performance degrade..." Use the exact use case name from the provided list, but rewrite the problem statement and MongoDB fit in your own words, adapted specifically to this contact's role and signal. Never copy the reference list's sentence verbatim, word for word. Never write a pure capability line that only describes MongoDB without naming the problem. Do not invent new use cases.
6. Proof point: choose the single most relevant proof point from the provided list below, matching industry or theme. Introduce it with a natural connector phrase such as "I wanted to share an example of...", "For example, ...", or "One example that comes to mind is..." -- never literally write the word "Proof point" as a label or heading in the output. Write it as 2-3 sentences: the challenge, then the MongoDB outcome. Never invent a customer name, metric, or detail not in the provided list.
7. Closing CTA: end with a soft, direct question inviting a low-pressure next step, phrased close to "Would you be open to an introductory session to share more about [topic], which seems relevant to your priorities?" Do not mention a specific day or time.

Formatting rules, critical for downstream automation:
- Plain text only. Never use asterisks, bold, italics, markdown headers, or any other markdown formatting anywhere in the output. This text gets pasted directly into an email tool that does not render markdown, so any asterisks would show up literally in the sent email.
- Separate each section (greeting, introduction, hook, bridge, each use case, proof point, CTA) with a real blank line (an actual newline character, not the literal text "\\n"). The email should read as distinct short paragraphs, not one dense block of text.

Tone and style rules:
- Short sentences, warm and human, not corporate
- Confident, not pushy
- No buzzwords: avoid synergy, leverage, best in class, cutting edge, revolutionize
- No dashes of any kind, no em dashes, no hyphens as connectors -- use commas or rephrase
- British spelling throughout: organisations not organizations, colour not color
- Total length 150 to 250 words, never exceed 300

Subject line topic: provide a short topic phrase only, the single most relevant use case for this prospect, 4-6 words, no hyphens, no company name, and no "MongoDB" mention -- just the topic itself, for example "Real Time Fraud Detection" or "Scaling Write Intensive Workloads". The full subject line is assembled separately from this phrase, you only provide the short phrase.

Output valid JSON only, in this exact shape, with no text before or after the JSON:
{"topic": "...", "email_body": "..."}"""

EMAIL_JAPAN_PROMPT = """You write bilingual (English then Japanese) cold outreach emails from Kayley Yeo, Account Development Representative at MongoDB, to a prospect at a Japan-based account. The email must reflect Japanese business formality: respectful, structured, confident without being pushy.

You provide the English and Japanese content as separate pieces below. Do not assemble a subject line and do not combine the two language bodies yourself -- that is handled separately, outside of what you write. Just write excellent content for each piece.

ENGLISH EMAIL BODY structure, in order:
1. Formal greeting: "Dear [Name],"
2. Self-introduction: full name, role, and remit (Account Development Representative at MongoDB, supporting organisations across the contact's industry sector in the Japan region). 2 sentences, formal.
3. Signal-based hook: reference the trigger or signal from the contact's context, respectfully, 2-3 sentences.
4. MongoDB relevance to their industry: 1-2 sentences.
5. 2 to 3 use cases, chosen from the provided list below, prioritising real-time applications, scaling, flexible data architecture, and AI/search themes. Avoid Kubernetes, platform engineering, or observability as the primary angle unless the contact's context clearly justifies it. Write each use case as a short label followed by a colon, then a 2-4 sentence description on the same line. Use the exact use case name from the provided list, but rewrite the description in your own words, adapted to this specific contact. Never copy the reference list's sentence verbatim.
6. Proof point: choose the single most relevant from the provided list, matching industry or theme. 2-3 sentences, then the reference URL on its own line. Never invent a customer, metric, or URL not in the provided list.
7. Formal CTA inviting a discussion at their convenience.
8. One final polite sentence only.

JAPANESE EMAIL BODY structure (a faithful adaptation of the English version's content, not a literal translation, covering the same use cases and the same proof point), in order:
1. Greeting with apology for unsolicited contact
2. Self-introduction
3. Reason for contact
4. MongoDB relevance to their industry
5. The same use cases as the English version, translated into natural business Japanese
6. The same proof point, translated faithfully, keep the reference URL in English on its own line
7. CTA at their convenience
8. One final polite sentence only

Use keigo throughout the Japanese version. Address the contact as [Name]\u6a23.

TOPIC: a short topic phrase, the single most relevant use case for this prospect, 4-6 words, no hyphens, no company name, and no "MongoDB" mention -- just the topic itself.
TOPIC TRANSLATED: a natural Japanese translation of that exact topic phrase only, not a full subject line.

Rules for both email bodies:
- Plain text only. Never use asterisks, bold, italics, markdown headers, or any other markdown formatting anywhere. This text gets pasted directly into an email tool that does not render markdown.
- Separate each section with a real blank line (an actual newline character, not the literal text "\\n").
- Never include a sign-off, signature block, or sender identity line at the end of either body. Never include "Warm regards", "Kind regards", "Kayley Yeo" as a closing line, her title as a closing line, or any Japanese closing formula. End only with the CTA and the one final polite sentence.
- Never use dashes of any kind, in either language.
- British spelling in the English version.
- No buzzwords.
- English body 200-320 words.

Both the English and Japanese bodies are required, with no exceptions. A response missing either one is incomplete and incorrect.

Output valid JSON only, in this exact shape, with no text before or after the JSON:
{"topic_en": "...", "topic_jp": "...", "email_body_en": "...", "email_body_jp": "..."}"""

WHATSAPP_DEFAULT_PROMPT = """You write short WhatsApp outreach messages from Kayley, an SDR at MongoDB, to a prospect. Messages must be brief, conversational, never a pitch.

Step 1, classify the contact's seniority tier from their title:
- Tier 1, working level: Developer, Engineer, Analyst, Architect, individual contributor. Tone: casual, curious, peer to peer, no sales speak. Lead with genuine interest in what they're building. Mention MongoDB only with a clean relevant hook.
- Tier 2, mid management: Manager, Senior Manager, Head of (technical or functional team), Team Lead. Tone: professional, approachable, use-case led. Position MongoDB around one relevant use case or theme that fits their function.
- Tier 3, senior or executive: Director, VP, C-suite, Country or Regional Head. Tone: concise, outcome oriented, respectful of their time. Lead with strategic priorities, not product features.

Step 2, if a hook exists (event attendance, webinar, campaign engagement, LinkedIn activity, company news), use it as a single opening sentence, replacing the default opener. Use the strongest hook only, never stack multiple hooks, skip it if weak or generic.

Step 3, choose one relevant theme or use case from the provided list below that fits the contact's role, industry, and tier. Never invent a use case not in the provided list.

Greeting: vary the wording each time, but always use first name only, introduce yourself as Kayley from MongoDB, friendly and brief, 0-1 emoji max.

Output rules:
- 3 to 6 sentences total
- One CTA only, tier-appropriate
- No bullet points, headers, or markdown formatting -- plain text only
- No MongoDB jargon unless the contact is clearly technical
- Never use dashes of any kind, use commas or periods instead
- No hashtags, no exclamation marks unless an energetic tone is clearly appropriate

Output only the final message text, nothing else."""

WHATSAPP_JAPAN_PROMPT = """You write short WhatsApp outreach messages from Kayley, an SDR at MongoDB, to a prospect at a Japan-based account. The message itself stays in English.

Step 1, classify the contact's seniority tier from their title:
- Tier 1, working level: Developer, Engineer, Analyst, Architect, individual contributor. Tone: casual, curious, peer to peer.
- Tier 2, mid management: Manager, Senior Manager, Head of (technical or functional team), Team Lead. Tone: professional, use-case led.
- Tier 3, senior or executive: Director, VP, C-suite, Country or Regional Head. Tone: concise, outcome oriented.

Step 2, if a hook exists in the contact's context, use it as a single opening sentence. Use the strongest hook only, skip if weak or generic.

Step 3, choose one relevant theme or use case from the provided list below, prioritising real-time, scaling, flexible data architecture, and AI/search themes. Avoid Kubernetes, platform engineering, or observability as the main angle. Never invent a use case not in the provided list.

Greeting: first name only, introduce yourself as Kayley from MongoDB, friendly and brief, 0-1 emoji max.

Output rules, strict for automation compatibility:
- 6 sentences or fewer
- Plain text only -- absolutely no bold, no asterisks, no markdown formatting of any kind
- One CTA only, tier-appropriate
- Never use dashes of any kind, use commas or periods instead
- No hashtags, no bullet points

Output only the final message text, nothing else."""

CALL_SCRIPT_DEFAULT_PROMPT = """You write a tailored MongoDB cold call script for SDR phone outreach, based on the contact and account context provided. Write for live conversation, not marketing copy. Keep it natural and easy to speak aloud, not stiff or scripted.

Produce exactly these sections, in this order:

OPENING
"Hi [Name], it's Kayley from MongoDB, [we have not spoken before, or another fitting context phrase]. Do you have 30 seconds?"

REASON FOR CALLING
Choose one value hook based on the account context:
- If MongoDB already exists in the account: reference that internal relationship and ask if there's a fit for their team.
- If their tech stack is known: reference it and connect to how similar technical leaders use MongoDB alongside it.
- If there's a clear trigger or signal: reference it and connect to relevant themes from the provided use case list below.
- Otherwise: reference working with similar teams on relevant themes from the provided use case list.
Choose only one hook, do not mix several. End with one discovery question about what they're currently working on or evaluating.

THE ASK
A simple next-step ask for a 30 minute conversation, naming the meeting type that fits their technical depth.

VOICEMAIL VERSION
A short voicemail using the same hook, ending with "I'll send a quick follow up note by email. Talk soon."

OBJECTION HANDLING (2 to 3 most likely objections for this contact)
For each: Acknowledge (one short empathetic line), Explore (one curious follow-up question), Redirect (one line connecting back to a relevant use case from the provided list). Never argue, never overexplain.

Rules:
- Never use dashes of any kind, use commas or periods instead
- Do not overpitch the platform, do not lead with AI or vector search unless the contact's context clearly justifies it
- Do not name-drop internal contacts
- Do not make unsupported claims, never invent account facts not provided
- Choose use cases and value hooks only from the provided list below, never invent your own
- Plain text only, no bold or markdown formatting

Output only the script sections above, nothing else."""

CALL_SCRIPT_JAPAN_PROMPT = """You write a tailored MongoDB cold call script in English for SDR phone outreach to a contact at a Japan-based account. Write for live conversation, natural and easy to speak aloud.

Produce exactly these sections, in this order:

OPENING
"Hi [Name], it's Kayley from MongoDB, [appropriate context phrase]. Do you have 30 seconds?"

REASON FOR CALLING
Choose one value hook, prioritising themes from the provided use case list below that relate to real-time applications, scaling, flexible data architecture, or AI/search. Avoid Kubernetes, platform engineering, or observability as the primary angle. End with one discovery question about what they're currently working on.

THE ASK
A simple next-step ask for a 30 minute conversation, naming an appropriate meeting type.

VOICEMAIL VERSION
A short voicemail using the same hook, ending with "I'll send a quick follow up note by email. Talk soon."

OBJECTION HANDLING (2 to 3 most likely objections)
For each: Acknowledge, Explore, Redirect using a relevant use case from the provided list. Never argue or overexplain.

Rules:
- English only
- Never use dashes of any kind
- Do not overpitch, do not lead with AI or vector search unless clearly justified
- Never invent account facts, use cases, or proof points not in the provided lists
- Plain text only, no bold or markdown formatting

Output only the script sections above, nothing else."""

DEFAULT_GENERATION_SETTINGS = {
    "email": {"default": EMAIL_DEFAULT_PROMPT, "japan": EMAIL_JAPAN_PROMPT},
    "whatsapp": {"default": WHATSAPP_DEFAULT_PROMPT, "japan": WHATSAPP_JAPAN_PROMPT},
    "call_script": {"default": CALL_SCRIPT_DEFAULT_PROMPT, "japan": CALL_SCRIPT_JAPAN_PROMPT},
}


LIBRARY_FILES = {
    "accounts": ACCOUNTS_PATH,
    "proof_points": PROOF_POINTS_PATH,
    "use_cases": USE_CASES_PATH,
    "news_keywords": NEWS_KEYWORDS_PATH,
}
LIBRARY_LABELS = {
    "accounts": "Accounts",
    "proof_points": "Proof Points",
    "use_cases": "Use Cases",
    "news_keywords": "News Keywords",
}


def load_csv_full(path):
    """Like load_tracker, but for any CSV file -- used for the reference
    libraries. Drops the None key from malformed rows the same way."""
    if not os.path.exists(path):
        return [], []
    with open(path, newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        headers = reader.fieldnames or []
        rows = []
        for row in reader:
            row.pop(None, None)
            rows.append(row)
    return headers, rows


def write_csv_full(path, headers, rows):
    """Writes atomically: build the file fully in a temp file in the same
    directory, then os.replace() it into place. os.replace is atomic on
    POSIX, so any concurrent reader either sees the complete old file or
    the complete new one -- never a half-written/interleaved mix. This
    matters because background job threads (scraper completion copies)
    and request handlers can touch the same staging CSV close together in
    time; without this, a reader or another writer can catch a file
    mid-write and corrupt content across rows/columns."""
    directory = os.path.dirname(path) or "."
    fd, tmp_path = tempfile.mkstemp(prefix=".tmp_write_", dir=directory)
    try:
        with os.fdopen(fd, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=headers)
            writer.writeheader()
            writer.writerows(rows)
        os.replace(tmp_path, path)
    except Exception:
        if os.path.exists(tmp_path):
            os.remove(tmp_path)
        raise


def atomic_copyfile(src, dst):
    """Same atomicity guarantee as write_csv_full, for the "copy scraper
    output straight into the staging CSV" case. shutil.copyfile alone is
    a chunked read/write, not atomic -- a request that reads or writes
    `dst` while this copy is mid-flight can catch a half-written file.
    Copying to a temp file first and os.replace()-ing it in closes that
    window."""
    directory = os.path.dirname(dst) or "."
    fd, tmp_path = tempfile.mkstemp(prefix=".tmp_copy_", dir=directory)
    os.close(fd)
    try:
        shutil.copyfile(src, tmp_path)
        os.replace(tmp_path, dst)
    except Exception:
        if os.path.exists(tmp_path):
            os.remove(tmp_path)
        raise


def _sync_meta_path(staging_path):
    """Sidecar path for the "which raw scraper file is this staging CSV
    currently synced from" marker -- lives right next to the staging CSV
    itself, e.g. account_staging.csv -> account_staging.csv.sync.json."""
    return staging_path + ".sync.json"


def write_staging_sync_meta(staging_path, output_file):
    """Records which timestamped scraper output file was just copied into
    `staging_path`, and when. Called right alongside every atomic_copyfile
    that syncs a fresh scrape into a fixed-name staging CSV, so the preview
    pages can show "synced from <file>" instead of leaving that invisible.
    Best-effort: a failure here should never take down the actual scrape/
    copy it's describing, so this only ever logs to stderr, never raises."""
    try:
        meta = {
            "output_file": output_file,
            "synced_at": datetime.now().isoformat(timespec="seconds"),
        }
        tmp_path = _sync_meta_path(staging_path) + ".tmp"
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(meta, f)
        os.replace(tmp_path, _sync_meta_path(staging_path))
    except Exception as e:
        print(f"WARNING: could not write sync metadata for {staging_path}: {e}", file=sys.stderr)


def read_staging_sync_meta(staging_path):
    """Returns {"output_file": ..., "synced_at": ...} for the given staging
    CSV, or None if it's never been synced from a scraper run (e.g. it was
    hand-edited/uploaded, or no run has completed yet in this install)."""
    meta_path = _sync_meta_path(staging_path)
    if not os.path.exists(meta_path):
        return None
    try:
        with open(meta_path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def move_to_working_files(src_path, prefix):
    """Moves a finished automation output file out of its workspace/ folder
    and into WORKING_FILES_DIR, renamed to '<prefix>-<timestamp><ext>'.

    Only call this AFTER src_path has already been copied into its staging
    path (or otherwise fully consumed, e.g. Salesloft's results JSON) --
    this function does not itself preserve anything, it just relocates the
    file. Calling it too early (before that copy/read happens) would lose
    data, not just move it.

    Best-effort and non-fatal: a missing source file or a move failure is
    logged to stderr and returns None rather than raising, since this is
    tidiness, not a step the actual pipeline depends on."""
    if not src_path or not os.path.exists(src_path):
        return None
    try:
        ext = os.path.splitext(src_path)[1]
        timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        dest_name = f"{prefix}-{timestamp}{ext}"
        dest_path = os.path.join(WORKING_FILES_DIR, dest_name)
        # Guard against 2 jobs finishing in the same second (e.g. Glean's
        # 2 prompts-per-contact jobs, or a quick back-to-back manual run).
        counter = 1
        while os.path.exists(dest_path):
            dest_name = f"{prefix}-{timestamp}-{counter}{ext}"
            dest_path = os.path.join(WORKING_FILES_DIR, dest_name)
            counter += 1
        shutil.move(src_path, dest_path)
        return dest_path
    except Exception as e:
        print(f"WARNING: could not move {src_path} to working_files: {e}", file=sys.stderr)
        return None


_CSV_PATH_LOCKS = {}
_CSV_PATH_LOCKS_META_LOCK = threading.Lock()


def csv_lock(path):
    """Returns a threading.Lock dedicated to this exact file path (created
    on first use, reused after). Use as a context manager around any
    read-modify-write sequence (load_csv_full -> mutate -> write_csv_full)
    or copy-into-place operation on that path, so two threads touching the
    same staging CSV at once serialize instead of interleaving. Atomicity
    from write_csv_full/atomic_copyfile prevents torn bytes even without
    this, but the lock additionally prevents a read-modify-write from
    clobbering a near-simultaneous copy (or vice versa) with stale data."""
    normalized = os.path.abspath(path)
    with _CSV_PATH_LOCKS_META_LOCK:
        lock = _CSV_PATH_LOCKS.get(normalized)
        if lock is None:
            lock = threading.Lock()
            _CSV_PATH_LOCKS[normalized] = lock
        return lock


SCRIPTS_DIR = os.path.join(BASE_DIR, "scripts")
os.makedirs(SCRIPTS_DIR, exist_ok=True)

# Registry of runnable scripts. Sprint 8/9 will add the real LinkedIn scraper
# and Salesloft script here -- same shape, just a different command.
AVAILABLE_SCRIPTS = {
    "test_dummy": {
        "label": "Test script (dummy)",
        "description": "Proves the run / status / log plumbing works. Counts to 5 with a short pause between each, then finishes.",
        "command": [sys.executable, "-u", os.path.join(SCRIPTS_DIR, "test_dummy.py")],
    },
}

SCRIPT_JOBS = {}
SCRIPT_JOBS_LOCK = threading.Lock()


def run_script_job(job_id, command):
    with SCRIPT_JOBS_LOCK:
        SCRIPT_JOBS[job_id]["status"] = "running"

    try:
        process = subprocess.Popen(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
        )
        for line in process.stdout:
            with SCRIPT_JOBS_LOCK:
                SCRIPT_JOBS[job_id]["log"].append(line.rstrip("\n"))
        process.wait()
        with SCRIPT_JOBS_LOCK:
            SCRIPT_JOBS[job_id]["status"] = "done" if process.returncode == 0 else "error"
            SCRIPT_JOBS[job_id]["returncode"] = process.returncode
    except Exception as e:
        with SCRIPT_JOBS_LOCK:
            SCRIPT_JOBS[job_id]["status"] = "error"
            SCRIPT_JOBS[job_id]["log"].append("ERROR: " + str(e))


SCRAPER_WORKSPACE_DIR = os.path.join(BASE_DIR, "scraper_workspace")
os.makedirs(SCRAPER_WORKSPACE_DIR, exist_ok=True)
SCRAPER_SCRIPT_PATH = os.path.join(SCRAPER_WORKSPACE_DIR, "zoominfo_scrap.js")
STAGING_CSV_PATH = os.path.join(SCRAPER_WORKSPACE_DIR, "staging.csv")

# Separate from SCRIPT_JOBS (Sprint 7) on purpose -- this job needs to keep a
# live reference to the subprocess itself so we can write to its stdin later
# (the script pauses waiting for Enter after you log in to Sales Navigator).
SCRAPER_JOBS = {}
SCRAPER_JOBS_LOCK = threading.Lock()


def run_scraper_job(job_id, input_csv_path, unlock_mobile=False):
    with SCRAPER_JOBS_LOCK:
        SCRAPER_JOBS[job_id]["status"] = "running"

    try:
        job_env = os.environ.copy()
        job_env["LEADIQ_UNLOCK_MOBILE"] = "1" if unlock_mobile else "0"
        process = subprocess.Popen(
            ["node", SCRAPER_SCRIPT_PATH, input_csv_path],
            cwd=SCRAPER_WORKSPACE_DIR,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
            env=job_env,
        )
        with SCRAPER_JOBS_LOCK:
            SCRAPER_JOBS[job_id]["process"] = process

        output_file = None
        work_done = False

        for line in process.stdout:
            clean_line = line.rstrip("\n")
            with SCRAPER_JOBS_LOCK:
                SCRAPER_JOBS[job_id]["log"].append(clean_line)

            if clean_line.startswith("Output CSV: "):
                output_file = clean_line[len("Output CSV: "):].strip()

            # The script prints this once everything that actually matters
            # is finished. Treat that as "done" immediately, rather than
            # waiting for the process to fully exit -- Playwright's
            # browser.close() on a persistent context can hang well after
            # the real work is complete.
            if (not work_done) and clean_line.startswith("Done.") and "scraped" in clean_line:
                work_done = True
                with SCRAPER_JOBS_LOCK:
                    SCRAPER_JOBS[job_id]["status"] = "done"
                    SCRAPER_JOBS[job_id]["output_file"] = output_file

                if output_file:
                    try:
                        output_path = os.path.join(SCRAPER_WORKSPACE_DIR, output_file)
                        if os.path.exists(output_path):
                            with csv_lock(STAGING_CSV_PATH):
                                atomic_copyfile(output_path, STAGING_CSV_PATH)
                                write_staging_sync_meta(STAGING_CSV_PATH, output_file)
                            # Safe to move now -- staging.csv already has a
                            # full copy of everything in this file.
                            move_to_working_files(output_path, "zoominfo-scraper")
                        else:
                            with SCRAPER_JOBS_LOCK:
                                SCRAPER_JOBS[job_id]["log"].append("WARNING: expected output file not found at " + output_path)
                    except Exception as copy_err:
                        with SCRAPER_JOBS_LOCK:
                            SCRAPER_JOBS[job_id]["log"].append("WARNING: could not copy output to staging (" + str(copy_err) + ")")

        # The loop above only ends once the process actually exits (or its
        # stdout closes). If we never saw the Done line, fall back to the
        # process's real exit code to decide done vs error.
        process.wait()
        if not work_done:
            with SCRAPER_JOBS_LOCK:
                SCRAPER_JOBS[job_id]["status"] = "done" if process.returncode == 0 else "error"
                SCRAPER_JOBS[job_id]["returncode"] = process.returncode
    except Exception as e:
        with SCRAPER_JOBS_LOCK:
            if SCRAPER_JOBS[job_id]["status"] != "done":
                SCRAPER_JOBS[job_id]["status"] = "error"
            SCRAPER_JOBS[job_id]["log"].append("ERROR: " + str(e))


# Tracks whether the currently staged batch still needs to be added to the
# master tracker before running (Option 2: fresh CSV import) or is already
# part of it (Option 1: rows selected directly from the tracker).
SALESLOFT_STATE = {"pending_tracker_commit": False}
SALESLOFT_STATE_LOCK = threading.Lock()


# ── Consolidated pipeline: bulk import + cadence enrollment (API calls)
# then per-contact compose + send-now/schedule (browser UI), in one job.
# Reads/writes the SAME staging CSV as the manual + schedule flows above
# (SALESLOFT_STAGING_PATH) so nothing about staging/import/editing
# contacts changes -- this just replaces what you DO with a staged
# batch. Cadence is picked at run time (cadence_id), never hardcoded.
SALESLOFT_PIPELINE_WORKSPACE_DIR = os.path.join(BASE_DIR, "salesloft_pipeline_workspace")
os.makedirs(SALESLOFT_PIPELINE_WORKSPACE_DIR, exist_ok=True)
SALESLOFT_PIPELINE_SCRIPT_PATH = os.path.join(SALESLOFT_PIPELINE_WORKSPACE_DIR, "salesloft_pipeline.py")
SALESLOFT_PIPELINE_INPUT_PATH = os.path.join(SALESLOFT_PIPELINE_WORKSPACE_DIR, "contacts.csv")
SALESLOFT_PIPELINE_RESULTS_PATH = os.path.join(SALESLOFT_PIPELINE_WORKSPACE_DIR, "automation_results.xlsx")
# Was salesloft_workspace/staging.csv back when the manual + schedule flows
# owned staging; now that the pipeline is the only consumer, it lives here.
SALESLOFT_STAGING_PATH = os.path.join(SALESLOFT_PIPELINE_WORKSPACE_DIR, "staging.csv")

SALESLOFT_PIPELINE_JOBS = {}
SALESLOFT_PIPELINE_JOBS_LOCK = threading.Lock()

# Must match COL_EMAIL / COL_SUBJECT / COL_BODY / COL_NAME inside
# salesloft_pipeline.py.
SALESLOFT_PIPELINE_COL_EMAIL = "Email"
SALESLOFT_PIPELINE_COL_SUBJECT = "Subject Line"
SALESLOFT_PIPELINE_COL_BODY = "Messaging"
SALESLOFT_PIPELINE_COL_NAME = "Contact Name"
# Optional per-row schedule override -- must match COL_SEND_AT in
# salesloft_pipeline.py. Lets a batch be "split": some rows get their
# own Send At value, everything else falls back to the send_at picked
# in the UI's single date field.
SALESLOFT_PIPELINE_COL_SEND_AT = "Send At"
SALESLOFT_PIPELINE_RESULTS_JSON_PATH = os.path.join(SALESLOFT_PIPELINE_WORKSPACE_DIR, "automation_results.json")


def ensure_send_at_column(headers, rows):
    """Adds the optional per-row 'Send At' column to a freshly-staged
    batch if it isn't already there, so it shows up as just another
    editable cell in the define-stage table (same mechanism as Subject /
    Messaging) without any extra plumbing."""
    if SALESLOFT_PIPELINE_COL_SEND_AT not in headers:
        headers = list(headers) + [SALESLOFT_PIPELINE_COL_SEND_AT]
    for row in rows:
        row.setdefault(SALESLOFT_PIPELINE_COL_SEND_AT, "")
    return headers, rows


def tracker_today_str():
    """'3 Jun 26' -- day with no leading zero, matching the tracker's
    existing 'DD Mon YY' style (see _parse_tracker_date) but without the
    zero-pad, since that's the format actually used across the sheet."""
    now = datetime.now()
    return f"{now.day} {now.strftime('%b %y')}"


def mark_tracker_contacted(emails):
    """After a successful Salesloft schedule run, stamps the matching
    tracker rows: First Contact (only if it was blank -- never overwrites
    an existing first-touch date), Last Contacted = today, and
    Status = 'Pending Response'. Matches purely on Email, case-insensitive."""
    if not emails:
        return
    wanted = {e.strip().lower() for e in emails if e and e.strip()}
    if not wanted:
        return

    today_str = tracker_today_str()
    headers, rows = load_csv_full(TRACKER_PATH)
    if not rows:
        return

    changed = False
    for row in rows:
        email = (row.get("Email") or "").strip().lower()
        if email not in wanted:
            continue
        if not (row.get("First Contact") or "").strip():
            row["First Contact"] = today_str
        row["Last Contacted"] = today_str
        row["Status"] = "Pending Response"
        changed = True

    if changed:
        write_csv_full(TRACKER_PATH, headers, rows)


def update_tracker_salesloft_ids(email_to_person_id):
    """Writes the Salesloft person id captured during Phase 2 (from the
    contact's own profile page, so it's authoritative) into the
    tracker's 'Salesloft Link' column -- same bare-numeric-id format
    used everywhere else (see build_tracker_lookup_by_salesloft_id).
    Runs for both send modes, since Phase 2 opens every contact's
    profile regardless of now/schedule."""
    if not email_to_person_id:
        return 0
    headers, rows = load_csv_full(TRACKER_PATH)
    if not rows:
        return 0
    if "Salesloft Link" not in headers:
        headers = list(headers) + ["Salesloft Link"]

    updated = 0
    for row in rows:
        email = (row.get("Email") or "").strip().lower()
        person_id = email_to_person_id.get(email)
        if not person_id:
            continue
        if (row.get("Salesloft Link") or "").strip() != person_id:
            row["Salesloft Link"] = person_id
            updated += 1

    if updated:
        write_csv_full(TRACKER_PATH, headers, rows)
    return updated


def apply_pipeline_results_to_tracker(job_id, mode):
    """Reads the JSON twin of automation_results.xlsx written by
    salesloft_pipeline.py at the end of a run, and:
      - always writes back each contact's Salesloft person id (Phase 2
        opens every contact's profile regardless of send mode), and
      - for --mode schedule specifically, marks contacts whose status
        came back 'scheduled' as contacted (First/Last Contacted,
        Status = Pending Response).
    Safe to call even if the file is missing (e.g. the job errored
    before producing results)."""
    try:
        with open(SALESLOFT_PIPELINE_RESULTS_JSON_PATH, encoding="utf-8") as f:
            results = json.load(f)
    except (OSError, ValueError):
        with SALESLOFT_PIPELINE_JOBS_LOCK:
            if job_id in SALESLOFT_PIPELINE_JOBS:
                SALESLOFT_PIPELINE_JOBS[job_id]["log"].append(
                    "⚠️  Could not read automation_results.json -- tracker was not updated."
                )
        return

    email_to_person_id = {
        (r.get("email") or "").strip().lower(): (r.get("salesloft_person_id") or "").strip()
        for r in results
        if (r.get("email") or "").strip() and (r.get("salesloft_person_id") or "").strip()
    }
    id_count = update_tracker_salesloft_ids(email_to_person_id)

    log_lines = []
    if id_count:
        log_lines.append(f"📌 Tracker updated: {id_count} Salesloft Link id(s) written.")

    if mode == "schedule":
        scheduled_emails = [r.get("email") for r in results if r.get("status") == "scheduled"]
        mark_tracker_contacted(scheduled_emails)
        if scheduled_emails:
            log_lines.append(
                f"📌 Tracker updated: {len(scheduled_emails)} contact(s) marked Pending Response, Last Contacted {tracker_today_str()}."
            )

    if log_lines:
        with SALESLOFT_PIPELINE_JOBS_LOCK:
            if job_id in SALESLOFT_PIPELINE_JOBS:
                SALESLOFT_PIPELINE_JOBS[job_id]["log"].extend(log_lines)


def run_salesloft_pipeline_job(job_id, cadence_id, mode, send_at, exclude_existing):
    with SALESLOFT_PIPELINE_JOBS_LOCK:
        SALESLOFT_PIPELINE_JOBS[job_id]["status"] = "running"

    cmd = [
        "python3", SALESLOFT_PIPELINE_SCRIPT_PATH,
        "--csv", "contacts.csv",
        "--cadence-id", str(cadence_id),
        "--mode", mode,
    ]
    if mode == "schedule" and send_at:
        cmd += ["--send-at", send_at]
    if exclude_existing:
        cmd.append("--exclude-existing")

    try:
        process = subprocess.Popen(
            cmd,
            cwd=SALESLOFT_PIPELINE_WORKSPACE_DIR,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
        )
        with SALESLOFT_PIPELINE_JOBS_LOCK:
            SALESLOFT_PIPELINE_JOBS[job_id]["process"] = process

        # Same completion-detection approach as the other two Salesloft
        # jobs: the script prints this exact line once results are
        # written, which is more reliable than waiting on process exit
        # (Playwright's browser teardown can hang).
        work_done = False
        for line in process.stdout:
            clean_line = line.rstrip("\n")
            with SALESLOFT_PIPELINE_JOBS_LOCK:
                SALESLOFT_PIPELINE_JOBS[job_id]["log"].append(clean_line)

            if "Results saved to automation_results.xlsx" in clean_line:
                work_done = True
                with SALESLOFT_PIPELINE_JOBS_LOCK:
                    SALESLOFT_PIPELINE_JOBS[job_id]["status"] = "done"
                apply_pipeline_results_to_tracker(job_id, mode)
                # automation_results.xlsx/.json are both fully written by
                # this point (the script writes them, then prints this
                # line) and apply_pipeline_results_to_tracker() has already
                # read the json twin into tracker.csv above, so both are
                # safe to move now.
                move_to_working_files(SALESLOFT_PIPELINE_RESULTS_PATH, "salesloft-pipeline-results")
                move_to_working_files(SALESLOFT_PIPELINE_RESULTS_JSON_PATH, "salesloft-pipeline-results")

        process.wait()
        # contacts_salesloft_final.csv is written AFTER the "Results saved"
        # line above (see write_final_salesloft_output() in
        # salesloft_pipeline.py, called after that print) -- so it isn't
        # ready yet inside the loop, only once the process has actually
        # exited. Only move it if the run really finished, not on an
        # error/partial exit where it may be stale from a previous run.
        if work_done:
            salesloft_final_path = os.path.join(SALESLOFT_PIPELINE_WORKSPACE_DIR, "contacts_salesloft_final.csv")
            move_to_working_files(salesloft_final_path, "salesloft-pipeline-final")
        if not work_done:
            with SALESLOFT_PIPELINE_JOBS_LOCK:
                SALESLOFT_PIPELINE_JOBS[job_id]["status"] = "done" if process.returncode == 0 else "error"
                SALESLOFT_PIPELINE_JOBS[job_id]["returncode"] = process.returncode
    except Exception as e:
        with SALESLOFT_PIPELINE_JOBS_LOCK:
            if SALESLOFT_PIPELINE_JOBS[job_id]["status"] != "done":
                SALESLOFT_PIPELINE_JOBS[job_id]["status"] = "error"
            SALESLOFT_PIPELINE_JOBS[job_id]["log"].append("ERROR: " + str(e))


VALIDATION_RULES_PATH = os.path.join(BASE_DIR, "validation_rules.json")

# Seeded from the enums already documented in your original PG Tracker
# schema. Only used the first time this file doesn't exist -- after that,
# whatever you save in the Data Validation settings page takes over.
DEFAULT_VALIDATION_RULES = {
    "Patch": ["Malaysia", "Indonesia", "Japan"],
    "Company Name": {"type": "lookup", "source": "accounts", "column": "Company Name"},
    "Status": [
        "Pending Response", "Contacted - Working", "Meeting Pending TBC",
        "Meeting booked", "No Interest / No Response / Blocked",
        "Left Company", "For AE Contact",
    ],
    "Source / Signal": [
        "Outbound", "MQL", "Event Source", "Moved Leads", "News",
        "Sales Report", "Panelist", "Intent Signals", "MDB Skills",
        "Reconnect", "Previous Opp", "AE Contact",
    ],
    "Potential Coffee Chats": ["TRUE", "FALSE"],
}

# Columns frozen in place when scrolling the main table horizontally.
# These are also forced to be the first display columns in dashboard_page().
FROZEN_COLUMNS = ["Company Name", "Contact Name"]

# Must match .actions-col { width } in style.css.
ACTIONS_COL_WIDTH = 72


def compute_frozen_offsets(headers=None):
    """Computes the CSS `left` value for each frozen column by walking the
    actual column order in `headers`. Callers that don't have headers yet
    (e.g. during startup) get an empty dict back, which disables frozen
    styling gracefully."""
    offsets = {}
    cumulative = ACTIONS_COL_WIDTH
    for col in (headers or []):
        if col in FROZEN_COLUMNS:
            offsets[col] = cumulative
        cumulative += DEFAULT_WIDTHS.get(col, 140)
    return offsets


def load_validation_rules():
    if os.path.exists(VALIDATION_RULES_PATH):
        try:
            with open(VALIDATION_RULES_PATH, "r", encoding="utf-8") as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError):
            pass
    save_validation_rules(DEFAULT_VALIDATION_RULES)
    return json.loads(json.dumps(DEFAULT_VALIDATION_RULES))


def save_validation_rules(rules):
    with open(VALIDATION_RULES_PATH, "w", encoding="utf-8") as f:
        json.dump(rules, f, indent=2)


def resolve_validation_options(rules):
    """Turns whatever's stored in validation_rules.json into a plain list
    of strings per column, regardless of whether it's a fixed list or a
    live lookup against one of the reference libraries (e.g. Company Name
    pulling from accounts.csv instead of a hand-typed list)."""
    resolved = {}
    for col, rule in rules.items():
        if isinstance(rule, list):
            resolved[col] = rule
        elif isinstance(rule, dict) and rule.get("type") == "lookup":
            source_path = LIBRARY_FILES.get(rule.get("source", "accounts"))
            source_column = rule.get("column", col)
            values = []
            seen = set()
            if source_path:
                for r in load_csv_lookup(source_path):
                    v = (r.get(source_column) or "").strip()
                    if v and v not in seen:
                        seen.add(v)
                        values.append(v)
            resolved[col] = values
        elif isinstance(rule, dict):
            resolved[col] = rule.get("values", [])
    return resolved


def validation_mismatch_count(column, allowed_values):
    """Counts existing non-blank values that wouldn't match a given list --
    this is the actual answer to 'will this affect my existing data,'
    computed against your real tracker.csv, not guessed at."""
    if not allowed_values:
        return 0, 0
    _, rows = load_tracker()
    allowed_set = set(allowed_values)
    total_filled = 0
    mismatches = 0
    for r in rows:
        val = (r.get(column) or "").strip()
        if not val:
            continue
        total_filled += 1
        if val not in allowed_set:
            mismatches += 1
    return total_filled, mismatches


ACCOUNT_SCRAPER_SCRIPT_PATH = os.path.join(SCRAPER_WORKSPACE_DIR, "sales_nav_scraper.js")
ACCOUNT_STAGING_CSV_PATH = os.path.join(SCRAPER_WORKSPACE_DIR, "account_staging.csv")

ACCOUNT_SCRAPER_JOBS = {}
ACCOUNT_SCRAPER_JOBS_LOCK = threading.Lock()
LATEST_ACCOUNT_JOB = {"id": None}


def run_account_scraper_job(job_id, unlock_mobile=False):
    with ACCOUNT_SCRAPER_JOBS_LOCK:
        ACCOUNT_SCRAPER_JOBS[job_id]["status"] = "running"

    output_file = None
    work_done = False
    graceful_stop = False  # set when the script's own "stop" command path
                            # finished cleanly, vs. a hard kill from the
                            # /account-scraper/stop route below
    try:
        job_env = os.environ.copy()
        job_env["LEADIQ_UNLOCK_MOBILE"] = "1" if unlock_mobile else "0"
        process = subprocess.Popen(
            ["node", ACCOUNT_SCRAPER_SCRIPT_PATH],
            cwd=SCRAPER_WORKSPACE_DIR,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
            start_new_session=True,  # own process group -- so a hard stop can
                                      # signal Node AND the Chrome process it
                                      # spawns, instead of orphaning Chrome
                                      # holding the persistent profile lock
            env=job_env,
        )
        with ACCOUNT_SCRAPER_JOBS_LOCK:
            ACCOUNT_SCRAPER_JOBS[job_id]["process"] = process

        for line in process.stdout:
            clean_line = line.rstrip("\n")
            with ACCOUNT_SCRAPER_JOBS_LOCK:
                ACCOUNT_SCRAPER_JOBS[job_id]["log"].append(clean_line)

            if clean_line.startswith("Output CSV: "):
                output_file = clean_line[len("Output CSV: "):].strip()

            if (not work_done) and clean_line.startswith("Done!") and "saved to" in clean_line:
                work_done = True
                with ACCOUNT_SCRAPER_JOBS_LOCK:
                    ACCOUNT_SCRAPER_JOBS[job_id]["status"] = "done"
                    ACCOUNT_SCRAPER_JOBS[job_id]["output_file"] = output_file

                # Copy to staging right here, not after the loop ends --
                # the loop only ends once the process fully exits, and
                # browser.close() can hang well past this point even
                # though everything that actually matters is done.
                if output_file:
                    try:
                        output_path = os.path.join(SCRAPER_WORKSPACE_DIR, output_file)
                        if os.path.exists(output_path):
                            with csv_lock(ACCOUNT_STAGING_CSV_PATH):
                                atomic_copyfile(output_path, ACCOUNT_STAGING_CSV_PATH)
                                write_staging_sync_meta(ACCOUNT_STAGING_CSV_PATH, output_file)
                            # Only move on this fully-"Done!" path, never on
                            # "Stopped." below -- sales_nav_scraper.js only
                            # deletes its progress_<Account>.json checkpoint
                            # here, which means a run that ends here is the
                            # only case guaranteed not to resume (and thus
                            # not append more rows to this exact filename)
                            # later. Moving it on the "Stopped." path would
                            # leave a resumed run appending to a brand new,
                            # empty file at the old path -- silently losing
                            # every contact already scraped this run.
                            move_to_working_files(output_path, "sales-nav-scraper")
                        else:
                            with ACCOUNT_SCRAPER_JOBS_LOCK:
                                ACCOUNT_SCRAPER_JOBS[job_id]["log"].append("WARNING: expected output file not found at " + output_path)
                    except Exception as copy_err:
                        with ACCOUNT_SCRAPER_JOBS_LOCK:
                            ACCOUNT_SCRAPER_JOBS[job_id]["log"].append("WARNING: could not copy output to staging (" + str(copy_err) + ")")

            # The script prints this when it stops gracefully because it
            # was told to (typed "stop", or the /account-scraper/input
            # route sent "stop" on our behalf) -- as opposed to being
            # killed outright by /account-scraper/stop below. Recognize
            # it the same way as the Done! line so the run gets labeled
            # "stopped" (not "done") and the partial CSV still gets
            # staged immediately.
            if (not work_done) and (not graceful_stop) and clean_line.startswith("Stopped.") and "saved to" in clean_line:
                graceful_stop = True
                with ACCOUNT_SCRAPER_JOBS_LOCK:
                    ACCOUNT_SCRAPER_JOBS[job_id]["status"] = "stopped"
                    ACCOUNT_SCRAPER_JOBS[job_id]["output_file"] = output_file

                if output_file:
                    try:
                        output_path = os.path.join(SCRAPER_WORKSPACE_DIR, output_file)
                        if os.path.exists(output_path):
                            with csv_lock(ACCOUNT_STAGING_CSV_PATH):
                                atomic_copyfile(output_path, ACCOUNT_STAGING_CSV_PATH)
                                write_staging_sync_meta(ACCOUNT_STAGING_CSV_PATH, output_file)
                        else:
                            with ACCOUNT_SCRAPER_JOBS_LOCK:
                                ACCOUNT_SCRAPER_JOBS[job_id]["log"].append("WARNING: expected output file not found at " + output_path)
                    except Exception as copy_err:
                        with ACCOUNT_SCRAPER_JOBS_LOCK:
                            ACCOUNT_SCRAPER_JOBS[job_id]["log"].append("WARNING: could not copy output to staging (" + str(copy_err) + ")")

        process.wait()

        with ACCOUNT_SCRAPER_JOBS_LOCK:
            was_stopped = ACCOUNT_SCRAPER_JOBS[job_id].get("stop_requested", False)

        if graceful_stop:
            # Status is already correctly set to "stopped" above -- the
            # script always exits 0 even when stopped on purpose, so we
            # only need to record the real exit code here, not let the
            # block below re-decide status from returncode and clobber it.
            with ACCOUNT_SCRAPER_JOBS_LOCK:
                ACCOUNT_SCRAPER_JOBS[job_id]["returncode"] = process.returncode
        elif not work_done:
            with ACCOUNT_SCRAPER_JOBS_LOCK:
                if was_stopped:
                    ACCOUNT_SCRAPER_JOBS[job_id]["status"] = "stopped"
                    ACCOUNT_SCRAPER_JOBS[job_id]["log"].append("-- Stopped manually. Whatever was already scraped is kept below. --")
                else:
                    ACCOUNT_SCRAPER_JOBS[job_id]["status"] = "done" if process.returncode == 0 else "error"
                ACCOUNT_SCRAPER_JOBS[job_id]["returncode"] = process.returncode
                ACCOUNT_SCRAPER_JOBS[job_id]["output_file"] = output_file
    except Exception as e:
        with ACCOUNT_SCRAPER_JOBS_LOCK:
            if ACCOUNT_SCRAPER_JOBS[job_id]["status"] not in ("done", "stopped"):
                ACCOUNT_SCRAPER_JOBS[job_id]["status"] = "error"
            ACCOUNT_SCRAPER_JOBS[job_id]["log"].append("ERROR: " + str(e))
        return

    # Only needed for the "stopped" or "no Done line seen" cases -- the
    # work_done=True case already copied to staging the moment Done was
    # detected, and re-copying here could overwrite edits already made
    # while the process was still hanging on browser.close() in the
    # background.
    if output_file and not work_done and not graceful_stop:
        try:
            output_path = os.path.join(SCRAPER_WORKSPACE_DIR, output_file)
            if os.path.exists(output_path):
                with csv_lock(ACCOUNT_STAGING_CSV_PATH):
                    atomic_copyfile(output_path, ACCOUNT_STAGING_CSV_PATH)
                    write_staging_sync_meta(ACCOUNT_STAGING_CSV_PATH, output_file)
            else:
                with ACCOUNT_SCRAPER_JOBS_LOCK:
                    ACCOUNT_SCRAPER_JOBS[job_id]["log"].append("WARNING: expected output file not found at " + output_path)
        except Exception as copy_err:
            with ACCOUNT_SCRAPER_JOBS_LOCK:
                ACCOUNT_SCRAPER_JOBS[job_id]["log"].append("WARNING: could not copy output to staging (" + str(copy_err) + ")")


INTENT_WORKSPACE_DIR = os.path.join(BASE_DIR, "intent_signal_scrap_workspace")
os.makedirs(INTENT_WORKSPACE_DIR, exist_ok=True)
INTENT_CONTACT_SCRIPT_PATH = os.path.join(INTENT_WORKSPACE_DIR, "zoominfo-contact.js")
INTENT_LINKEDIN_SCRIPT_PATH = os.path.join(INTENT_WORKSPACE_DIR, "zoominfo-linkedin-scrap.js")
INTENT_CONTACT_STAGING_PATH = os.path.join(INTENT_WORKSPACE_DIR, "contact_staging.csv")
INTENT_LINKEDIN_STAGING_PATH = os.path.join(INTENT_WORKSPACE_DIR, "linkedin_staging.csv")

# Same shape as ACCOUNT_SCRAPER_JOBS -- two independent job tables since both
# options can conceivably be kicked off from the same page, each needing its
# own live process handle for stdin forwarding (the "press ENTER" prompts).
INTENT_CONTACT_JOBS = {}
INTENT_CONTACT_JOBS_LOCK = threading.Lock()
LATEST_INTENT_CONTACT_JOB = {"id": None}

INTENT_LINKEDIN_JOBS = {}
INTENT_LINKEDIN_JOBS_LOCK = threading.Lock()
LATEST_INTENT_LINKEDIN_JOB = {"id": None}


def run_intent_contact_job(job_id):
    with INTENT_CONTACT_JOBS_LOCK:
        INTENT_CONTACT_JOBS[job_id]["status"] = "running"

    output_file = None
    work_done = False
    try:
        process = subprocess.Popen(
            ["node", INTENT_CONTACT_SCRIPT_PATH],
            cwd=INTENT_WORKSPACE_DIR,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
            start_new_session=True,  # own process group -- lets /stop reach
                                      # the Chrome process Node spawns too,
                                      # same reasoning as run_account_scraper_job
        )
        with INTENT_CONTACT_JOBS_LOCK:
            INTENT_CONTACT_JOBS[job_id]["process"] = process

        for line in process.stdout:
            clean_line = line.rstrip("\n")
            with INTENT_CONTACT_JOBS_LOCK:
                INTENT_CONTACT_JOBS[job_id]["log"].append(clean_line)

            if clean_line.startswith("Output CSV: "):
                output_file = clean_line[len("Output CSV: "):].strip()

            # Detect completion from the script's own final log line rather
            # than waiting for the process to exit -- Playwright's
            # browser.close() on a persistent context can hang well after
            # the actual work (and the CSV) is already finished.
            if (not work_done) and clean_line.startswith("Done.") and "scraped" in clean_line:
                work_done = True
                with INTENT_CONTACT_JOBS_LOCK:
                    INTENT_CONTACT_JOBS[job_id]["status"] = "done"
                    INTENT_CONTACT_JOBS[job_id]["output_file"] = output_file

                if output_file:
                    try:
                        output_path = os.path.join(INTENT_WORKSPACE_DIR, output_file)
                        if os.path.exists(output_path):
                            with csv_lock(INTENT_CONTACT_STAGING_PATH):
                                atomic_copyfile(output_path, INTENT_CONTACT_STAGING_PATH)
                            move_to_working_files(output_path, "intent-signal-zi-contact")
                        else:
                            with INTENT_CONTACT_JOBS_LOCK:
                                INTENT_CONTACT_JOBS[job_id]["log"].append("WARNING: expected output file not found at " + output_path)
                    except Exception as copy_err:
                        with INTENT_CONTACT_JOBS_LOCK:
                            INTENT_CONTACT_JOBS[job_id]["log"].append("WARNING: could not copy output to staging (" + str(copy_err) + ")")

        process.wait()

        with INTENT_CONTACT_JOBS_LOCK:
            was_stopped = INTENT_CONTACT_JOBS[job_id].get("stop_requested", False)

        if not work_done:
            with INTENT_CONTACT_JOBS_LOCK:
                if was_stopped:
                    INTENT_CONTACT_JOBS[job_id]["status"] = "stopped"
                else:
                    INTENT_CONTACT_JOBS[job_id]["status"] = "done" if process.returncode == 0 else "error"
                INTENT_CONTACT_JOBS[job_id]["returncode"] = process.returncode
    except Exception as e:
        with INTENT_CONTACT_JOBS_LOCK:
            if INTENT_CONTACT_JOBS[job_id]["status"] not in ("done", "stopped"):
                INTENT_CONTACT_JOBS[job_id]["status"] = "error"
            INTENT_CONTACT_JOBS[job_id]["log"].append("ERROR: " + str(e))


def run_intent_linkedin_job(job_id):
    with INTENT_LINKEDIN_JOBS_LOCK:
        INTENT_LINKEDIN_JOBS[job_id]["status"] = "running"

    output_file = None
    work_done = False
    try:
        process = subprocess.Popen(
            ["node", INTENT_LINKEDIN_SCRIPT_PATH],
            cwd=INTENT_WORKSPACE_DIR,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
            start_new_session=True,  # two persistent browser contexts in this
                                      # one (ZoomInfo + Sales Nav) -- same
                                      # process-group-kill reasoning applies
        )
        with INTENT_LINKEDIN_JOBS_LOCK:
            INTENT_LINKEDIN_JOBS[job_id]["process"] = process

        for line in process.stdout:
            clean_line = line.rstrip("\n")
            with INTENT_LINKEDIN_JOBS_LOCK:
                INTENT_LINKEDIN_JOBS[job_id]["log"].append(clean_line)

            if clean_line.startswith("Output CSV: "):
                output_file = clean_line[len("Output CSV: "):].strip()

            if (not work_done) and clean_line.startswith("Done.") and "enriched" in clean_line:
                work_done = True
                with INTENT_LINKEDIN_JOBS_LOCK:
                    INTENT_LINKEDIN_JOBS[job_id]["status"] = "done"
                    INTENT_LINKEDIN_JOBS[job_id]["output_file"] = output_file

                if output_file:
                    try:
                        output_path = os.path.join(INTENT_WORKSPACE_DIR, output_file)
                        if os.path.exists(output_path):
                            with csv_lock(INTENT_LINKEDIN_STAGING_PATH):
                                atomic_copyfile(output_path, INTENT_LINKEDIN_STAGING_PATH)
                            move_to_working_files(output_path, "intent-signal-zi-linkedin")
                        else:
                            with INTENT_LINKEDIN_JOBS_LOCK:
                                INTENT_LINKEDIN_JOBS[job_id]["log"].append("WARNING: expected output file not found at " + output_path)
                    except Exception as copy_err:
                        with INTENT_LINKEDIN_JOBS_LOCK:
                            INTENT_LINKEDIN_JOBS[job_id]["log"].append("WARNING: could not copy output to staging (" + str(copy_err) + ")")

        process.wait()

        with INTENT_LINKEDIN_JOBS_LOCK:
            was_stopped = INTENT_LINKEDIN_JOBS[job_id].get("stop_requested", False)

        if not work_done:
            with INTENT_LINKEDIN_JOBS_LOCK:
                if was_stopped:
                    INTENT_LINKEDIN_JOBS[job_id]["status"] = "stopped"
                else:
                    INTENT_LINKEDIN_JOBS[job_id]["status"] = "done" if process.returncode == 0 else "error"
                INTENT_LINKEDIN_JOBS[job_id]["returncode"] = process.returncode
    except Exception as e:
        with INTENT_LINKEDIN_JOBS_LOCK:
            if INTENT_LINKEDIN_JOBS[job_id]["status"] not in ("done", "stopped"):
                INTENT_LINKEDIN_JOBS[job_id]["status"] = "error"
            INTENT_LINKEDIN_JOBS[job_id]["log"].append("ERROR: " + str(e))


GLEAN_WORKSPACE_DIR = os.path.join(BASE_DIR, "glean_workspace")
os.makedirs(GLEAN_WORKSPACE_DIR, exist_ok=True)
GLEAN_SCRIPT_PATH = os.path.join(GLEAN_WORKSPACE_DIR, "glean-pg-tracker-automation.js")
GLEAN_INPUT_STAGING_PATH = os.path.join(GLEAN_WORKSPACE_DIR, "glean_input_staging.csv")
GLEAN_OUTPUT_STAGING_PATH = os.path.join(GLEAN_WORKSPACE_DIR, "glean_output_staging.csv")

# News signal report -- a different use case from the per-contact script
# above (one batch prompt across selected articles, not one prompt per
# row), but lives in the same glean_workspace/ so it shares the already
# logged-in browser profile (./glean-profile).
GLEAN_NEWS_REPORT_SCRIPT_PATH = os.path.join(GLEAN_WORKSPACE_DIR, "glean-news-report.js")
GLEAN_NEWS_REPORT_INPUT_PATH = os.path.join(GLEAN_WORKSPACE_DIR, "news_report_input.csv")
GLEAN_NEWS_REPORT_OUTPUT_BASE = os.path.join(GLEAN_WORKSPACE_DIR, "news_report_output")
GLEAN_NEWS_REPORT_OUTPUT_MD_PATH = GLEAN_NEWS_REPORT_OUTPUT_BASE + ".md"
GLEAN_NEWS_REPORT_WORKBOOK_PATH = GLEAN_NEWS_REPORT_OUTPUT_BASE + ".xlsx"
DEFAULT_NEWS_REPORT_PROMPT = (
    "Based on this news report, help me analyse and assess which is high, "
    "medium, low signal. For each of the key signals, help me with why "
    "anything, why MongoDB, why now, key use case and persona."
)

# Internal columns that ride along in the staging CSVs to track which
# tracker.csv row (if any) each entry came from. Hidden from the preview
# table, never shown to the user, but essential at commit time to tell
# "update this existing row" apart from "create a new one."
GLEAN_INDEX_COL = "_tracker_row_index"
GLEAN_FINGERPRINT_COL = "_tracker_fingerprint"
GLEAN_INTERNAL_COLS = [GLEAN_INDEX_COL, GLEAN_FINGERPRINT_COL]

# Optional free-text column shown in the input staging table so a row can
# get one-off extra context (e.g. "part of the IDC campaign") folded into
# its prompts. Not a real tracker column -- it's dropped automatically on
# commit since glean_output_commit only copies known tracker_headers over.
GLEAN_EXTRA_CONTEXT_COL = "Additional Context"

GLEAN_JOBS = {}
GLEAN_JOBS_LOCK = threading.Lock()
LATEST_GLEAN_JOB = {"id": None}


# Prefer an isolated venv inside news_workspace/ if one exists (created via
# `python3 -m venv venv` + `pip install -r requirements.txt` per its
# README) -- keeps feedparser/trafilatura/googlenewsdecoder out of
# whatever environment actually runs Flask. Falls back to Flask's own
# interpreter if no venv is set up.
NEWS_VENV_PYTHON = os.path.join(NEWS_WORKSPACE_DIR, "venv", "bin", "python3")
NEWS_PYTHON = NEWS_VENV_PYTHON if os.path.exists(NEWS_VENV_PYTHON) else sys.executable

NEWS_JOBS = {}
NEWS_JOBS_LOCK = threading.Lock()


def run_news_job(job_id, limit=None, accounts_csv_path=None, keywords_csv_path=None):
    with NEWS_JOBS_LOCK:
        NEWS_JOBS[job_id]["status"] = "running"

    command = [NEWS_PYTHON, "-u", NEWS_SCRIPT_PATH,
               "--accounts-csv", accounts_csv_path or ACCOUNTS_PATH,
               "--keywords-csv", keywords_csv_path or NEWS_KEYWORDS_PATH]
    if limit:
        command += ["--limit", str(limit)]

    try:
        process = subprocess.Popen(
            command,
            cwd=NEWS_WORKSPACE_DIR,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
            start_new_session=True,  # own process group, so Stop can signal
                                      # it safely without touching Flask's
                                      # own process group (same pattern as
                                      # the other scraper jobs)
        )
        with NEWS_JOBS_LOCK:
            NEWS_JOBS[job_id]["process"] = process

        for line in process.stdout:
            with NEWS_JOBS_LOCK:
                NEWS_JOBS[job_id]["log"].append(line.rstrip("\n"))

        process.wait()

        with NEWS_JOBS_LOCK:
            was_stopped = NEWS_JOBS[job_id].get("stop_requested", False)
            # main.py catches the stop signal, finishes the in-flight
            # article, and writes out whatever it collected before exiting
            # cleanly -- so a stopped run still exits 0. Use the flag (not
            # the return code) to tell "stopped" apart from "done".
            if was_stopped:
                NEWS_JOBS[job_id]["status"] = "stopped"
            else:
                NEWS_JOBS[job_id]["status"] = "done" if process.returncode == 0 else "error"
            NEWS_JOBS[job_id]["returncode"] = process.returncode

            output_file = None
            if process.returncode == 0 and os.path.isdir(NEWS_OUTPUT_DIR):
                csvs = sorted(
                    [f for f in os.listdir(NEWS_OUTPUT_DIR) if f.endswith(".csv")],
                    reverse=True,
                )
                if csvs:
                    output_file = csvs[0]
            NEWS_JOBS[job_id]["output_file"] = output_file

    except Exception as e:
        with NEWS_JOBS_LOCK:
            NEWS_JOBS[job_id]["status"] = "error"
            NEWS_JOBS[job_id]["log"].append("ERROR: " + str(e))


# ── Email Live Feed: scrapes Salesloft's live feed (notifications-center)
# for email opens/clicks/hot-leads. Phase 1 only -- no email address
# resolution or API lookup yet. Same job pattern as run_news_job: launch
# the script, capture its stdout as the log, then look for the newest
# CSV in the output dir once it exits (the script's own filename includes
# a timestamp so this always picks up what it just wrote).
LIVE_FEED_WORKSPACE_DIR = os.path.join(BASE_DIR, "live_feed_workspace")
os.makedirs(LIVE_FEED_WORKSPACE_DIR, exist_ok=True)
LIVE_FEED_SCRIPT_PATH = os.path.join(LIVE_FEED_WORKSPACE_DIR, "scrape_live_feed.js")
LIVE_FEED_OUTPUT_DIR = os.path.join(LIVE_FEED_WORKSPACE_DIR, "output")
os.makedirs(LIVE_FEED_OUTPUT_DIR, exist_ok=True)

LIVE_FEED_JOBS = {}
LIVE_FEED_JOBS_LOCK = threading.Lock()


def run_live_feed_job(job_id, target_page):
    with LIVE_FEED_JOBS_LOCK:
        LIVE_FEED_JOBS[job_id]["status"] = "running"

    command = ["node", LIVE_FEED_SCRIPT_PATH, str(target_page)]

    try:
        process = subprocess.Popen(
            command,
            cwd=LIVE_FEED_WORKSPACE_DIR,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
        )
        with LIVE_FEED_JOBS_LOCK:
            LIVE_FEED_JOBS[job_id]["process"] = process

        output_file = None
        work_done = False

        for line in process.stdout:
            clean_line = line.rstrip("\n")
            with LIVE_FEED_JOBS_LOCK:
                LIVE_FEED_JOBS[job_id]["log"].append(clean_line)

            if clean_line.startswith("Output CSV: "):
                output_file = clean_line[len("Output CSV: "):].strip()

            # Same lesson as the other scrapers: detect completion from the
            # script's own final log line rather than waiting for the
            # process to fully exit -- Playwright's context.close() on a
            # persistent context can hang well after the real work (and
            # the CSV write) is already done.
            if (not work_done) and clean_line.startswith("Done.") and "scraped" in clean_line:
                work_done = True
                with LIVE_FEED_JOBS_LOCK:
                    LIVE_FEED_JOBS[job_id]["status"] = "done"
                    LIVE_FEED_JOBS[job_id]["output_file"] = output_file

        process.wait()
        if not work_done:
            with LIVE_FEED_JOBS_LOCK:
                LIVE_FEED_JOBS[job_id]["status"] = "done" if process.returncode == 0 else "error"
                LIVE_FEED_JOBS[job_id]["returncode"] = process.returncode

    except Exception as e:
        with LIVE_FEED_JOBS_LOCK:
            if LIVE_FEED_JOBS[job_id]["status"] != "done":
                LIVE_FEED_JOBS[job_id]["status"] = "error"
            LIVE_FEED_JOBS[job_id]["log"].append("ERROR: " + str(e))


def make_fingerprint(row):
    return (row.get("Contact Name") or "").strip() + "||" + (row.get("Company Name") or "").strip()


def run_glean_job(job_id, input_path, output_path):
    with GLEAN_JOBS_LOCK:
        GLEAN_JOBS[job_id]["status"] = "running"

    work_done = False
    try:
        process = subprocess.Popen(
            ["node", GLEAN_SCRIPT_PATH, input_path, output_path, str(GLEAN_JOBS[job_id].get("row_limit", 999))],
            cwd=GLEAN_WORKSPACE_DIR,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
            start_new_session=True,  # see note in run_account_scraper_job --
                                      # same orphaned-browser-profile risk
        )
        with GLEAN_JOBS_LOCK:
            GLEAN_JOBS[job_id]["process"] = process

        for line in process.stdout:
            clean_line = line.rstrip("\n")
            with GLEAN_JOBS_LOCK:
                GLEAN_JOBS[job_id]["log"].append(clean_line)

            if (not work_done) and clean_line.startswith("Output saved to: "):
                work_done = True
                with GLEAN_JOBS_LOCK:
                    GLEAN_JOBS[job_id]["status"] = "done"

                # Copy here, the moment Done is detected -- not after the
                # process fully exits, same lesson as the other scrapers.
                try:
                    if os.path.exists(output_path):
                        with csv_lock(GLEAN_OUTPUT_STAGING_PATH):
                            atomic_copyfile(output_path, GLEAN_OUTPUT_STAGING_PATH)
                        move_to_working_files(output_path, "glean-automation")
                    else:
                        with GLEAN_JOBS_LOCK:
                            GLEAN_JOBS[job_id]["log"].append("WARNING: expected output file not found at " + output_path)
                except Exception as copy_err:
                    with GLEAN_JOBS_LOCK:
                        GLEAN_JOBS[job_id]["log"].append("WARNING: could not copy output to staging (" + str(copy_err) + ")")

        process.wait()

        with GLEAN_JOBS_LOCK:
            was_stopped = GLEAN_JOBS[job_id].get("stop_requested", False)
        if not work_done:
            with GLEAN_JOBS_LOCK:
                if was_stopped:
                    GLEAN_JOBS[job_id]["status"] = "stopped"
                    GLEAN_JOBS[job_id]["log"].append("-- Stopped manually. --")
                    # The script writes progress after every row, so even a
                    # stop mid-run should have left a usable partial file.
                    try:
                        if os.path.exists(output_path):
                            with csv_lock(GLEAN_OUTPUT_STAGING_PATH):
                                atomic_copyfile(output_path, GLEAN_OUTPUT_STAGING_PATH)
                            move_to_working_files(output_path, "glean-automation")
                    except Exception:
                        pass
                else:
                    GLEAN_JOBS[job_id]["status"] = "done" if process.returncode == 0 else "error"
                GLEAN_JOBS[job_id]["returncode"] = process.returncode
    except Exception as e:
        with GLEAN_JOBS_LOCK:
            if GLEAN_JOBS[job_id]["status"] != "done":
                GLEAN_JOBS[job_id]["status"] = "error"
            GLEAN_JOBS[job_id]["log"].append("ERROR: " + str(e))


GLEAN_REPORT_JOBS = {}
GLEAN_REPORT_JOBS_LOCK = threading.Lock()


def run_glean_report_job(job_id, input_path, output_base, prompt_text):
    """One-shot, unlike run_glean_job above -- a single batch prompt, not a
    per-row loop -- so there's no progressive-output safety net needed.
    Just wait for it to finish and report the result."""
    with GLEAN_REPORT_JOBS_LOCK:
        GLEAN_REPORT_JOBS[job_id]["status"] = "running"

    try:
        process = subprocess.Popen(
            ["node", GLEAN_NEWS_REPORT_SCRIPT_PATH, input_path, output_base, prompt_text],
            cwd=GLEAN_WORKSPACE_DIR,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
            start_new_session=True,
        )
        with GLEAN_REPORT_JOBS_LOCK:
            GLEAN_REPORT_JOBS[job_id]["process"] = process

        for line in process.stdout:
            with GLEAN_REPORT_JOBS_LOCK:
                GLEAN_REPORT_JOBS[job_id]["log"].append(line.rstrip("\n"))

        process.wait()

        with GLEAN_REPORT_JOBS_LOCK:
            GLEAN_REPORT_JOBS[job_id]["status"] = "done" if process.returncode == 0 else "error"
            GLEAN_REPORT_JOBS[job_id]["returncode"] = process.returncode

    except Exception as e:
        with GLEAN_REPORT_JOBS_LOCK:
            GLEAN_REPORT_JOBS[job_id]["status"] = "error"
            GLEAN_REPORT_JOBS[job_id]["log"].append("ERROR: " + str(e))


def load_generation_settings():
    if os.path.exists(GENERATION_SETTINGS_PATH):
        try:
            with open(GENERATION_SETTINGS_PATH, "r", encoding="utf-8") as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError):
            pass
    save_generation_settings(DEFAULT_GENERATION_SETTINGS)
    return json.loads(json.dumps(DEFAULT_GENERATION_SETTINGS))


def save_generation_settings(settings):
    with open(GENERATION_SETTINGS_PATH, "w", encoding="utf-8") as f:
        json.dump(settings, f, indent=2)


def load_csv_lookup(path):
    if not os.path.exists(path):
        return []
    with open(path, newline="", encoding="utf-8-sig") as f:
        return list(csv.DictReader(f))


def find_account(company_name):
    if not company_name:
        return {}
    target = company_name.strip().lower()
    for row in load_csv_lookup(ACCOUNTS_PATH):
        name = (row.get("Company Name") or "").strip().lower()
        alt = (row.get("Alternative Name") or "").strip().lower()
        if target and (target == name or (alt and target == alt)):
            return row
    return {}


def format_reference_list(rows, fields):
    lines = []
    for r in rows:
        parts = [(r.get(f) or "").strip() for f in fields]
        parts = [p for p in parts if p]
        if parts:
            lines.append(" | ".join(parts))
    return "\n".join(lines)


def build_row_context(row):
    company = (row.get("Company Name") or "").strip()
    account = find_account(company)
    industry = (account.get("Industry") or "").strip()

    lines = [
        "Contact Name: " + (row.get("Contact Name") or "Unknown"),
        "Title / Role: " + (row.get("Title / Role") or "Unknown"),
        "Company Name: " + (company or "Unknown"),
        "Patch: " + (row.get("Patch") or "Unknown"),
    ]
    if row.get("Source / Signal"):
        lines.append("Source / Signal: " + row.get("Source / Signal"))
    if row.get("Interest / Role Research"):
        lines.append("Interest / Role Research: " + row.get("Interest / Role Research"))
    if industry:
        lines.append("Industry: " + industry)
    if account.get("Account Tiering"):
        lines.append("Account Tier: " + account.get("Account Tiering"))
    if account.get("SDR Notes"):
        lines.append("SDR Notes: " + account.get("SDR Notes"))
    if account.get("Remarks"):
        lines.append("Account Remarks: " + account.get("Remarks"))

    use_case_text = format_reference_list(
        load_csv_lookup(USE_CASES_PATH),
        ["Use Case Name", "Problem Statement", "Why MongoDB", "Fits Industries", "Priority Theme"],
    )
    proof_point_text = format_reference_list(
        load_csv_lookup(PROOF_POINTS_PATH),
        ["Customer", "Industry", "Themes", "Summary", "Reference URL"],
    )

    return "\n".join(lines), use_case_text, proof_point_text


def call_ollama(user_prompt, system_prompt=None, json_mode=False):
    messages = []
    if system_prompt:
        messages.append({"role": "system", "content": system_prompt})
    messages.append({"role": "user", "content": user_prompt})

    payload = {
        "model": OLLAMA_MODEL,
        "messages": messages,
        "stream": False,
        "think": False,
    }
    if json_mode:
        payload["format"] = "json"

    req = urllib.request.Request(
        OLLAMA_URL,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=180) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    return (data.get("message", {}) or {}).get("content", "").strip()


@app.route("/library/<name>")
def library_page(name):
    if name not in LIBRARY_FILES:
        return redirect(url_for("library_page", name="accounts"))
    headers, rows = load_csv_full(LIBRARY_FILES[name])
    return render_template(
        "library.html",
        name=name,
        tabs=list(LIBRARY_LABELS.items()),
        headers=headers,
        rows=rows,
        row_count=len(rows),
    )


@app.route("/library/<name>/add", methods=["POST"])
def library_add_row(name):
    if name not in LIBRARY_FILES:
        return redirect(url_for("library_page", name="accounts"))
    path = LIBRARY_FILES[name]
    headers, _ = load_csv_full(path)
    if not headers:
        return redirect(url_for("library_page", name=name))

    new_row = {h: request.form.get(h, "").strip() for h in headers}
    with open(path, "a", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=headers)
        writer.writerow(new_row)

    return redirect(url_for("library_page", name=name))


@app.route("/library/<name>/import", methods=["POST"])
def library_import(name):
    if name not in LIBRARY_FILES:
        return redirect(url_for("library_page", name="accounts"))
    path = LIBRARY_FILES[name]
    headers, _ = load_csv_full(path)
    file = request.files.get("csv_file")
    if not headers or not file or file.filename == "":
        return redirect(url_for("library_page", name=name))

    token = uuid.uuid4().hex
    temp_path = os.path.join(UPLOADS_DIR, f"{token}.csv")
    file.save(temp_path)
    incoming = read_csv_dicts(temp_path)
    os.remove(temp_path)

    # Only columns matching this library's headers by name get copied in --
    # extra columns in the uploaded file are ignored, missing ones are left
    # blank, same permissive philosophy as the tracker import.
    new_rows = [{h: (r.get(h) or "").strip() for h in headers} for r in incoming]

    with open(path, "a", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=headers)
        writer.writerows(new_rows)

    return redirect(url_for("library_page", name=name))


@app.route("/library/<name>/update_cell", methods=["POST"])
def library_update_cell(name):
    if name not in LIBRARY_FILES:
        return jsonify({"error": "unknown library"}), 400
    data = request.get_json(silent=True) or {}
    try:
        row_index = int(data.get("row"))
    except (TypeError, ValueError):
        return jsonify({"error": "bad row index"}), 400
    column = data.get("column")
    value = data.get("value", "")

    path = LIBRARY_FILES[name]
    headers, rows = load_csv_full(path)
    if column not in headers or row_index < 0 or row_index >= len(rows):
        return jsonify({"error": "not found"}), 400

    rows[row_index][column] = value
    write_csv_full(path, headers, rows)
    return jsonify({"ok": True})


@app.route("/library/<name>/delete_row", methods=["POST"])
def library_delete_row(name):
    if name not in LIBRARY_FILES:
        return redirect(url_for("library_page", name="accounts"))
    try:
        row_index = int(request.form.get("row"))
    except (TypeError, ValueError):
        return redirect(url_for("library_page", name=name))

    path = LIBRARY_FILES[name]
    headers, rows = load_csv_full(path)
    if 0 <= row_index < len(rows):
        rows.pop(row_index)
        write_csv_full(path, headers, rows)

    return redirect(url_for("library_page", name=name))


@app.route("/generate/<action>", methods=["POST"])
def generate_action(action):
    if action not in GENERATION_ACTIONS:
        return jsonify({"error": "Unknown generation action."}), 400

    data = request.get_json(silent=True) or {}
    try:
        row_index = int(data.get("row"))
    except (TypeError, ValueError):
        return jsonify({"error": "Bad row index"}), 400

    headers, rows = load_tracker()
    if row_index < 0 or row_index >= len(rows):
        return jsonify({"error": "Row out of range"}), 400

    row = rows[row_index]
    company = (row.get("Company Name") or "").strip()
    contact = (row.get("Contact Name") or "").strip()
    if not company and not contact:
        return jsonify({"error": "This row has no Company or Contact Name to write about yet."}), 400

    patch = (row.get("Patch") or "").strip().lower()
    variant = "japan" if patch == "japan" else "default"

    settings = load_generation_settings()
    system_prompt = (settings.get(action, {}) or {}).get(variant, "")
    if not system_prompt:
        return jsonify({"error": "No instructions configured for this action. Check Settings."}), 400

    context_block, use_case_text, proof_point_text = build_row_context(row)
    user_prompt = (
        "Contact and account context:\n" + context_block + "\n\n"
        "Available use cases to choose from (pick the most relevant, never invent your own header):\n"
        + (use_case_text or "(none available)") + "\n\n"
        "Available proof points to choose from (pick the single most relevant, never invent a customer or metric):\n"
        + (proof_point_text or "(none available)")
    )

    spec = GENERATION_ACTIONS[action]
    try:
        if spec["json_output"]:
            raw = call_ollama(user_prompt, system_prompt=system_prompt, json_mode=True)
            try:
                parsed = json.loads(raw)
            except (json.JSONDecodeError, TypeError):
                return jsonify({"error": "The model did not return valid JSON. Raw output: " + (raw or "")[:300]}), 502

            company_label = company or "this account"

            if variant == "japan":
                topic_en = (parsed.get("topic_en") or "").strip()
                topic_jp = (parsed.get("topic_jp") or "").strip()
                body_en = (parsed.get("email_body_en") or "").strip()
                body_jp = (parsed.get("email_body_jp") or "").strip()

                if topic_en or body_en:
                    subject = (company_label + " <> MongoDB | " + topic_en + " " + topic_jp).strip()
                    if not body_jp:
                        body_jp = "[Japanese section was not generated -- please regenerate or write manually]"
                    email_body = (body_en + "\n\n" + body_jp) if body_en else body_jp
                else:
                    # Settings weren't refreshed to the new prompt shape -- fall back
                    # to the old single-field response so it doesn't come out empty.
                    subject = (parsed.get("subject_line") or "").strip()
                    email_body = (parsed.get("email_body") or "").strip()
            else:
                topic = (parsed.get("topic") or "").strip()
                body = (parsed.get("email_body") or "").strip()
                if topic:
                    subject = (company_label + " <> MongoDB - Introduction // " + topic).strip()
                    email_body = body
                else:
                    subject = (parsed.get("subject_line") or "").strip()
                    email_body = body

            values = {"Subject Line": subject, "Messaging": email_body}
        else:
            text = call_ollama(user_prompt, system_prompt=system_prompt)
            values = {spec["target_fields"][0]: text.strip()}
    except Exception as e:
        return jsonify({"error": "Could not reach Ollama at localhost:11434 -- is it running? (" + str(e) + ")"}), 502

    if not any(values.values()):
        return jsonify({"error": "Ollama returned an empty response."}), 502

    for field, val in values.items():
        if field in headers:
            rows[row_index][field] = val
    write_tracker(headers, rows)

    return jsonify({"ok": True, "values": values})


@app.route("/settings/validation", methods=["GET", "POST"])
def validation_settings_page():
    rules = load_validation_rules()
    headers = get_headers()

    if request.method == "POST":
        column = request.form.get("column", "")
        rule_type = request.form.get("rule_type", "fixed")
        if column in headers:
            if rule_type == "lookup":
                lookup_column = request.form.get("lookup_column", column)
                rules[column] = {"type": "lookup", "source": "accounts", "column": lookup_column}
                save_validation_rules(rules)
            else:
                raw_values = request.form.get("values", "")
                values = [v.strip() for v in raw_values.split("\n") if v.strip()]
                if values:
                    rules[column] = values
                else:
                    rules.pop(column, None)
                save_validation_rules(rules)
        return redirect(url_for("validation_settings_page"))

    resolved = resolve_validation_options(rules)
    accounts_headers, _ = load_csv_full(ACCOUNTS_PATH)

    columns_info = []
    for h in headers:
        rule = rules.get(h)
        is_lookup = isinstance(rule, dict) and rule.get("type") == "lookup"
        allowed = resolved.get(h, [])
        total_filled, mismatches = validation_mismatch_count(h, allowed)
        columns_info.append({
            "name": h,
            "is_lookup": is_lookup,
            "lookup_column": (rule.get("column", h) if is_lookup else h),
            "allowed": allowed,
            "allowed_text": "" if is_lookup else "\n".join(allowed),
            "total_filled": total_filled,
            "mismatches": mismatches,
        })

    return render_template("validation_settings.html", columns_info=columns_info, accounts_headers=accounts_headers)


@app.route("/settings", methods=["GET", "POST"])
def settings_page():
    settings = load_generation_settings()
    if request.method == "POST":
        for action_id in GENERATION_ACTIONS:
            for variant in ("default", "japan"):
                val = request.form.get(action_id + "__" + variant, "").strip()
                if val:
                    settings.setdefault(action_id, {})[variant] = val
        save_generation_settings(settings)
        return redirect(url_for("settings_page", saved="1"))

    actions = [(aid, spec["label"]) for aid, spec in GENERATION_ACTIONS.items()]
    return render_template(
        "settings.html",
        settings=settings,
        actions=actions,
        defaults=DEFAULT_GENERATION_SETTINGS,
        saved=request.args.get("saved") == "1",
    )


@app.route("/scripts")
def scripts_page():
    scripts = [(sid, s["label"], s["description"]) for sid, s in AVAILABLE_SCRIPTS.items()]
    return render_template("scripts.html", scripts=scripts)


@app.route("/scripts/run/<script_id>", methods=["POST"])
def run_script(script_id):
    if script_id not in AVAILABLE_SCRIPTS:
        return jsonify({"error": "Unknown script"}), 400

    job_id = uuid.uuid4().hex
    with SCRIPT_JOBS_LOCK:
        SCRIPT_JOBS[job_id] = {
            "script_id": script_id,
            "status": "queued",
            "log": [],
            "returncode": None,
        }

    command = AVAILABLE_SCRIPTS[script_id]["command"]
    thread = threading.Thread(target=run_script_job, args=(job_id, command), daemon=True)
    thread.start()

    return jsonify({"ok": True, "job_id": job_id})


@app.route("/scripts/status/<job_id>")
def script_status(job_id):
    with SCRIPT_JOBS_LOCK:
        job = SCRIPT_JOBS.get(job_id)
        if not job:
            return jsonify({"error": "Unknown job"}), 404
        return jsonify({
            "status": job["status"],
            "log": job["log"],
            "returncode": job["returncode"],
        })


@app.route("/scraper")
def scraper_page():
    has_staging = os.path.exists(STAGING_CSV_PATH)
    return render_template("scraper.html", has_staging=has_staging)


@app.route("/news")
def news_page():
    recent_files = []
    if os.path.isdir(NEWS_OUTPUT_DIR):
        recent_files = sorted(
            [f for f in os.listdir(NEWS_OUTPUT_DIR) if f.endswith(".csv")],
            reverse=True,
        )[:10]

    account_rows = read_csv_dicts(ACCOUNTS_PATH) if os.path.exists(ACCOUNTS_PATH) else []
    accounts = [
        {
            "name": (r.get("Company Name") or "").strip(),
            "alt_name": (r.get("Alternative Name") or "").strip(),
            "patch": (r.get("Patch") or "").strip(),
            "industry": (r.get("Industry") or "").strip(),
            "tier": (r.get("Account Tiering") or "").strip(),
        }
        for r in account_rows if (r.get("Company Name") or "").strip()
    ]

    keyword_headers, keyword_rows = load_csv_full(NEWS_KEYWORDS_PATH)

    return render_template(
        "news.html",
        recent_files=recent_files,
        accounts=accounts,
        account_count=len(accounts),
        keyword_headers=keyword_headers,
        keyword_rows=keyword_rows,
    )


@app.route("/news/run", methods=["POST"])
def news_run():
    if not os.path.exists(NEWS_SCRIPT_PATH):
        return jsonify({"error": "news_workspace/main.py not found."}), 400

    limit_raw = (request.form.get("limit") or "").strip()
    limit = int(limit_raw) if limit_raw.isdigit() else None

    selected_companies = request.form.getlist("companies")
    accounts_csv_path = ACCOUNTS_PATH

    if selected_companies:
        selected_set = set(selected_companies)
        headers, rows = load_csv_full(ACCOUNTS_PATH)
        filtered_rows = [r for r in rows if (r.get("Company Name") or "").strip() in selected_set]
        write_csv_full(NEWS_SELECTED_ACCOUNTS_PATH, headers, filtered_rows)
        accounts_csv_path = NEWS_SELECTED_ACCOUNTS_PATH

    # Keywords use the same "nothing checked = run everything, otherwise
    # run only what's checked" pattern as accounts above. Selection is by
    # row index into keywords.csv (matches the order the template renders
    # them in), since query text isn't guaranteed unique.
    selected_keyword_indices = request.form.getlist("keywords")
    keywords_csv_path = NEWS_KEYWORDS_PATH

    if selected_keyword_indices:
        try:
            idx_set = {int(i) for i in selected_keyword_indices}
        except ValueError:
            return jsonify({"error": "Bad keyword selection."}), 400
        headers, rows = load_csv_full(NEWS_KEYWORDS_PATH)
        filtered_rows = [r for i, r in enumerate(rows) if i in idx_set]
        # A row the user explicitly ticked should run even if its stored
        # "active" flag is NO -- the checkbox is the intent here, not the
        # CSV's own flag.
        for r in filtered_rows:
            r["active"] = "YES"
        write_csv_full(NEWS_SELECTED_KEYWORDS_PATH, headers, filtered_rows)
        keywords_csv_path = NEWS_SELECTED_KEYWORDS_PATH

    job_id = uuid.uuid4().hex
    with NEWS_JOBS_LOCK:
        NEWS_JOBS[job_id] = {
            "status": "queued", "log": [], "returncode": None, "process": None,
            "output_file": None, "stop_requested": False,
        }

    thread = threading.Thread(
        target=run_news_job, args=(job_id, limit, accounts_csv_path, keywords_csv_path), daemon=True,
    )
    thread.start()

    return jsonify({"ok": True, "job_id": job_id})


@app.route("/news/stop/<job_id>", methods=["POST"])
def news_stop(job_id):
    with NEWS_JOBS_LOCK:
        job = NEWS_JOBS.get(job_id)
        process = job.get("process") if job else None
        if job:
            job["stop_requested"] = True
    if not process:
        return jsonify({"error": "Job not found or not running."}), 404

    thread = threading.Thread(target=_terminate_then_kill, args=(process, job_id, NEWS_JOBS, NEWS_JOBS_LOCK), daemon=True)
    thread.start()

    return jsonify({"ok": True})


@app.route("/news/status/<job_id>")
def news_status(job_id):
    with NEWS_JOBS_LOCK:
        job = NEWS_JOBS.get(job_id)
        if not job:
            return jsonify({"error": "Unknown job"}), 404
        return jsonify({
            "status": job["status"],
            "log": job["log"],
            "returncode": job["returncode"],
            "output_file": job.get("output_file"),
        })


@app.route("/news/preview/<filename>")
def news_preview(filename):
    safe_name = os.path.basename(filename)
    path = os.path.join(NEWS_OUTPUT_DIR, safe_name)
    headers, rows = load_csv_full(path)
    return render_template(
        "news_preview.html",
        filename=safe_name,
        headers=headers,
        rows=rows,
        row_count=len(rows),
        default_prompt=DEFAULT_NEWS_REPORT_PROMPT,
    )


@app.route("/news/download/<filename>")
def news_download(filename):
    safe_name = os.path.basename(filename)
    path = os.path.join(NEWS_OUTPUT_DIR, safe_name)
    if not os.path.exists(path):
        return "Not found", 404
    return send_file(path, as_attachment=True, download_name=safe_name, mimetype="text/csv")


@app.route("/live-feed")
def live_feed_page():
    recent_files = []
    if os.path.isdir(LIVE_FEED_OUTPUT_DIR):
        recent_files = sorted(
            [f for f in os.listdir(LIVE_FEED_OUTPUT_DIR) if f.endswith(".csv")],
            reverse=True,
        )[:10]
    return render_template("live_feed.html", recent_files=recent_files)


@app.route("/live-feed/run", methods=["POST"])
def live_feed_run():
    if not os.path.exists(LIVE_FEED_SCRIPT_PATH):
        return jsonify({"error": "live_feed_workspace/scrape_live_feed.js not found."}), 400

    page_raw = (request.form.get("page") or "").strip()
    if not page_raw.isdigit() or int(page_raw) < 1:
        return jsonify({"error": "Enter a valid page number (1 or higher)."}), 400
    target_page = int(page_raw)

    job_id = uuid.uuid4().hex
    with LIVE_FEED_JOBS_LOCK:
        LIVE_FEED_JOBS[job_id] = {"status": "queued", "log": [], "returncode": None, "process": None, "output_file": None}

    thread = threading.Thread(target=run_live_feed_job, args=(job_id, target_page), daemon=True)
    thread.start()

    return jsonify({"ok": True, "job_id": job_id})


@app.route("/live-feed/continue/<job_id>", methods=["POST"])
def live_feed_continue(job_id):
    with LIVE_FEED_JOBS_LOCK:
        job = LIVE_FEED_JOBS.get(job_id)
        process = job.get("process") if job else None
    if not process:
        return jsonify({"error": "Job not found or not running."}), 404
    try:
        process.stdin.write("\n")
        process.stdin.flush()
    except Exception as e:
        return jsonify({"error": str(e)}), 500
    return jsonify({"ok": True})


@app.route("/live-feed/status/<job_id>")
def live_feed_status(job_id):
    with LIVE_FEED_JOBS_LOCK:
        job = LIVE_FEED_JOBS.get(job_id)
        if not job:
            return jsonify({"error": "Unknown job"}), 404
        return jsonify({
            "status": job["status"],
            "log": job["log"],
            "returncode": job["returncode"],
            "output_file": job.get("output_file"),
        })


def _parse_tracker_date(value):
    """Parses the tracker's 'DD Mon YY' date strings (e.g. '26 Jun 26').
    Returns None if empty/unparseable so callers can treat those rows
    as always older than any row with a real date."""
    value = (value or "").strip()
    if not value:
        return None
    try:
        return datetime.strptime(value, "%d %b %y")
    except ValueError:
        return None


def build_tracker_lookup_by_salesloft_id():
    """Maps Salesloft person ID -> tracker row, for joining against the
    Live Feed scraper's person_id (both are the same Salesloft ID space --
    'Salesloft Link' in the tracker is just the bare numeric ID). When a
    Salesloft ID shows up on more than one tracker row, the most recently
    contacted row wins (falls back to First Contact, then whichever row
    came first in the file if neither date parses)."""
    _, rows = load_csv_full(TRACKER_PATH)
    lookup = {}
    for row in rows:
        salesloft_id = (row.get("Salesloft Link") or "").strip()
        if not salesloft_id:
            continue
        existing = lookup.get(salesloft_id)
        if existing is None:
            lookup[salesloft_id] = row
            continue
        new_date = _parse_tracker_date(row.get("Last Contacted")) or _parse_tracker_date(row.get("First Contact"))
        existing_date = _parse_tracker_date(existing.get("Last Contacted")) or _parse_tracker_date(existing.get("First Contact"))
        if new_date and (not existing_date or new_date > existing_date):
            lookup[salesloft_id] = row
    return lookup


def get_first_phone(tracker_row):
    """Returns the first non-empty phone number across the tracker's phone
    fields, in priority order, as raw text (unformatted)."""
    phone_fields = ["Phone", "LinkedIn Phone", "Cognism Phone", "LeadIQ Phone", "ZoomInfo Phone"]
    for field in phone_fields:
        raw = (tracker_row.get(field) or "").strip()
        if raw:
            return raw
    return ""


def build_whatsapp_link(phone):
    """Builds a wa.me link from a phone number. wa.me needs digits only in
    international format (country code, no leading 0 or +) -- this just
    strips non-digits, so numbers that aren't already stored in
    international format may not resolve to the right contact."""
    digits = re.sub(r"\D", "", phone or "")
    return f"https://wa.me/{digits}" if digits else ""


def build_contact_card_data(tracker_row):
    phone = get_first_phone(tracker_row)
    return {
        "in_tracker": True,
        "company_name": tracker_row.get("Company Name", ""),
        "title": tracker_row.get("Title / Role", ""),
        "account_executive": tracker_row.get("Account Executive", ""),
        "status": tracker_row.get("Status", ""),
        "sentiments": tracker_row.get("Sentiments", ""),
        "last_contacted": tracker_row.get("Last Contacted", ""),
        "linkedin": tracker_row.get("LinkedIn Profile", ""),
        "phone": phone,
        "whatsapp_message": tracker_row.get("Whatsapp Message", ""),
        "about": tracker_row.get("About", ""),
        "whatsapp_link": build_whatsapp_link(phone),
    }


@app.route("/live-feed/preview/<filename>")
def live_feed_preview(filename):
    safe_name = os.path.basename(filename)
    path = os.path.join(LIVE_FEED_OUTPUT_DIR, safe_name)
    headers, rows = load_csv_full(path)
    event_types = sorted({(r.get("event_type") or "").strip() for r in rows if (r.get("event_type") or "").strip()})

    # Join each live feed row's Salesloft person_id against the tracker so
    # the preview can show a contact card -- with a visible "not tracked
    # yet" flag when someone is showing activity but isn't in the tracker.
    tracker_lookup = build_tracker_lookup_by_salesloft_id()
    contacts_by_person_id = {}
    for r in rows:
        person_id = (r.get("person_id") or "").strip()
        if not person_id or person_id in contacts_by_person_id:
            continue
        tracker_row = tracker_lookup.get(person_id)
        contacts_by_person_id[person_id] = (
            build_contact_card_data(tracker_row) if tracker_row else {"in_tracker": False}
        )

    return render_template(
        "live_feed_preview.html",
        filename=safe_name,
        headers=headers,
        rows=rows,
        row_count=len(rows),
        default_widths=LIVE_FEED_DEFAULT_WIDTHS,
        event_types=event_types,
        contacts_by_person_id=contacts_by_person_id,
    )


@app.route("/live-feed/download/<filename>")
def live_feed_download(filename):
    safe_name = os.path.basename(filename)
    path = os.path.join(LIVE_FEED_OUTPUT_DIR, safe_name)
    if not os.path.exists(path):
        return "Not found", 404
    return send_file(path, as_attachment=True, download_name=safe_name, mimetype="text/csv")


@app.route("/scraper/run", methods=["POST"])
def scraper_run():
    file = request.files.get("csv_file")
    if not file or file.filename == "":
        return jsonify({"error": "No CSV file provided."}), 400
    if not os.path.exists(SCRAPER_SCRIPT_PATH):
        return jsonify({"error": "zoominfo_scrap.js not found in scraper_workspace/."}), 400

    input_path = os.path.join(SCRAPER_WORKSPACE_DIR, "input_" + uuid.uuid4().hex + ".csv")
    file.save(input_path)

    unlock_mobile = request.form.get("unlock_mobile") == "1"

    job_id = uuid.uuid4().hex
    with SCRAPER_JOBS_LOCK:
        SCRAPER_JOBS[job_id] = {"status": "queued", "log": [], "returncode": None, "process": None, "output_file": None}

    thread = threading.Thread(target=run_scraper_job, args=(job_id, input_path, unlock_mobile), daemon=True)
    thread.start()

    return jsonify({"ok": True, "job_id": job_id})


@app.route("/scraper/continue/<job_id>", methods=["POST"])
def scraper_continue(job_id):
    with SCRAPER_JOBS_LOCK:
        job = SCRAPER_JOBS.get(job_id)
        process = job.get("process") if job else None
    if not process:
        return jsonify({"error": "Job not found or not running."}), 404
    try:
        process.stdin.write("\n")
        process.stdin.flush()
    except Exception as e:
        return jsonify({"error": str(e)}), 500
    return jsonify({"ok": True})


@app.route("/scraper/status/<job_id>")
def scraper_status(job_id):
    with SCRAPER_JOBS_LOCK:
        job = SCRAPER_JOBS.get(job_id)
        if not job:
            return jsonify({"error": "Unknown job"}), 404
        return jsonify({
            "status": job["status"],
            "log": job["log"],
            "returncode": job["returncode"],
        })


@app.route("/scraper/preview")
def scraper_preview():
    headers, rows = load_csv_full(STAGING_CSV_PATH)
    return render_template(
        "scraper_preview.html",
        headers=headers,
        rows=rows,
        row_count=len(rows),
        # Always offer all four sources -- row.get(col, "") already
        # handles a column being absent from this particular file, so
        # filtering against `headers` just risked hiding the picker
        # entirely on a header mismatch (extra whitespace, BOM, etc.)
        phone_source_cols=PHONE_SOURCE_COLUMNS,
        email_source_cols=EMAIL_SOURCE_COLUMNS,
        sync_info=read_staging_sync_meta(STAGING_CSV_PATH),
    )


@app.route("/scraper/preview/update_cell", methods=["POST"])
def scraper_preview_update_cell():
    data = request.get_json(silent=True) or {}
    try:
        row_index = int(data.get("row"))
    except (TypeError, ValueError):
        return jsonify({"error": "bad row index"}), 400
    column = data.get("column")
    value = data.get("value", "")

    headers, rows = load_csv_full(STAGING_CSV_PATH)
    if column not in headers or row_index < 0 or row_index >= len(rows):
        return jsonify({"error": "not found"}), 400

    rows[row_index][column] = value
    write_csv_full(STAGING_CSV_PATH, headers, rows)
    return jsonify({"ok": True})


@app.route("/scraper/preview/delete_row", methods=["POST"])
def scraper_preview_delete_row():
    try:
        row_index = int(request.form.get("row"))
    except (TypeError, ValueError):
        return redirect(url_for("scraper_preview"))

    headers, rows = load_csv_full(STAGING_CSV_PATH)
    if 0 <= row_index < len(rows):
        rows.pop(row_index)
        write_csv_full(STAGING_CSV_PATH, headers, rows)

    return redirect(url_for("scraper_preview"))


@app.route("/scraper/preview/export")
def scraper_preview_export():
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    return send_file(
        STAGING_CSV_PATH,
        as_attachment=True,
        download_name=f"scraper_output_{timestamp}.csv",
        mimetype="text/csv",
    )


@app.route("/scraper/preview/commit", methods=["POST"])
def scraper_preview_commit():
    staging_headers, staging_rows = load_csv_full(STAGING_CSV_PATH)
    tracker_headers = get_headers()
    if not staging_rows or not tracker_headers:
        return redirect(url_for("scraper_preview"))

    new_rows = [{h: (r.get(h) or "") for h in tracker_headers} for r in staging_rows]
    with open(TRACKER_PATH, "a", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=tracker_headers)
        writer.writerows(new_rows)

    return redirect(url_for("dashboard_page"))


# ── Contact Finisher tools -- Research Builder + Phone/Email Select ──
#
# These two features can be reached two ways:
#   1. Standalone pages under /tools, which work against any of three
#      sources: the scraper staging CSV, the live tracker.csv, or a
#      one-off CSV uploaded just for this session.
#   2. Inline actions embedded directly in the /scraper/preview table
#      (that page always targets source=staging), calling these same
#      JSON endpoints -- see the inline snippet notes alongside this
#      code for what to add to scraper_preview.html.
#
# Both entry points write straight through to whichever file backs the
# chosen source (same "persist on every edit" pattern as update_cell
# elsewhere in this app), so there's no separate "save" step -- only
# Export and Commit-to-tracker are explicit actions.

TOOLS_UPLOAD_PATH = os.path.join(UPLOADS_DIR, "tools_working.csv")

# Maps the `source` query/body param used throughout /tools to the file
# it reads from and writes back to.
SOURCE_PATHS = {
    "staging": STAGING_CSV_PATH,
    "account_staging": ACCOUNT_STAGING_CSV_PATH,
    "tracker": TRACKER_PATH,
    "upload": TOOLS_UPLOAD_PATH,
    # Not part of the /tools UI itself -- added so glean_preview.html can
    # reuse the same generic mass-update/bulk-delete endpoints instead of
    # duplicating that logic for its own staging files.
    "glean_input": GLEAN_INPUT_STAGING_PATH,
    "glean_output": GLEAN_OUTPUT_STAGING_PATH,
}

SOURCE_LABELS = {
    "staging": "LinkedIn URL Scraper staging",
    "account_staging": "LinkedIn Account Scrape staging",
    "tracker": "Live tracker.csv",
    "upload": "Uploaded CSV",
    "glean_input": "Glean input staging",
    "glean_output": "Glean output staging",
}

PHONE_SOURCE_COLUMNS = ["LinkedIn Phone", "Cognism Phone", "LeadIQ Phone", "ZoomInfo Phone"]
EMAIL_SOURCE_COLUMNS = ["LinkedIn Email", "Cognism Email", "LeadIQ Email", "ZoomInfo Email"]


def get_source_path_or_404(source):
    path = SOURCE_PATHS.get(source)
    if not path:
        abort(404, description="Unknown source: " + str(source))
    return path


def build_research_text(row):
    """Same concatenation rule as the standalone Contact Finisher tool:
    About: {x}, Experience Description: {y}, Latest Post: {z}."""
    about = (row.get("About") or "").strip()
    experience = (row.get("Experience Description") or "").strip()
    post = (row.get("Latest Post") or "").strip()
    return f"About: {about}, Experience Description: {experience}, Latest Post: {post}"


@app.route("/tools")
def tools_landing():
    has_staging = os.path.exists(STAGING_CSV_PATH) and os.path.getsize(STAGING_CSV_PATH) > 0
    has_account_staging = os.path.exists(ACCOUNT_STAGING_CSV_PATH) and os.path.getsize(ACCOUNT_STAGING_CSV_PATH) > 0
    has_tracker = os.path.exists(TRACKER_PATH) and os.path.getsize(TRACKER_PATH) > 0
    has_upload = os.path.exists(TOOLS_UPLOAD_PATH) and os.path.getsize(TOOLS_UPLOAD_PATH) > 0
    return render_template(
        "tools_landing.html",
        has_staging=has_staging,
        has_account_staging=has_account_staging,
        has_tracker=has_tracker,
        has_upload=has_upload,
        source_labels=SOURCE_LABELS,
        # Row count + last-synced info shown directly on each card, so you
        # can tell at a glance whether it's your latest run before opening
        # it -- rather than only finding out once you're already inside
        # tools_preview.html.
        staging_row_count=len(load_csv_full(STAGING_CSV_PATH)[1]) if has_staging else 0,
        account_staging_row_count=len(load_csv_full(ACCOUNT_STAGING_CSV_PATH)[1]) if has_account_staging else 0,
        staging_sync_info=read_staging_sync_meta(STAGING_CSV_PATH) if has_staging else None,
        account_staging_sync_info=read_staging_sync_meta(ACCOUNT_STAGING_CSV_PATH) if has_account_staging else None,
    )


@app.route("/tools/upload", methods=["POST"])
def tools_upload():
    """Accepts a one-off CSV for either tool and stages it at
    TOOLS_UPLOAD_PATH, then drops the user into an editable preview of
    every row so they can clean anything up before specializing into
    Research Builder or Phone / Email Select."""
    file = request.files.get("csv_file")
    if not file or file.filename == "":
        return redirect(url_for("tools_landing"))

    file.save(TOOLS_UPLOAD_PATH)
    return redirect(url_for("tools_preview_page", source="upload"))


# -- Raw preview / freeform edit --------------------------------------
#
# The single shared hub for all three /tools sources (account scraper
# staging, live tracker, one-off upload) -- Research Builder and Phone /
# Email Select only ever touch their own dedicated column(s), so this is
# where general cleanup, mass updates, deletes, and the Glean bridge all
# live, identically, regardless of which source you came in through.
# Switching sources from the dropdown here just re-navigates to this
# same route with a different `source` -- there is only one template to
# keep in sync.

@app.route("/tools/preview")
def tools_preview_page():
    source = request.args.get("source", "upload")
    path = get_source_path_or_404(source)
    headers, rows = load_csv_full(path)
    return render_template(
        "tools_preview.html",
        source=source,
        source_label=SOURCE_LABELS.get(source, source),
        source_labels=SOURCE_LABELS,
        headers=headers,
        rows=rows,
        row_count=len(rows),
        # Only staging/account_staging are ever synced from a scraper run --
        # tracker and upload are hand-maintained, so there's nothing to
        # report there and read_staging_sync_meta will just return None.
        sync_info=read_staging_sync_meta(path),
    )


@app.route("/tools/preview/update_cell", methods=["POST"])
def tools_preview_update_cell():
    """Generic single-cell edit -- same persist-on-every-edit shape as
    the other update_cell endpoints, but keyed by arbitrary column name
    instead of a fixed field, since this page has no fixed schema."""
    data = request.get_json(silent=True) or {}
    source = data.get("source", "upload")
    path = get_source_path_or_404(source)
    column = data.get("column")
    if not column:
        return jsonify({"error": "missing column"}), 400

    try:
        row_index = int(data.get("row"))
    except (TypeError, ValueError):
        return jsonify({"error": "bad row index"}), 400
    value = data.get("value", "")

    with csv_lock(path):
        headers, rows = load_csv_full(path)
        if row_index < 0 or row_index >= len(rows):
            return jsonify({"error": "not found"}), 400
        if column not in headers:
            headers.append(column)

        rows[row_index][column] = value
        write_csv_full(path, headers, rows)
        return jsonify({"ok": True})


@app.route("/tools/preview/fill_column", methods=["POST"])
def tools_preview_fill_column():
    """Bulk-sets one column to the same value across a list of rows --
    powers the mass-update bar (e.g. set Patch to "Indonesia" for every
    row instead of editing cell-by-cell). If `rows` is omitted entirely,
    applies to every row in the file; otherwise only the given indices
    (the UI sends whatever's currently visible under the search filter)."""
    data = request.get_json(silent=True) or {}
    source = data.get("source", "upload")
    path = get_source_path_or_404(source)
    column = data.get("column")
    if not column:
        return jsonify({"error": "missing column"}), 400
    value = data.get("value", "")
    raw_rows = data.get("rows")

    with csv_lock(path):
        headers, rows = load_csv_full(path)
        if column not in headers:
            headers.append(column)

        if raw_rows is None:
            target_indices = list(range(len(rows)))
        else:
            try:
                target_indices = sorted({int(i) for i in raw_rows})
            except (TypeError, ValueError):
                return jsonify({"error": "bad row index"}), 400
            for i in target_indices:
                if i < 0 or i >= len(rows):
                    return jsonify({"error": f"row {i} not found"}), 400

        for i in target_indices:
            rows[i][column] = value

        write_csv_full(path, headers, rows)
        return jsonify({"ok": True, "count": len(target_indices)})


@app.route("/tools/preview/delete_rows", methods=["POST"])
def tools_preview_delete_rows():
    """Deletes one or more rows by index -- powers both the per-row ×
    button and the bulk 'Delete these rows' action next to the mass-
    update bar. Indices are resolved and removed highest-first so
    earlier deletions in the same batch don't shift later indices out
    from under us."""
    data = request.get_json(silent=True) or {}
    source = data.get("source", "upload")
    path = get_source_path_or_404(source)
    raw_rows = data.get("rows")
    if not isinstance(raw_rows, list) or not raw_rows:
        return jsonify({"error": "no rows given"}), 400
    try:
        target_indices = sorted({int(i) for i in raw_rows}, reverse=True)
    except (TypeError, ValueError):
        return jsonify({"error": "bad row index"}), 400

    with csv_lock(path):
        headers, rows = load_csv_full(path)
        for i in target_indices:
            if i < 0 or i >= len(rows):
                return jsonify({"error": f"row {i} not found"}), 400
        for i in target_indices:
            rows.pop(i)
        write_csv_full(path, headers, rows)
        return jsonify({"ok": True, "deleted": len(target_indices), "remaining": len(rows)})


@app.route("/tools/preview/export")
def tools_preview_export():
    source = request.args.get("source", "upload")
    path = get_source_path_or_404(source)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    return send_file(
        path,
        as_attachment=True,
        download_name=f"preview_{source}_{timestamp}.csv",
        mimetype="text/csv",
    )


@app.route("/tools/preview/send_to_glean", methods=["POST"])
def tools_preview_send_to_glean():
    """Bridges this generic preview into the Glean Message Enhance flow:
    takes whatever's currently in this source's file -- including any
    mass-update/cleanup/delete done right here on the preview page -- and
    stages it as a fresh Glean input batch (same column-mapping and
    tracker-fingerprint matching as a direct /glean/upload), then drops
    the user straight into that review screen instead of back here."""
    source = request.form.get("source", "upload")
    path = get_source_path_or_404(source)

    _, incoming = load_csv_full(path)
    if not incoming:
        return redirect(url_for("tools_preview_page", source=source))

    tracker_headers = get_headers()
    staged = build_glean_staging_rows(incoming, tracker_headers)
    if not staged:
        return redirect(url_for("tools_preview_page", source=source))

    staging_headers = tracker_headers + [GLEAN_EXTRA_CONTEXT_COL] + GLEAN_INTERNAL_COLS
    write_csv_full(GLEAN_INPUT_STAGING_PATH, staging_headers, staged)
    return redirect(url_for("glean_preview", stage="input"))



# -- Research Builder -----------------------------------------------

@app.route("/tools/research")
def research_builder_page():
    source = request.args.get("source", "staging")
    path = get_source_path_or_404(source)
    headers, rows = load_csv_full(path)
    return render_template(
        "research_builder.html",
        source=source,
        source_label=SOURCE_LABELS.get(source, source),
        headers=headers,
        rows=rows,
        row_count=len(rows),
    )


@app.route("/tools/research/generate", methods=["POST"])
def research_builder_generate():
    """Generates the Interest / Role Research summary. Omit `row` to
    generate for every row at once."""
    data = request.get_json(silent=True) or {}
    source = data.get("source", "staging")
    path = get_source_path_or_404(source)

    with csv_lock(path):
        headers, rows = load_csv_full(path)
        if "Interest / Role Research" not in headers:
            headers.append("Interest / Role Research")

        row_index = data.get("row")
        if row_index is None:
            for row in rows:
                row["Interest / Role Research"] = build_research_text(row)
            write_csv_full(path, headers, rows)
            return jsonify({"ok": True, "count": len(rows)})

        try:
            row_index = int(row_index)
        except (TypeError, ValueError):
            return jsonify({"error": "bad row index"}), 400
        if row_index < 0 or row_index >= len(rows):
            return jsonify({"error": "not found"}), 400

        rows[row_index]["Interest / Role Research"] = build_research_text(rows[row_index])
        write_csv_full(path, headers, rows)
        return jsonify({"ok": True, "value": rows[row_index]["Interest / Role Research"]})


@app.route("/tools/research/update_cell", methods=["POST"])
def research_builder_update_cell():
    """Manual edits to the Interest / Role Research textarea -- same
    persist-on-every-edit shape as the other update_cell endpoints."""
    data = request.get_json(silent=True) or {}
    source = data.get("source", "staging")
    path = get_source_path_or_404(source)
    try:
        row_index = int(data.get("row"))
    except (TypeError, ValueError):
        return jsonify({"error": "bad row index"}), 400
    value = data.get("value", "")

    with csv_lock(path):
        headers, rows = load_csv_full(path)
        if row_index < 0 or row_index >= len(rows):
            return jsonify({"error": "not found"}), 400
        if "Interest / Role Research" not in headers:
            headers.append("Interest / Role Research")

        rows[row_index]["Interest / Role Research"] = value
        write_csv_full(path, headers, rows)
        return jsonify({"ok": True})


@app.route("/tools/research/export")
def research_builder_export():
    source = request.args.get("source", "staging")
    path = get_source_path_or_404(source)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    return send_file(
        path,
        as_attachment=True,
        download_name=f"research_{source}_{timestamp}.csv",
        mimetype="text/csv",
    )


# -- Phone / Email Select ---------------------------------------------

@app.route("/tools/contacts")
def contact_select_page():
    source = request.args.get("source", "staging")
    path = get_source_path_or_404(source)
    headers, rows = load_csv_full(path)
    return render_template(
        "contact_select.html",
        source=source,
        source_label=SOURCE_LABELS.get(source, source),
        headers=headers,
        rows=rows,
        row_count=len(rows),
        # Always offer all four sources per field -- see the comment on
        # this same pattern in scraper_preview() above.
        phone_source_cols=PHONE_SOURCE_COLUMNS,
        email_source_cols=EMAIL_SOURCE_COLUMNS,
    )


@app.route("/tools/contacts/select", methods=["POST"])
def contact_select_apply():
    """Copies one source column's value (e.g. Cognism Phone) into the
    row's final Phone or Email column. Whatever runs last -- a select
    or a manual edit via update_cell below -- wins, since both just
    overwrite the same cell."""
    data = request.get_json(silent=True) or {}
    source = data.get("source", "staging")
    path = get_source_path_or_404(source)
    field = data.get("field")
    source_column = data.get("source_column")

    valid_columns = (
        PHONE_SOURCE_COLUMNS if field == "Phone"
        else EMAIL_SOURCE_COLUMNS if field == "Email"
        else None
    )
    if not valid_columns or source_column not in valid_columns:
        return jsonify({"error": "invalid field/source_column"}), 400

    try:
        row_index = int(data.get("row"))
    except (TypeError, ValueError):
        return jsonify({"error": "bad row index"}), 400

    with csv_lock(path):
        headers, rows = load_csv_full(path)
        if row_index < 0 or row_index >= len(rows):
            return jsonify({"error": "not found"}), 400
        if field not in headers:
            headers.append(field)

        new_value = rows[row_index].get(source_column, "") or ""
        rows[row_index][field] = new_value
        write_csv_full(path, headers, rows)
        return jsonify({"ok": True, "value": new_value})


@app.route("/tools/contacts/select_bulk", methods=["POST"])
def contact_select_apply_bulk():
    """Batched version of /tools/contacts/select -- the radio buttons in
    contact_select.html write their picks into local JS state as the user
    clicks around (no network call per click), then this one call applies
    every pending {row, field, source_column} selection in a single
    read-modify-write-once instead of one round-trip per row. Faster UX,
    and incidentally removes N-scattered-writes as a corruption vector
    since the whole batch lands in one write_csv_full call.

    Body: {"source": "account_staging", "selections": [
        {"row": 0, "field": "Phone", "source_column": "ZoomInfo Phone"},
        {"row": 0, "field": "Email", "source_column": "LeadIQ Email"},
        ...
    ]}
    """
    data = request.get_json(silent=True) or {}
    source = data.get("source", "staging")
    path = get_source_path_or_404(source)
    selections = data.get("selections")
    if not isinstance(selections, list) or not selections:
        return jsonify({"error": "no selections"}), 400

    # Validate everything up front, before touching the file -- a single
    # bad row shouldn't partially apply the rest.
    cleaned = []
    for sel in selections:
        field = sel.get("field")
        source_column = sel.get("source_column")
        valid_columns = (
            PHONE_SOURCE_COLUMNS if field == "Phone"
            else EMAIL_SOURCE_COLUMNS if field == "Email"
            else None
        )
        if not valid_columns or source_column not in valid_columns:
            return jsonify({"error": f"invalid field/source_column: {field}/{source_column}"}), 400
        try:
            row_index = int(sel.get("row"))
        except (TypeError, ValueError):
            return jsonify({"error": "bad row index"}), 400
        cleaned.append((row_index, field, source_column))

    applied = {}
    with csv_lock(path):
        headers, rows = load_csv_full(path)
        for field in ("Phone", "Email"):
            if field not in headers and any(f == field for _, f, _ in cleaned):
                headers.append(field)

        for row_index, field, source_column in cleaned:
            if row_index < 0 or row_index >= len(rows):
                return jsonify({"error": f"row {row_index} not found"}), 400
            new_value = rows[row_index].get(source_column, "") or ""
            rows[row_index][field] = new_value
            applied[f"{row_index}:{field}"] = new_value

        write_csv_full(path, headers, rows)

    return jsonify({"ok": True, "count": len(cleaned), "applied": applied})


@app.route("/tools/contacts/update_cell", methods=["POST"])
def contact_select_update_cell():
    """Manual edit of the final Phone/Email value (typing directly into
    the field instead of picking a source radio)."""
    data = request.get_json(silent=True) or {}
    source = data.get("source", "staging")
    path = get_source_path_or_404(source)
    field = data.get("field")
    if field not in ("Phone", "Email"):
        return jsonify({"error": "invalid field"}), 400

    try:
        row_index = int(data.get("row"))
    except (TypeError, ValueError):
        return jsonify({"error": "bad row index"}), 400
    value = data.get("value", "")

    with csv_lock(path):
        headers, rows = load_csv_full(path)
        if row_index < 0 or row_index >= len(rows):
            return jsonify({"error": "not found"}), 400
        if field not in headers:
            headers.append(field)

        rows[row_index][field] = value
        write_csv_full(path, headers, rows)
        return jsonify({"ok": True})


@app.route("/tools/contacts/export")
def contact_select_export():
    source = request.args.get("source", "staging")
    path = get_source_path_or_404(source)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    return send_file(
        path,
        as_attachment=True,
        download_name=f"contacts_{source}_{timestamp}.csv",
        mimetype="text/csv",
    )


@app.route("/tools/commit_to_tracker", methods=["POST"])
def tools_commit_to_tracker():
    """Appends this source's rows into the live tracker -- same append
    logic as /scraper/preview/commit, generalized so it also works for
    an ad-hoc uploaded CSV finished off in these tools. No-op if the
    source IS the tracker already."""
    source = request.form.get("source", "staging")
    path = get_source_path_or_404(source)

    if source == "tracker":
        return redirect(url_for("dashboard_page"))

    with csv_lock(path):
        src_headers, src_rows = load_csv_full(path)
    tracker_headers = get_headers()
    if not src_rows or not tracker_headers:
        return redirect(request.referrer or url_for("tools_landing"))

    new_rows = [{h: (r.get(h) or "") for h in tracker_headers} for r in src_rows]
    with csv_lock(TRACKER_PATH):
        with open(TRACKER_PATH, "a", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=tracker_headers)
            writer.writerows(new_rows)

    return redirect(url_for("dashboard_page"))


@app.route("/salesloft/stage_from_tracker", methods=["POST"])
def salesloft_stage_from_tracker():
    raw_indices = request.form.getlist("rows")
    try:
        indices = sorted({int(i) for i in raw_indices})
    except ValueError:
        return redirect(url_for("dashboard_page"))

    headers, rows = load_tracker()
    selected_rows = [rows[i] for i in indices if 0 <= i < len(rows)]
    if not selected_rows:
        return redirect(url_for("dashboard_page"))

    headers, selected_rows = ensure_send_at_column(headers, selected_rows)
    write_csv_full(SALESLOFT_STAGING_PATH, headers, selected_rows)
    with SALESLOFT_STATE_LOCK:
        SALESLOFT_STATE["pending_tracker_commit"] = False

    return redirect(url_for("salesloft_define_stage"))


@app.route("/salesloft/import")
def salesloft_import_page():
    return render_template("salesloft_import.html")


@app.route("/salesloft/import/upload", methods=["POST"])
def salesloft_import_upload():
    file = request.files.get("csv_file")
    if not file or file.filename == "":
        return redirect(url_for("salesloft_import_page"))

    token = uuid.uuid4().hex
    temp_path = os.path.join(SALESLOFT_PIPELINE_WORKSPACE_DIR, "upload_" + token + ".csv")
    file.save(temp_path)
    incoming = read_csv_dicts(temp_path)
    os.remove(temp_path)

    tracker_headers = get_headers()
    new_rows = [{h: (r.get(h) or "") for h in tracker_headers} for r in incoming]
    tracker_headers, new_rows = ensure_send_at_column(tracker_headers, new_rows)
    write_csv_full(SALESLOFT_STAGING_PATH, tracker_headers, new_rows)

    with SALESLOFT_STATE_LOCK:
        SALESLOFT_STATE["pending_tracker_commit"] = True

    return redirect(url_for("salesloft_define_stage"))


# ── Cadence settings -- add/edit/delete the cadences the pipeline can
# target, so a cadence ID never has to be hardcoded into a script again.
@app.route("/cadence-settings")
def cadence_settings():
    return render_template("cadence_settings.html", cadences=cadence_store.list_cadences())


@app.route("/cadence-settings/add", methods=["POST"])
def cadence_settings_add():
    try:
        cadence_store.add_cadence(
            request.form.get("label", ""),
            request.form.get("cadence_id", ""),
            request.form.get("name", ""),
        )
    except (ValueError, TypeError):
        pass  # bad/duplicate ID -- silently ignore rather than 500ing the page
    return redirect(url_for("cadence_settings"))


@app.route("/cadence-settings/<int:cadence_id>/update", methods=["POST"])
def cadence_settings_update(cadence_id):
    data = request.get_json(silent=True) or {}
    field = data.get("field")
    value = data.get("value", "")
    if field not in ("label", "name"):
        return jsonify({"error": "bad field"}), 400
    try:
        cadence_store.update_cadence(cadence_id, **{field: value})
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    return jsonify({"ok": True})


@app.route("/cadence-settings/<int:cadence_id>/delete", methods=["POST"])
def cadence_settings_delete(cadence_id):
    try:
        cadence_store.delete_cadence(cadence_id)
    except ValueError:
        pass
    return redirect(url_for("cadence_settings"))


@app.route("/salesloft/preview/delete_row", methods=["POST"])
def salesloft_preview_delete_row():
    """Removes a row from the Salesloft staging table. Referenced by
    salesloft_define_stage.html's per-row delete button -- was missing
    entirely (BuildError on any page load once a row exists), same
    pattern as scraper_preview_delete_row / account_scraper_preview_delete_row."""
    try:
        row_index = int(request.form.get("row"))
    except (TypeError, ValueError):
        return redirect(url_for("salesloft_define_stage"))

    headers, rows = load_csv_full(SALESLOFT_STAGING_PATH)
    if 0 <= row_index < len(rows):
        rows.pop(row_index)
        write_csv_full(SALESLOFT_STAGING_PATH, headers, rows)

    return redirect(url_for("salesloft_define_stage"))


@app.route("/salesloft/preview/update_cell", methods=["POST"])
def salesloft_preview_update_cell():
    """Persists an inline edit (staging-table cell, or the Subject/Messaging
    review fields further down the page) back to the Salesloft staging
    file. Same missing-route bug as delete_row above."""
    data = request.get_json(silent=True) or {}
    try:
        row_index = int(data.get("row"))
    except (TypeError, ValueError):
        return jsonify({"error": "bad row index"}), 400
    column = data.get("column")
    value = data.get("value", "")

    headers, rows = load_csv_full(SALESLOFT_STAGING_PATH)
    if column not in headers or row_index < 0 or row_index >= len(rows):
        return jsonify({"error": "not found"}), 400

    rows[row_index][column] = value
    write_csv_full(SALESLOFT_STAGING_PATH, headers, rows)
    return jsonify({"ok": True})


# ── Define stage -- consolidated staging screen: CSV rows + cadence
# picker + send-now/schedule toggle + Subject/Messaging review, all in
# one place. The single page both staging entry points (stage_from_tracker,
# CSV upload) redirect to, reading/writing SALESLOFT_STAGING_PATH.
@app.route("/salesloft/define_stage")
def salesloft_define_stage():
    headers, rows = load_csv_full(SALESLOFT_STAGING_PATH)
    with SALESLOFT_STATE_LOCK:
        pending_commit = SALESLOFT_STATE.get("pending_tracker_commit", False)
    missing = [c for c in (SALESLOFT_PIPELINE_COL_EMAIL, SALESLOFT_PIPELINE_COL_SUBJECT, SALESLOFT_PIPELINE_COL_BODY)
               if headers and c not in headers]
    return render_template(
        "salesloft_define_stage.html",
        headers=headers,
        rows=rows,
        row_count=len(rows),
        pending_commit=pending_commit,
        missing_columns=missing,
        col_email=SALESLOFT_PIPELINE_COL_EMAIL,
        col_subject=SALESLOFT_PIPELINE_COL_SUBJECT,
        col_body=SALESLOFT_PIPELINE_COL_BODY,
        col_name=SALESLOFT_PIPELINE_COL_NAME,
        cadences=cadence_store.list_cadences(),
    )


@app.route("/salesloft/pipeline/run", methods=["POST"])
def salesloft_pipeline_run():
    data = request.get_json(silent=True) or {}
    cadence_id = data.get("cadence_id")
    mode = data.get("mode")
    send_at = data.get("send_at")
    exclude_existing = bool(data.get("exclude_existing", False))
    row_indices = data.get("row_indices")  # optional: run only these staged rows, leave the rest staged untouched

    if not cadence_id or mode not in ("now", "schedule"):
        return jsonify({"error": "Missing cadence_id or invalid mode."}), 400

    headers, staging_rows = load_csv_full(SALESLOFT_STAGING_PATH)
    if not staging_rows:
        return jsonify({"error": "Nothing staged to run."}), 400

    if row_indices:
        try:
            wanted = {int(i) for i in row_indices}
        except (TypeError, ValueError):
            return jsonify({"error": "Invalid row selection."}), 400
        run_rows = [r for i, r in enumerate(staging_rows) if i in wanted]
        if not run_rows:
            return jsonify({"error": "None of the selected rows were found -- try reselecting and running again."}), 400
    else:
        run_rows = staging_rows

    if mode == "schedule" and not send_at:
        # No batch-wide default -- every single row needs its own Send At
        # value, or the run can't know when to schedule it.
        missing = [
            r.get(SALESLOFT_PIPELINE_COL_NAME) or r.get(SALESLOFT_PIPELINE_COL_EMAIL) or "(unnamed row)"
            for r in run_rows
            if not (r.get(SALESLOFT_PIPELINE_COL_SEND_AT) or "").strip()
        ]
        if missing:
            return jsonify({
                "error": "No batch send time set, and these contacts have no Send At value of their own: "
                         + ", ".join(missing[:8]) + ("…" if len(missing) > 8 else "")
            }), 400

    with SALESLOFT_STATE_LOCK:
        pending_commit = SALESLOFT_STATE.get("pending_tracker_commit", False)

    if pending_commit:
        # Committing to the tracker is a one-time "these contacts are now
        # tracked" step, independent of which subset actually runs through
        # Salesloft in this particular job -- so this always commits every
        # row currently staged, not just run_rows, and only ever fires
        # once (pending_commit is cleared right after).
        tracker_headers = get_headers()
        new_rows = [{h: (r.get(h) or "") for h in tracker_headers} for r in staging_rows]
        with open(TRACKER_PATH, "a", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=tracker_headers)
            writer.writerows(new_rows)
        with SALESLOFT_STATE_LOCK:
            SALESLOFT_STATE["pending_tracker_commit"] = False

    # Only the selected subset (or everyone, if no selection was made) is
    # what actually gets uploaded to Salesloft -- SALESLOFT_STAGING_PATH
    # itself is left exactly as-is, so any rows not part of this run (with
    # their own cleaned-up messaging) stay staged for a later run, e.g.
    # with a different cadence.
    write_csv_full(SALESLOFT_PIPELINE_INPUT_PATH, headers, run_rows)

    job_id = uuid.uuid4().hex
    with SALESLOFT_PIPELINE_JOBS_LOCK:
        SALESLOFT_PIPELINE_JOBS[job_id] = {"status": "queued", "log": [], "returncode": None, "process": None}

    thread = threading.Thread(
        target=run_salesloft_pipeline_job,
        args=(job_id, cadence_id, mode, send_at, exclude_existing),
        daemon=True,
    )
    thread.start()

    return jsonify({"ok": True, "job_id": job_id, "row_count": len(run_rows)})


@app.route("/salesloft/pipeline/continue/<job_id>", methods=["POST"])
def salesloft_pipeline_continue(job_id):
    """Generic stdin relay -- the pipeline only ever prompts for the SSO
    login ENTER, plus (in --mode now) a per-contact review pause. Same
    pattern as salesloft_continue / salesloft_schedule_continue above."""
    data = request.get_json(silent=True) or {}
    text = data.get("text", "")
    with SALESLOFT_PIPELINE_JOBS_LOCK:
        job = SALESLOFT_PIPELINE_JOBS.get(job_id)
        process = job.get("process") if job else None
    if not process:
        return jsonify({"error": "Job not found or not running."}), 404
    try:
        process.stdin.write(text + "\n")
        process.stdin.flush()
    except Exception as e:
        return jsonify({"error": str(e)}), 500
    return jsonify({"ok": True})


@app.route("/salesloft/pipeline/status/<job_id>")
def salesloft_pipeline_status(job_id):
    with SALESLOFT_PIPELINE_JOBS_LOCK:
        job = SALESLOFT_PIPELINE_JOBS.get(job_id)
        if not job:
            return jsonify({"error": "Unknown job"}), 404
        return jsonify({"status": job["status"], "log": job["log"], "returncode": job["returncode"]})


@app.route("/account-scraper")
def account_scraper_page():
    has_staging = os.path.exists(ACCOUNT_STAGING_CSV_PATH)
    return render_template("account_scraper.html", has_staging=has_staging)


@app.route("/account-scraper/run", methods=["POST"])
def account_scraper_run():
    if not os.path.exists(ACCOUNT_SCRAPER_SCRIPT_PATH):
        return jsonify({"error": "sales_nav_scraper.js not found in scraper_workspace/."}), 400

    job_id = uuid.uuid4().hex
    with ACCOUNT_SCRAPER_JOBS_LOCK:
        ACCOUNT_SCRAPER_JOBS[job_id] = {"status": "queued", "log": [], "returncode": None, "process": None, "output_file": None}
        LATEST_ACCOUNT_JOB["id"] = job_id

    data = request.get_json(silent=True) or {}
    unlock_mobile = bool(data.get("unlock_mobile"))

    thread = threading.Thread(target=run_account_scraper_job, args=(job_id, unlock_mobile), daemon=True)
    thread.start()

    return jsonify({"ok": True, "job_id": job_id})


@app.route("/account-scraper/latest")
def account_scraper_latest():
    with ACCOUNT_SCRAPER_JOBS_LOCK:
        job_id = LATEST_ACCOUNT_JOB["id"]
        if not job_id or job_id not in ACCOUNT_SCRAPER_JOBS:
            return jsonify({"job_id": None})
        job = ACCOUNT_SCRAPER_JOBS[job_id]
        return jsonify({"job_id": job_id, "status": job["status"], "log": job["log"]})


@app.route("/account-scraper/input/<job_id>", methods=["POST"])
def account_scraper_input(job_id):
    data = request.get_json(silent=True) or {}
    text = data.get("text", "")
    with ACCOUNT_SCRAPER_JOBS_LOCK:
        job = ACCOUNT_SCRAPER_JOBS.get(job_id)
        process = job.get("process") if job else None
    if not process:
        return jsonify({"error": "Job not found or not running."}), 404
    try:
        process.stdin.write(text + "\n")
        process.stdin.flush()
    except Exception as e:
        return jsonify({"error": str(e)}), 500
    return jsonify({"ok": True})


def _terminate_then_kill(process, job_id, jobs_dict, jobs_lock, grace_seconds=3):
    # process was launched with start_new_session=True, so process.pid is
    # also that session's process group id (pgid) -- signaling the group
    # (not just the Node pid) reaches the Chrome browser Node spawned too.
    # Without this, SIGTERM only ever killed Node; the Chrome subprocess
    # underneath was left running and orphaned, still holding the lock on
    # the persistent profile dir, which is exactly what broke the next run.
    try:
        pgid = os.getpgid(process.pid)
    except ProcessLookupError:
        return  # already exited on its own -- nothing to signal

    try:
        os.killpg(pgid, signal.SIGTERM)
        try:
            process.wait(timeout=grace_seconds)
        except subprocess.TimeoutExpired:
            with jobs_lock:
                jobs_dict[job_id]["log"].append(
                    "-- Process didn't exit after the stop signal, forcing a hard kill --"
                )
            try:
                os.killpg(pgid, signal.SIGKILL)
            except ProcessLookupError:
                pass  # group already gone
    except ProcessLookupError:
        pass


@app.route("/account-scraper/stop/<job_id>", methods=["POST"])
def account_scraper_stop(job_id):
    with ACCOUNT_SCRAPER_JOBS_LOCK:
        job = ACCOUNT_SCRAPER_JOBS.get(job_id)
        process = job.get("process") if job else None
        if job:
            job["stop_requested"] = True
    if not process:
        return jsonify({"error": "Job not found or not running."}), 404

    thread = threading.Thread(target=_terminate_then_kill, args=(process, job_id, ACCOUNT_SCRAPER_JOBS, ACCOUNT_SCRAPER_JOBS_LOCK), daemon=True)
    thread.start()

    return jsonify({"ok": True})


@app.route("/account-scraper/status/<job_id>")
def account_scraper_status(job_id):
    with ACCOUNT_SCRAPER_JOBS_LOCK:
        job = ACCOUNT_SCRAPER_JOBS.get(job_id)
        if not job:
            return jsonify({"error": "Unknown job"}), 404
        return jsonify({"status": job["status"], "log": job["log"], "returncode": job["returncode"]})


@app.route("/account-scraper/preview")
def account_scraper_preview():
    headers, rows = load_csv_full(ACCOUNT_STAGING_CSV_PATH)
    return render_template(
        "account_scraper_preview.html",
        headers=headers,
        rows=rows,
        row_count=len(rows),
        tracker_headers=get_headers(),
        phone_source_cols=PHONE_SOURCE_COLUMNS,
        email_source_cols=EMAIL_SOURCE_COLUMNS,
        sync_info=read_staging_sync_meta(ACCOUNT_STAGING_CSV_PATH),
    )


@app.route("/account-scraper/preview/add", methods=["POST"])
def account_scraper_preview_add():
    with csv_lock(ACCOUNT_STAGING_CSV_PATH):
        headers, rows = load_csv_full(ACCOUNT_STAGING_CSV_PATH)
        if not headers:
            headers = get_headers()  # staging file doesn't exist yet -- seed with the tracker's own schema

        new_row = {h: request.form.get(h, "").strip() for h in headers}
        rows.append(new_row)
        write_csv_full(ACCOUNT_STAGING_CSV_PATH, headers, rows)

    return redirect(url_for("account_scraper_preview"))


@app.route("/account-scraper/preview/update_cell", methods=["POST"])
def account_scraper_preview_update_cell():
    data = request.get_json(silent=True) or {}
    try:
        row_index = int(data.get("row"))
    except (TypeError, ValueError):
        return jsonify({"error": "bad row index"}), 400
    column = data.get("column")
    value = data.get("value", "")

    with csv_lock(ACCOUNT_STAGING_CSV_PATH):
        headers, rows = load_csv_full(ACCOUNT_STAGING_CSV_PATH)
        if column not in headers or row_index < 0 or row_index >= len(rows):
            return jsonify({"error": "not found"}), 400

        rows[row_index][column] = value
        write_csv_full(ACCOUNT_STAGING_CSV_PATH, headers, rows)
        return jsonify({"ok": True})


@app.route("/account-scraper/preview/delete_row", methods=["POST"])
def account_scraper_preview_delete_row():
    try:
        row_index = int(request.form.get("row"))
    except (TypeError, ValueError):
        return redirect(url_for("account_scraper_preview"))

    with csv_lock(ACCOUNT_STAGING_CSV_PATH):
        headers, rows = load_csv_full(ACCOUNT_STAGING_CSV_PATH)
        if 0 <= row_index < len(rows):
            rows.pop(row_index)
            write_csv_full(ACCOUNT_STAGING_CSV_PATH, headers, rows)

    return redirect(url_for("account_scraper_preview"))


@app.route("/account-scraper/preview/export")
def account_scraper_preview_export():
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    return send_file(
        ACCOUNT_STAGING_CSV_PATH,
        as_attachment=True,
        download_name=f"account_scraper_output_{timestamp}.csv",
        mimetype="text/csv",
    )


@app.route("/account-scraper/preview/commit", methods=["POST"])
def account_scraper_preview_commit():
    with csv_lock(ACCOUNT_STAGING_CSV_PATH):
        staging_headers, staging_rows = load_csv_full(ACCOUNT_STAGING_CSV_PATH)
    tracker_headers = get_headers()
    if not staging_rows or not tracker_headers:
        return redirect(url_for("account_scraper_preview"))

    new_rows = [{h: (r.get(h) or "") for h in tracker_headers} for r in staging_rows]

    # Know exactly where these rows will land before appending (a plain
    # append, so it's always the current tail of the file) -- that lets us
    # tag each one with its real tracker row index below, rather than
    # relying on a fingerprint lookup that could go wrong on duplicate
    # names.
    with csv_lock(TRACKER_PATH):
        _, existing_tracker_rows = load_tracker()
        first_new_index = len(existing_tracker_rows)

        with open(TRACKER_PATH, "a", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=tracker_headers)
            writer.writerows(new_rows)

    # Connect straight into Glean Message Enhance -- committing here is the
    # signal that these contacts are ready to move to the next step, so
    # stage them as a fresh Glean input batch (same shape glean_upload/
    # glean_stage_from_tracker use) and land on that review table instead
    # of the dashboard. Same as those two flows, this overwrites whatever
    # was staged for Glean before.
    glean_staged = []
    for i, row in enumerate(new_rows):
        glean_row = dict(row)
        glean_row[GLEAN_EXTRA_CONTEXT_COL] = ""
        glean_row[GLEAN_INDEX_COL] = str(first_new_index + i)
        glean_row[GLEAN_FINGERPRINT_COL] = make_fingerprint(glean_row)
        glean_staged.append(glean_row)

    glean_staging_headers = tracker_headers + [GLEAN_EXTRA_CONTEXT_COL] + GLEAN_INTERNAL_COLS
    write_csv_full(GLEAN_INPUT_STAGING_PATH, glean_staging_headers, glean_staged)

    return redirect(url_for("glean_preview", stage="input"))


@app.route("/intent-signal")
def intent_signal_page():
    contact_has_staging = os.path.exists(INTENT_CONTACT_STAGING_PATH)
    linkedin_has_staging = os.path.exists(INTENT_LINKEDIN_STAGING_PATH)
    return render_template(
        "intent_signal_scrap.html",
        contact_has_staging=contact_has_staging,
        linkedin_has_staging=linkedin_has_staging,
    )


# ── Option 1: ZoomInfo Contact Only ─────────────────────────────────────────

@app.route("/intent-signal/contact/run", methods=["POST"])
def intent_contact_run():
    if not os.path.exists(INTENT_CONTACT_SCRIPT_PATH):
        return jsonify({"error": "zoominfo-contact.js not found in intent_signal_scrap_workspace/."}), 400

    job_id = uuid.uuid4().hex
    with INTENT_CONTACT_JOBS_LOCK:
        INTENT_CONTACT_JOBS[job_id] = {"status": "queued", "log": [], "returncode": None, "process": None, "output_file": None}
        LATEST_INTENT_CONTACT_JOB["id"] = job_id

    thread = threading.Thread(target=run_intent_contact_job, args=(job_id,), daemon=True)
    thread.start()

    return jsonify({"ok": True, "job_id": job_id})


@app.route("/intent-signal/contact/latest")
def intent_contact_latest():
    with INTENT_CONTACT_JOBS_LOCK:
        job_id = LATEST_INTENT_CONTACT_JOB["id"]
        if not job_id or job_id not in INTENT_CONTACT_JOBS:
            return jsonify({"job_id": None})
        job = INTENT_CONTACT_JOBS[job_id]
        return jsonify({"job_id": job_id, "status": job["status"], "log": job["log"]})


@app.route("/intent-signal/contact/input/<job_id>", methods=["POST"])
def intent_contact_input(job_id):
    data = request.get_json(silent=True) or {}
    text = data.get("text", "")
    with INTENT_CONTACT_JOBS_LOCK:
        job = INTENT_CONTACT_JOBS.get(job_id)
        process = job.get("process") if job else None
    if not process:
        return jsonify({"error": "Job not found or not running."}), 404
    try:
        process.stdin.write(text + "\n")
        process.stdin.flush()
    except Exception as e:
        return jsonify({"error": str(e)}), 500
    return jsonify({"ok": True})


@app.route("/intent-signal/contact/stop/<job_id>", methods=["POST"])
def intent_contact_stop(job_id):
    with INTENT_CONTACT_JOBS_LOCK:
        job = INTENT_CONTACT_JOBS.get(job_id)
        process = job.get("process") if job else None
        if job:
            job["stop_requested"] = True
    if not process:
        return jsonify({"error": "Job not found or not running."}), 404

    thread = threading.Thread(target=_terminate_then_kill, args=(process, job_id, INTENT_CONTACT_JOBS, INTENT_CONTACT_JOBS_LOCK), daemon=True)
    thread.start()

    return jsonify({"ok": True})


@app.route("/intent-signal/contact/status/<job_id>")
def intent_contact_status(job_id):
    with INTENT_CONTACT_JOBS_LOCK:
        job = INTENT_CONTACT_JOBS.get(job_id)
        if not job:
            return jsonify({"error": "Unknown job"}), 404
        return jsonify({"status": job["status"], "log": job["log"], "returncode": job["returncode"]})


@app.route("/intent-signal/contact/preview")
def intent_contact_preview():
    headers, rows = load_csv_full(INTENT_CONTACT_STAGING_PATH)
    return render_template(
        "intent_signal_preview.html",
        option_label="ZoomInfo Contact Only",
        staging_kind="contact",
        headers=headers,
        rows=rows,
        row_count=len(rows),
    )


@app.route("/intent-signal/contact/preview/delete_row", methods=["POST"])
def intent_contact_preview_delete_row():
    try:
        row_index = int(request.form.get("row"))
    except (TypeError, ValueError):
        return redirect(url_for("intent_contact_preview"))

    headers, rows = load_csv_full(INTENT_CONTACT_STAGING_PATH)
    if 0 <= row_index < len(rows):
        rows.pop(row_index)
        write_csv_full(INTENT_CONTACT_STAGING_PATH, headers, rows)

    return redirect(url_for("intent_contact_preview"))


@app.route("/intent-signal/contact/preview/export")
def intent_contact_preview_export():
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    return send_file(
        INTENT_CONTACT_STAGING_PATH,
        as_attachment=True,
        download_name=f"zoominfo_contact_only_{timestamp}.csv",
        mimetype="text/csv",
    )


@app.route("/intent-signal/contact/preview/commit", methods=["POST"])
def intent_contact_preview_commit():
    staging_headers, staging_rows = load_csv_full(INTENT_CONTACT_STAGING_PATH)
    tracker_headers = get_headers()
    if not staging_rows or not tracker_headers:
        return redirect(url_for("intent_contact_preview"))

    new_rows = [{h: (r.get(h) or "") for h in tracker_headers} for r in staging_rows]
    with open(TRACKER_PATH, "a", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=tracker_headers)
        writer.writerows(new_rows)

    return redirect(url_for("dashboard_page"))


# ── Option 2: ZoomInfo LinkedIn Scrap ───────────────────────────────────────

@app.route("/intent-signal/linkedin/run", methods=["POST"])
def intent_linkedin_run():
    if not os.path.exists(INTENT_LINKEDIN_SCRIPT_PATH):
        return jsonify({"error": "zoominfo-linkedin-scrap.js not found in intent_signal_scrap_workspace/."}), 400

    job_id = uuid.uuid4().hex
    with INTENT_LINKEDIN_JOBS_LOCK:
        INTENT_LINKEDIN_JOBS[job_id] = {"status": "queued", "log": [], "returncode": None, "process": None, "output_file": None}
        LATEST_INTENT_LINKEDIN_JOB["id"] = job_id

    thread = threading.Thread(target=run_intent_linkedin_job, args=(job_id,), daemon=True)
    thread.start()

    return jsonify({"ok": True, "job_id": job_id})


@app.route("/intent-signal/linkedin/latest")
def intent_linkedin_latest():
    with INTENT_LINKEDIN_JOBS_LOCK:
        job_id = LATEST_INTENT_LINKEDIN_JOB["id"]
        if not job_id or job_id not in INTENT_LINKEDIN_JOBS:
            return jsonify({"job_id": None})
        job = INTENT_LINKEDIN_JOBS[job_id]
        return jsonify({"job_id": job_id, "status": job["status"], "log": job["log"]})


@app.route("/intent-signal/linkedin/input/<job_id>", methods=["POST"])
def intent_linkedin_input(job_id):
    data = request.get_json(silent=True) or {}
    text = data.get("text", "")
    with INTENT_LINKEDIN_JOBS_LOCK:
        job = INTENT_LINKEDIN_JOBS.get(job_id)
        process = job.get("process") if job else None
    if not process:
        return jsonify({"error": "Job not found or not running."}), 404
    try:
        process.stdin.write(text + "\n")
        process.stdin.flush()
    except Exception as e:
        return jsonify({"error": str(e)}), 500
    return jsonify({"ok": True})


@app.route("/intent-signal/linkedin/stop/<job_id>", methods=["POST"])
def intent_linkedin_stop(job_id):
    with INTENT_LINKEDIN_JOBS_LOCK:
        job = INTENT_LINKEDIN_JOBS.get(job_id)
        process = job.get("process") if job else None
        if job:
            job["stop_requested"] = True
    if not process:
        return jsonify({"error": "Job not found or not running."}), 404

    thread = threading.Thread(target=_terminate_then_kill, args=(process, job_id, INTENT_LINKEDIN_JOBS, INTENT_LINKEDIN_JOBS_LOCK), daemon=True)
    thread.start()

    return jsonify({"ok": True})


@app.route("/intent-signal/linkedin/status/<job_id>")
def intent_linkedin_status(job_id):
    with INTENT_LINKEDIN_JOBS_LOCK:
        job = INTENT_LINKEDIN_JOBS.get(job_id)
        if not job:
            return jsonify({"error": "Unknown job"}), 404
        return jsonify({"status": job["status"], "log": job["log"], "returncode": job["returncode"]})


@app.route("/intent-signal/linkedin/preview")
def intent_linkedin_preview():
    headers, rows = load_csv_full(INTENT_LINKEDIN_STAGING_PATH)
    return render_template(
        "intent_signal_preview.html",
        option_label="ZoomInfo LinkedIn Scrap",
        staging_kind="linkedin",
        headers=headers,
        rows=rows,
        row_count=len(rows),
    )


@app.route("/intent-signal/linkedin/preview/delete_row", methods=["POST"])
def intent_linkedin_preview_delete_row():
    try:
        row_index = int(request.form.get("row"))
    except (TypeError, ValueError):
        return redirect(url_for("intent_linkedin_preview"))

    headers, rows = load_csv_full(INTENT_LINKEDIN_STAGING_PATH)
    if 0 <= row_index < len(rows):
        rows.pop(row_index)
        write_csv_full(INTENT_LINKEDIN_STAGING_PATH, headers, rows)

    return redirect(url_for("intent_linkedin_preview"))


@app.route("/intent-signal/linkedin/preview/export")
def intent_linkedin_preview_export():
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    return send_file(
        INTENT_LINKEDIN_STAGING_PATH,
        as_attachment=True,
        download_name=f"zoominfo_linkedin_scrap_{timestamp}.csv",
        mimetype="text/csv",
    )


@app.route("/intent-signal/linkedin/preview/commit", methods=["POST"])
def intent_linkedin_preview_commit():
    staging_headers, staging_rows = load_csv_full(INTENT_LINKEDIN_STAGING_PATH)
    tracker_headers = get_headers()
    if not staging_rows or not tracker_headers:
        return redirect(url_for("intent_linkedin_preview"))

    new_rows = [{h: (r.get(h) or "") for h in tracker_headers} for r in staging_rows]
    with open(TRACKER_PATH, "a", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=tracker_headers)
        writer.writerows(new_rows)

    return redirect(url_for("dashboard_page"))


@app.route("/export/selected", methods=["POST"])
def export_selected_csv():
    raw_indices = request.form.getlist("rows")
    try:
        indices = sorted({int(i) for i in raw_indices})
    except ValueError:
        return redirect(url_for("dashboard_page"))

    headers, rows = load_tracker()
    selected_rows = [rows[i] for i in indices if 0 <= i < len(rows)]

    buffer = io.StringIO()
    writer = csv.DictWriter(buffer, fieldnames=headers)
    writer.writeheader()
    writer.writerows(selected_rows)

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    response = Response(buffer.getvalue(), mimetype="text/csv")
    response.headers["Content-Disposition"] = "attachment; filename=PG_Tracker_selected_" + timestamp + ".csv"
    return response


@app.route("/dashboard")
def dashboard_page():
    headers, rows = load_tracker()
    # Reorder display columns so Company Name and Contact Name are always
    # the first two visible columns, without touching the CSV.
    _priority = ["Company Name", "Contact Name"]
    display_headers = [c for c in _priority if c in headers] + \
                      [h for h in headers if h not in _priority]
    duplicates = find_duplicate_contact_names(rows)
    rules = load_validation_rules()
    return render_template(
        "dashboard.html",
        headers=display_headers,
        rows=rows,
        row_count=len(rows),
        duplicates=duplicates,
        default_widths=DEFAULT_WIDTHS,
        validation_rules=rules,
        column_options=resolve_validation_options(rules),
        frozen_offsets={},  # pinned/frozen columns removed by decision -- was an
        # unresolved source of cross-browser layout bugs; Company Name and
        # Contact Name are now ordinary scrolling columns like everything else.
        col_body=SALESLOFT_PIPELINE_COL_BODY,
        actions_col_width=ACTIONS_COL_WIDTH,
    )



@app.route("/glean")
def glean_page():
    has_input_staging = os.path.exists(GLEAN_INPUT_STAGING_PATH)
    return render_template("glean.html", has_input_staging=has_input_staging)


@app.route("/glean/stage_from_tracker", methods=["POST"])
def glean_stage_from_tracker():
    raw_indices = request.form.getlist("rows")
    try:
        indices = sorted({int(i) for i in raw_indices})
    except ValueError:
        return redirect(url_for("dashboard_page"))

    headers, rows = load_tracker()
    staged = []
    for i in indices:
        if 0 <= i < len(rows):
            r = dict(rows[i])
            r[GLEAN_EXTRA_CONTEXT_COL] = ""
            r[GLEAN_INDEX_COL] = str(i)
            r[GLEAN_FINGERPRINT_COL] = make_fingerprint(r)
            staged.append(r)

    if not staged:
        return redirect(url_for("dashboard_page"))

    staging_headers = headers + [GLEAN_EXTRA_CONTEXT_COL] + GLEAN_INTERNAL_COLS
    write_csv_full(GLEAN_INPUT_STAGING_PATH, staging_headers, staged)
    return redirect(url_for("glean_preview", stage="input"))


def build_glean_staging_rows(incoming, tracker_headers):
    """Maps arbitrary rows onto the tracker's column schema and
    fingerprint-matches each one against the live tracker, so a later
    commit knows whether to update an existing row or create a new one.
    Shared by /glean/upload and the "send to Glean" bridge out of the
    generic /tools/preview page -- same matching rule either way: only a
    UNIQUE fingerprint match gets tagged; ambiguous (0 or 2+ matches) is
    left blank and falls back to "create new row" on commit."""
    _, tracker_rows = load_tracker()
    fingerprint_to_indices = {}
    for i, tr in enumerate(tracker_rows):
        fingerprint_to_indices.setdefault(make_fingerprint(tr), []).append(i)

    staged = []
    for r in incoming:
        new_row = {h: (r.get(h) or "") for h in tracker_headers}
        # Carry over an "Additional Context" column if it was already present
        # (e.g. re-uploading a previously exported batch); otherwise it just
        # starts blank and is editable in the staging table.
        new_row[GLEAN_EXTRA_CONTEXT_COL] = r.get(GLEAN_EXTRA_CONTEXT_COL) or ""
        fp = make_fingerprint(new_row)
        candidates = fingerprint_to_indices.get(fp, [])
        if fp.strip("|") and len(candidates) == 1:
            new_row[GLEAN_INDEX_COL] = str(candidates[0])
            new_row[GLEAN_FINGERPRINT_COL] = fp
        else:
            new_row[GLEAN_INDEX_COL] = ""
            new_row[GLEAN_FINGERPRINT_COL] = ""
        staged.append(new_row)
    return staged


@app.route("/glean/upload", methods=["POST"])
def glean_upload():
    file = request.files.get("csv_file")
    if not file or file.filename == "":
        return redirect(url_for("glean_page"))

    token = uuid.uuid4().hex
    temp_path = os.path.join(GLEAN_WORKSPACE_DIR, "upload_" + token + ".csv")
    file.save(temp_path)
    incoming = read_csv_dicts(temp_path)
    os.remove(temp_path)

    tracker_headers = get_headers()
    staged = build_glean_staging_rows(incoming, tracker_headers)

    if not staged:
        return redirect(url_for("glean_page"))

    staging_headers = tracker_headers + [GLEAN_EXTRA_CONTEXT_COL] + GLEAN_INTERNAL_COLS
    write_csv_full(GLEAN_INPUT_STAGING_PATH, staging_headers, staged)
    return redirect(url_for("glean_preview", stage="input"))


@app.route("/glean/<stage>/preview")
def glean_preview(stage):
    if stage not in ("input", "output"):
        return redirect(url_for("glean_page"))
    path = GLEAN_INPUT_STAGING_PATH if stage == "input" else GLEAN_OUTPUT_STAGING_PATH
    headers, rows = load_csv_full(path)
    display_headers = [h for h in headers if h not in GLEAN_INTERNAL_COLS]
    return render_template(
        "glean_preview.html",
        stage=stage,
        headers=display_headers,
        rows=rows,
        row_count=len(rows),
        tracker_headers=get_headers(),
        col_body=SALESLOFT_PIPELINE_COL_BODY,
    )


@app.route("/glean/<stage>/update_cell", methods=["POST"])
def glean_update_cell(stage):
    if stage not in ("input", "output"):
        return jsonify({"error": "bad stage"}), 400
    path = GLEAN_INPUT_STAGING_PATH if stage == "input" else GLEAN_OUTPUT_STAGING_PATH

    data = request.get_json(silent=True) or {}
    try:
        row_index = int(data.get("row"))
    except (TypeError, ValueError):
        return jsonify({"error": "bad row index"}), 400
    column = data.get("column")
    value = data.get("value", "")

    headers, rows = load_csv_full(path)
    if column not in headers or column in GLEAN_INTERNAL_COLS or row_index < 0 or row_index >= len(rows):
        return jsonify({"error": "not found"}), 400

    rows[row_index][column] = value
    write_csv_full(path, headers, rows)
    return jsonify({"ok": True})


@app.route("/glean/<stage>/delete_row", methods=["POST"])
def glean_delete_row(stage):
    if stage not in ("input", "output"):
        return redirect(url_for("glean_page"))
    path = GLEAN_INPUT_STAGING_PATH if stage == "input" else GLEAN_OUTPUT_STAGING_PATH

    try:
        row_index = int(request.form.get("row"))
    except (TypeError, ValueError):
        return redirect(url_for("glean_preview", stage=stage))

    headers, rows = load_csv_full(path)
    if 0 <= row_index < len(rows):
        rows.pop(row_index)
        write_csv_full(path, headers, rows)

    return redirect(url_for("glean_preview", stage=stage))


@app.route("/glean/<stage>/add", methods=["POST"])
def glean_add_row(stage):
    if stage not in ("input", "output"):
        return redirect(url_for("glean_page"))
    path = GLEAN_INPUT_STAGING_PATH if stage == "input" else GLEAN_OUTPUT_STAGING_PATH

    headers, rows = load_csv_full(path)
    if not headers:
        headers = get_headers() + [GLEAN_EXTRA_CONTEXT_COL] + GLEAN_INTERNAL_COLS

    new_row = {h: request.form.get(h, "").strip() for h in headers if h not in GLEAN_INTERNAL_COLS}
    for ic in GLEAN_INTERNAL_COLS:
        new_row[ic] = ""  # manually added rows have no tracker identity -- always create on commit
    rows.append(new_row)
    write_csv_full(path, headers, rows)

    return redirect(url_for("glean_preview", stage=stage))


@app.route("/glean/run", methods=["POST"])
def glean_run():
    if not os.path.exists(GLEAN_SCRIPT_PATH):
        return jsonify({"error": "glean-pg-tracker-automation.js not found in glean_workspace/."}), 400
    if not os.path.exists(GLEAN_INPUT_STAGING_PATH):
        return jsonify({"error": "Nothing staged to run -- select rows from the tracker or upload a CSV first."}), 400

    _, staged_rows = load_csv_full(GLEAN_INPUT_STAGING_PATH)
    row_limit = max(len(staged_rows), 1)  # process everything staged, not the script's own cautious default of 5
    run_stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    output_path = os.path.join(GLEAN_WORKSPACE_DIR, f"glean_run_output_{run_stamp}.csv")

    job_id = uuid.uuid4().hex
    with GLEAN_JOBS_LOCK:
        GLEAN_JOBS[job_id] = {
            "status": "queued", "log": [], "returncode": None, "process": None,
            "row_limit": row_limit,
        }
        LATEST_GLEAN_JOB["id"] = job_id

    thread = threading.Thread(
        target=run_glean_job,
        args=(job_id, GLEAN_INPUT_STAGING_PATH, output_path),
        daemon=True,
    )
    thread.start()

    return jsonify({"ok": True, "job_id": job_id})


@app.route("/glean/stop/<job_id>", methods=["POST"])
def glean_stop(job_id):
    with GLEAN_JOBS_LOCK:
        job = GLEAN_JOBS.get(job_id)
        process = job.get("process") if job else None
        if job:
            job["stop_requested"] = True
    if not process:
        return jsonify({"error": "Job not found or not running."}), 404

    thread = threading.Thread(target=_terminate_then_kill, args=(process, job_id, GLEAN_JOBS, GLEAN_JOBS_LOCK), daemon=True)
    thread.start()

    return jsonify({"ok": True})


@app.route("/glean/status/<job_id>")
def glean_status(job_id):
    with GLEAN_JOBS_LOCK:
        job = GLEAN_JOBS.get(job_id)
        if not job:
            return jsonify({"error": "Unknown job"}), 404
        return jsonify({"status": job["status"], "log": job["log"], "returncode": job["returncode"]})


@app.route("/glean/latest")
def glean_latest():
    with GLEAN_JOBS_LOCK:
        job_id = LATEST_GLEAN_JOB["id"]
        if not job_id or job_id not in GLEAN_JOBS:
            return jsonify({"job_id": None})
        job = GLEAN_JOBS[job_id]
        return jsonify({"job_id": job_id, "status": job["status"], "log": job["log"]})


@app.route("/glean/output/commit", methods=["POST"])
def glean_output_commit():
    headers, staged_rows = load_csv_full(GLEAN_OUTPUT_STAGING_PATH)
    if not staged_rows:
        return jsonify({"error": "Nothing staged to commit."}), 400

    tracker_headers = get_headers()
    tracker_h, tracker_rows = load_tracker()

    # Track exactly which tracker row each committed contact ends up at
    # (updated in place, or newly appended) -- so the caller can offer to
    # stage these same contacts straight into Salesloft afterward without
    # having to re-derive who they were.
    committed_indices = []

    for r in staged_rows:
        target_row = {h: (r.get(h) or "") for h in tracker_headers}
        idx_str = (r.get(GLEAN_INDEX_COL) or "").strip()
        fingerprint = (r.get(GLEAN_FINGERPRINT_COL) or "").strip()

        matched_idx = None
        if idx_str:
            try:
                idx = int(idx_str)
            except ValueError:
                idx = None
            if idx is not None and 0 <= idx < len(tracker_rows) and make_fingerprint(tracker_rows[idx]) == fingerprint:
                matched_idx = idx
            elif fingerprint:
                # Index drifted since staging (tracker changed underneath
                # us) -- fall back to searching by identity rather than
                # trusting position blindly.
                candidates = [i for i, tr in enumerate(tracker_rows) if make_fingerprint(tr) == fingerprint]
                if len(candidates) == 1:
                    matched_idx = candidates[0]

        if matched_idx is not None:
            tracker_rows[matched_idx] = target_row
            committed_indices.append(matched_idx)
        else:
            tracker_rows.append(target_row)
            committed_indices.append(len(tracker_rows) - 1)

    write_tracker(tracker_h, tracker_rows)
    return jsonify({"ok": True, "count": len(staged_rows), "row_indices": committed_indices})


@app.route("/news/preview/<filename>/send_to_glean", methods=["POST"])
def news_send_to_glean(filename):
    if not os.path.exists(GLEAN_NEWS_REPORT_SCRIPT_PATH):
        return jsonify({"error": "glean-news-report.js not found in glean_workspace/."}), 400

    safe_name = os.path.basename(filename)
    source_path = os.path.join(NEWS_OUTPUT_DIR, safe_name)
    headers, rows = load_csv_full(source_path)
    if not rows:
        return jsonify({"error": "Source CSV not found or empty."}), 400

    selected_indices = request.form.getlist("rows")
    if selected_indices:
        try:
            idx_set = {int(i) for i in selected_indices}
        except ValueError:
            return jsonify({"error": "Bad row index."}), 400
        selected_rows = [r for i, r in enumerate(rows) if i in idx_set]
    else:
        selected_rows = rows  # nothing checked -- send everything in this file

    if not selected_rows:
        return jsonify({"error": "No rows to send."}), 400

    # Selecting a subset still writes a filtered CSV the same way as
    # before -- the only thing that changed is that the script now
    # uploads this file directly rather than embedding it as prompt text.
    write_csv_full(GLEAN_NEWS_REPORT_INPUT_PATH, headers, selected_rows)

    prompt_text = (request.form.get("prompt") or "").strip() or DEFAULT_NEWS_REPORT_PROMPT

    job_id = uuid.uuid4().hex
    with GLEAN_REPORT_JOBS_LOCK:
        GLEAN_REPORT_JOBS[job_id] = {"status": "queued", "log": [], "returncode": None, "process": None}

    thread = threading.Thread(
        target=run_glean_report_job,
        args=(job_id, GLEAN_NEWS_REPORT_INPUT_PATH, GLEAN_NEWS_REPORT_OUTPUT_BASE, prompt_text),
        daemon=True,
    )
    thread.start()

    return jsonify({"ok": True, "job_id": job_id, "row_count": len(selected_rows)})


@app.route("/news/glean_report/status/<job_id>")
def news_glean_report_status(job_id):
    with GLEAN_REPORT_JOBS_LOCK:
        job = GLEAN_REPORT_JOBS.get(job_id)
        if not job:
            return jsonify({"error": "Unknown job"}), 404
        return jsonify({
            "status": job["status"],
            "log": job["log"],
            "returncode": job["returncode"],
            "has_workbook": os.path.exists(GLEAN_NEWS_REPORT_WORKBOOK_PATH),
        })


@app.route("/news/glean_report/view")
def news_glean_report_view():
    report_text = ""
    if os.path.exists(GLEAN_NEWS_REPORT_OUTPUT_MD_PATH):
        with open(GLEAN_NEWS_REPORT_OUTPUT_MD_PATH, "r", encoding="utf-8") as f:
            report_text = f.read()
    has_workbook = os.path.exists(GLEAN_NEWS_REPORT_WORKBOOK_PATH)
    return render_template("news_glean_report.html", report_text=report_text, has_workbook=has_workbook)


@app.route("/news/glean_report/download")
def news_glean_report_download():
    if not os.path.exists(GLEAN_NEWS_REPORT_OUTPUT_MD_PATH):
        return "Not found", 404
    return send_file(
        GLEAN_NEWS_REPORT_OUTPUT_MD_PATH,
        as_attachment=True,
        download_name="news_signal_report.md",
        mimetype="text/markdown",
    )


@app.route("/news/glean_report/download_workbook")
def news_glean_report_download_workbook():
    if not os.path.exists(GLEAN_NEWS_REPORT_WORKBOOK_PATH):
        return "Not found", 404
    return send_file(
        GLEAN_NEWS_REPORT_WORKBOOK_PATH,
        as_attachment=True,
        download_name="news_signal_report.xlsx",
        mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )


@app.route("/export")
def export_csv():
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    return send_file(
        TRACKER_PATH,
        as_attachment=True,
        download_name=f"PG_Tracker_export_{timestamp}.csv",
        mimetype="text/csv",
    )


if __name__ == "__main__":
    app.run(debug=True, port=5000, threaded=True)
