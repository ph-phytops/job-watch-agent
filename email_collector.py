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
import unicodedata
from email.header import decode_header, make_header
from email.message import Message
from urllib.parse import quote_plus, unquote
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

# The label an Indeed posting is grouped under when its card names no
# employer, kept in one place because it is also the fallback the card parser
# is allowed to overwrite.
_INDEED_SOURCE = "Indeed (alerte email)"
_COLLECTIVE_SOURCE = "Collective (alerte email)"

# An Indeed alert link that carries no posting id. A recurring alert links
# straight to the posting; the letter confirming a newly created alert routes
# every card through a per-recipient redirector instead. That token names the
# mailbox owner, so it can be neither followed nor written to the memory the
# repository tracks, and the posting has to be recognised by its card instead.
_INDEED_REDIRECTOR = "engage.indeed.com"
# Paid placements. No jk=, so _canonical_url() cannot key them; see
# _untracked_indeed_posting() for how they are keyed instead.
_INDEED_SPONSORED = "indeed.com/pagead/clk"

# The identity rebuilt for such a card, which doubles as a usable link: a
# plain Indeed search for the posting. It holds nothing but the title and the
# employer, so it is the same from one run to the next and carries no token.
_INDEED_SEARCH = "https://fr.indeed.com/jobs?q="

# That template writes "Employer [rating] - Location" on the card's second
# line. Unlike a title, which often contains the same dash, this line is
# unambiguous: over every such card in a live mailbox the separator appeared
# there exactly once, so partitioning on it cannot cut a field in half.
_INDEED_CARD_SEPARATOR = " - "

# The employer rating, printed between the employer and that separator.
_TRAILING_RATING = re.compile(r"\s+\d+([.,]\d+)?$")

# An employer cell holding nothing but a number is Indeed's star rating, which
# sits next to the employer on the same row of some templates.
_RATING_ONLY = re.compile(r"^\d+([.,]\d+)?$")

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

    # The subject rides along with the body: a Collective "new opportunity"
    # email names the posting's company there and nowhere in the body.
    html_bodies: list[tuple[str, str]] = []
    with imaplib.IMAP4_SSL(cfg.get("imap_host", "imap.gmail.com")) as imap:
        imap.login(user, password)
        imap.select("INBOX", readonly=True)

        message_ids: set[str] = set()
        for query in queries:
            _, data = imap.search(None, query)
            raw_ids = data[0]
            if raw_ids:
                message_ids.update(raw_ids.decode().split())
        # Oldest message first, and never the order a set happens to iterate
        # in: that order is randomised at every process start, and two anchors
        # describing the same posting are merged below by keeping the richer
        # of them, so an unsorted read makes the digest differ from one run to
        # the next on the same mailbox.
        for msg_id in sorted(message_ids, key=_message_order):
            _, msg_data = imap.fetch(msg_id, "(RFC822)")
            part = msg_data[0] if msg_data else None
            if not isinstance(part, tuple):
                continue
            message = email.message_from_bytes(part[1])
            body = _html_body(message)
            if body:
                html_bodies.append((body, _subject(message)))

    # Dedupe by canonical URL: one LinkedIn card carries two links to the same
    # posting, an outer one wrapping the whole card and an inner one on the
    # title alone. Only the outer one names the employer and the location.
    jobs: dict[str, dict] = {}
    for body, subject in html_bodies:
        for job in _extract_jobs(body, subject):
            existing = jobs.get(job["url"])
            if existing is None or _richer(job, existing):
                jobs[job["url"]] = job
    return _fold_twins(list(jobs.values()))


def _message_order(msg_id: str) -> tuple[int, int, str]:
    """Sort key for an IMAP message id, which must never raise.

    RFC 3501 numbers messages, so int() would do. It is not worth an
    exception if a server ever answers otherwise: jobwatch catches the error
    around the whole mailbox and would lose every email posting of the day
    behind one line of log. Anything unnumbered sorts last, in its own order.
    """
    return (0, int(msg_id), "") if msg_id.isdigit() else (1, 0, msg_id)


