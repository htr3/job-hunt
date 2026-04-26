"""Indeed scraper — Selenium, with Cloudflare challenge detection.

Uses the public search URL. If Cloudflare/Imperva shows an interstitial,
we log a warning and bail on that city/title pair rather than looping.
"""
from __future__ import annotations

import urllib.parse
from typing import Any

from .base_scraper import BaseScraper, Job, handle_driver_error

_CLOUDFLARE_HINTS = (
    "checking your browser",
    "just a moment",
    "attention required",
    "please verify you are a human",
    "cf-challenge",
)


def _build_url(title: str, city: str, start: int = 0) -> str:
    q = urllib.parse.quote_plus(title or "")
    loc = urllib.parse.quote_plus(city or "")
    return f"https://www.indeed.com/jobs?q={q}&l={loc}&start={start}"


class IndeedScraper(BaseScraper):
    PLATFORM_NAME = "indeed"

    MAX_PAGES = 2

    def _looks_blocked(self, page_text: str) -> bool:
        t = page_text.lower()
        return any(h in t for h in _CLOUDFLARE_HINTS)

    def search_one(self, title: str, city: str) -> list[Job]:
        if self._should_stop():
            return []

        from selenium.common.exceptions import (
            NoSuchElementException,
            StaleElementReferenceException,
            TimeoutException,
            WebDriverException,
        )
        from selenium.webdriver.common.by import By
        from selenium.webdriver.support import expected_conditions as EC
        from selenium.webdriver.support.ui import WebDriverWait

        collected: list[Job] = []
        seen: set[str] = set()

        for page in range(self.MAX_PAGES):
            if self._should_stop():
                break
            url = _build_url(title, city, start=page * 10)
            try:
                self.driver.get(url)
            except WebDriverException as e:
                handle_driver_error(self, url, e)
                return collected

            try:
                body_text = self.driver.find_element(By.TAG_NAME, "body").text
            except WebDriverException as e:
                handle_driver_error(self, url, e)
                return collected

            if self._looks_blocked(body_text):
                self.logger.warning(
                    "[indeed] Cloudflare challenge detected for %r / %r — skipping.",
                    title, city,
                )
                return collected

            try:
                WebDriverWait(self.driver, 10).until(
                    EC.presence_of_element_located(
                        (By.CSS_SELECTOR, "div.job_seen_beacon, li div.cardOutline")
                    )
                )
            except TimeoutException:
                self.logger.info(
                    "[indeed] No cards on page %d for %r / %r.", page + 1, title, city
                )
                break

            try:
                cards = self.driver.find_elements(
                    By.CSS_SELECTOR, "div.job_seen_beacon, li div.cardOutline"
                )
            except WebDriverException as e:
                handle_driver_error(self, url, e)
                return collected

            for card in cards:
                if self._should_stop():
                    break
                try:
                    title_el = card.find_element(By.CSS_SELECTOR, "h2.jobTitle a, a.jcs-JobTitle")
                    job_title = title_el.text.strip() or title_el.get_attribute("aria-label") or ""
                    href = (title_el.get_attribute("href") or "").split("&", 1)[0]
                except (NoSuchElementException, StaleElementReferenceException):
                    continue
                if not href or href in seen:
                    continue
                seen.add(href)

                def _safe(css: str) -> str:
                    try:
                        return card.find_element(By.CSS_SELECTOR, css).text.strip()
                    except (NoSuchElementException, StaleElementReferenceException):
                        return ""

                company = _safe("span.companyName, [data-testid='company-name']")
                location = _safe("div.companyLocation, [data-testid='text-location']")
                salary = _safe("div.metadata.salary-snippet-container, div.salary-snippet-container")
                posted = _safe("span.date, [data-testid='myJobsStateDate']")

                collected.append(
                    Job(
                        title=job_title,
                        company=company,
                        location=location,
                        url=href,
                        platform=self.PLATFORM_NAME,
                        salary=salary,
                        posted_date=posted,
                    )
                )

            self._polite_delay(1.5, 3.0)

        self.logger.info(
            "[indeed] %d matches for title=%r, city=%r.", len(collected), title, city
        )
        return collected
