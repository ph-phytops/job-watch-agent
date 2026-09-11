"""Collect job links from job-alert emails (LinkedIn, Indeed, ...) over IMAP.

The agent reads a dedicated mailbox that only receives job alerts. Each
alert email contains links to postings; we extract those links, reduce them
to a canonical URL (tracking parameters stripped) and read the employer,
location and referral signal out of the card around each link.

Credentials are read from the environment (.env file), never from the repo.
"""

from __future__ import annotations

import datetime as dt
import email
import imaplib
import re
from email.message import Message
from urllib.parse import unquote
from zoneinfo import ZoneInfo

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
    r"\d+\s+anciens?\s+élèves?",
]

# LinkedIn separates the employer from the location with a SPACED middle dot.
# The spaces are load-bearing: French inclusive writing ("Ingénieur·e",
# "Chef·fe", "Développeur·se") uses the same character with no space around it,
# and splitting on the bare dot cut those titles in half.
_CARD_SEPARATOR = " · "

# Referral signals printed on a LinkedIn card, strongest lead first.
_NETWORK_SIGNALS = [
    (r"(\d+)\s+anciens?\s+collègues?", "⭐ {} ancien(s) collègue(s)"),
    (r"(\d+)\s+anciens?\s+élèves?", "🎓 {} ancien(s) élève(s)"),
    (r"(\d+)\s+relations?", "{} relation(s)"),
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
    since = dt.datetime.now(ZoneInfo("Europe/Paris")).date() - dt.timedelta(
        days=cfg.get("since_days", 2)
    )
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

        message_ids: set[str] = set()
        for query in queries:
            _, data = imap.search(None, query)
            raw_ids = data[0]
            if raw_ids:
                message_ids.update(raw_ids.decode().split())
        for msg_id in message_ids:
            _, msg_data = imap.fetch(msg_id, "(RFC822)")
            part = msg_data[0] if msg_data else None
            if not isinstance(part, tuple):
                continue
            message = email.message_from_bytes(part[1])
            body = _html_body(message)
            if body:
                html_bodies.append(body)

    # Dedupe by canonical URL: one LinkedIn card carries two links to the same
    # posting, an outer one wrapping the whole card and an inner one on the
    # title alone. Only the outer one names the employer and the location.
    jobs: dict[str, dict] = {}
    for body in html_bodies:
        for job in _extract_jobs(body):
            existing = jobs.get(job["url"])
            if existing is None or _richer(job, existing):
                jobs[job["url"]] = job
    return list(jobs.values())


def _richer(candidate: dict, current: dict) -> bool:
    """True when the candidate anchor carries more of the posting.

    Comparing title length alone used to do this job, because the unparsed
    outer anchor happened to produce a longer title (the employer was glued to
    it). Now that both anchors yield the same clean title, the tie-break has to
    say out loud what it is really after: the anchor that named the employer.
    """
    if bool(candidate["location"]) != bool(current["location"]):
        return bool(candidate["location"])
    return len(candidate["title"]) > len(current["title"])


def _html_body(message: Message) -> str:
    """Return the decoded text/html part of an email, or an empty string."""
    for part in message.walk():
        if part.get_content_type() != "text/html":
            continue
        payload = part.get_payload(decode=True)
        if not isinstance(payload, bytes):
            continue
        charset = part.get_content_charset() or "utf-8"
        try:
            return payload.decode(charset, errors="replace")
        except LookupError:
            return payload.decode("utf-8", errors="replace")
    return ""


def _extract_jobs(html: str) -> list[dict]:
    """Pull every job link out of one email body."""
    soup = BeautifulSoup(html, "html.parser")
    found = []
    for anchor in soup.find_all("a", href=True):
        href = anchor.get("href")
        if not isinstance(href, str):
            continue
        canonical, source = _canonical_url(href)
        if not canonical:
            continue
        raw = " ".join(anchor.get_text(" ", strip=True).split())
        if len(raw) < 5 or raw.lower() in _JUNK_TITLES:
            continue
        card = _split_card(anchor, raw)
        if card:
            # A parsed card gives a title with nothing else in it, so a short
            # one is genuine: "PMO" is a real posting title.
            title, company, location = card
            minimum = 3
        else:
            # Indeed and Free-Work put the bare title in the anchor and nothing
            # else; a short one there is navigation noise, not a posting.
            title, company, location = _clean_title(raw), "", ""
            minimum = 5
        if len(title) < minimum:
            continue
        found.append(
            {
                # The employer when the card names it, the alert source
                # otherwise, so the digest always groups under something real.
                "company": company or source,
                "title": title,
                "location": location,
                "url": canonical,
                "source": source,
                "network": _network_signal(raw),
            }
        )
    return found


def _split_card(anchor, raw: str) -> tuple[str, str, str] | None:
    """Split a LinkedIn job card into (title, employer, location).

    The card puts the title in one element and "Employer · Location (mode)" in
    another, so the SMALLEST element carrying the separator is that second
    line, whatever the generated CSS classes happen to be called this month.
    Reading the text flat instead would be ambiguous: LinkedIn writes no
    separator between the title and the employer, which is why the employer
    used to end up glued to the title and the location was lost.

    Returns None for anchors that are not LinkedIn cards.
    """
    flavour = ""
    for node in anchor.find_all(["p", "span", "td", "div"]):
        text = " ".join(node.get_text(" ", strip=True).split())
        if _CARD_SEPARATOR in text and (not flavour or len(text) < len(flavour)):
            flavour = text
    if not flavour or flavour not in raw:
        return None
    company, _, location = flavour.partition(_CARD_SEPARATOR)
    title = raw.split(flavour)[0].strip()
    if not title:
        return None
    return title, company.strip(), location.strip()


def _network_signal(raw: str) -> str:
    """The referral lead printed on the card, or "" when there is none.

    Worth keeping rather than discarding: an opening where the mailbox owner
    already knows someone is not the same opening as a cold one.
    """
    for pattern, label in _NETWORK_SIGNALS:
        match = re.search(pattern, raw)
        if match:
            return label.format(match.group(1))
    return ""


def _clean_title(raw: str) -> str:
    """Strip card noise from an anchor whose text is the title and nothing else."""
    title = raw.split(_CARD_SEPARATOR)[0]
    for pattern in _TITLE_NOISE:
        title = re.sub(pattern, "", title)
    return " ".join(title.split())


def _canonical_url(href: str) -> tuple[str | None, str]:
    """Strip tracking noise; return (canonical_url, source_label).

    Forwarded emails often wrap links (e.g. Outlook SafeLinks) with the real
    URL percent-encoded inside, so decoding twice uncovers it.
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
    match = re.search(r"(https?://(?:www\.)?free-work\.com/[^\s\"'>]*job[^\s\"'>]*)", href)
    if match:
        return match.group(1).split("?")[0], "Free-Work (alerte email)"
    match = re.search(r"(https?://(?:www\.)?collective\.work/[^\s\"'>]+)", href)
    if match:
        url = match.group(1).split("?")[0]
        # Skip navigation links (homepage, login...), keep deep mission links.
        if len(url.rstrip("/").split("/")) > 4:
            return url, "Collective (alerte email)"
    return None, ""