def _richer(candidate: dict, current: dict) -> bool:
    """True when the candidate anchor carries more of the posting.

    Comparing title length alone used to do this job, because the unparsed
    outer anchor happened to produce a longer title (the employer was glued to
    it). Now that both anchors yield the same clean title, the tie-break has to
    say out loud what it is really after: the anchor that named the employer.

    Read with .get(): a producer that forgets a key must degrade here, not
    raise, because jobwatch catches the exception around the whole mailbox and
    would lose every email posting of the day behind one line of log.
    """
    if bool(candidate.get("location")) != bool(current.get("location")):
        return bool(candidate.get("location"))
    return len(candidate.get("title", "")) > len(current.get("title", ""))


def _subject(message: Message) -> str:
    """The decoded subject line, or "" when it cannot be decoded.

    Same rule as everywhere here: a malformed header degrades to nothing
    rather than raising, since jobwatch would lose the whole mailbox.
    """
    try:
        return str(make_header(decode_header(message.get("Subject") or "")))
    except Exception:  # noqa: BLE001, a bad header must not cost the run
        return ""


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


def _extract_jobs(html: str, subject: str = "") -> list[dict]:
    """Pull every job link out of one email body."""
    soup = BeautifulSoup(html, "html.parser")
    found = []
    for anchor in soup.find_all("a", href=True):
        href = anchor.get("href")
        if not isinstance(href, str):
            continue
        canonical, source = _canonical_url(href)
        if not canonical:
            posting = _untracked_indeed_posting(anchor, href)
            if posting:
                found.append(posting)
            continue
        raw = " ".join(anchor.get_text(" ", strip=True).split())
        if source == _COLLECTIVE_SOURCE:
            # Its posting link is labelled "Voir l'offre", which the junk
            # filter below rightly throws away everywhere else. Until this
            # branch existed, every Collective email yielded nothing.
            title = _collective_offer_title(soup)
            if title:
                found.append(
                    {
                        "company": _collective_sender(subject) or source,
                        "title": title,
                        "location": "",
                        "url": canonical,
                        "source": source,
                        "network": "",
                    }
                )
            continue
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
            if source.startswith("Indeed"):
                sides = _split_indeed_alert_card(anchor)
                if sides:
                    company, location = sides
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


def _untracked_indeed_posting(anchor, href: str) -> dict | None:
    """A posting whose link names the recipient, keyed by its card instead.

    Two kinds of link land here and they carry the same problem. A confirmation
    letter routes through a redirector, and a sponsored card carries no jk= at
    all, just 1.1 kB of per-recipient parameters (ad, xkcb, tmtk, alid) which
    all change between two sends of the same posting. Neither can be written to
    a memory that is committed, and neither dedupes against itself. So both are
    keyed on what the card says rather than on where it points.

    Returns None for every other unrecognised link, which is what the vast
    majority of them are: an email is mostly navigation.
    """
    if _INDEED_REDIRECTOR in href:
        card = _split_indeed_confirmation_card(anchor)
        if card is None:
            return None
        title, company, location = card
    elif _INDEED_SPONSORED in href:
        # A sponsored card is laid out like an ordinary alert card, measured
        # on 327 of 327, so the same splitter reads it. Only the title has to
        # come from the anchor, which is where the alert format keeps it.
        sides = _split_indeed_alert_card(anchor)
        if sides is None:
            return None
        title = _clean_title(" ".join(anchor.get_text(" ", strip=True).split()))
        company, location = sides
        # Without both, _search_url() would key the posting on a fragment and
        # two different jobs could collapse into one.
        if len(title) < 5 or not company:
            return None
    else:
        return None
    return {
        "company": company,
        "title": title,
        "location": location,
        "url": _search_url(title, company),
        "source": _INDEED_SOURCE,
        "network": "",
    }


