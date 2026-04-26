"""Glassdoor scraper — Selenium.

Scrapes the Jobs search page. Also opportunistically captures the company
rating shown on job cards for use by company_intel.
"""
from __future__ import annotations

import re
import urllib.parse
from typing import Any

from .base_scraper import BaseScraper, Job, handle_driver_error

_RATING_RE = re.compile(r"(\d+(?:\.\d+)?)")


def _build_url(title: str, city: str) -> str:
    q = urllib.parse.quote_plus(title or "")
    loc = urllib.parse.quote_plus(city or "")
    return f"https://www.glassdoor.com/Job/jobs.htm?sc.keyword={q}&locKeyword={loc}"


class GlassdoorScraper(BaseScraper):
    PLATFORM_NAME = "glassdoor"

    def __init__(self, config: dict[str, Any]):
        super().__init__(config)
        self.company_ratings: dict[str, float] = {}

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
                    (By.CSS_SELECTOR, "li.react-job-listing, div.JobsList_jobListItem__JBBUV, li[data-test='jobListing']")
                )
            )
        except TimeoutException:
            self.logger.info(
                "[glassdoor] No cards for %r / %r (may require login).", title, city
            )
            return []

        try:
            cards = self.driver.find_elements(
                By.CSS_SELECTOR,
                "li.react-job-listing, div.JobsList_jobListItem__JBBUV, li[data-test='jobListing']",
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
                link = card.find_element(By.CSS_SELECTOR, "a.jobLink, a[data-test='job-link']")
                href = (link.get_attribute("href") or "").split("?", 1)[0]
                job_title = link.text.strip()
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

            company_text = _safe("div.EmployerProfile_compactEmployerName__9MGcV, span.employerName, a.employerName")
            rating_text = _safe("span.rating, div.compactStars")
            location = _safe("div.location, span.loc, div[data-test='emp-location']")
            salary = _safe("div.salary-estimate, [data-test='detailSalary']")
            posted = _safe("div.listing-age, [data-test='job-age']")

            company = company_text.split("\n", 1)[0].strip()
            rating_match = _RATING_RE.search(rating_text or "")
            if company and rating_match:
                try:
                    self.company_ratings[company.lower()] = float(rating_match.group(1))
                except ValueError:
                    pass

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

        self.logger.info(
            "[glassdoor] %d matches for title=%r, city=%r.", len(collected), title, city
        )
        return collected
