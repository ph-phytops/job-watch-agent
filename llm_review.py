"""Qualitative LLM pass over the best-scoring postings — local only.

scoring.py ranks thousands of *titles* for free and explains every point.
This module reads the actual job *descriptions*, but only for the finalists:
sending 4 000 postings to a model would be absurd, sending ten is trivial.

It runs on demand (`uv run jobwatch.py --llm`) and never in CI. The scheduled
cloud digest stays free, credential-free and fully explainable; the expensive
qualitative read happens on the machine of whoever has twenty minutes to act
on it.

Nothing here is allowed to break a run: any failure returns no verdicts and
the caller keeps the deterministic ranking.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from pathlib import Path

TIMEOUT = 300

INSTRUCTIONS = """\
You screen job postings for one candidate. Answer with a JSON array and \
nothing else — no prose, no markdown fence.

One object per posting, in the same order:
{
  "url": "<the posting url, copied verbatim>",
  "verdict": "apply" | "dig" | "skip",
  "fit": ["2-3 requirements the candidate clearly meets"],
  "gaps": ["requirements the candidate genuinely does not meet"],
  "soft_gap": "<a requirement that LOOKS disqualifying but is not — a \
certification required only after hire, an indicative years-of-experience \
range, a scope stated as a ceiling rather than a floor. Empty string if none.>",
  "note": "<one sentence: the angle to lead with, or why to skip>"
}

Be blunt about real gaps: a padded assessment wastes the candidate's week. \
But the "soft_gap" field matters as much — this candidate has a documented \
habit of self-rejecting on requirements that do not actually exclude him.\
"""


def command_for(cfg: dict) -> str:
    """The CLI the prompt is piped into. JOBWATCH_LLM_COMMAND (read from .env,
    which is gitignored) overrides config.toml, so a machine-specific absolute
    path never has to be committed to a public repo."""
    return os.environ.get("JOBWATCH_LLM_COMMAND") or cfg.get("command", "")


def load_profile(cfg: dict, root: Path) -> str:
    """Read the candidate profile. It lives outside config.toml on purpose:
    config.toml is committed and public, the profile is not."""
    path = root / cfg.get("profile_path", "data/profile.md")
    return path.read_text(encoding="utf-8") if path.is_file() else ""


def unavailable(cfg: dict, root: Path) -> str | None:
    """Return why the LLM pass cannot run, or None when it can."""
    if not cfg:
        return "no [llm] section in config.toml"
    command = command_for(cfg)
    if not command:
        return "no [llm].command set in config.toml"
    if shutil.which(command) is None:
        return (f"{command!r} not found — set [llm].command, or "
                "JOBWATCH_LLM_COMMAND in .env to an absolute path")
    if not load_profile(cfg, root).strip():
        path = cfg.get("profile_path", "data/profile.md")
        return f"{path} is missing or empty (it is gitignored — write your own)"
    return None


def _prompt(jobs: list[dict], cfg: dict, profile: str) -> str:
    max_chars = cfg.get("max_chars", 6000)
    blocks = []
    for job in jobs:
        content = (job.get("content") or "").strip()
        if len(content) > max_chars:
            content = content[:max_chars] + "\n[...truncated]"
        blocks.append(
            f"--- POSTING\n"
            f"url: {job['url']}\n"
            f"company: {job['company']}\n"
            f"title: {job['title']}\n"
            f"location: {job.get('location', '')}\n"
            f"deterministic score: {job.get('score', '?')} "
            f"({' · '.join(job.get('why', [])) or 'n/a'})\n"
            f"description:\n{content or '(not available from this ATS)'}\n"
        )
    return (
        f"{INSTRUCTIONS}\n\n"
        f"CANDIDATE PROFILE\n{profile.strip()}\n\n"
        f"POSTINGS ({len(jobs)})\n\n" + "\n".join(blocks)
    )


def _parse(raw: str) -> list[dict]:
    """Pull the JSON array out of the model's answer, fence or no fence."""
    match = re.search(r"\[.*\]", raw, re.DOTALL)
    if not match:
        raise ValueError("no JSON array in the answer")
    return json.loads(match.group(0))


def review(jobs: list[dict], cfg: dict, profile: str) -> dict[str, dict]:
    """Return {url: verdict} for the given jobs, reviewed in batches.

    One posting carries several thousand characters of description, so a large
    sweep is split: fifty in a single prompt degrades the per-posting judgement
    and risks a truncated answer that no longer parses. A batch that fails is
    reported and skipped; the others still return their verdicts.
    """
    if not jobs:
        return {}
    size = max(1, cfg.get("batch_size", 10))
    if len(jobs) > size:
        verdicts: dict[str, dict] = {}
        batches = [jobs[i:i + size] for i in range(0, len(jobs), size)]
        for number, batch in enumerate(batches, start=1):
            print(f"  batch {number}/{len(batches)} ({len(batch)} postings)...")
            verdicts.update(_review_one(batch, cfg, profile))
        return verdicts
    return _review_one(jobs, cfg, profile)


def _review_one(jobs: list[dict], cfg: dict, profile: str) -> dict[str, dict]:
    """One model call over one batch."""
    command = [command_for(cfg), *cfg.get("args", [])]
    try:
        result = subprocess.run(
            command,
            input=_prompt(jobs, cfg, profile),
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=TIMEOUT,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        print(f"  [!] LLM call failed: {exc}")
        return {}
    if result.returncode != 0:
        print(f"  [!] {command_for(cfg)} exited {result.returncode}: "
              f"{(result.stderr or '').strip()[:200]}")
        return {}
    try:
        verdicts = _parse(result.stdout)
    except (ValueError, json.JSONDecodeError) as exc:
        print(f"  [!] could not read the LLM answer: {exc}")
        return {}
    return {v["url"]: v for v in verdicts if isinstance(v, dict) and v.get("url")}


ORDER = {"apply": 0, "dig": 1, "skip": 2}


def render(jobs: list[dict], verdicts: dict[str, dict], date: str) -> str:
    """Render the reviewed jobs as Markdown, most actionable first."""
    lines = [
        f"# Job review — {date}",
        "",
        f"{len(verdicts)} posting(s) read in full by the model, out of "
        f"{len(jobs)} finalists ranked by the deterministic scorer.",
        "",
    ]
    ranked = sorted(
        jobs,
        key=lambda j: (
            ORDER.get((verdicts.get(j["url"], {}) or {}).get("verdict"), 3),
            -j.get("score", 0),
        ),
    )
    for job in ranked:
        verdict = verdicts.get(job["url"])
        where = f" — {job['location']}" if job.get("location") else ""
        if not verdict:
            lines += [
                f"## ⚪ [{job['title']}]({job['url']}) — {job['company']}{where}",
                "",
                "_Not reviewed by the model._",
                "",
            ]
            continue
        badge = {"apply": "🟢 APPLY", "dig": "🟡 DIG", "skip": "⚪ SKIP"}.get(
            verdict.get("verdict", ""), "⚪"
        )
        lines += [
            f"## {badge} — [{job['title']}]({job['url']}) — "
            f"{job['company']}{where}",
            "",
            f"**Score {job.get('score', '?')}** · {verdict.get('note', '')}",
            "",
        ]
        if verdict.get("fit"):
            lines += ["**Fits:** " + " · ".join(verdict["fit"]), ""]
        if verdict.get("gaps"):
            lines += ["**Real gaps:** " + " · ".join(verdict["gaps"]), ""]
        if (verdict.get("soft_gap") or "").strip():
            lines += [f"> ⚠️ **Looks disqualifying but is not:** {verdict['soft_gap']}", ""]
    return "\n".join(lines) + "\n"
