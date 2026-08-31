# job-watch-agent

An autonomous agent that monitors job boards daily, scores openings against a
candidate profile, and surfaces only the ones worth applying to.

> **Status: in development.** This repository is built in public as I learn to
> design and ship LLM-based agents. Commits are incremental and intentionally
> readable.

---

## Why this project

Job searching at scale has a signal-to-noise problem. Applying to 100 openings
produces roughly 3 real conversations — not because the volume is too low, but
because the targeting is poor and most postings are a bad fit that only reveals
itself after the application is sent.

The goal here is not "apply to more jobs". It is **to spend the same amount of
effort on a much smaller, much better-selected set of openings**, and to make
the selection step reproducible instead of intuitive.

## What it should do

1. **Collect** — pull new postings every day from a defined set of sources.
2. **Normalise** — reduce heterogeneous postings to a common structure
   (title, company, location, seniority, stack, compensation when disclosed).
3. **Score** — rank each posting against an explicit candidate profile:
   hard filters (location, commute time, salary floor) then a qualitative fit
   assessment.
4. **Report** — deliver a short daily digest of the few openings that clear the
   bar, with the reasoning behind each score.

## Design constraints

These are deliberate, not accidental:

- **Zero recurring cost.** No paid API tier, no SaaS subscription. If it cannot
  run on free or already-owned resources, it does not go in.
- **Explicit scoring.** The agent must justify every ranking. A black-box
  relevance score is not useful for deciding where to spend an afternoon.
- **Local first.** Runs on a single machine, no infrastructure to maintain.

## Tech stack

- Python 3.13
- [uv](https://docs.astral.sh/uv/) for dependencies and execution — `uv.lock`
  is committed, so every run (local or CI) resolves to identical versions.

Additional dependencies will be added as the scope firms up, and documented here
rather than left implicit.

## Roadmap

- [x] Project scaffolding and reproducible environment
- [x] Source collection from public ATS APIs (Greenhouse, Lever, Ashby)
- [x] Email-alert collection over IMAP (dedicated mailbox, read-only,
      credentials in a gitignored `.env` — see `.env.example`)
- [x] Posting normalisation to a common shape
- [x] Title keyword filtering (config-driven)
- [x] Daily digest output (Markdown)
- [x] Memory: only surface postings not seen on previous runs
- [x] Scheduled execution (Windows Task Scheduler → `run_daily.cmd`,
      logs in `data/run.log`, catch-up run if the machine was off)
- [x] Email notification: the digest lands in your inbox when new
      positions are found
- [x] Transparent scoring and ranking (config-driven weights; every point
      is explained in the digest — Top 3 with reasons, Top 10, the rest)
- [ ] LLM-based qualitative scoring (deep-reading job descriptions)

## Getting started

```bash
git clone https://github.com/ph-phytops/job-watch-agent.git
cd job-watch-agent

uv run jobwatch.py            # writes digests/YYYY-MM-DD.md
uv run jobwatch.py --dry-run  # preview only: writes nothing, sends nothing
uv run jobwatch.py --llm      # local: read the finalists' full descriptions
```

`uv run` provisions Python 3.13 and syncs the environment on its own, so there
is no install step — [install uv](https://docs.astral.sh/uv/getting-started/installation/)
and that one command is the whole setup.

Edit `config.toml` to set your own target companies and keywords.

## Running it for someone else

Everything specific to a person lives in two files — no code changes needed:

1. **`config.toml`** — their target companies (find the ATS and slug from the
   careers page URL: `boards.greenhouse.io/<slug>`, `jobs.lever.co/<slug>`,
   `jobs.ashbyhq.com/<slug>`) and their title keywords.
2. **`.env`** — a dedicated Gmail mailbox receiving their job alerts
   (LinkedIn/Indeed subscriptions or forwards), an app password, and the
   address where the digest should be delivered. See `.env.example`.

Then schedule `run_daily.cmd` (Windows Task Scheduler) or an equivalent cron
job, and the agent works for them.

---

**Author** — Pierre Hamoir · [LinkedIn](https://www.linkedin.com/in/pierre-hamoir/)
