"""
main.py
News scraping automation: accounts + keywords -> CSV.

Usage:
    python3 main.py
    python3 main.py --accounts-csv /path/to/accounts.csv --limit 5

Reads accounts.csv (the real PG Tracker accounts library -- Company Name,
Alternative Name, Patch, Industry columns) and keywords.csv (industry
signal queries), pulls Google News RSS results for each, resolves the
real publisher URL, extracts a clean article body, scores relevance,
dedupes against prior runs, and writes output/news_<date>.csv.

Output columns "company_name" and "country" are named to match the
existing "google_news" import profile in app.py -- the CSV drops
straight into the existing Import flow with no new mapping needed.

Free, local-first, no API keys, no cloud costs. Run it manually, trigger
it from the Flask app's job-runner pattern, or stick it on a cron job
(see README.md).
"""

import argparse
import csv
import os
import signal
import time
from datetime import datetime, timedelta

import feedparser

from decoder import resolve_url
from extractor import fetch_article_body
from scorer import score_relevance
from state import load_seen, save_seen

# ── GRACEFUL STOP ─────────────────────────────────────────
# app.py's Stop button sends SIGTERM (escalating to SIGKILL only if this
# doesn't exit in time). Rather than dying mid-request and losing
# everything collected so far, catch the signal, finish the article
# currently in flight, then break out of the account/keyword loops early
# and fall through to the normal "sort + write CSV" path -- so a stopped
# run still produces a real (partial) output file, same as letting it
# finish on its own.
_stop_requested = False


def _handle_stop_signal(signum, frame):
    global _stop_requested
    _stop_requested = True
    print(f"\n-- Stop requested (signal {signum}) -- finishing the current article, then saving progress... --")


signal.signal(signal.SIGTERM, _handle_stop_signal)
signal.signal(signal.SIGINT, _handle_stop_signal)

# ── CONFIG ────────────────────────────────────────────────
CONFIG = {
    "max_articles_per_account": 5,
    "max_articles_per_keyword": 3,
    "sleep_between_requests": 1.0,   # seconds, between RSS queries
    "language": "en",               # fallback if an account's Patch isn't in PATCH_LOCALE below
    "region": "MY",                 # fallback region
    "max_age_days": 7,
}

# Maps your accounts.csv "Patch" column to the right Google News locale.
# Add more entries here if your patch list grows beyond these three.
PATCH_LOCALE = {
    "Malaysia": {"region": "MY", "language": "en"},
    "Indonesia": {"region": "ID", "language": "en"},
    "Japan": {"region": "JP", "language": "en"},
    # Japan note: English coverage of Japanese enterprise news is thin.
    # If you want broader coverage for Japan accounts specifically, change
    # "language" above to "ja" -- but know that the relevance scorer
    # matches the account name as a plain string, so this only works well
    # if the account's Company Name/Alternative Name in accounts.csv is
    # how that company is actually referred to in Japanese-language
    # articles (often still romanized for foreign-style names, not
    # guaranteed for Japanese company names).
}


def get_locale(patch: str) -> dict:
    return PATCH_LOCALE.get((patch or "").strip(), {"region": CONFIG["region"], "language": CONFIG["language"]})


BASE_DIR = os.path.dirname(__file__)
OUTPUT_DIR = os.path.join(BASE_DIR, "output")


# ── RSS FETCH ─────────────────────────────────────────────
def fetch_google_news(query: str, max_results: int, max_age_days: int,
                       language: str, region: str) -> list:
    """Returns a list of dicts: title, link, source, pub_date"""
    encoded = query.replace(" ", "+")
    url = (
        f"https://news.google.com/rss/search?q={encoded}"
        f"&hl={language}&gl={region}&ceid={region}:{language}"
    )

    feed = feedparser.parse(url)
    cutoff = datetime.utcnow() - timedelta(days=max_age_days)

    results = []
    for entry in feed.entries:
        if len(results) >= max_results:
            break

        pub_date = None
        if getattr(entry, "published_parsed", None):
            pub_date = datetime(*entry.published_parsed[:6])

        if pub_date and pub_date < cutoff:
            continue

        title = entry.get("title", "").strip()
        link = entry.get("link", "").strip()
        if not title or not link:
            continue

        source = ""
        if hasattr(entry, "source") and hasattr(entry.source, "title"):
            source = entry.source.title

        results.append({
            "title": title,
            "link": link,
            "source": source,
            "pub_date": pub_date.isoformat() if pub_date else "",
        })

    return results


# ── PROCESS A SINGLE ARTICLE ──────────────────────────────
def process_article(article: dict, entity_name: str, industry: str,
                     country: str, signal_type: str, query_used: str,
                     seen: set):
    resolved = resolve_url(article["link"])
    canonical_url = resolved["url"]

    if canonical_url in seen:
        return None  # true duplicate, skip

    body_result = fetch_article_body(canonical_url)
    body = body_result["body"]

    relevance = score_relevance(entity_name, article["title"], body)

    seen.add(canonical_url)

    return {
        "scraped_at": datetime.utcnow().isoformat(),
        "company_name": entity_name,
        "industry": industry,
        "country": country,
        "signal_type": signal_type,
        "headline": article["title"],
        "source": article["source"],
        "published_date": article["pub_date"],
        "url": canonical_url,
        "url_resolved": resolved["ok"],
        "query_used": query_used,
        "relevance_score": relevance["score"],
        "relevance_level": relevance["level"],
        "relevance_reasons": "; ".join(relevance["reasons"]),
        "body": body if body_result["ok"] else f"[{body_result['reason']}]",
    }


