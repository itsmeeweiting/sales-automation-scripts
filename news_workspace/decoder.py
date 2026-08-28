"""
decoder.py
Resolves Google News RSS redirect links to their real publisher URL.

Google News RSS links look like:
    https://news.google.com/rss/articles/CBMi...

These are NOT the real article URL — they're an encoded redirect token.
Since 2024, the only reliable way to resolve them is Google's internal
batchexecute endpoint, which `googlenewsdecoder` implements and keeps
patched as Google tweaks the protocol. Hand-rolling this yourself is a
losing game — it breaks every time Google changes the encoding, and
maintaining a fix for an undocumented internal API isn't worth your time.
"""

import time
from googlenewsdecoder import gnewsdecoder

DECODE_INTERVAL_SECONDS = 1  # be polite — also reduces 429s
MAX_RETRIES = 2


def is_google_news_redirect(url: str) -> bool:
    return "news.google.com" in url


def resolve_url(url: str, retries: int = MAX_RETRIES) -> dict:
    """
    Returns {"ok": True, "url": "<real publisher url>"}
    or      {"ok": False, "url": "<original url>", "error": "<reason>"}

    Falls back to the original URL on failure so the pipeline never
    silently drops an article — it just flags it as unresolved.
    """
    if not is_google_news_redirect(url):
        return {"ok": True, "url": url}

    last_error = None
    for attempt in range(retries + 1):
        try:
            result = gnewsdecoder(url, interval=DECODE_INTERVAL_SECONDS)
            if result.get("status") and result.get("decoded_url"):
                return {"ok": True, "url": result["decoded_url"]}
            last_error = result.get("message", "unknown decode failure")
        except Exception as e:
            last_error = str(e)

        if attempt < retries:
            time.sleep(2)  # back off before retrying

    return {"ok": False, "url": url, "error": last_error}