def _split_indeed_confirmation_card(anchor) -> tuple[str, str, str] | None:
    """Read (title, employer, location) off a card that carries no posting id.

    The card nests two links to the same posting, an outer one wrapping the
    whole card and an inner one on the title alone, and only the inner one
    holds the title by itself. The outer one is turned away here rather than
    later, because both would otherwise be read as two different postings.

    The card's second line does the sorting: it names the employer and the
    location, the outer link repeats it inside its own text and the inner link
    cannot. Gating on a generated CSS class would be shorter and would break
    the month the template renames it.

    Returns None for anything that is not such a card, navigation included.
    """
    table = anchor.find_parent("table")
    if table is None:
        return None
    cells = [_flat(cell.get_text(" ", strip=True)) for cell in table.find_all("td")]
    cells = [cell for cell in cells if cell]
    if len(cells) < 2:
        return None
    title, line = cells[0], cells[1]
    if _INDEED_CARD_SEPARATOR not in line:
        return None
    raw = _flat(anchor.get_text(" ", strip=True))
    if line in raw or title != raw:
        return None
    company, _, location = line.partition(_INDEED_CARD_SEPARATOR)
    company = _TRAILING_RATING.sub("", company).strip()
    location = location.strip()
    if not company or not location:
        return None
    return title, company, location


def _slug(text: str) -> str:
    """Fold text to lowercase ASCII words, so one posting yields one identity.

    The result keys a posting that has no id of its own, and the same posting
    does not always reach the mailbox with the same casing or accents.
    """
    flat = unicodedata.normalize("NFKD", text)
    flat = "".join(char for char in flat if not unicodedata.combining(char))
    return " ".join(re.sub(r"[^a-z0-9]+", " ", flat.lower()).split())


def _search_url(title: str, company: str) -> str:
    """A stable Indeed search standing in for a posting with no id."""
    return _INDEED_SEARCH + quote_plus(_slug(f"{title} {company}"))


def _fold_twins(jobs: list[dict]) -> list[dict]:
    """Merge a card-keyed posting into the same posting seen with a real link.

    One posting can reach the mailbox twice, through an alert that links
    straight to it and through a confirmation letter that cannot. Keeping both
    would list it twice and spend two reading slots on it. The copy worth
    keeping is the one that opens the posting rather than a search for it, so
    the card hands over what it knows and steps aside.

    Matching on title and employer is the strict case. A posting whose own
    card was left unsplit carries no employer at all, so on that side the
    title has to match alone, or the pair is never recognised and the posting
    is listed twice, once under a real employer and once under the fallback
    label. Both sides are Indeed postings from one mailbox and one window,
    which is what keeps a title-only match narrow enough to be safe.
    """
    def key(job: dict) -> str:
        return _slug(f"{job.get('title', '')} {job.get('company', '')}")

    identified: dict[str, dict] = {}
    for job in jobs:
        if job.get("url", "").startswith(_INDEED_SEARCH):
            continue
        for candidate in (key(job), _slug(job.get("title", ""))
                          if job.get("company") == _INDEED_SOURCE else ""):
            if candidate:
                identified.setdefault(candidate, job)

    kept = []
    for job in jobs:
        if not job.get("url", "").startswith(_INDEED_SEARCH):
            kept.append(job)
            continue
        twin = identified.get(key(job)) or identified.get(_slug(job.get("title", "")))
        if twin is None:
            kept.append(job)
            continue
        # The twin owns the better link; the card owns the better fields.
        if twin.get("company") == _INDEED_SOURCE and job.get("company"):
            twin["company"] = job["company"]
        if not twin.get("location"):
            twin["location"] = job.get("location", "")
    return kept


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


