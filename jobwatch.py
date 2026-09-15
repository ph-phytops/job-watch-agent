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
from pathlib import Path
from zoneinfo import ZoneInfo

import requests
from bs4 import BeautifulSoup
from dotenv import load_dotenv

from email_collector import fetch_email_jobs
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


FETCHERS = {
    "greenhouse": fetch_greenhouse,
    "lever": fetch_lever,
    "ashby": fetch_ashby,
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


def fill_missing_descriptions(jobs: list[dict]) -> int:
    """Fetch the description of finalists that arrived without one.

    In practice only email-sourced postings get here empty-handed. A failure is
    reported and skipped: reviewing one posting on its title is worse than
    reviewing it in full, and far better than aborting the run.
    """
    fetched = 0
    for job in jobs:
        if job.get("content"):
            continue
        try:
            job["content"] = fetch_linkedin_description(job["url"])
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
    happened on 15/09: 17 of the 41 postings read that day, four of them worth
    digging into, kept a one-word verdict in reviewed.json and lost the analysis
    they were read for. These reports are gitignored, so there is no history to
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
        applications = load_applications()
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
