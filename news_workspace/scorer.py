"""
scorer.py
Heuristic relevance scoring — replaces the blank signal_type column
that never got populated for account-based scrapes in the old script.

This is a free, local heuristic, not a true classifier. It's meant as
a first-pass noise filter: did this article actually feature the
account prominently, or just mention it in passing (e.g. a roundup
article, a sports score, a different company with the same name)?

EXTENSION POINT: if you want sharper classification later, swap
score_relevance() out for a call to your Glean workflow (same pattern
as glean-pg-tracker-automation.js) — pass it the headline + body and
have it return a YES/NO + reason instead of this heuristic.
"""

DEFAULT_SIGNAL_KEYWORDS = [
    "rfp", "tender", "partnership", "launch", "expansion", "investment",
    "acquire", "acquisition", "digital transformation", "core banking",
    "license", "licence", "compliance", "regulation", "rmit",
    "modernization", "modernisation", "upgrade", "migration",
]

HIGH_THRESHOLD = 60
MEDIUM_THRESHOLD = 30


def score_relevance(entity_name: str, headline: str, body: str,
                     signal_keywords=None) -> dict:
    """
    Returns {"score": int 0-100, "level": "high"|"medium"|"low", "reasons": [...]}
    """
    signal_keywords = signal_keywords or DEFAULT_SIGNAL_KEYWORDS
    headline_l = (headline or "").lower()
    body_l = (body or "").lower()
    name_l = entity_name.lower()

    score = 0
    reasons = []

    if name_l in headline_l:
        score += 40
        reasons.append("name in headline")

    lead = body_l[:200]
    if name_l in lead:
        score += 20
        reasons.append("name in lead paragraph")

    mentions = body_l.count(name_l)
    if mentions > 1:
        bonus = min((mentions - 1) * 8, 16)
        score += bonus
        reasons.append(f"{mentions} mentions in body")

    matched_signals = [kw for kw in signal_keywords if kw in headline_l or kw in body_l]
    if matched_signals:
        score += min(len(matched_signals) * 12, 24)
        reasons.append("signal terms: " + ", ".join(matched_signals[:3]))

    score = min(score, 100)

    if score >= HIGH_THRESHOLD:
        level = "high"
    elif score >= MEDIUM_THRESHOLD:
        level = "medium"
    else:
        level = "low"

    return {"score": score, "level": level, "reasons": reasons}
