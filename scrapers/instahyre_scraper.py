"""Instahyre scraper — Selenium.

Instahyre's listings typically require login for full details. This scraper
runs anonymously against the public opportunity feed; when gated, it logs
a warning and returns whatever cards it could see.
"""
from __future__ import annotations

import urllib.parse

from .base_scraper import BaseScraper, Job, handle_driver_error


def _build_url(title: str, city: str) -> str:
    q = urllib.parse.quote_plus(title or "")
    loc = urllib.parse.quote_plus(city or "")
    base = "https://www.instahyre.com/search-jobs/"
    return f"{base}?search_string={q}&location={loc}" if q or loc else base


class InstahyreScraper(BaseScraper):
    PLATFORM_NAME = "instahyre"

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

        url = _build_url(title, city)
        try:
            self.driver.get(url)
        except WebDriverException as e:
            handle_driver_error(self, url, e)
            return []

        try:
            WebDriverWait(self.driver, 12).until(
                EC.presence_of_element_located(
                    (By.CSS_SELECTOR, "div.opportunity, div.job-card, li.opportunity")
                )
            )
        except TimeoutException:
            self.logger.info(
                "[instahyre] No visible cards for %r / %r (login may be required).",
                title, city,
            )
            return []

        try:
            cards = self.driver.find_elements(
                By.CSS_SELECTOR, "div.opportunity, div.job-card, li.opportunity"
            )
        except WebDriverException as e:
            handle_driver_error(self, url, e)
            return []

        collected: list[Job] = []
        seen: set[str] = set()
        for card in cards:
            if self._should_stop():
                break
            try:
                link = card.find_element(By.CSS_SELECTOR, "a")
                href = (link.get_attribute("href") or "").split("?", 1)[0]
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

            job_title = _safe("h3, div.title, a.title") or link.text.strip()
            company = _safe("div.company, h4, span.employer")
            location = _safe("div.location, span.locations")
            exp = _safe("div.experience, span.experience")
            salary = _safe("div.salary, span.salary")

            if not job_title:
                continue

            collected.append(
                Job(
                    title=job_title,
                    company=company,
                    location=location,
                    url=href,
                    platform=self.PLATFORM_NAME,
                    salary=salary,
                    experience=exp,
                )
            )

        self.logger.info(
            "[instahyre] %d matches for title=%r, city=%r.", len(collected), title, city
        )
        return collected
