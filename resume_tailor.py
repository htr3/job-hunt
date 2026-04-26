"""LLM-backed per-job resume tailor.

For each of the top-N scored jobs the LLM returns:
- a rewritten professional summary (3-4 lines)
- 5-7 bullet points re-angled at the JD's priorities
- a keyword-matched skills list (pulled from JD)

Each tailored resume is written as Markdown under
`tailored_resumes/<company>_<title>_<timestamp>.md`.

Gracefully degrades:
- No OpenAI key → logs a warning, returns [].
- API / rate-limit error per job → that job is skipped, others continue.
- `enabled=false` → no-op.
"""
from __future__ import annotations

import json
import logging
import re
from datetime import datetime
from pathlib import Path
from typing import Any

logger = logging.getLogger("resume_tailor")

try:
    from openai import OpenAI  # type: ignore
except ImportError:  # pragma: no cover
    OpenAI = None  # type: ignore

PROJECT_ROOT = Path(__file__).resolve().parent
OUT_DIR = PROJECT_ROOT / "tailored_resumes"


def _safe_slug(s: str, max_len: int = 40) -> str:
    slug = re.sub(r"[^A-Za-z0-9._-]+", "_", (s or "").strip())
    return slug[:max_len].strip("_") or "job"


class ResumeTailor:
    def __init__(self, config: dict[str, Any]) -> None:
        self.config = config or {}
        rt = self.config.get("resume_tailoring", {}) or {}
        self.enabled: bool = bool(rt.get("enabled"))
        self.top_n: int = int(rt.get("top_n") or 5)

        ai = self.config.get("ai_matching", {}) or {}
        self.api_key = (ai.get("openai_api_key") or "").strip()
        self.model = rt.get("model") or ai.get("model") or "gpt-4o-mini"

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

    def _profile_blob(self) -> str:
        profile = self.config.get("profile", {}) or {}
        search = self.config.get("search", {}) or {}
        data = {
            "name": profile.get("name") or "",
            "email": profile.get("email") or "",
            "location": profile.get("location") or "",
            "experience_years": profile.get("experience_years") or 0,
            "current_role": profile.get("current_role") or "",
            "target_titles": search.get("job_titles") or [],
            "skills": search.get("skills") or [],
            "resume_path": profile.get("resume_path") or "",
        }
        return json.dumps(data, ensure_ascii=False)

    def tailor(self, jobs: list) -> list[Path]:
        if not self.enabled:
            return []
        if not jobs:
            return []
        if not self.available():
            logger.warning(
                "resume_tailoring enabled but no OpenAI client (missing key?) — skipping."
            )
            return []

        OUT_DIR.mkdir(parents=True, exist_ok=True)
        sample = jobs[: self.top_n]
        written: list[Path] = []
        profile_blob = self._profile_blob()
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")

        for idx, job in enumerate(sample, 1):
            try:
                out = self._tailor_one(job, profile_blob)
            except Exception as e:
                logger.warning(
                    "[%d/%d] tailor failed for %s @ %s: %s",
                    idx, len(sample),
                    getattr(job, "title", "?"), getattr(job, "company", "?"), e,
                )
                continue
            if not out:
                continue
            title = _safe_slug(getattr(job, "title", ""))
            company = _safe_slug(getattr(job, "company", ""))
            path = OUT_DIR / f"{company}__{title}__{stamp}_{idx}.md"
            path.write_text(out, encoding="utf-8")
            written.append(path)

        if written:
            logger.info("Resume tailor: wrote %d tailored resumes to %s", len(written), OUT_DIR)
        return written

    def _tailor_one(self, job: Any, profile_blob: str) -> str | None:
        jd = (
            f"Title: {getattr(job, 'title', '') or ''}\n"
            f"Company: {getattr(job, 'company', '') or ''}\n"
            f"Location: {getattr(job, 'location', '') or ''}\n"
            f"Platform: {getattr(job, 'platform', '') or ''}\n"
            f"Skills-listed: {', '.join(getattr(job, 'skills', []) or [])}\n"
            f"Description:\n{(getattr(job, 'description', '') or '')[:4000]}"
        )
        system = (
            "You are an expert technical resume writer. Given a candidate profile "
            "(JSON) and a job description, produce a concise Markdown resume section "
            "tailored to the JD. Output MUST be a single JSON object with keys: "
            "summary (string, 3-4 sentences), bullets (list of 5-7 strings, impact-first), "
            "keywords (list of 10-20 JD-relevant keywords). No prose outside the JSON."
        )
        user = f"CANDIDATE_PROFILE:\n{profile_blob}\n\nJOB_DESCRIPTION:\n{jd}"

        resp = self._client.chat.completions.create(  # type: ignore[union-attr]
            model=self.model,
            temperature=0.4,
            response_format={"type": "json_object"},
            messages=[{"role": "system", "content": system}, {"role": "user", "content": user}],
            timeout=60,
        )
        raw = (resp.choices[0].message.content or "").strip()
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            logger.debug("non-JSON response; skipping: %s", raw[:200])
            return None

        summary = str(data.get("summary") or "").strip()
        bullets = [str(b).strip() for b in (data.get("bullets") or []) if str(b).strip()]
        keywords = [str(k).strip() for k in (data.get("keywords") or []) if str(k).strip()]
        if not summary or not bullets:
            return None

        profile = self.config.get("profile", {}) or {}
        md = [
            f"# Tailored resume — {getattr(job, 'title', '')}",
            f"**Target company:** {getattr(job, 'company', '')} · **Location:** {getattr(job, 'location', '')}",
            f"**Source:** {getattr(job, 'platform', '')} · **Job URL:** {getattr(job, 'url', '')}",
            f"**Match score:** {float(getattr(job, 'match_score', 0) or 0):.0f}",
            "",
            "## Candidate",
            f"{profile.get('name') or ''} · {profile.get('email') or ''}",
            "",
            "## Summary",
            summary,
            "",
            "## Highlights",
        ]
        md += [f"- {b}" for b in bullets]
        md += [
            "",
            "## Keywords to surface (mirror in your resume)",
            ", ".join(keywords),
            "",
        ]
        return "\n".join(md)


def tailor_resumes(config: dict[str, Any], jobs: list) -> list[Path]:
    return ResumeTailor(config).tailor(jobs)
