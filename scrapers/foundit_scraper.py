"""Foundit (formerly Monster India) scraper — Selenium."""
from __future__ import annotations

import re

from .base_scraper import BaseScraper, Job, handle_driver_error


def _slug(text: str) -> str:
    text = (text or "").lower().strip()
    text = re.sub(r"[^a-z0-9\s-]", "", text)
    text = re.sub(r"\s+", "-", text)
    return text


def _build_url(title: str, city: str) -> str:
    t = _slug(title) or "software"
    c = _slug(city)
    base = "https://www.foundit.in/srp/results"
    if c:
        return f"{base}?query={t}&locations={c}"
    return f"{base}?query={t}"


class FounditScraper(BaseScraper):
    PLATFORM_NAME = "foundit"

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
                    (By.CSS_SELECTOR, "div.cardContainer, div.srpResultCardContainer, li.srp-jobtuple")
                )
            )
        except TimeoutException:
            self.logger.info(
                "[foundit] No cards for %r / %r.", title, city
            )
            return []

        try:
            cards = self.driver.find_elements(
                By.CSS_SELECTOR, "div.cardContainer, div.srpResultCardContainer, li.srp-jobtuple"
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

            job_title = _safe("h3, div.jobTitle, a.title") or link.text.strip()
            company = _safe("div.companyName, span.companyName, a.compName")
            location = _safe("div.location, span.jobLocation, li.location")
            exp = _safe("div.experience, span.experience")
            salary = _safe("div.salary, span.salary")
            posted = _safe("div.postedDate, span.postedDate")

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
                    posted_date=posted,
                )
            )

        self.logger.info(
            "[foundit] %d matches for title=%r, city=%r.", len(collected), title, city
        )
        return collected