def _split_indeed_alert_card(anchor) -> tuple[str, str] | None:
    """Read (employer, location) off a recurring Indeed alert card.

    Indeed writes no separator at all between the three fields: read flat, a
    card is "Chef de projet IT (H/F) TRAPIL La Defense (92)" and there is
    nothing to cut on, while half the titles contain " - " themselves. So the
    split comes from the markup, as it does for LinkedIn, but for the opposite
    reason: there the separator exists and is ambiguous, here there is none.

    The title stays untouched, since the anchor already holds it alone. Only
    the employer and the location are missing, and the card keeps them in a
    sibling cell OUTSIDE the anchor, which is why the LinkedIn splitter can
    never find them however hard it looks inside.

    Exactly two paragraphs is a gate, not an observation. The day Indeed
    prints a third one (an employer rating would land right there, it already
    does in the other template) the card is handed back unsplit rather than
    read one field out of step. Nothing is lost when that happens: the anchor
    carries the posting id, so the posting is collected exactly as it is
    today, just without its employer and its location.
    """
    heading = anchor.find_parent("h2")
    if heading is None:
        return None
    card = heading.find_parent("table")
    if card is None:
        return None
    lines = [_flat(node.get_text(" ", strip=True)) for node in card.find_all("p")]
    lines = [line for line in lines if line]
    if len(lines) != 2 or _RATING_ONLY.match(lines[0]):
        return None
    return lines[0], lines[1]


def _flat(text: str) -> str:
    """Collapse every run of whitespace, the no-break space included.

    Indeed writes a no-break space after the employer, before its rating.
    Left alone it turns one employer into two.
    """
    return " ".join(text.split())


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
            return url, _INDEED_SOURCE
    match = re.search(r"(https?://(?:www\.)?free-work\.com/[^\s\"'>]*job[^\s\"'>]*)", href)
    if match:
        return match.group(1).split("?")[0], "Free-Work (alerte email)"
    # Only the public posting page. Every other deep link in a Collective
    # email points into app.collective.work, and those addresses are built per
    # member: the same id comes back in the "apply", "application sent" and
    # "application seen" emails of one account, so it identifies the mailbox
    # owner and must never reach the memory the repository publishes. The
    # search collector writes this same address, so both routes agree on it.
    # The language segment is dropped for /fr/: the slug is the posting's
    # identity, and one posting must have one key whichever language the
    # member's account happens to be set to.
    match = re.search(
        r"https?://(?:www\.)?collective\.work/jobs/[a-z]{2}/([\w-]+)", href
    )
    if match:
        return (
            f"https://www.collective.work/jobs/fr/{match.group(1)}",
            _COLLECTIVE_SOURCE,
        )
    return None, ""


def _collective_offer_title(soup: BeautifulSoup) -> str:
    """The title a Collective "new opportunity" email announces.

    Its only link to the posting is labelled "Voir l'offre", which is
    navigation, and the title sits in the text above it: "Offre : <title>
    Postuler Voir l'offre". One offer per email, so the first match is it.
    """
    text = " ".join(soup.get_text(" ", strip=True).split())
    match = re.search(r"Offre\s*:\s*(.+?)\s+(?:Postuler|Voir l['’]offre)\b", text)
    return match.group(1).strip() if match else ""


def _collective_sender(subject: str) -> str:
    """The company in a "[Member x Company] Nouvelle opportunité" subject.

    Only what follows the " x " is kept: what precedes it is the member's own
    first name. Searched, not anchored, because a forwarded copy prefixes the
    subject ("TR : [...]", "Fwd: [...]"). A sender that spells itself with a
    dot between every letter ("A.c.m.e.") is undone; a real dotted name such
    as "Example.com" has more than one letter between its dots and is left.
    """
    match = re.search(r"\[[^\]]*?\sx\s+([^\]]+?)\s*\]", subject or "")
    if not match:
        return ""
    # A folded header keeps its line break inside the name.
    name = " ".join(match.group(1).split())
    if re.fullmatch(r"(?:\w\.)+\w?", name):
        name = name.replace(".", "")
    return name
