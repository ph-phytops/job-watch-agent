"""Transparent job scoring — every point is explainable.

score_job() returns (score, reasons): the digest shows WHY a job ranks
where it does, in line with the project's "explicit scoring" constraint
(a black-box relevance score is not useful for deciding where to spend
an afternoon).

Weights live in config.toml ([scoring.*]): tuning the ranking is a config
change, not a code change.
"""

from __future__ import annotations

import re


def score_job(job: dict, cfg: dict) -> tuple[int, list[str]]:
    score = 0
    reasons: list[str] = []

    title = job["title"].lower()
    company_text = f"{job['company']} {job['title']}".lower()
    location = job.get("location", "").lower()

    # Title keywords accumulate (a "Technical Program Manager, Datacenter"
    # deserves both boosts).
    for keyword, points in cfg.get("title", {}).items():
        if keyword.lower() in title:
            score += points
            reasons.append(f"{keyword} {points:+d}")

    # Company and location: single best match each (avoids double-counting
    # "Amazon" + "AWS", or "Paris" + "France").
    for label, table in (("company", company_text), ("location", location)):
        best = _best_match(table, cfg.get(label, {}))
        if best:
            keyword, points = best
            score += points
            reasons.append(f"{keyword} {points:+d}")

    extras = cfg.get("extras", {})

    # Referral signal extracted from LinkedIn alerts ("N anciens collègues").
    if "collègue" in location or "relation" in location:
        points = extras.get("network_bonus", 15)
        score += points
        reasons.append(f"réseau {points:+d}")

    # Daily rate visible in the title (Free-Work shows €/day openly).
    match = re.search(r"(\d{3,4})\s*(?:-|–|à)?\s*\d*\s*€\s*/?\s*j", title)
    if match and int(match.group(1)) >= extras.get("rate_threshold", 600):
        points = extras.get("rate_bonus", 10)
        score += points
        reasons.append(f"TJM {points:+d}")

    return score, reasons


def _best_match(text: str, table: dict) -> tuple[str, int] | None:
    best: tuple[str, int] | None = None
    for keyword, points in table.items():
        if keyword.lower() in text and (best is None or points > best[1]):
            best = (keyword, points)
    return best
