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
import sys
import tomllib
from pathlib import Path

import requests

ROOT = Path(__file__).parent
DIGEST_DIR = ROOT / "digests"
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
# Digest
# --------------------------------------------------------------------------


def write_digest(jobs: list[dict], errors: list[str], stats: dict) -> Path:
    today = dt.date.today().isoformat()
    DIGEST_DIR.mkdir(exist_ok=True)
    path = DIGEST_DIR / f"{today}.md"

    lines = [
        f"# Job digest — {today}",
        "",
        f"{len(jobs)} matching position(s) across "
        f"{stats['companies_ok']}/{stats['companies_total']} companies "
        f"({stats['jobs_total']} postings scanned).",
        "",
    ]

    current_company = None
    for job in sorted(jobs, key=lambda j: (j["company"], j["title"])):
        if job["company"] != current_company:
            current_company = job["company"]
            lines += [f"## {current_company}", ""]
        location = f" — {job['location']}" if job["location"] else ""
        lines.append(f"- [{job['title']}]({job['url']}){location}")

    if errors:
        lines += ["", "## Collection errors", ""]
        lines += [f"- {error}" for error in errors]

    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


# --------------------------------------------------------------------------
# Main
# --------------------------------------------------------------------------


def main() -> int:
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

    stats = {
        "companies_total": len(config["companies"]),
        "companies_ok": companies_ok,
        "jobs_total": jobs_total,
    }
    path = write_digest(kept, errors, stats)
    print(f"\nDigest written to {path.relative_to(ROOT)}")
    print(f"{len(kept)} matching position(s) out of {jobs_total} scanned.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
