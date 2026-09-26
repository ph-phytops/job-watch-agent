"""job-watch-agent v0.1: collect job postings from public ATS APIs.

Pipeline: collect -> normalise -> filter -> digest.

Companies and search keywords live in config.toml. For each company we call
the public job-board API of its ATS (Greenhouse, Lever or Ashby), reduce
every posting to one common shape, keep the ones matching the search
keywords, and write a Markdown digest in digests/.

Usage:
    uv run jobwatch.py
    uv run jobwatch.py --dry-run   # collect and filter only, change nothing
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import sys
import time
import tomllib
import unicodedata
from pathlib import Path
from urllib.parse import urlencode, urlsplit
from zoneinfo import ZoneInfo

import requests
from bs4 import BeautifulSoup
from dotenv import load_dotenv

from email_collector import _slug, fetch_email_jobs
from llm_review import load_profile, render, review, unavailable
from notifier import send_digest
from scoring import score_job

ROOT = Path(__file__).parent
# BASE is where one person's data lives. It stays ROOT unless --profile names
# another one, in which case main() repoints it at profiles/<name>/.
BASE = ROOT
DIGEST_DIR = ROOT / "digests"
SEEN_PATH = ROOT / "data" / "seen.json"
REVIEWED_PATH = ROOT / "data" / "reviewed.json"
APPLICATIONS_PATH = ROOT / "data" / "applications.json"
# What --applied accepts. They all hide a posting from the reading slots; they
# differ only in what the report shows. "applied" and "interviewing" are live
# files, "rejected" and "closed" are dead ones worth keeping so they never
# climb back into the ranking.
STATUSES = ("applied", "interviewing", "rejected", "closed")
# Not a state a file can be in, a way out of one: --status forget removes the
# entry. Recording an application is otherwise irreversible from the command
# line, and the urls easiest to confuse are those differing by one digit on the
# same board, where recording the wrong one silently pulls a live conversation
# out of the ranking.
UNDO_STATUS = "forget"
# Sites that republish other companies' postings under their own name. They
# are not employers, and treating their name as one is what lets the same
# opening be read twice, once as theirs and once as the hiring company's.
# Slugged form, as _slug() writes it.
AGGREGATORS = frozenset({"jobgether"})
TIMEOUT = 20
HEADERS = {
    "User-Agent": "job-watch-agent/0.1 (personal project; "
    "github.com/ph-phytops/job-watch-agent)"
}

# --------------------------------------------------------------------------
# Collectors, one per ATS. Each returns a list of "normalised" jobs:
# {"company", "title", "location", "url"}
# The email collector adds two optional keys, "source" and "network", which
# an ATS cannot know about. Everything downstream reads them with .get() so
# the four-key contract above still holds.
# --------------------------------------------------------------------------


def fetch_greenhouse(
    company: str, slug: str, content: bool = False
) -> list[dict]:
    url = f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs"
    if content:
        # Same single request, larger payload: the full description ships
        # with the listing. Only asked for when --llm is going to read it.
        url += "?content=true"
    data = _get_json(url)
    return [
        {
            "company": company,
            "title": job.get("title", ""),
            "location": (job.get("location") or {}).get("name", ""),
            "url": job.get("absolute_url", ""),
            "content": _plain_text(job.get("content", "")) if content else "",
        }
        for job in data.get("jobs", [])
    ]


def fetch_lever(company: str, slug: str, content: bool = False) -> list[dict]:
    url = f"https://api.lever.co/v0/postings/{slug}?mode=json"
    data = _get_json(url)
    return [
        {
            "company": company,
            "title": job.get("text", ""),
            "location": (job.get("categories") or {}).get("location", ""),
            "url": job.get("hostedUrl", ""),
            "content": job.get("descriptionPlain", "") if content else "",
        }
        for job in data
    ]


def fetch_ashby(company: str, slug: str, content: bool = False) -> list[dict]:
    url = f"https://api.ashbyhq.com/posting-api/job-board/{slug}"
    data = _get_json(url)
    return [
        {
            "company": company,
            "title": job.get("title", ""),
            "location": job.get("location", ""),
            "url": job.get("jobUrl", ""),
            # Ashby ships descriptionPlain in the listing we already download.
            "content": job.get("descriptionPlain", "") if content else "",
        }
        for job in data.get("jobs", [])
    ]


def fetch_teamtailor(
    company: str, slug: str, content: bool = False
) -> list[dict]:
    """Postings from a Teamtailor career site's public JSON Feed.

    Teamtailor's own API answers 406 without a token, which the zero-cost rule
    puts out of reach, but every career site publishes the same postings as a
    JSON Feed at /jobs.json, description included.

    `slug` is the Teamtailor subdomain, and only that. The subdomain answers
    even when the employer serves its site from its own domain, so supporting
    both would buy nothing and cost the one thing that matters here: the same
    posting returns a different url through each door, and the url is what the
    memory remembers it by.
    """
    jobs = []
    page = f"https://{slug}.teamtailor.com/jobs.json"
    requested = set()
    while page and page not in requested:
        requested.add(page)
        data = _get_json(page)
        jobs += [
            {
                "company": company,
                "title": job.get("title", ""),
                "location": _teamtailor_location(job),
                "url": _teamtailor_url(slug, job),
                # The feed ships the description with the listing, so asking
                # for it costs no extra request, as on Ashby and Lever.
                "content": (
                    _plain_text(job.get("content_html", "")) if content else ""
                ),
            }
            for job in data.get("items", [])
        ]
        # A page holds at most 100 postings, per_page is capped server-side,
        # and the feed names its own continuation. Without this loop a board
        # of 148 is collected as 100 and the other 48 look like a board with
        # nothing more open. The set above is there so a feed that ever
        # pointed at itself would stop rather than spin.
        page = data.get("next_url")
    return jobs


def _teamtailor_url(slug: str, job: dict) -> str:
    """The address a posting is remembered by, stripped of its title.

    The feed's own url ends in a slug built from the title, so renaming a
    posting would hand back a url no memory has seen: it would resurface in
    the digest as new, spend a reading slot a second time, and shake off the
    application recorded against it. The numeric id alone resolves, so that is
    what gets stored, exactly as on the SmartRecruiters board below.
    """
    identifier = (job.get("_jobposting") or {}).get("identifier") or {}
    job_id = identifier.get("value")
    if not job_id:
        # No id to build from: a title-bearing url still reaches the posting,
        # which beats dropping it.
        return job.get("url", "")
    return f"https://{slug}.teamtailor.com/jobs/{job_id}"


def _teamtailor_location(job: dict) -> str:
    """Flatten the schema.org jobLocation a feed item carries.

    The other boards hand over one free-text location; here it is a list of
    postal addresses, and the scoring table needs it as a single string.
    """
    names = []
    for place in (job.get("_jobposting") or {}).get("jobLocation") or []:
        address = place.get("address") or {}
        # addressRegion sometimes repeats the city and sometimes names the
        # country, so keeping both and dropping duplicates yields "Paris,
        # France" without producing "Paris, Paris".
        parts = [address.get("addressLocality"), address.get("addressRegion")]
        names.append(", ".join(dict.fromkeys(p for p in parts if p)))
    return " / ".join(dict.fromkeys(n for n in names if n))


# How many postings one SmartRecruiters page returns. Its API caps the value
# at 100, and a board of a few hundred is normal there, unlike the tech boards
# above: Iliad alone publishes around 260.
SR_PAGE = 100


def fetch_smartrecruiters(
    company: str, slug: str, content: bool = False
) -> list[dict]:
    """Postings from a SmartRecruiters company board, page by page.

    The only board here whose listing does not carry the description: it sits
    behind a second request per posting, so asking for it at collection time
    would mean hundreds of calls to read the handful that survive the title
    filter. `content` is therefore accepted and deliberately ignored, and
    fetch_smartrecruiters_description() reads the finalists only, under --llm.

    Watch the slug: an unknown company answers 200 with an empty list rather
    than 404, so a typo reads exactly like a board with nothing open.
    """
    jobs = []
    offset = 0
    while True:
        page = _get_json(
            f"https://api.smartrecruiters.com/v1/companies/{slug}/postings"
            f"?limit={SR_PAGE}&offset={offset}"
        )
        postings = page.get("content", [])
        jobs += [
            {
                "company": company,
                "title": job.get("name", ""),
                "location": (job.get("location") or {}).get(
                    "fullLocation", ""
                ),
                # The listing omits postingUrl, which only the detail call
                # returns. The id-only form resolves without the title slug,
                # so it is built rather than guessed from the title.
                "url": (
                    "https://jobs.smartrecruiters.com/"
                    f"{slug}/{job.get('id', '')}"
                ),
                "content": "",
            }
            for job in postings
        ]
        offset += len(postings)
        # Either condition ends it on its own; both are here because a short
        # page and an exhausted count are two different ways for this API to
        # say "that was the last one", and an empty page must not loop.
        if len(postings) < SR_PAGE or offset >= page.get("totalFound", 0):
            return jobs


# How many postings one Workday page returns. Its API caps the value at 20,
# and asking for 21 hands back a body with no postings and no error, so a
# larger page reads exactly like a board with nothing open.
WD_PAGE = 20
# Workday serves at most this many postings for one query and reports `total`
# as that same number once a board is larger: NVIDIA answers 2000 while its
# own facets add up to 2653. A board over the cap therefore cannot be read
# whole, which is what the countries= form of the slug is for.
WD_CAP = 2000
# What the listing writes instead of a location when a posting is open in
# several places ("2 Locations", "5 Locations"). Measured on a real board:
# 79 of 102 European postings, so leaving it as the location would blind the
# [scoring.location] table on three quarters of them.
WD_MANY_PLACES = re.compile(r"^\d+\s+locations?$", re.I)
# Seconds between two pages of the same query. Only this board needs pacing,
# because only this board turns one employer into scores of requests.
WD_PACE = 0.5


def fetch_workday(
    company: str, slug: str, content: bool = False
) -> list[dict]:
    """Postings from a Workday career site, one country at a time.

    The slug carries the three parts a Workday address is built from, plus an
    optional country list: "tenant/dc/site" or
    "tenant/dc/site?countries=France, Germany". The data centre is the wdNN in
    the hostname and differs per employer, so none of the three can be guessed
    from the other two.

    Like SmartRecruiters, the listing does not carry the description, so
    `content` is accepted and ignored and fetch_workday_description() reads
    the finalists only, under --llm.

    Two behaviours of this API decide the shape of everything below. A board
    over WD_CAP cannot be served whole, so it is refused rather than silently
    halved. And a posting open in several countries is listed as "5
    Locations", which carries no location at all, so each country is asked for
    separately and answers for the postings it returns: it costs a handful of
    extra requests (11 against 6 on a real board) and it is the difference
    between a scored location and none.
    """
    tenant, dc, site, countries = _workday_parts(slug)
    api = (
        f"https://{tenant}.{dc}.myworkdayjobs.com/wday/cxs/"
        f"{tenant}/{site}/jobs"
    )
    if not countries:
        return [
            _workday_job(company, tenant, dc, site, posting, "")
            for posting in _workday_walk(api, {})
        ]
    param, ids = _workday_country_facet(api, countries)
    jobs: dict[str, dict] = {}
    # Over what resolved, not over what was asked for: a board offers only
    # the countries it has a posting in today, and the rest were reported.
    for country, facet_id in ids.items():
        for posting in _workday_walk(api, {param: [facet_id]}):
            job = _workday_job(company, tenant, dc, site, posting, country)
            known = jobs.get(job["url"])
            if known is None:
                jobs[job["url"]] = job
            elif job["location"] not in known["location"].split(" / "):
                # The same posting comes back under each of its countries.
                # Joining them beats keeping the first: "France / Germany"
                # is what the posting says, and both are scored.
                known["location"] += f" / {job['location']}"
    return list(jobs.values())


def _workday_parts(slug: str) -> tuple[str, str, str, list[str]]:
    """Split "tenant/dc/site?countries=A, B" into its four pieces."""
    address, _, query = slug.partition("?")
    parts = [p for p in address.split("/") if p]
    if len(parts) != 3:
        raise ValueError(
            f"workday slug must be tenant/dc/site, got {address!r}"
        )
    countries = []
    if query:
        key, _, value = query.partition("=")
        if key != "countries":
            raise ValueError(f"unknown workday slug option {key!r}")
        countries = [c.strip() for c in value.split(",") if c.strip()]
    return parts[0], parts[1], parts[2], countries


def _workday_walk(api: str, facets: dict) -> list[dict]:
    """Every posting for one query, page by page."""
    postings: list[dict] = []
    offset = 0
    while True:
        page = _post_json(
            api,
            {
                "appliedFacets": facets,
                "limit": WD_PAGE,
                "offset": offset,
                "searchText": "",
            },
        )
        found = page.get("jobPostings", [])
        if offset == 0 and page.get("total", 0) >= WD_CAP:
            raise RuntimeError(
                f"board serves at most {WD_CAP} postings and reports "
                f"{page.get('total')}; narrow it with ?countries=... in the "
                "slug, otherwise the rest is dropped without a word"
            )
        postings += found
        offset += len(found)
        # A short page is the only reliable end. `total` is served on the
        # first page and comes back as 0 on every later one, so using it as
        # the bound stops a 102-posting query at 40. The cap is a second
        # bound so a board that never shortens cannot spin: anything at or
        # over it was already refused above.
        if len(found) < WD_PAGE or offset >= WD_CAP:
            return postings
        # Twenty per page turns a large board into scores of requests, and a
        # board of 1493 answered seventy-odd of them by closing the
        # connection. The other five boards never needed this because they
        # answer in one call. Same courtesy as the description reads: this
        # is a handful of public pages, not a crawl.
        time.sleep(WD_PACE)


def _workday_country_facet(api: str, wanted: list[str]) -> tuple[str, dict]:
    """The facet parameter and ids that name the wanted countries.

    Neither the parameter nor the ids are portable: NVIDIA files countries
    under `locationHierarchy1`, Salesforce under a custom field whose name
    runs to sixty characters, and the same country carries a different id on
    each. Only the label is shared, so the facets are read and matched by it.

    The match has to be exact, and that is what separates a country from a
    site: a board that offers "France" also offers "France, Courbevoie" and
    "France - Paris", and starting-with would silently collect one city.
    """
    base = _post_json(
        api, {"appliedFacets": {}, "limit": WD_PAGE, "offset": 0,
              "searchText": ""}
    )
    offered: dict[str, dict[str, str]] = {}
    for param, label, ident in _workday_facet_values(base.get("facets")):
        offered.setdefault(param, {})[(label or "").strip().casefold()] = ident
    param, ids = "", {}
    for candidate, labels in offered.items():
        hits = {
            country: labels[country.casefold()]
            for country in wanted
            if country.casefold() in labels
        }
        if len(hits) > len(ids):
            param, ids = candidate, hits
    missing = [country for country in wanted if country not in ids]
    if missing and not ids:
        # Nothing resolved at all: a misspelling, a board that files
        # locations some other way, or an employer with nothing open in any
        # of them. Whichever it is, carrying on would filter nothing and
        # read as an employer with nothing open, so it stops here instead.
        raise RuntimeError(
            f"no country facet for {', '.join(missing)} on this board"
        )
    if missing:
        # A board only offers the countries it currently has a posting in, so
        # one going quiet is ordinary and must not take the whole employer
        # down with it. Said out loud, because a country that silently stops
        # being read looks exactly like a country with nothing open.
        print(f"  [i] no posting in {', '.join(missing)} on this board today")
    return param, ids


def _workday_facet_values(facets):
    """Walk a facet tree and yield (parameter, label, id) for every value.

    Locations sit one level down, under a group that carries no values of its
    own, and the level a tenant nests them at varies, so the tree is walked
    rather than indexed.
    """
    for facet in facets or []:
        param = facet.get("facetParameter")
        values = facet.get("values") or []
        groups = [
            value for value in values
            if isinstance(value, dict) and value.get("facetParameter")
        ]
        if groups:
            yield from _workday_facet_values(groups)
            continue
        for value in values:
            yield param, value.get("descriptor"), value.get("id")


def _workday_job(
    company: str, tenant: str, dc: str, site: str, posting: dict, country: str
) -> dict:
    """One listing record, normalised.

    The url keeps the whole externalPath. The req id alone answers 404, and
    so does the path with any other title, so neither can be trimmed away.
    That is safe here for the reason it was not on Teamtailor: Workday freezes
    the path at creation and a rename does not move it. Measured on a live
    posting whose listed title had gained three words the path never took, so
    the address the memory holds survives an employer editing the wording.
    """
    where = (posting.get("locationsText") or "").strip()
    return {
        "company": company,
        "title": posting.get("title", ""),
        # A posting open in several places names none of them, so the country
        # it was found under is the only location on offer, and it beats a
        # count of places that no scoring table can read.
        "location": country if country and WD_MANY_PLACES.match(where)
        else where,
        "url": (
            f"https://{tenant}.{dc}.myworkdayjobs.com/{site}"
            f"{posting.get('externalPath', '')}"
        ),
        "content": "",
    }


# Collective (collective.work) is a freelance marketplace, not an ATS, so its
# "board" is a search rather than an employer: the public search page renders
# its results server side and ships them in the page's __NEXT_DATA__, the
# same way its own front end receives them.
COLLECTIVE_SEARCH = "https://www.collective.work/jobs/fr"
COLLECTIVE_POSTING = "https://www.collective.work/jobs/fr/"
# The search reports `total` as 1500 whenever there is more (labelled "2 000+"
# on the page), exactly like Workday's cap: a query that wide cannot be read
# whole, so it is refused rather than silently cut. Page 51 of such a query
# answers 307, so the cap is also the last page the site will serve.
COLLECTIVE_CAP = 1500
# Seconds between two pages. A narrowed search is a handful of pages, but
# they are full HTML pages of a few hundred kilobytes, not an API.
COLLECTIVE_PACE = 0.5
# The URL options the search honours, measured one by one. `sort` is not among
# them, and neither is a page size: the site serves 30 per page, and nothing
# in the request can hold it there.
COLLECTIVE_OPTIONS = ("contractType", "hasDailyRate", "exclusive")
# Postings the walk may miss before it is treated as broken rather than as
# relevance noise. The order is relevance only and reshuffles ties between two
# identical requests, so a posting can slide across a page boundary between two
# pages of the same walk: measured once in seven live walks, one posting of 53.
# A second walk usually recovers it. A shortfall larger than this is not noise
# but a site change (a smaller page, `page` ignored) and fails the target.
COLLECTIVE_SLACK = 0.05
# Smallest page the site could plausibly serve, used only to bound the walk at
# the cap: never more than 150 requests, even if the page shrank to 10 and the
# checks in the walk were somehow defeated.
COLLECTIVE_PAGE_BOUND = 10
# How the listing names a posting's work mode, in the words the email cards
# use, so the same [scoring.location] keys read both routes.
COLLECTIVE_MODES = {
    "REMOTE": "À distance",
    "HYBRID": "Hybride",
    "ON_SITE": "Sur site",
}


def fetch_collective(
    company: str, slug: str, content: bool = False
) -> list[dict]:
    """Postings from one public Collective search, page by page.

    The slug is the search text, optionally followed by the options the site
    honours plus one of our own, the way a Workday slug carries its countries:
    "chef de projet data?contractType=Freelance&days=60". `days` drops what was
    published longer ago than that. The search is ordered by relevance and has
    no date sort, so it serves missions from months ago next to this morning's.

    The employer of each posting is the one the posting names, not the target
    name: one search spans hundreds of companies, most of them agencies
    placing a mission for a client they do not name.

    The listing carries the full description, like Greenhouse with content,
    so there is nothing left to fetch under --llm.

    The walk is bounded by the count, never by the shape of a page. Nothing in
    the request fixes the page size, so "a short page is the end" would read a
    site that moved to 20 per page as a search with 20 results, every run. The
    site serves `total` on every page, so the walk collects until it holds
    that many distinct postings, and a page that adds nothing new ends it
    (past the end the site answers an empty page; with `page` ignored it
    answers page one again). What is still missing then gets one more walk,
    and a shortfall beyond COLLECTIVE_SLACK fails the target out loud.
    """
    search, options, days = _collective_parts(slug)
    params = {"search": search, **options}
    projects: dict[str, dict] = {}
    total = 0
    for attempt in (1, 2):
        # Newness is judged within one walk: a second walk starts over from
        # page one, which the first walk already holds entirely.
        this_walk: set[str] = set()
        page = 1
        while True:
            results, echoed = _collective_search_page({**params, "page": page})
            if attempt == 1 and page == 1:
                _collective_check_echo(echoed, search, options)
                total = _collective_total(results)
            found = results.get("projects")
            if not isinstance(found, list):
                raise RuntimeError("search results no longer list projects")
            fresh = 0
            for project in found:
                key = project.get("id") or project.get("slug")
                if not key or key in this_walk:
                    continue
                this_walk.add(key)
                fresh += 1
                projects.setdefault(key, project)
            if not fresh or len(projects) >= total:
                break
            if page * COLLECTIVE_PAGE_BOUND >= COLLECTIVE_CAP:
                break
            page += 1
            time.sleep(COLLECTIVE_PACE)
        if len(projects) >= total:
            break
        time.sleep(COLLECTIVE_PACE)
    missing = total - len(projects)
    if missing > total * COLLECTIVE_SLACK:
        raise RuntimeError(
            f"search reports {total} postings and served {len(projects)}; "
            "its paging changed (page size, or the page parameter ignored)"
        )
    if missing > 0:
        # Relevance noise, not a fault: the posting is not in seen.json, so
        # the next run offers it again. Said out loud all the same.
        print(f"  [i] {company}: served {len(projects)} of {total}, "
              f"{missing} left for the next run")
    usable = [p for p in projects.values() if p.get("slug") and p.get("name")]
    if projects and not usable:
        # Every posting lacks a slug or a title: the site renamed a field,
        # and an empty list here would read as a quiet search.
        raise RuntimeError("search postings no longer carry a slug or name")
    oldest = (
        (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=days))
        .strftime("%Y-%m-%dT%H:%M:%S")
        if days else ""
    )
    jobs: dict[str, dict] = {}
    for project in usable:
        published = project.get("publishedAt") or ""
        # A posting with no date is kept: dropping it would read as "too
        # old", and a renamed field would then empty the whole target.
        if oldest and published and published < oldest:
            continue
        job = _collective_job(company, project, content)
        jobs.setdefault(job["url"], job)
    return list(jobs.values())


def _collective_total(results: dict) -> int:
    """The posting count page one reports, refusing a search over the cap.

    Required, not defaulted: without it the cap check and the completeness
    check both go blind, and a too-wide search would be cut at 1500 silently.
    """
    total = (results.get("pagination") or {}).get("total")
    if not isinstance(total, int):
        raise RuntimeError("search results no longer report a total")
    if total >= COLLECTIVE_CAP:
        raise RuntimeError(
            f"search reports {total} postings, the most it will serve; "
            "narrow it (contractType=..., a longer search) or the rest is "
            "dropped without a word"
        )
    return total


def _collective_parts(slug: str) -> tuple[str, dict, int]:
    """Split "search text?opt=value&days=60" into search, options, days."""
    search, _, query = slug.partition("?")
    # NFC because the site echoes the exact code points it received: text
    # pasted in decomposed form would pass the echo check and match nothing.
    search = unicodedata.normalize("NFC", " ".join(search.split()))
    if not search:
        # An empty search is the site's default listing, every posting it
        # holds. That is never a watch, only a typo.
        raise ValueError("collective slug needs search text before '?'")
    options: dict[str, str] = {}
    days = 0
    for pair in filter(None, query.split("&")):
        key, _, value = pair.partition("=")
        key, value = key.strip(), value.strip()
        if key == "days":
            days = int(value)
            if days < 0:
                # A cutoff in the future would drop every posting in silence.
                raise ValueError(f"collective days= must be positive, got {days}")
        elif key in COLLECTIVE_OPTIONS:
            options[key] = value
        else:
            raise ValueError(f"unknown collective slug option {key!r}")
    return search, options, days


def _collective_search_page(params: dict) -> tuple[dict, dict]:
    """One results page and the query the site says it ran."""
    data = _collective_next_data(
        f"{COLLECTIVE_SEARCH}?{urlencode(params)}"
    )
    for query in (data.get("dehydratedState") or {}).get("queries") or []:
        key = query.get("queryKey") or []
        if key and key[0] == "PublicPages_SearchJobs":
            echoed = (key[1] if len(key) > 1 else {}).get("data") or {}
            results = ((query.get("state") or {}).get("data") or {}).get(
                "results"
            ) or {}
            return results, echoed
    raise RuntimeError("search page no longer carries its results")


def _collective_check_echo(echoed: dict, search: str, options: dict) -> None:
    """Stop when the site ran a different query from the one asked for.

    The page answers 200 whatever the URL says, and a parameter it does not
    understand is dropped without a word: asked with ?query= instead of
    ?search=, it serves its default listing, every posting it holds. That
    reads exactly like a busy search, so the query the site echoes back is
    compared with ours before a single posting is kept.
    """
    if echoed.get("isDefaultSearch") or (
        (echoed.get("query") or "").casefold() != search.casefold()
    ):
        raise RuntimeError(
            f"site ignored the search {search!r}; its URL parameters changed"
        )
    for key, value in options.items():
        if str(echoed.get(key)).casefold() != value.casefold():
            raise RuntimeError(
                f"site ignored {key}={value} (ran {key}={echoed.get(key)!r})"
            )


def _collective_next_data(url: str) -> dict:
    """The pageProps a public Collective page was rendered with.

    Redirects are not followed: a posting address that names nothing answers
    307 to the search page rather than 404, and following it would read a
    listing where a posting was expected.
    """
    response = requests.get(
        url, headers=HEADERS, timeout=TIMEOUT, allow_redirects=False
    )
    response.raise_for_status()
    if response.status_code != 200:
        raise RuntimeError(f"HTTP {response.status_code} for {url}")
    match = re.search(
        r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', response.text, re.S
    )
    if not match:
        raise RuntimeError(f"no page data in {url}")
    return (json.loads(match.group(1)).get("props") or {}).get(
        "pageProps"
    ) or {}


def _collective_job(company: str, project: dict, content: bool) -> dict:
    """One search result, normalised.

    The url is the public posting page, never the app.collective.work one:
    that address is built per member, so it would name the mailbox owner in
    the memory the repository publishes. The slug ends in a short code and a
    wrong or truncated slug answers 307, so the address cannot drift silently.
    """
    place = project.get("location") or {}
    where = place.get("fullNameFrench") or place.get("fullNameEnglish") or ""
    modes = project.get("workPreferences") or []
    # One mode only, the way a LinkedIn card writes it: a posting offering
    # every mode says nothing about any of them.
    if len(modes) == 1 and modes[0] in COLLECTIVE_MODES:
        mode = COLLECTIVE_MODES[modes[0]]
        where = f"{where} ({mode})" if where else mode
    text = ""
    if content:
        # The day rate and the contract type are the two facts a freelance
        # mission is judged on first, and the description often omits both.
        facts = [
            f"Daily rate: {project['budgetBrief']}"
            if project.get("budgetBrief") else "",
            "Contract: " + ", ".join(project["contractTypes"])
            if project.get("contractTypes") else "",
            f"Published: {(project.get('publishedAt') or '')[:10]}",
        ]
        text = "\n".join(
            [fact for fact in facts if fact]
            + [_plain_text(project.get("description", ""))]
        )
    return {
        "company": (project.get("company") or {}).get("name") or company,
        "title": project.get("name", ""),
        "location": where,
        "url": COLLECTIVE_POSTING + (project.get("slug") or ""),
        "content": text,
    }


FETCHERS = {
    "greenhouse": fetch_greenhouse,
    "lever": fetch_lever,
    "ashby": fetch_ashby,
    "teamtailor": fetch_teamtailor,
    "smartrecruiters": fetch_smartrecruiters,
    "workday": fetch_workday,
    "collective": fetch_collective,
}


def fetch_linkedin_description(url: str) -> str:
    """Full text of a LinkedIn posting, or "" when it cannot be read.

    An ATS hands over the description with the listing; an email alert hands
    over a title and a link, so without this the qualitative pass would judge
    half of a mailbox-driven profile on its titles alone. LinkedIn serves the
    posting to logged-out visitors at the endpoint below, which we call under
    this project's own user agent, once per finalist, on demand and never in
    CI. Indeed answers 401 to the same request and stays title-only.
    """
    match = re.search(r"linkedin\.com/jobs/view/(\d+)", url)
    if not match:
        return ""
    guest = (
        "https://www.linkedin.com/jobs-guest/jobs/api/jobPosting/"
        f"{match.group(1)}"
    )
    response = requests.get(guest, headers=HEADERS, timeout=TIMEOUT)
    response.raise_for_status()
    body = BeautifulSoup(response.text, "html.parser").select_one(
        "div.show-more-less-html__markup, div.description__text"
    )
    return body.get_text("\n", strip=True) if body else ""


def fetch_smartrecruiters_description(url: str) -> str:
    """Full text of a SmartRecruiters posting, or "" for any other url.

    The sections are a company blurb, the job itself, the qualifications and
    the practicalities; the model wants all four, so they are flattened in the
    order the board lists them.
    """
    match = re.search(r"jobs\.smartrecruiters\.com/([^/]+)/(\d+)", url)
    if not match:
        return ""
    data = _get_json(
        "https://api.smartrecruiters.com/v1/companies/"
        f"{match.group(1)}/postings/{match.group(2)}"
    )
    sections = (data.get("jobAd") or {}).get("sections") or {}
    texts = (_plain_text((s or {}).get("text", "")) for s in sections.values())
    return "\n\n".join(text for text in texts if text)


def fetch_workday_description(url: str) -> str:
    """Full text of a Workday posting, or "" for any other url.

    The public page is a single-page app: it answers 200 for any path it is
    handed, including one that names no posting, so it can say nothing about
    whether a posting exists. The same path under /wday/cxs/ answers in JSON
    and 404s when it is wrong, which is why the description is read there.
    """
    match = re.match(
        r"https://([^.]+)\.(wd\d+)\.myworkdayjobs\.com/"
        r"(?:[a-z]{2}-[A-Z]{2}/)?([^/]+)(/job/.+)$",
        url,
    )
    if not match:
        return ""
    tenant, dc, site, path = match.groups()
    data = _get_json(
        f"https://{tenant}.{dc}.myworkdayjobs.com/wday/cxs/"
        f"{tenant}/{site}{path}"
    )
    return _plain_text((data.get("jobPostingInfo") or {}).get(
        "jobDescription", ""
    ))


def fetch_description(url: str) -> str:
    """Full text of a posting whose listing did not carry one.

    Three sources need this and they are unrelated: an email alert hands over
    a title and a link, and the SmartRecruiters and Workday boards both keep
    their descriptions one request away from the listing. An email alert is
    read at whichever site its link points to.
    """
    if "smartrecruiters.com" in url:
        return fetch_smartrecruiters_description(url)
    if "myworkdayjobs.com" in url:
        return fetch_workday_description(url)
    if url.startswith(COLLECTIVE_POSTING):
        return fetch_collective_description(url)
    return fetch_linkedin_description(url)


def fetch_collective_description(url: str) -> str:
    """Full text of a Collective posting that arrived by email.

    The search collector already carries the description; this is for the
    "new opportunity" emails, which carry a title and a link and nothing else.
    """
    project = _collective_next_data(url).get("project")
    if not project:
        # Raised, not returned empty, so fill_missing_descriptions() says so
        # instead of sending the posting to the model on its title alone.
        raise RuntimeError("posting page no longer carries its project")
    return _collective_job("", project, True)["content"]


def fill_missing_descriptions(jobs: list[dict]) -> int:
    """Fetch the description of finalists that arrived without one.

    Email-sourced and SmartRecruiters postings are the ones that get here
    empty-handed. A failure is reported and skipped: reviewing one posting on
    its title is worse than reviewing it in full, and far better than aborting
    the run.
    """
    fetched = 0
    for job in jobs:
        if job.get("content"):
            continue
        try:
            job["content"] = fetch_description(job["url"])
        except Exception as exc:  # noqa: BLE001, report and move on
            print(f"  [!] no description for {job['title'][:40]}: {exc}")
            continue
        if job["content"]:
            fetched += 1
            # One page per second. This is a courtesy read of a handful of
            # public pages, and it should stay visibly unlike a crawler.
            time.sleep(1)
    return fetched


def _plain_text(html: str) -> str:
    """Flatten an ATS description to readable text for the model."""
    return BeautifulSoup(html or "", "html.parser").get_text("\n", strip=True)


def _get_json(url: str):
    """GET a URL and return its JSON body, raising on HTTP errors."""
    response = requests.get(url, headers=HEADERS, timeout=TIMEOUT)
    response.raise_for_status()
    return response.json()


def _post_json(url: str, body: dict):
    """POST a JSON body and return the JSON answer, raising on HTTP errors.

    Only Workday needs this: it is the one board here that takes its query in
    a body rather than a query string. Its wrong-name answers are loud, 422
    for an unknown tenant and 404 for an unknown site, so raise_for_status()
    catches a typo that on SmartRecruiters would have read as an empty board.
    """
    response = requests.post(url, json=body, headers=HEADERS, timeout=TIMEOUT)
    response.raise_for_status()
    return response.json()


# --------------------------------------------------------------------------
# Filtering
# --------------------------------------------------------------------------


def matches(job: dict, include: list[str], exclude: list[str]) -> bool:
    """Keep a job if its title contains an include keyword and no exclude one."""
    title = job["title"].lower()
    if not any(keyword in title for keyword in include):
        return False
    return not any(keyword in title for keyword in exclude)


# --------------------------------------------------------------------------
# Memory: URLs already surfaced by previous runs
# (profiles/<name>/data/seen.json)
# --------------------------------------------------------------------------


def load_seen() -> set[str]:
    if SEEN_PATH.exists():
        return set(json.loads(SEEN_PATH.read_text(encoding="utf-8")))
    return set()


def save_seen(seen: set[str]) -> None:
    SEEN_PATH.parent.mkdir(exist_ok=True)
    SEEN_PATH.write_text(json.dumps(sorted(seen), indent=2), encoding="utf-8")


# The LLM pass keeps a memory of its own, and it is deliberately not seen.json.
# seen.json answers "has the digest ever surfaced this posting?", and the
# scheduled cloud run consumes it; this one answers "has the model ever READ
# this posting?", which only the local --llm pass consumes. Merging them would
# let the cloud run mark postings as reviewed without a model ever seeing them.
#
# It is never committed: .gitignore keeps every profiles/*/data/ file private
# except seen.json, and that is the right default here. A verdict names an
# employer, and a public "skip" on a company the candidate is still
# interviewing with would be read by that company.


def _free_report_path(today: str) -> Path:
    """Where today's review goes, without ever overwriting an earlier one.

    A second pass on the same day used to land on the same filename. That was
    harmless while both passes read the same top N and wrote the same thing;
    since the selection only returns postings the model has never read, two
    passes cover DISJOINT postings and the second destroyed the first. It
    happened on 15/09: 21 of the 41 postings read that day, two of them worth
    applying to and five worth digging into, kept a one-word verdict in
    reviewed.json and lost the analysis they were read for. These reports are gitignored, so there is no history to
    fall back on.
    """
    path = SEEN_PATH.parent / f"review-{today}.md"
    run = 2
    while path.exists():
        path = SEEN_PATH.parent / f"review-{today}-{run}.md"
        run += 1
    return path


def load_reviewed() -> dict[str, dict]:
    if REVIEWED_PATH.exists():
        return json.loads(REVIEWED_PATH.read_text(encoding="utf-8"))
    return {}


def save_reviewed(reviewed: dict[str, dict]) -> None:
    REVIEWED_PATH.parent.mkdir(exist_ok=True)
    REVIEWED_PATH.write_text(
        json.dumps(reviewed, indent=2, sort_keys=True, ensure_ascii=False),
        encoding="utf-8",
    )


# What you already did about a posting, which the model cannot know and keeps
# rediscovering. Left to itself it will rate a posting "apply" that you sent a
# letter for weeks ago, or that the employer has already turned down: it judges
# an advert, and this records the history that makes the advert moot.
#
# Deliberately separate from reviewed.json: "the model read this" is the
# agent's own bookkeeping, "I applied and was turned down" is a fact about you.
# Also gitignored, and for a stronger reason than the verdicts: this names
# employers and says where each conversation stands.


def load_applications() -> dict[str, dict]:
    if APPLICATIONS_PATH.exists():
        return json.loads(APPLICATIONS_PATH.read_text(encoding="utf-8"))
    return {}


def save_applications(applications: dict[str, dict]) -> None:
    APPLICATIONS_PATH.parent.mkdir(exist_ok=True)
    APPLICATIONS_PATH.write_text(
        json.dumps(applications, indent=2, sort_keys=True, ensure_ascii=False),
        encoding="utf-8",
    )


def _fold_duplicates(jobs: list[dict], reviewed: dict[str, dict],
                     applications: dict[str, dict]) -> list[dict]:
    """Keep one record per opening when it reached the run by several routes.

    An opening is not always collected once. LinkedIn and Indeed both mirror
    ATS boards, so the same job arrives as two records under two urls, and a
    site sometimes carries the same job twice over. Left alone every copy
    spends its own reading slot and comes back with its own verdict, and the
    same posting has been answered "apply" under one url and "dig" under the
    other on the same day.

    What must NOT be folded is one title opened in several places. A board
    commonly publishes one posting per location, so the same senior title
    exists in two countries at once, and a role can be open in two cities of
    the same country. Folding those would hide real openings, which is the
    opposite of the point. Hence the rule: one title and one employer are one
    posting when the copies come from different sites, one mirroring the
    other, or when they come from the same site and name the same place.
    Replayed over the postings already read, it folds eleven copies and
    leaves every multi-location group alone.

    Places are compared as sets of words, not as strings, because the same
    place is rarely spelled twice the same way: "Paris" and "Paris, Île de
    France, France" are one location written by two sites, "Bron" and
    "Versailles" are two. A copy that names no place at all is not held back
    by it, the way a card with no employer is matched on its title alone.

    The survivor is chosen, never taken at random. A url you applied to wins,
    so a file already closed stays recognised as closed instead of coming
    back under its twin's url: an application recorded weeks earlier has been
    handed back as a fresh recommendation that way. A url already read comes
    next, so no slot judges again what already has a verdict. Only then does
    the score decide, and since the list arrives sorted that keeps whichever
    location variant suits this profile best. What the survivor lacks, the copies hand
    over before they step aside, the description the model reads and the
    referral signal included.

    Scope is deliberate: this serves the reading pass only. The digest and
    seen.json keep every url they have always had, because dropping one here
    would make it look new again tomorrow.
    """
    def site(url: str) -> str:
        host = urlsplit(url).netloc.lower()
        return host[4:] if host.startswith("www.") else host

    def place(job: dict) -> frozenset:
        return frozenset(_slug(job.get("location", "")).split())

    def one_posting(first: dict, second: dict) -> bool:
        if site(first["url"]) != site(second["url"]):
            return True
        here, there = place(first), place(second)
        return here <= there or there <= here

    def standing(job: dict) -> tuple[bool, bool]:
        # False sorts first, so an applied url wins, then a read one.
        return (job["url"] not in applications, job["url"] not in reviewed)

    groups: list[list[int]] = []
    by_name: dict[tuple[str, str], list[int]] = {}
    by_title: dict[str, set[str]] = {}
    republished: list[int] = []
    for position, job in enumerate(jobs):
        title, company = _slug(job.get("title", "")), _slug(job.get("company", ""))
        if title and company in AGGREGATORS:
            # An aggregator prints its own name where the employer goes, so
            # that name is not an identity: the same posting arrives once as
            # "Jobgether" and once under the company that is actually hiring,
            # and the two never meet. Held back here and matched on the title
            # alone below, once every real employer in this run is known.
            republished.append(position)
            continue
        name = (title, company)
        if not name[0] or not name[1]:
            # No title or no employer, no identity. _fold_twins() can afford
            # to match on a title alone because it works inside one mailbox
            # and one window; here the run spans every site at once, and two
            # employers sharing a plain title would merge into one.
            groups.append([position])
            continue
        by_title.setdefault(title, set()).add(company)
        home = by_name.setdefault(name, [])
        for slot in home:
            if one_posting(jobs[groups[slot][0]], job):
                groups[slot].append(position)
                break
        else:
            home.append(len(groups))
            groups.append([position])

    for position in republished:
        owners = by_title.get(_slug(jobs[position].get("title", "")), set())
        if len(owners) != 1:
            # Nobody else carries that title, or several employers do and
            # there is no telling which one this card republishes. Measured on
            # 39 aggregator cards: six share a title with a board, five name a
            # single employer, and the sixth is "Account Executive", carried by
            # two. Folding that one would hide a real opening.
            groups.append([position])
            continue
        name = (_slug(jobs[position].get("title", "")), next(iter(owners)))
        groups[by_name[name][0]].append(position)

    kept: set[int] = set()
    for group in groups:
        keeper = min(group, key=lambda position: standing(jobs[position]))
        for position in group:
            if position == keeper:
                continue
            # "content" is the description the model reads, and carrying it
            # over also spares the survivor a LinkedIn fetch it no longer
            # needs. "network" is the referral signal, which only the email
            # copy of a posting ever has.
            for field in ("content", "network", "source", "location", "company"):
                if not jobs[keeper].get(field) and jobs[position].get(field):
                    jobs[keeper][field] = jobs[position][field]
        kept.add(keeper)
    return [job for position, job in enumerate(jobs) if position in kept]


def record_application(url: str, status: str, note: str, date: str = "",
                       force: bool = False) -> int:
    """Write one application down and stop. Touches no board and no mailbox:
    recording a rejection must not depend on the network being up.

    `date` exists for backfilling: stamping an application sent three weeks ago
    with today's date would put a false fact in a memory file, and memory files
    get trusted later precisely because nobody re-checks them.

    Every check happens BEFORE the write. The order used to be reversed, so the
    warning about an unknown url announced an entry that was already on disk,
    with no command to take it back, while a recorded posting leaves the reading
    slots for good (--recheck deliberately does not bring it back)."""
    if status == UNDO_STATUS:
        applications = load_applications()
        if applications.pop(url, None) is None:
            print(f"[applications] nothing recorded for {url}")
            return 1
        save_applications(applications)
        print(f"[applications] forgotten: {url}")
        return 0
    if date:
        try:
            date = dt.date.fromisoformat(date).isoformat()
        except ValueError:
            print(f"[applications] --date must be YYYY-MM-DD, got {date!r}")
            return 1
    # A url no run has ever collected is usually a typo, or one copied from the
    # browser bar rather than from a digest. It would filter nothing, so it is
    # refused rather than recorded. Both memories count: the message below
    # names a review report, and a report url reaches reviewed.json first.
    if not force and url not in load_seen() | set(load_reviewed()):
        print("[applications] this url has never been collected by a run, so "
              "it would filter nothing. Nothing was written.")
        print("  Copy it from a digest or a review report, or pass --force to "
              "record it anyway.")
        return 1
    applications = load_applications()
    previous = applications.get(url, {})
    applications[url] = {
        "status": status,
        "date": date or dt.datetime.now(ZoneInfo("Europe/Paris")).date().isoformat(),
        "note": note or previous.get("note", ""),
    }
    save_applications(applications)
    was = f" (was {previous['status']})" if previous.get("status") else ""
    print(f"[applications] {status}{was}: {url}")
    print(f"  Undo with --applied {url} --status {UNDO_STATUS}")
    return 0


def show_applications() -> int:
    applications = load_applications()
    if not applications:
        print("[applications] none recorded yet "
              "(use --applied <url> --status <status>)")
        return 0
    by_status: dict[str, list[tuple[str, dict]]] = {}
    for url, entry in applications.items():
        by_status.setdefault(entry.get("status", "?"), []).append((url, entry))
    print(f"[applications] {len(applications)} recorded")
    for status in STATUSES:
        rows = by_status.get(status, [])
        for url, entry in sorted(rows, key=lambda row: row[1].get("date", "")):
            note = f"  {entry['note']}" if entry.get("note") else ""
            print(f"  {status:<13} {entry.get('date', '?')}  {url}{note}")
    return 0


# --------------------------------------------------------------------------
# Digest
# --------------------------------------------------------------------------


def _context(job: dict) -> str:
    """Trailing ' · location · referral signal' for a digest line."""
    return "".join(
        f" · {bit}" for bit in (job.get("location"), job.get("network")) if bit
    )


def write_digest(jobs: list[dict], errors: list[str], stats: dict) -> Path:
    """jobs must arrive scored (job["score"], job["why"]) and sorted."""
    today = dt.datetime.now(ZoneInfo("Europe/Paris")).date().isoformat()
    DIGEST_DIR.mkdir(exist_ok=True)
    path = DIGEST_DIR / f"{today}.md"

    summary = " ".join(
        [
            f"{len(jobs)} new matching position(s) out of",
            f"{stats['jobs_total']} postings scanned across",
            f"{stats['companies_ok']}/{stats['companies_total']} companies",
            "and email alerts;",
            f"{stats['already_seen']} matching position(s)",
            "already surfaced by previous runs.",
        ]
    )

    lines = [
        f"# Job digest {today}",
        "",
        summary,
        "",
    ]

    top = jobs[:10]
    if top:
        lines += ["## 🥇 Top 3", ""]
        for rank, job in enumerate(top[:3], start=1):
            where = _context(job)
            lines += [
                f"### {rank}. [{job['title']}]({job['url']}) "
                f"· {job['company']}{where}",
                f"**Score {job['score']}** : "
                f"{' · '.join(job['why']) or 'no rule matched'}",
                "",
            ]
        lines += ["## Top 10", ""]
        for rank, job in enumerate(top, start=1):
            lines.append(
                f"{rank}. ({job['score']}) [{job['title']}]({job['url']}) "
                f"· {job['company']}"
            )
        lines.append("")

    rest = jobs[10:]
    if rest:
        lines += [f"## Autres nouveautés ({len(rest)})", ""]
        current_company = None
        for job in sorted(rest, key=lambda j: (j["company"], j["title"])):
            if job["company"] != current_company:
                current_company = job["company"]
                lines += [f"### {current_company}", ""]
            lines.append(f"- [{job['title']}]({job['url']}){_context(job)}")
        lines.append("")

    if errors:
        lines += ["## Collection errors", ""]
        lines += [f"- {error}" for error in errors]

    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


# --------------------------------------------------------------------------
# Main
# --------------------------------------------------------------------------


def main() -> int:
    # Windows defaults stdout to cp1252, which raises UnicodeEncodeError on
    # any character outside it. Employer names routinely carry emoji, and an
    # emoji in a listed company name crashed --dry-run after the mailbox had
    # already been read, losing the whole run to a print statement. Printing
    # is never worth failing a run for: replace what cannot be encoded and
    # carry on.
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")

    parser = argparse.ArgumentParser(description="Collect and report job postings.")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="collect and filter only: no digest written, memory and mailbox "
        "left untouched, no mail sent",
    )
    parser.add_argument(
        "--llm",
        action="store_true",
        help="local qualitative pass: read the finalists' full descriptions, "
        "write data/review-<date>.md, touch no shared state",
    )
    parser.add_argument(
        "--top",
        type=int,
        metavar="N",
        help="with --llm: review the N best-scoring open postings instead of "
        "[llm].top_n. Use it for a one-off sweep of the backlog",
    )
    parser.add_argument(
        "--recheck",
        action="store_true",
        help="with --llm: also review postings the model has already read. "
        "Use it when the profile changed, not as a routine. It does NOT bring "
        "back postings you applied to: an application is a fact, not a verdict",
    )
    parser.add_argument(
        "--applied",
        metavar="URL",
        help="record that you applied to this posting and stop. Copy the url "
        "from a digest or a review report. Postings recorded here are never "
        "sent to the model again, and are listed at the top of the report",
    )
    parser.add_argument(
        "--status",
        choices=(*STATUSES, UNDO_STATUS),
        default="applied",
        help="with --applied: where the file stands (default: applied). "
        "Use 'forget' to remove an entry recorded on the wrong url",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="with --applied: record a url no run has collected, instead of "
        "refusing it as a probable typo",
    )
    parser.add_argument(
        "--note",
        default="",
        metavar="TEXT",
        help="with --applied: a short reminder, e.g. the recruiter's name",
    )
    parser.add_argument(
        "--date",
        default="",
        metavar="YYYY-MM-DD",
        help="with --applied: when it happened, for backfilling old files "
        "(default: today)",
    )
    parser.add_argument(
        "--applications",
        action="store_true",
        help="list what you have applied to, grouped by status, and stop",
    )
    parser.add_argument(
        "--profile",
        metavar="NAME",
        help="run for another person: read everything from profiles/NAME/ "
        "(config.toml, .env, profile.md) and write its memory and digests "
        "there. Omit it to run the default profile at the repository root",
    )
    args = parser.parse_args()

    # --dry-run promises that nothing is written, and that promise is printed
    # in the README. Two combinations broke it in silence: --llm is tested
    # before the dry-run branch is ever reached, so it wrote its report and
    # data/reviewed.json right after announcing that memory was untouched, and
    # --applied writes before the collection even starts. Refusing beats
    # half-writing, since both commands exist in order to leave a trace.
    if args.dry_run and (args.llm or args.applied):
        writes = ("--llm", "a report and data/reviewed.json") if args.llm \
            else ("--applied", "data/applications.json")
        print(f"[dry run] {writes[0]} exists to write {writes[1]}, so it "
              f"cannot be combined with --dry-run. Run {writes[0]} on its own.")
        return 1
    # "limit" ends up in a slice, where a negative value quietly keeps almost
    # the whole backlog (pending[:-1]) instead of failing, and 0 is swallowed
    # by the `or` that reads top_n.
    if args.top is not None and args.top < 1:
        print(f"[llm] --top must be 1 or more, got {args.top}.")
        return 1

    # A profile is one person's whole setup (config, secrets, memory, digests,
    # candidate profile) under profiles/<name>/. profiles/ is gitignored except
    # for the repository owner's, so nobody else's search criteria can reach
    # this public repository. Every run needs one: there is no default profile
    # at the repository root, and the guard below says so instead of raising.
    global BASE, DIGEST_DIR, SEEN_PATH, REVIEWED_PATH, APPLICATIONS_PATH
    if args.profile:
        BASE = ROOT / "profiles" / args.profile
        if not BASE.is_dir():
            print(f"[profile] {BASE.relative_to(ROOT)} does not exist. "
                  f"Copy profiles.example/someone/ to create it.")
            return 1
        DIGEST_DIR = BASE / "digests"
        SEEN_PATH = BASE / "data" / "seen.json"
        REVIEWED_PATH = BASE / "data" / "reviewed.json"
        APPLICATIONS_PATH = BASE / "data" / "applications.json"
        print(f"[profile] {args.profile}")

    # A profile reads its OWN .env and never falls back to the root one. The
    # fallback looks harmless and is not: a profile without credentials would
    # silently open someone else's mailbox and mail them someone else's digest.
    # Missing credentials are reported by the email collector and the run
    # continues on its ATS targets.
    load_dotenv(BASE / ".env")

    config_path = BASE / "config.toml"
    if not config_path.is_file():
        print(f"[config] {config_path.relative_to(ROOT)} not found. Every run "
              f"needs a profile: copy profiles.example/someone/ to "
              f"profiles/<name>/ and pass --profile <name>.")
        return 1
    config = tomllib.loads(config_path.read_text(encoding="utf-8"))

    # Bookkeeping commands stop here, before any board or mailbox is touched.
    # They sit after the profile guard above so they write to the right person's
    # file, and before the collection so recording a rejection works offline.
    if args.applied:
        return record_application(args.applied, args.status, args.note,
                                  args.date, args.force)
    if args.applications:
        return show_applications()

    # Fail before the collection, not after: --llm downloads full descriptions,
    # so an unusable configuration must stop the run immediately.
    llm_cfg = config.get("llm", {})
    if args.llm:
        reason = unavailable(llm_cfg, BASE)
        if reason:
            print(f"[llm] cannot run: {reason}")
            return 1

    include = [k.lower() for k in config["search"]["include_keywords"]]
    exclude = [k.lower() for k in config["search"]["exclude_keywords"]]

    kept: list[dict] = []
    errors: list[str] = []
    jobs_total = 0
    companies_ok = 0

    for target in config["companies"]:
        name, ats, slug = target["name"], target["ats"], target["slug"]
        try:
            jobs = FETCHERS[ats](name, slug, args.llm)
        except Exception as exc:  # noqa: BLE001, report and move on
            errors.append(f"{name} ({ats}/{slug}): {exc}")
            print(f"  [!] {name}: {exc}")
            continue
        companies_ok += 1
        jobs_total += len(jobs)
        matching = [job for job in jobs if matches(job, include, exclude)]
        kept.extend(matching)
        print(f"  [+] {name}: {len(jobs)} postings, {len(matching)} matching")

    # ---- Email alerts (dedicated mailbox: LinkedIn, Indeed, ...) --------
    email_cfg = config.get("email", {})
    if email_cfg.get("enabled"):
        user = os.environ.get("JOBWATCH_EMAIL_USER")
        password = os.environ.get("JOBWATCH_EMAIL_PASSWORD")
        if not (user and password):
            errors.append(
                "email alerts: credentials missing in .env "
                "(JOBWATCH_EMAIL_USER / JOBWATCH_EMAIL_PASSWORD)"
            )
            print("  [!] email alerts: credentials missing in .env")
        else:
            try:
                mail_jobs = fetch_email_jobs(email_cfg, user, password)
            except Exception as exc:  # noqa: BLE001, report and move on
                errors.append(f"email alerts: {exc}")
                print(f"  [!] email alerts: {exc}")
            else:
                jobs_total += len(mail_jobs)
                matching = [j for j in mail_jobs if matches(j, include, exclude)]
                kept.extend(matching)
                print(
                    f"  [+] email alerts: {len(mail_jobs)} job links, "
                    f"{len(matching)} matching"
                )

    # One record per url. Until the Collective search, no two sources could
    # hand over the same url: a board is one employer, and the mailbox dedupes
    # itself. A Collective email now points at the posting the search already
    # returned, and two overlapping searches return the same posting twice.
    # The first record wins, and boards are collected before the mailbox, so
    # the one that names the employer and carries the description survives.
    unique: dict[str, dict] = {}
    for job in kept:
        unique.setdefault(job["url"], job)
    kept = list(unique.values())

    # ---- Memory: only surface what previous runs have not shown ---------
    seen = load_seen()
    new_jobs = [job for job in kept if job["url"] not in seen]

    # ---- Scoring: transparent ranking, best first ------------------------
    scoring_cfg = config.get("scoring", {})
    for job in new_jobs:
        job["score"], job["why"] = score_job(job, scoring_cfg)
    new_jobs.sort(key=lambda job: job["score"], reverse=True)

    stats = {
        "companies_total": len(config["companies"]),
        "companies_ok": companies_ok,
        "jobs_total": jobs_total,
        "already_seen": len(kept) - len(new_jobs),
    }
    # --llm stops here: it reviews what is OPEN, not only what is new, since
    # the scheduled cloud run has already consumed "new". It writes a local
    # report and touches no shared state.
    if args.llm:
        for job in kept:
            job["score"], job["why"] = score_job(job, scoring_cfg)
        kept.sort(key=lambda job: job["score"], reverse=True)
        limit = args.top or llm_cfg.get("top_n", 10)
        reviewed = load_reviewed()
        applications = load_applications()
        # One opening, one record. The same job reaches the run by several
        # routes and each copy would otherwise spend its own slot and come
        # back with its own verdict.
        copies = len(kept)
        kept = _fold_duplicates(kept, reviewed, applications)
        copies -= len(kept)
        # Spend the reading slots on what has NOT been read yet. The ranking
        # alone cannot do this: a target employer is worth up to +30, so its
        # whole board outranks any unknown company every single day, and the
        # same postings won the slots over and over (nine of them took 81 of
        # the first 146 reads). Two genuinely better postings were never read
        # because they scored 60 and 55 behind that wall.
        # Nothing disappears from the ranking: the ones already judged are
        # listed under the report with the verdict they got, and --recheck
        # sends them back to the model when the profile has moved.
        # A posting you already applied to leaves the running entirely, and
        # --recheck does not bring it back: an application is a fact, not a
        # verdict to revise. Without this the model keeps recommending closed
        # files, having no way to know they are closed.
        live = [job for job in kept if job["url"] not in applications]
        pending = [job for job in live if job["url"] not in reviewed]
        top = (live if args.recheck else pending)[:limit]
        chosen = {job["url"] for job in top}
        previously = [job for job in live[:limit]
                      if job["url"] in reviewed and job["url"] not in chosen]
        applied = [job for job in kept if job["url"] in applications]
        print(f"\n[llm] reading {len(top)} full description(s): "
              f"{len(pending)} never reviewed, {len(kept)} open in total"
              + (", --recheck on" if args.recheck else "") + "...")
        if copies:
            print(f"  {copies} duplicate record(s) folded into the posting "
                  f"they copy (another site, or the same one twice).")
        if previously:
            print(f"  {len(previously)} already-reviewed posting(s) skipped "
                  f"(they held these slots before; --recheck to re-read).")
        if applied:
            print(f"  {len(applied)} posting(s) you already applied to skipped "
                  f"(listed at the top of the report).")
        fetched = fill_missing_descriptions(top)
        if fetched:
            print(f"  {fetched} of them read from LinkedIn "
                  f"(email alerts carry no description).")
        verdicts = review(top, llm_cfg, load_profile(llm_cfg, BASE))
        today = dt.datetime.now(ZoneInfo("Europe/Paris")).date().isoformat()
        path = _free_report_path(today)
        path.parent.mkdir(exist_ok=True)
        path.write_text(
            render(top, verdicts, today, previously, reviewed,
                   applied, applications),
            encoding="utf-8")
        print(f"Review written to {path.relative_to(ROOT)} "
              f"(gitignored; seen.json and digests untouched)")
        # Ordered by irreversibility, like save_seen() after write_digest():
        # marking a posting as read is what stops it from being read again, so
        # it happens only once the report exists on disk. And only postings the
        # model actually answered for are marked: a failed batch returns no
        # verdict (it happened on 04/09, one verdict out of ten), and recording
        # those would bury them for good. A url the model did not copy verbatim
        # matches no job here and is skipped for the same reason.
        by_url = {job["url"]: job for job in top}
        for url, verdict in verdicts.items():
            job = by_url.get(url)
            if job is None:
                continue
            reviewed[url] = {
                "date": today,
                "verdict": verdict.get("verdict", ""),
                "company": job.get("company", ""),
                "title": job.get("title", ""),
            }
        save_reviewed(reviewed)
    # A dry run stops here: it has read the boards and the mailbox, but must
    # not remember anything, write a digest, or send mail.
    elif args.dry_run:
        print("\n[dry run] nothing written, nothing sent. Would surface:")
        for job in sorted(new_jobs, key=lambda j: (j["company"], j["title"])):
            print(f"  - {job['company']} · {job['title']}")
    elif new_jobs:
        # Order matters: marking a URL as seen is irreversible, so the memory
        # is only saved once the digest is safely on disk. A crash in
        # write_digest() leaves the postings for the next run.
        path = write_digest(new_jobs, errors, stats)
        print(f"\nDigest written to {path.relative_to(ROOT)}")
        save_seen(seen | {job["url"] for job in kept})
        # Notification comes last: the digest file is the durable record,
        # mailing is best-effort and must not re-surface the postings.
        if config.get("notify", {}).get("enabled"):
            try:
                send_digest(
                    f"Job digest {dt.datetime.now(ZoneInfo('Europe/Paris')).date().isoformat()}: "
                    f"{len(new_jobs)} new position(s)",
                    path.read_text(encoding="utf-8"),
                )
                print("Digest sent by email.")
            except Exception as exc:  # noqa: BLE001, notification is best-effort
                print(f"  [!] email notification failed: {exc}")
    else:
        print("\nNothing new, no digest written (previous one kept).")
    print(
        f"{len(new_jobs)} new matching position(s) "
        f"({stats['already_seen']} already seen) out of {jobs_total} scanned."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
