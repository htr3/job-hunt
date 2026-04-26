"""LinkedIn scraper — cookie-based, no Selenium.

Uses the `li_at` authentication cookie against the guest-job-search HTML
endpoint. Works without login for public listings too, but `li_at` improves
result quality and relevance.
"""
from __future__ import annotations

import urllib.parse
from typing import Any

from bs4 import BeautifulSoup  # type: ignore

from .base_scraper import BaseScraper, Job

_SEARCH_URL = (
    "https://www.linkedin.com/jobs-guest/jobs/api/seeMoreJobPostings/search"
    "?keywords={keywords}&location={location}&start={start}"
)

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}


class LinkedInScraper(BaseScraper):
    PLATFORM_NAME = "linkedin"

    def __init__(self, config: dict[str, Any]):
        super().__init__(config)
        li_cfg = (config.get("platforms", {}) or {}).get("linkedin", {}) or {}
        self.li_at: str = (li_cfg.get("li_at_cookie") or "").strip()
        if self.li_at:
            self.session.cookies.set("li_at", self.li_at, domain=".linkedin.com")

    def _fetch_page(self, keywords: str, location: str, start: int) -> str:
        url = _SEARCH_URL.format(
            keywords=urllib.parse.quote_plus(keywords),
            location=urllib.parse.quote_plus(location),
            start=start,
        )
        try:
            resp = self.session.get(url, headers=_HEADERS, timeout=30)
        except Exception as e:
            self.logger.warning("[linkedin] Network error (%s): %s", url, e)
            return ""
        if resp.status_code == 429:
            self.logger.warning("[linkedin] Rate-limited (429); backing off.")
            return ""
        if resp.status_code >= 400:
            self.logger.debug("[linkedin] HTTP %s for %s", resp.status_code, url)
            return ""
        return resp.text or ""

    @staticmethod
    def _text(node: Any) -> str:
        return node.get_text(strip=True) if node else ""

    def _parse_cards(self, html: str) -> list[Job]:
        if not html.strip():
            return []
        soup = BeautifulSoup(html, "html.parser")
        cards = soup.select("li, div.base-card")
        results: list[Job] = []
        for card in cards:
            title_el = card.select_one(
                ".base-search-card__title, h3.base-search-card__title"
            )
            company_el = card.select_one(
                ".base-search-card__subtitle a, .base-search-card__subtitle, h4"
            )
            loc_el = card.select_one(".job-search-card__location")
            link_el = card.select_one("a.base-card__full-link, a[href*='/jobs/view/']")
            posted_el = card.select_one("time")

            title = self._text(title_el)
            company = self._text(company_el)
            location = self._text(loc_el)
            href = link_el.get("href") if link_el else ""
            posted = posted_el.get("datetime") if posted_el else ""

            if not title or not href:
                continue

            url = href.split("?", 1)[0]
            if not url.startswith("http"):
                url = f"https://www.linkedin.com{url}"

            results.append(
                Job(
                    title=title,
                    company=company,
                    location=location,
                    url=url,
                    platform=self.PLATFORM_NAME,
                    posted_date=posted or "",
                )
            )
        return results

    def search_one(self, title: str, city: str) -> list[Job]:
        if self._should_stop():
            return []
        collected: list[Job] = []
        seen: set[str] = set()
        for start in (0, 25, 50):
            if self._should_stop():
                break
            html = self._fetch_page(title, city, start)
            batch = self._parse_cards(html)
            if not batch:
                break
            for job in batch:
                if job.url in seen:
                    continue
                seen.add(job.url)
                collected.append(job)
            self._polite_delay(1.5, 3.0)
        self.logger.info(
            "[linkedin] %d matches for title=%r, city=%r.", len(collected), title, city
        )
        return collected
