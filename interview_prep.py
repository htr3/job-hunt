"""LLM-backed interview prep per job.

For each of the top-N jobs the LLM returns a JSON with three sections:
- technical questions (8-12) with concise model answers
- behavioral questions (4-6) with a STAR-framed model answer
- questions to ask the interviewer (3-5)

Each prep packet is written as Markdown under
`interview_prep/<company>_<title>_<timestamp>.md`.

Gracefully degrades when OpenAI is unreachable or the user has no key.
"""
from __future__ import annotations

import json
import logging
import re
from datetime import datetime
from pathlib import Path
from typing import Any

logger = logging.getLogger("interview_prep")

try:
    from openai import OpenAI  # type: ignore
except ImportError:  # pragma: no cover
    OpenAI = None  # type: ignore

PROJECT_ROOT = Path(__file__).resolve().parent
OUT_DIR = PROJECT_ROOT / "interview_prep"


def _safe_slug(s: str, max_len: int = 40) -> str:
    slug = re.sub(r"[^A-Za-z0-9._-]+", "_", (s or "").strip())
    return slug[:max_len].strip("_") or "job"


class InterviewPrep:
    def __init__(self, config: dict[str, Any]) -> None:
        self.config = config or {}
        ip = self.config.get("interview_prep", {}) or {}
        self.enabled: bool = bool(ip.get("enabled"))
        self.top_n: int = int(ip.get("top_n") or 5)

        ai = self.config.get("ai_matching", {}) or {}
        self.api_key = (ai.get("openai_api_key") or "").strip()
        self.model = ip.get("model") or ai.get("model") or "gpt-4o-mini"

        self._client = None
        key = self.api_key
        missing_key = (
            not key
            or key.startswith("${")
            or key.lower() in {"none", "null", "your_key_here", "sk-"}
        )
        if self.enabled and not missing_key and OpenAI is not None:
            try:
                self._client = OpenAI(api_key=key)
            except Exception as e:
                logger.warning("Failed to init OpenAI client: %s", e)

    def available(self) -> bool:
        return self._client is not None

    def generate(self, jobs: list) -> list[Path]:
        if not self.enabled:
            return []
        if not jobs:
            return []
        if not self.available():
            logger.warning(
                "interview_prep enabled but no OpenAI client (missing key?) — skipping."
            )
            return []

        OUT_DIR.mkdir(parents=True, exist_ok=True)
        sample = jobs[: self.top_n]
        written: list[Path] = []
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")

        profile = self.config.get("profile", {}) or {}
        search = self.config.get("search", {}) or {}
        prof_blob = json.dumps(
            {
                "current_role": profile.get("current_role") or "",
                "experience_years": profile.get("experience_years") or 0,
                "skills": search.get("skills") or [],
            },
            ensure_ascii=False,
        )

        for idx, job in enumerate(sample, 1):
            try:
                md = self._prep_one(job, prof_blob)
            except Exception as e:
                logger.warning(
                    "[%d/%d] interview prep failed for %s @ %s: %s",
                    idx, len(sample),
                    getattr(job, "title", "?"), getattr(job, "company", "?"), e,
                )
                continue
            if not md:
                continue
            title = _safe_slug(getattr(job, "title", ""))
            company = _safe_slug(getattr(job, "company", ""))
            path = OUT_DIR / f"{company}__{title}__{stamp}_{idx}.md"
            path.write_text(md, encoding="utf-8")
            written.append(path)

        if written:
            logger.info("Interview prep: wrote %d prep packets to %s", len(written), OUT_DIR)
        return written

    def _prep_one(self, job: Any, profile_blob: str) -> str | None:
        jd = (
            f"Title: {getattr(job, 'title', '') or ''}\n"
            f"Company: {getattr(job, 'company', '') or ''}\n"
            f"Location: {getattr(job, 'location', '') or ''}\n"
            f"Skills-listed: {', '.join(getattr(job, 'skills', []) or [])}\n"
            f"Description:\n{(getattr(job, 'description', '') or '')[:4000]}"
        )
        system = (
            "You are a senior engineering interviewer and career coach. "
            "Given a candidate profile (JSON) and a job description, produce an interview "
            "prep packet as a single JSON object with keys: "
            "technical (list of {question, answer}), "
            "behavioral (list of {question, star_answer}), "
            "questions_to_ask (list of strings). "
            "Aim for 8-12 technical, 4-6 behavioral, 3-5 questions_to_ask. "
            "Answers should be concise (3-6 sentences). No prose outside the JSON."
        )
        user = f"CANDIDATE_PROFILE:\n{profile_blob}\n\nJOB_DESCRIPTION:\n{jd}"

        resp = self._client.chat.completions.create(  # type: ignore[union-attr]
            model=self.model,
            temperature=0.3,
            response_format={"type": "json_object"},
            messages=[{"role": "system", "content": system}, {"role": "user", "content": user}],
            timeout=75,
        )
        raw = (resp.choices[0].message.content or "").strip()
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            logger.debug("non-JSON prep response; skipping: %s", raw[:200])
            return None

        tech = data.get("technical") or []
        beh = data.get("behavioral") or []
        asks = data.get("questions_to_ask") or []
        if not tech and not beh:
            return None

        md = [
            f"# Interview prep — {getattr(job, 'title', '')}",
            f"**{getattr(job, 'company', '')}** · {getattr(job, 'location', '')} · "
            f"{getattr(job, 'platform', '')}",
            f"**Match:** {float(getattr(job, 'match_score', 0) or 0):.0f}  ·  "
            f"**URL:** {getattr(job, 'url', '')}",
            "",
            "## Technical questions",
            "",
        ]
        for i, q in enumerate(tech, 1):
            if not isinstance(q, dict):
                continue
            md.append(f"**{i}. {str(q.get('question','')).strip()}**")
            md.append("")
            md.append(str(q.get("answer", "")).strip())
            md.append("")

        md += ["## Behavioral (STAR format)", ""]
        for i, q in enumerate(beh, 1):
            if not isinstance(q, dict):
                continue
            md.append(f"**{i}. {str(q.get('question','')).strip()}**")
            md.append("")
            md.append(str(q.get("star_answer", "")).strip())
            md.append("")

        md += ["## Smart questions to ask the interviewer", ""]
        for q in asks:
            md.append(f"- {str(q).strip()}")
        md.append("")

        return "\n".join(md)


def generate_interview_prep(config: dict[str, Any], jobs: list) -> list[Path]:
    return InterviewPrep(config).generate(jobs)
