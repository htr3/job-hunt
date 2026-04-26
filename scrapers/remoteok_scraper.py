"""RemoteOK scraper — uses the public JSON feed at https://remoteok.com/api.

No Selenium needed. Filters results by configured titles / skills and returns
a list of `Job` instances.
"""
from __future__ import annotations

from typing import Any

from .base_scraper import BaseScraper, Job

REMOTEOK_API_URL = "https://remoteok.com/api"

_DEFAULT_HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; JobHuntAIAgent/1.0; +https://example.com)",
    "Accept": "application/json",
}


class RemoteOKScraper(BaseScraper):
    PLATFORM_NAME = "remoteok"

    def __init__(self, config: dict[str, Any]):
        super().__init__(config)
        self._cache: list[dict[str, Any]] | None = None

    def _fetch_feed(self) -> list[dict[str, Any]]:
        """Fetch and cache the full RemoteOK feed once per run."""
        if self._cache is not None:
            return self._cache
        try:
            resp = self.session.get(REMOTEOK_API_URL, headers=_DEFAULT_HEADERS, timeout=30)
            resp.raise_for_status()
            data = resp.json()
        except Exception as e:
            self.logger.error("[remoteok] Failed to fetch feed: %s", e)
            self._cache = []
            return self._cache

        # First element is metadata; skip it.
        jobs_raw = [item for item in data if isinstance(item, dict) and item.get("id")]
        self._cache = jobs_raw
        self.logger.info("[remoteok] Fetched %d raw postings from feed.", len(jobs_raw))
        return self._cache

    @staticmethod
    def _match_title(title: str, item: dict[str, Any]) -> bool:
        target = title.lower().strip()
        if not target:
            return True
        haystack = " ".join(
            [
                str(item.get("position", "")),
                str(item.get("description", "")),
                " ".join(item.get("tags", []) or []),
            ]
        ).lower()
        tokens = [t for t in target.split() if t]
        return all(tok in haystack for tok in tokens)

    def _to_job(self, item: dict[str, Any]) -> Job:
        tags = item.get("tags") or []
        if isinstance(tags, str):
            tags = [tags]
        skills = [str(t) for t in tags]

        salary_parts: list[str] = []
        if item.get("salary_min") and item.get("salary_max"):
            salary_parts.append(
                f"${int(item['salary_min']):,} - ${int(item['salary_max']):,}"
            )
        elif item.get("salary"):
            salary_parts.append(str(item["salary"]))

        url = item.get("url") or item.get("apply_url") or ""
        if url and not url.startswith("http"):
            url = f"https://remoteok.com{url}"

        return Job(
            title=str(item.get("position") or item.get("title") or "").strip(),
            company=str(item.get("company") or "").strip(),
            location=str(item.get("location") or "Remote").strip() or "Remote",
            url=url,
            platform=self.PLATFORM_NAME,
            description=str(item.get("description") or "").strip(),
            salary=" ".join(salary_parts),
            experience="",
            job_type="remote",
            posted_date=str(item.get("date") or "").strip(),
            skills=skills,
        )

    def search_one(self, title: str, city: str) -> list[Job]:
        """Return jobs from the feed matching `title`. `city` is ignored (remote-only)."""
        if self._should_stop():
            return []

        feed = self._fetch_feed()
        if not feed:
            return []

        matched: list[Job] = []
        for item in feed:
            if self._should_stop():
                break
            if not self._match_title(title, item):
                continue
            job = self._to_job(item)
            if not job.url or not job.title:
                continue
            matched.append(job)

        self.logger.info(
            "[remoteok] %d matches for title=%r (city ignored for remote feed).",
            len(matched), title,
        )
        return matched
