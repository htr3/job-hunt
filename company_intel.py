"""Company intelligence / filtering.

Phase 2 scope:
- Blacklist / whitelist filtering by company name (substring, case-insensitive).
- Lightweight company rating cache (populated opportunistically by Glassdoor
  scraper output; falls back to a heuristic stub if unavailable).
"""
from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)


class CompanyIntel:
    """In-memory company filter + rating registry."""

    def __init__(self, config: dict[str, Any]):
        ci = (config or {}).get("company_intel", {}) or {}
        self.enabled: bool = bool(ci.get("enabled", False))
        self.blacklist: list[str] = [s.lower().strip() for s in (ci.get("blacklist") or []) if s]
        self.whitelist: list[str] = [s.lower().strip() for s in (ci.get("whitelist") or []) if s]
        self.min_rating: float = float(ci.get("min_glassdoor_rating") or 0.0)
        self._ratings: dict[str, float] = {}

    def register_rating(self, company: str, rating: float) -> None:
        if not company:
            return
        self._ratings[company.lower().strip()] = float(rating)

    def rating_for(self, company: str) -> float | None:
        if not company:
            return None
        return self._ratings.get(company.lower().strip())

    def _matches(self, company: str, needles: list[str]) -> bool:
        if not company or not needles:
            return False
        cl = company.lower()
        return any(n in cl for n in needles)

    def filter(self, jobs: list[Any]) -> list[Any]:
        """Apply blacklist/whitelist. Always runs (config.enabled gates rating checks only)."""
        if not jobs:
            return jobs

        before = len(jobs)
        filtered: list[Any] = []
        dropped_bl = 0
        dropped_wl = 0
        dropped_rating = 0

        for job in jobs:
            company = getattr(job, "company", "") or ""

            if self.blacklist and self._matches(company, self.blacklist):
                dropped_bl += 1
                continue

            if self.whitelist and not self._matches(company, self.whitelist):
                dropped_wl += 1
                continue

            if self.enabled and self.min_rating > 0:
                rating = self.rating_for(company)
                if rating is not None and rating < self.min_rating:
                    dropped_rating += 1
                    continue

            filtered.append(job)

        if before != len(filtered):
            logger.info(
                "CompanyIntel: %d -> %d jobs (blacklist:%d, whitelist:%d, rating:%d).",
                before, len(filtered), dropped_bl, dropped_wl, dropped_rating,
            )
        return filtered
