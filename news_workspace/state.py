"""
state.py
Local dedup tracking so reruns only append genuinely new articles.

Keyed on the DECODED canonical URL (not the Google News wrapper link) —
the old script deduped on the wrapper link, which let true duplicates
slip through when the same story surfaced via different queries with
different Google tracking tokens.
"""

import json
import os

STATE_FILE = os.path.join(os.path.dirname(__file__), "seen_urls.json")


def load_seen() -> set:
    if not os.path.exists(STATE_FILE):
        return set()
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            return set(json.load(f))
    except (json.JSONDecodeError, IOError):
        return set()


def save_seen(seen: set) -> None:
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(sorted(seen), f, indent=2)
