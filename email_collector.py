"""Collect job links from job-alert emails (LinkedIn, Indeed, ...) over IMAP.

The agent reads a dedicated mailbox that only receives job alerts. Each
alert email contains links to postings; we extract those links, reduce them
to a canonical URL (tracking parameters stripped) and reuse the anchor text
as the job title.

Credentials are read from the environment (.env file), never from the repo.
"""

from __future__ import annotations

import datetime as dt
import email
import imaplib
import re
from email.message import Message
from urllib.parse import unquote

from bs4 import BeautifulSoup

# Noise that LinkedIn appends after the job title inside the anchor text.
_TITLE_NOISE = [
    r"Recrutement actif",
    r"Candidature simplifiée",
    r"\(À distance\)",
    r"\(Hybride\)",
    r"\(Sur site\)",
    r"\d+\s+relations?",
    r"\d+\s+anciens?\s+collègues?",
]

# Anchor texts that are navigation, not job titles.
_JUNK_TITLES = {
    "voir l'offre",
    "voir plus",
    "voir toutes les offres",
    "see job",
    "view job",
    "see all jobs",
    "postuler",
    "apply",
    "apply now",
    "linkedin",
    "indeed",
}


def fetch_email_jobs(cfg: dict, user: str, password: str) -> list[dict]:
    """Return normalised jobs found in recent alert emails."""
    since = dt.date.today() - dt.timedelta(days=cfg.get("since_days", 2))
    since_imap = since.strftime("%d-%b-%Y")

    # The mailbox is dedicated to job alerts, so by default we scan every
    # recent message (alerts may arrive forwarded, with any sender). An
    # optional "senders" list in config narrows the search if needed.
    senders = cfg.get("senders", [])
    queries = [f'(FROM "{s}" SINCE {since_imap})' for s in senders] or [
        f"(SINCE {since_imap})"
    ]

    html_bodies: list[str] = []
    with imaplib.IMAP4_SSL(cfg.get("imap_host", "imap.gmail.com")) as imap:
        imap.login(user, password)
        imap.select("INBOX", readonly=True)
        message_ids: set[bytes] = set()
        for query in queries:
            _, data = imap.search(None, query)
            message_ids.update(data[0].split())
        for msg_id in message_ids:
            _, msg_data = imap.fetch(msg_id, "(RFC822)")
            message = email.message_from_bytes(msg_data[0][1])
            body = _html_body(message)
            if body:
                html_bodies.append(body)

    # Dedupe by canonical URL; when the same posting appears under several
    # links (logo + title), keep the anchor with the longest text.
    jobs: dict[str, dict] = {}
    for body in html_bodies:
        for job in _extract_jobs(body):
            existing = jobs.get(job["url"])
            if existing is None or len(job["title"]) > len(existing["title"]):
                jobs[job["url"]] = job
    return list(jobs.values())


def _html_body(message: Message) -> str:
    """Return the decoded text/html part of an email, or an empty string."""
    parts = message.walk() if message.is_multipart() else [message]
    for part in parts:
        if part.get_content_type() == "text/html":
            payload = part.get_payload(decode=True)
            charset = part.get_content_charset() or "utf-8"
            return payload.decode(charset, errors="replace")
    return ""


def _extract_jobs(html: str) -> list[dict]:
    """Pull every job link out of one email body."""
    soup = BeautifulSoup(html, "html.parser")
    found = []
    for anchor in soup.find_all("a", href=True):
        canonical, source = _canonical_url(anchor["href"])
        if not canonical:
            continue
        raw = " ".join(anchor.get_text(" ", strip=True).split())
        if len(raw) < 5 or raw.lower() in _JUNK_TITLES:
            continue
        title, network = _clean_title(raw)
        if len(title) < 5:
            continue
        found.append(
            {"company": source, "title": title, "location": network, "url": canonical}
        )
    return found


def _clean_title(raw: str) -> tuple[str, str]:
    """Split a noisy LinkedIn card text into (clean title, network signal).

    The 'N anciens collègues / relations' mention is a referral lead worth
    keeping — it is returned separately instead of being thrown away.
    """
    network = ""
    match = re.search(r"(\d+)\s+anciens?\s+collègues?", raw)
    if match:
        network = f"⭐ {match.group(1)} ancien(s) collègue(s)"
    else:
        match = re.search(r"(\d+)\s+relations?", raw)
        if match:
            network = f"{match.group(1)} relation(s)"

    title = re.split(r"\s*·\s*", raw)[0]
    for pattern in _TITLE_NOISE:
        title = re.sub(pattern, "", title)
    return " ".join(title.split()), network


def _canonical_url(href: str) -> tuple[str | None, str]:
    """Strip tracking noise; return (canonical_url, source_label).

    Forwarded emails often wrap links (e.g. Outlook SafeLinks) with the real
    URL percent-encoded inside — decoding twice uncovers it.
    """
    href = unquote(unquote(href))
    match = re.search(r"linkedin\.com/(?:comm/)?jobs/view/(\d+)", href)
    if match:
        url = f"https://www.linkedin.com/jobs/view/{match.group(1)}/"
        return url, "LinkedIn (alerte email)"
    if "indeed.com" in href:
        match = re.search(r"[?&]jk=([0-9a-fA-F]+)", href)
        if match:
            url = f"https://fr.indeed.com/viewjob?jk={match.group(1)}"
            return url, "Indeed (alerte email)"
    return None, ""
