"""
extractor.py
Pulls clean article body text from a resolved publisher URL.

Uses trafilatura instead of regex <p> matching — it's boilerplate-aware
(strips nav, captions, related-article teasers, ad copy, bylines) rather
than relying on a hardcoded blocklist of phrases to filter out.
"""

import trafilatura

MIN_BODY_CHARS = 80
MAX_BODY_CHARS = 800


def fetch_article_body(url: str) -> dict:
    """
    Returns {"ok": True, "body": "<clean text>"}
    or      {"ok": False, "body": "", "reason": "paywall_or_empty" | "fetch_failed"}
    """
    try:
        downloaded = trafilatura.fetch_url(url)
        if not downloaded:
            return {"ok": False, "body": "", "reason": "fetch_failed"}

        text = trafilatura.extract(downloaded, favor_precision=True)
        if not text or len(text) < MIN_BODY_CHARS:
            return {"ok": False, "body": "", "reason": "paywall_or_empty"}

        return {"ok": True, "body": text[:MAX_BODY_CHARS].strip()}

    except Exception as e:
        return {"ok": False, "body": "", "reason": f"error: {e}"}
