# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with
code in this repository.

## Commands

Dependencies and execution go through **uv**; never use `pip` or activate a
venv by hand, and never reintroduce `requirements.txt` — `uv.lock` is the
single source of truth and is committed.

```bash
uv run jobwatch.py              # the whole agent: collect -> filter -> digest -> notify
uv run jobwatch.py --dry-run    # collect and filter only: writes nothing, sends nothing
uv add <package>                # adds to pyproject.toml and relocks
uv lock                         # after editing pyproject.toml by hand
```

`uv run` provisions Python 3.13 (from `.python-version`) and syncs `.venv` on
its own, so there is no setup step. CI uses `uv run --frozen` so a stale lock
fails the run instead of silently resolving something else.

There is no test suite, linter config, or build step, and `jobwatch.py` is the
only entry point.

**Use `--dry-run` for every local iteration.** It still hits the ATS APIs and
the mailbox (IMAP is opened read-only), but leaves `digests/`, `data/seen.json`
and the outbox untouched — so it cannot bury a posting in the memory or mail
the user a test digest. A plain `uv run jobwatch.py` on a dev machine consumes
real postings from the digest of the day, irreversibly (see Memory below).

Scheduled execution exists in two places and both must stay in sync when the
run command or the dependency install changes:

- `.github/workflows/daily.yml` — cloud run at 06:30 UTC; `uv run --frozen`
  and commits the new digest and `data/seen.json` back to the repo.
- `run_daily.cmd` — Windows Task Scheduler; `uv run --frozen`, appending to
  `data/run.log`. uv must be on the PATH of the account running the task.

## Architecture

Four stages, all driven from `main()` in `jobwatch.py`:

1. **Collect** — one fetcher per ATS (`fetch_greenhouse`, `fetch_lever`,
   `fetch_ashby`), registered in the `FETCHERS` dict keyed by the `ats` field
   in `config.toml`. Adding a board means adding a function with the same
   signature `(company, slug) -> list[dict]` and one `FETCHERS` entry.
   `email_collector.fetch_email_jobs` is a parallel collector reading a
   dedicated IMAP mailbox of job alerts.
2. **Normalise** — every collector returns the same four-key shape
   `{"company", "title", "location", "url"}`. This contract is what lets the
   rest of the pipeline stay collector-agnostic; the email collector bends it
   deliberately (`company` = source label like `"LinkedIn (alerte email)"`,
   `location` = referral signal like `"⭐ 2 ancien(s) collègue(s)"`).
3. **Filter** — `matches()` does substring matching on the lowercased title
   against `include_keywords` / `exclude_keywords`. Title-only by design: the
   README reserves location/seniority/qualitative scoring for a later stage.
4. **Report** — `write_digest()` writes `digests/YYYY-MM-DD.md` grouped by
   company, then `notifier.send_digest()` mails it via Gmail SMTP.

Collector failures never abort the run: each is caught, appended to `errors`,
and surfaced in a "Collection errors" section of the digest.

Dates come from `Europe/Paris` explicitly, not the host timezone — the GitHub
Actions runner is UTC, and a run after 22:00 Paris would otherwise date the
digest with the previous day.

### Memory

`data/seen.json` is a flat set of job URLs already surfaced. Only unseen URLs
reach the digest, and **no digest file is written at all when nothing is new** —
the previous day's file is left in place. This file is the one exception to the
`data/*` gitignore rule and is committed, because the GitHub Actions run needs
the memory to persist across cloud runs. (The comment above `load_seen()` still
calls it gitignored; that is stale.)

Marking a job as seen is irreversible in practice: it will never appear in a
future digest. Note that `main()` currently calls `save_seen()` **before**
`write_digest()`, so a crash while writing the digest still loses that day's
postings. Moving the save after the write is the fix if you touch this; until
then, do not add failure-prone work between the two.

### Configuration and secrets

Everything person-specific lives in two files, so the code never needs editing
to run the agent for someone else:

- `config.toml` — keywords, target companies (`name` / `ats` / `slug`), and the
  `enabled` switches for email collection and notification.
- `.env` (gitignored, template in `.env.example`) — `JOBWATCH_EMAIL_USER`,
  `JOBWATCH_EMAIL_PASSWORD` (Gmail app password), `JOBWATCH_NOTIFY_TO`. In CI
  the same three names come from repository Actions secrets.

### Email parsing

`email_collector.py` carries the messy part: alert emails wrap links in
tracking redirects (including double-encoded Outlook SafeLinks), so
`_canonical_url()` unquotes twice and rebuilds a canonical LinkedIn/Indeed URL
from the job id — that canonical form is what the memory dedupes on. Anchor
text is cleaned by `_clean_title()` against the `_TITLE_NOISE` /
`_JUNK_TITLES` lists, which are **French-locale LinkedIn strings**; a
non-French mailbox needs those lists extended.

## Working rules

Karpathy's four rules for working with an LLM agent, applied to this repo. They
override any habit of "being thorough" — this is a ~400-line project owned by
one person who is learning agent design by reading every line of it.

**1. One concrete change at a time.** Do the roadmap item that was asked for,
not the two adjacent ones it makes tempting. A location filter and a seniority
filter are two changes. Refactoring `matches()` into a scoring module and adding
a new criterion are two changes. Small readable diffs are an explicit goal of
this repo, not a style preference.

**2. Nothing the owner cannot read back.** Every diff has to be reviewable
without a debugger. Prefer a plain function over a class, an explicit loop over
a clever comprehension, and standard library over a new dependency — a package
added to save five lines is a bad trade here, and one that costs money violates
the zero-cost constraint outright. If a change needs a paragraph of explanation
to be understood, it is the wrong change.

**3. Keep the context tight.** `jobwatch.py`, `email_collector.py` and
`notifier.py` are the whole program and fit in one read. Read them directly
instead of grepping for fragments, and do not dispatch subagents or parallel
searches at this scale — the coordination costs more than the work.

**4. Verify by running, not by reading.** After any change, run
`uv run jobwatch.py --dry-run` and report the real counts it prints. This
matters most for `email_collector.py`: the regexes in `_canonical_url()` and
`_clean_title()` handle URL shapes and French LinkedIn card text that look
obvious and are not. Test them against a real captured `href` or anchor
string, and never claim a parsing change works because the pattern looks
right.

## Commits

- **Never mention Claude, an LLM, or AI assistance** — not in the message, not
  in a trailer, no `Co-Authored-By: Claude`, no generation footer. The commit
  history reads as the author's own work.
- Short and clear: one imperative subject line saying what changed, under ~70
  characters. Add a body only when the *why* is not obvious from the diff.
- One logical change per commit, matching working rule 1 above.

## Constraints from the README

- Zero recurring cost — no paid API tier or SaaS. Public ATS endpoints and free
  GitHub Actions minutes only, no scraping, no authenticated job-board calls.
- Scoring must be explainable; a black-box relevance number is not acceptable.
- Comments and prose in the repo are English; user-facing digest content and the
  email-parsing constants are French.
