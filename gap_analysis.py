"""Skill-gap report.

Scans every job in the final shortlist, extracts skills (from the dedicated
`skills` field when present, falls back to a simple keyword sweep over the JD),
and diffs against `config.search.skills` (the user's self-declared skills).

Writes a Markdown report to `reports/gap_analysis_YYYYMMDD_HHMMSS.md`.

Pure-Python, no LLM required — runs instantly.
"""
from __future__ import annotations

import logging
import re
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any

logger = logging.getLogger("gap_analysis")

PROJECT_ROOT = Path(__file__).resolve().parent
REPORTS_DIR = PROJECT_ROOT / "reports"

# Canonical known skills we can sniff from free-text JD if the `skills` field is empty.
# Not exhaustive; just the common suspects in SWE/DS/DevOps/ML postings.
_KNOWN_SKILLS = [
    # languages
    "python", "java", "kotlin", "scala", "go", "golang", "rust", "c++", "c#",
    "typescript", "javascript", "ruby", "php", "sql", "bash", "powershell",
    # frameworks / libs
    "spring", "spring boot", "django", "flask", "fastapi", "rails", "express",
    "react", "angular", "vue", "next.js", "node.js", "graphql", "grpc",
    "pytorch", "tensorflow", "scikit-learn", "pandas", "numpy", "spark", "hadoop",
    # infra / ops
    "docker", "kubernetes", "terraform", "ansible", "jenkins", "github actions",
    "circleci", "argocd", "helm", "prometheus", "grafana", "datadog",
    # cloud
    "aws", "gcp", "azure", "lambda", "s3", "ec2", "bigquery", "redshift",
    # data stores
    "postgres", "postgresql", "mysql", "mongodb", "cassandra", "redis",
    "elasticsearch", "kafka", "rabbitmq", "snowflake", "dynamodb",
    # misc
    "rest", "microservices", "ci/cd", "tdd", "agile", "scrum", "git", "linux",
]


def _normalize(s: str) -> str:
    return re.sub(r"[\s+.\-]+", " ", s.strip().lower())


def _extract_skills(job: Any) -> set[str]:
    """Prefer `job.skills`; fall back to sniffing the JD text."""
    raw = getattr(job, "skills", None)
    if raw:
        if isinstance(raw, str):
            tokens = [t.strip() for t in re.split(r"[,;|]", raw) if t.strip()]
        else:
            tokens = [str(t).strip() for t in raw if t]
        return {_normalize(t) for t in tokens if t}

    text = " ".join(
        str(getattr(job, k, "") or "")
        for k in ("title", "description", "job_type", "experience")
    ).lower()
    if not text.strip():
        return set()
    hits: set[str] = set()
    for s in _KNOWN_SKILLS:
        pattern = r"\b" + re.escape(s) + r"\b"
        if re.search(pattern, text):
            hits.add(_normalize(s))
    return hits


class GapAnalyzer:
    def __init__(self, config: dict[str, Any]) -> None:
        ga_cfg = config.get("gap_analysis", {}) or {}
        self.enabled = bool(ga_cfg.get("enabled"))
        self.top_n = int(ga_cfg.get("top_n") or 25)
        self.user_skills = {
            _normalize(s) for s in (config.get("search", {}) or {}).get("skills", []) if s
        }

    def analyze(self, jobs: list) -> Path | None:
        if not self.enabled:
            return None
        if not jobs:
            logger.info("Gap analysis: no jobs in shortlist, skipping.")
            return None

        sample = jobs[: self.top_n]
        counter: Counter[str] = Counter()
        for j in sample:
            for skill in _extract_skills(j):
                counter[skill] += 1

        if not counter:
            logger.info("Gap analysis: no skills detected in top %d jobs.", len(sample))
            return None

        demanded = [(sk, n) for sk, n in counter.most_common() if n >= 1]
        have = self.user_skills
        missing = [(sk, n) for sk, n in demanded if sk not in have]
        matched = [(sk, n) for sk, n in demanded if sk in have]

        return self._write_report(sample, demanded, matched, missing)

    def _write_report(
        self,
        sample: list,
        demanded: list[tuple[str, int]],
        matched: list[tuple[str, int]],
        missing: list[tuple[str, int]],
    ) -> Path:
        REPORTS_DIR.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        path = REPORTS_DIR / f"gap_analysis_{stamp}.md"

        lines: list[str] = [
            "# Skill Gap Analysis",
            "",
            f"Generated: {datetime.now().isoformat(timespec='seconds')}",
            f"Sample: top {len(sample)} jobs by match score.",
            f"Your declared skills: {', '.join(sorted(self.user_skills)) or '(none)'}",
            "",
            "## Summary",
            "",
            f"- **Distinct skills seen across sample:** {len(demanded)}",
            f"- **Your matches:** {len(matched)}",
            f"- **Gaps (skills not in your profile):** {len(missing)}",
            "",
            "## Top gaps (learn these first)",
            "",
            "| Skill | Jobs asking for it |",
            "| --- | ---: |",
        ]
        for sk, n in missing[:25]:
            lines.append(f"| {sk} | {n} |")
        if not missing:
            lines.append("| _None — you cover the top demands_ | |")

        lines += [
            "",
            "## Skills you already have that matched",
            "",
            "| Skill | Jobs mentioning it |",
            "| --- | ---: |",
        ]
        for sk, n in matched[:25]:
            lines.append(f"| {sk} | {n} |")
        if not matched:
            lines.append("| _No overlap with posted demand_ | |")

        lines += [
            "",
            "## Full demand ranking (sample)",
            "",
            "| Skill | Count | In your profile? |",
            "| --- | ---: | :---: |",
        ]
        for sk, n in demanded[:50]:
            lines.append(f"| {sk} | {n} | {'yes' if sk in self.user_skills else 'no'} |")

        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        logger.info(
            "Gap analysis: wrote %s (%d gaps, %d matches in top %d jobs).",
            path.name, len(missing), len(matched), len(sample),
        )
        return path


def run_gap_analysis(config: dict[str, Any], jobs: list) -> Path | None:
    return GapAnalyzer(config).analyze(jobs)
