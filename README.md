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
- Isolated virtual environment (`.venv`)

Additional dependencies will be added as the scope firms up, and documented here
rather than left implicit.

## Roadmap

- [x] Project scaffolding and reproducible environment
- [x] Source collection from public ATS APIs (Greenhouse, Lever, Ashby)
- [x] Posting normalisation to a common shape
- [x] Title keyword filtering (config-driven)
- [x] Daily digest output (Markdown)
- [ ] Memory: only surface postings not seen on previous runs
- [ ] Location and seniority filters
- [ ] Qualitative scoring against a candidate profile
- [ ] Scheduled execution

## Getting started

```bash
git clone https://github.com/ph-phytops/job-watch-agent.git
cd job-watch-agent

python -m venv .venv
.venv\Scripts\activate        # Windows
source .venv/bin/activate     # macOS / Linux

pip install -r requirements.txt
python jobwatch.py            # writes digests/YYYY-MM-DD.md
```

Edit `config.toml` to set your own target companies and keywords.

---

**Author** — Pierre Hamoir · [LinkedIn](https://www.linkedin.com/in/pierre-hamoir/)
