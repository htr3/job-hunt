"""OpenAI-backed job scoring.

Each job gets a 0-100 match score based on the candidate's profile and
search/skills config. When the OpenAI key is missing or the call fails,
the matcher no-ops (returns the jobs untouched with score 0).
"""
from __future__ import annotations

import concurrent.futures
import json
import logging
import re
from typing import Any

logger = logging.getLogger(__name__)

try:
    from openai import OpenAI  # type: ignore
except ImportError:  # pragma: no cover
    OpenAI = None  # type: ignore


_SCORE_RE = re.compile(r"\b(\d{1,3})\b")


class AIJobMatcher:
    """Score jobs 0-100 against the candidate profile with an LLM."""

    def __init__(self, config: dict[str, Any]):
        self.config = config or {}
        ai = self.config.get("ai_matching", {}) or {}
        self.enabled: bool = bool(ai.get("enabled"))
        self.api_key: str = (ai.get("openai_api_key") or "").strip()
        self.model: str = ai.get("model") or "gpt-4o-mini"
        self.min_score: float = float(ai.get("min_match_score") or 0)
        self.max_workers: int = int(ai.get("max_workers") or 4)

        self._client = None
        self._scored_successfully: bool = False
        key = self.api_key
        missing_key = (
            not key
            or key.startswith("${")
            or key.lower() in {"none", "null", "your_key_here", "sk-", ""}
        )
        if self.enabled and not missing_key and OpenAI is not None:
            try:
                self._client = OpenAI(api_key=key)
            except Exception as e:  # pragma: no cover
                logger.warning("Failed to init OpenAI client: %s", e)
                self._client = None
        elif self.enabled and missing_key:
            logger.warning(
                "AI matching enabled but OPENAI_API_KEY is missing/empty — skipping AI scoring."
            )

    # --- public ----------------------------------------------------------- #

    def available(self) -> bool:
        return self._client is not None

    def score_jobs(self, jobs: list[Any]) -> list[Any]:
        """Score each job in place (sets `job.match_score`) and return the list."""
        if not jobs:
            return jobs
        if not self.available():
            if self.enabled:
                logger.warning(
                    "AI matching enabled but no OpenAI client (missing key?) — skipping."
                )
            else:
                logger.info("AI matching disabled; leaving match_score at 0.")
            return jobs

        logger.info("Scoring %d jobs with model=%s ...", len(jobs), self.model)
        any_success = False
        with concurrent.futures.ThreadPoolExecutor(max_workers=self.max_workers) as pool:
            futures = {pool.submit(self._score_one, job): job for job in jobs}
            for fut in concurrent.futures.as_completed(futures):
                job = futures[fut]
                try:
                    score, ok = fut.result()
                except Exception as e:
                    logger.warning(
                        "Scoring failed for %r: %s", getattr(job, "title", ""), e
                    )
                    job.match_score = 0.0
                    continue
                job.match_score = float(score)
                if ok:
                    any_success = True

        self._scored_successfully = any_success
        if not any_success:
            logger.warning(
                "AI scoring produced no successful responses (auth/network failure?) — "
                "match_score left at 0 for all jobs."
            )
        return jobs

    def filter_by_score(self, jobs: list[Any]) -> list[Any]:
        """Drop jobs below `min_match_score` (only if AI produced real scores)."""
        if not self.available() or self.min_score <= 0:
            return jobs
        if not self._scored_successfully:
            logger.info("Skipping AI score filter since no scores were produced.")
            return jobs
        kept = [j for j in jobs if float(getattr(j, "match_score", 0) or 0) >= self.min_score]
        dropped = len(jobs) - len(kept)
        if dropped:
            logger.info(
                "AI filter dropped %d/%d jobs below min_match_score=%.1f.",
                dropped, len(jobs), self.min_score,
            )
        return kept

    # --- internals -------------------------------------------------------- #

    def _profile_blob(self) -> str:
        profile = self.config.get("profile", {}) or {}
        search = self.config.get("search", {}) or {}
        loc = self.config.get("location", {}) or {}
        filters = self.config.get("filters", {}) or {}
        return json.dumps(
            {
                "role": profile.get("current_role", ""),
                "experience_years": profile.get("experience_years", 0),
                "location": profile.get("location", ""),
                "skills": search.get("skills", []),
                "target_titles": search.get("job_titles", []),
                "preferred_cities": loc.get("preferred_cities", []),
                "remote_only": loc.get("remote_only", False),
                "min_experience": filters.get("min_experience", 0),
                "max_experience": filters.get("max_experience", 99),
            },
            ensure_ascii=False,
        )

    def _score_one(self, job: Any) -> tuple[float, bool]:
        """Return (score, ok_flag). ok_flag is False when the call errored."""
        assert self._client is not None
        prompt = (
            "You score how well a job listing matches a candidate. "
            "Return ONLY a single integer 0-100. No words, no JSON, no punctuation."
        )
        job_blob = json.dumps(
            {
                "title": getattr(job, "title", ""),
                "company": getattr(job, "company", ""),
                "location": getattr(job, "location", ""),
                "skills": getattr(job, "skills", []),
                "experience": getattr(job, "experience", ""),
                "description": (getattr(job, "description", "") or "")[:1500],
            },
            ensure_ascii=False,
        )
        user = f"CANDIDATE:\n{self._profile_blob()}\n\nJOB:\n{job_blob}\n\nScore:"

        try:
            resp = self._client.chat.completions.create(
                model=self.model,
                messages=[
                    {"role": "system", "content": prompt},
                    {"role": "user", "content": user},
                ],
                temperature=0.0,
                max_tokens=5,
            )
            content = (resp.choices[0].message.content or "").strip()
        except Exception as e:
            logger.debug("OpenAI call failed for %r: %s", getattr(job, "title", ""), e)
            return (0.0, False)

        m = _SCORE_RE.search(content)
        if not m:
            return (0.0, True)
        val = max(0, min(100, int(m.group(1))))
        return (float(val), True)
