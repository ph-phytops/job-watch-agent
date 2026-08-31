"""job-watch-agent v0.1 — collect job postings from public ATS APIs.

Pipeline: collect -> normalise -> filter -> digest.

Companies and search keywords live in config.toml. For each company we call
the public job-board API of its ATS (Greenhouse, Lever or Ashby), reduce
every posting to one common shape, keep the ones matching the search
keywords, and write a Markdown digest in digests/.

Usage:
    python jobwatch.py
"""

from __future__ import annotations

import datetime as dt
import json
import os
import sys
import tomllib
from pathlib import Path

import requests
from dotenv import load_dotenv

from email_collector import fetch_email_jobs
from notifier import send_digest
from scoring import score_job

ROOT = Path(__file__).parent
DIGEST_DIR = ROOT / "digests"
SEEN_PATH = ROOT / "data" / "seen.json"
TIMEOUT = 20
HEADERS = {
    "User-Agent": "job-watch-agent/0.1 (personal project; "
    "github.com/ph-phytops/job-watch-agent)"
}

# --------------------------------------------------------------------------
# Collectors — one per ATS. Each returns a list of "normalised" jobs:
# {"company", "title", "location", "url"}
# --------------------------------------------------------------------------


def fetch_greenhouse(company: str, slug: str) -> list[dict]:
    url = f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs"
    data = _get_json(url)
    return [
        {
            "company": company,
            "title": job.get("title", ""),
            "location": (job.get("location") or {}).get("name", ""),
            "url": job.get("absolute_url", ""),
        }
        for job in data.get("jobs", [])
    ]


def fetch_lever(company: str, slug: str) -> list[dict]:
    url = f"https://api.lever.co/v0/postings/{slug}?mode=json"
    data = _get_json(url)
    return [
        {
            "company": company,
            "title": job.get("text", ""),
            "location": (job.get("categories") or {}).get("location", ""),
            "url": job.get("hostedUrl", ""),
        }
        for job in data
    ]


def fetch_ashby(company: str, slug: str) -> list[dict]:
    url = f"https://api.ashbyhq.com/posting-api/job-board/{slug}"
    data = _get_json(url)
    return [
        {
            "company": company,
            "title": job.get("title", ""),
            "location": job.get("location", ""),
            "url": job.get("jobUrl", ""),
        }
        for job in data.get("jobs", [])
    ]


FETCHERS = {
    "greenhouse": fetch_greenhouse,
    "lever": fetch_lever,
    "ashby": fetch_ashby,
}


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
    if any(keyword in title for keyword in exclude):
        return False
    return True


# --------------------------------------------------------------------------
# Memory — URLs already surfaced by previous runs (data/seen.json, gitignored)
# --------------------------------------------------------------------------


def load_seen() -> set[str]:
    if SEEN_PATH.exists():
        return set(json.loads(SEEN_PATH.read_text(encoding="utf-8")))
    return set()


def save_seen(seen: set[str]) -> None:
    SEEN_PATH.parent.mkdir(exist_ok=True)
    SEEN_PATH.write_text(json.dumps(sorted(seen), indent=2), encoding="utf-8")


# --------------------------------------------------------------------------
# Digest
# --------------------------------------------------------------------------


def write_digest(jobs: list[dict], errors: list[str], stats: dict) -> Path:
    """jobs must arrive scored (job["score"], job["why"]) and sorted."""
    today = dt.date.today().isoformat()
    DIGEST_DIR.mkdir(exist_ok=True)
    path = DIGEST_DIR / f"{today}.md"

    lines = [
        f"# Job digest — {today}",
        "",
        f"{len(jobs)} new matching position(s) — {stats['jobs_total']} postings "
        f"scanned across {stats['companies_ok']}/{stats['companies_total']} "
        f"companies and email alerts; {stats['already_seen']} matching "
        f"position(s) already surfaced by previous runs.",
        "",
    ]

    top = jobs[:10]
    if top:
        lines += ["## 🥇 Top 3", ""]
        for rank, job in enumerate(top[:3], start=1):
            where = f" — {job['location']}" if job["location"] else ""
            lines += [
                f"### {rank}. [{job['title']}]({job['url']}) "
                f"— {job['company']}{where}",
                f"**Score {job['score']}** : {' · '.join(job['why']) or '—'}",
                "",
            ]
        lines += ["## Top 10", ""]
        for rank, job in enumerate(top, start=1):
            lines.append(
                f"{rank}. ({job['score']}) [{job['title']}]({job['url']}) "
                f"— {job['company']}"
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
            location = f" — {job['location']}" if job["location"] else ""
            lines.append(f"- [{job['title']}]({job['url']}){location}")
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
    load_dotenv(ROOT / ".env")
    config = tomllib.loads((ROOT / "config.toml").read_text(encoding="utf-8"))
    include = [k.lower() for k in config["search"]["include_keywords"]]
    exclude = [k.lower() for k in config["search"]["exclude_keywords"]]

    kept: list[dict] = []
    errors: list[str] = []
    jobs_total = 0
    companies_ok = 0

    for target in config["companies"]:
        name, ats, slug = target["name"], target["ats"], target["slug"]
        try:
            jobs = FETCHERS[ats](name, slug)
        except Exception as exc:  # noqa: BLE001 — report and move on
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
            except Exception as exc:  # noqa: BLE001 — report and move on
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
    save_seen(seen | {job["url"] for job in kept})
    if new_jobs:
        path = write_digest(new_jobs, errors, stats)
        print(f"\nDigest written to {path.relative_to(ROOT)}")
        if config.get("notify", {}).get("enabled"):
            try:
                send_digest(
                    f"Job digest {dt.date.today().isoformat()} — "
                    f"{len(new_jobs)} new position(s)",
                    path.read_text(encoding="utf-8"),
                )
                print("Digest sent by email.")
            except Exception as exc:  # noqa: BLE001 — notification is best-effort
                print(f"  [!] email notification failed: {exc}")
    else:
        print("\nNothing new — no digest written (previous one kept).")
    print(
        f"{len(new_jobs)} new matching position(s) "
        f"({stats['already_seen']} already seen) out of {jobs_total} scanned."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