# ── CSV READERS ────────────────────────────────────────────
def read_accounts(path: str) -> list:
    """Reads the real accounts.csv schema: Company Name, Alternative Name,
    Patch, Industry. No 'active' flag exists on this file -- it's the
    shared accounts library used elsewhere in the app, so every row with
    a non-blank Company Name is included."""
    if not os.path.exists(path):
        return []
    with open(path, newline="", encoding="utf-8-sig") as f:
        rows = list(csv.DictReader(f))

    result = []
    for r in rows:
        name = (r.get("Company Name") or "").strip()
        if not name:
            continue
        alias = (r.get("Alternative Name") or "").strip() or name
        result.append({
            "name": name,
            "alias": alias,
            "industry": (r.get("Industry") or "").strip(),
            "country": (r.get("Patch") or "").strip(),
        })
    return result


def read_active_rows(path: str) -> list:
    """For keywords.csv only -- a separate file scoped to this tool, with
    its own active YES/NO column."""
    if not os.path.exists(path):
        return []
    with open(path, newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    return [r for r in rows if r.get("active", "").strip().upper() == "YES"]


# ── MAIN ───────────────────────────────────────────────────
def run(accounts_csv_path: str, keywords_csv_path: str, limit=None):
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    seen = load_seen()
    all_rows = []
    stopped_early = False

    accounts = read_accounts(accounts_csv_path)
    if limit:
        accounts = accounts[:limit]
        print(f"--limit {limit} applied -- test run, not the full account list")

    keywords = read_active_rows(keywords_csv_path)

    print(f"Accounts: {len(accounts)} | Keywords: {len(keywords)} active")

    # ── Accounts ──
    for row in accounts:
        if _stop_requested:
            stopped_early = True
            break

        name = row["name"]
        alias = row["alias"]
        industry = row["industry"]
        country = row["country"]
        locale = get_locale(country)

        print(f"  [account] {name} ({country} -> hl={locale['language']}, gl={locale['region']}) ...")
        articles = fetch_google_news(
            alias, CONFIG["max_articles_per_account"], CONFIG["max_age_days"],
            locale["language"], locale["region"],
        )

        for article in articles:
            if _stop_requested:
                stopped_early = True
                break
            result = process_article(
                article, name, industry, country,
                signal_type="", query_used=alias, seen=seen,
            )
            if result:
                all_rows.append(result)

        if stopped_early:
            break

        time.sleep(CONFIG["sleep_between_requests"])

    # ── Keywords ──
    if not stopped_early:
        for row in keywords:
            if _stop_requested:
                stopped_early = True
                break

            query = row["query"].strip()
            industry = row.get("industry", "").strip()
            signal_type = row.get("signal_type", "").strip()
            patch = row.get("patch", "").strip()  # optional -- blank uses CONFIG default
            locale = get_locale(patch) if patch else {"region": CONFIG["region"], "language": CONFIG["language"]}

            print(f"  [keyword] {query} (hl={locale['language']}, gl={locale['region']}) ...")
            articles = fetch_google_news(
                query, CONFIG["max_articles_per_keyword"], CONFIG["max_age_days"],
                locale["language"], locale["region"],
            )

            for article in articles:
                if _stop_requested:
                    stopped_early = True
                    break
                result = process_article(
                    article, f"[INDUSTRY] {industry}", industry, patch or "MY/ID",
                    signal_type=signal_type, query_used=query, seen=seen,
                )
                if result:
                    all_rows.append(result)

            if stopped_early:
                break

            time.sleep(CONFIG["sleep_between_requests"])

    # Persist dedup state regardless of whether we ran to completion or were
    # stopped early -- articles already processed before the stop are real
    # work done and shouldn't be re-fetched next run.
    save_seen(seen)

    if not all_rows:
        if stopped_early:
            print("Stopped before any articles were collected -- nothing to write.")
        else:
            print("No new articles found this run.")
        return

    # Sort highest relevance first
    all_rows.sort(key=lambda r: r["relevance_score"], reverse=True)

    out_path = os.path.join(OUTPUT_DIR, f"news_{datetime.now().strftime('%Y%m%d_%H%M')}.csv")
    fieldnames = list(all_rows[0].keys())
    with open(out_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(all_rows)

    if stopped_early:
        print(f"\n-- Stopped early by request — {len(all_rows)} article(s) collected so far were saved to {out_path} --")
    else:
        print(f"\nDone — {len(all_rows)} new articles written to {out_path}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Account + keyword news scraper")
    parser.add_argument(
        "--accounts-csv",
        default=os.path.join(BASE_DIR, "accounts.csv"),
        help="Path to the accounts CSV (Company Name, Alternative Name, Patch, Industry columns)",
    )
    parser.add_argument(
        "--keywords-csv",
        default=os.path.join(BASE_DIR, "keywords.csv"),
        help="Path to the industry keywords CSV",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Only process the first N accounts -- use this for a test run before going full-list",
    )
    args = parser.parse_args()
    run(args.accounts_csv, args.keywords_csv, limit=args.limit)
