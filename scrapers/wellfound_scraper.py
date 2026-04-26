"""Wellfound (formerly AngelList Talent) scraper — Selenium.

Wellfound's public role search usually gates heavy browsing behind auth.
This scraper reads whatever public role cards are visible on the discover
page for a given role slug and city.
"""
from __future__ import annotations

import re

from .base_scraper import BaseScraper, Job, handle_driver_error


def _slug(text: str) -> str:
    text = (text or "").lower().strip()
    text = re.sub(r"[^a-z0-9\s-]", "", text)
    text = re.sub(r"\s+", "-", text)
    return text


def _build_url(title: str, city: str) -> str:
    t = _slug(title) or "engineer"
    c = _slug(city)
    if c:
        return f"https://wellfound.com/role/l/{t}/{c}"
    return f"https://wellfound.com/role/{t}"


class WellfoundScraper(BaseScraper):
    PLATFORM_NAME = "wellfound"

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
                    (By.CSS_SELECTOR, "div[data-test='StartupResult'], a[href*='/jobs/']")
                )
            )
        except TimeoutException:
            self.logger.info(
                "[wellfound] No public cards for %r / %r (may require login).", title, city
            )
            return []

        try:
            cards = self.driver.find_elements(
                By.CSS_SELECTOR, "div[data-test='StartupResult'], div[data-test='JobSearchCard']"
            )
            if not cards:
                cards = self.driver.find_elements(By.CSS_SELECTOR, "a[href*='/jobs/']")
        except WebDriverException as e:
            handle_driver_error(self, url, e)
            return []

        collected: list[Job] = []
        seen: set[str] = set()
        for card in cards:
            if self._should_stop():
                break
            try:
                link = (
                    card if card.tag_name == "a" else card.find_element(By.CSS_SELECTOR, "a[href*='/jobs/']")
                )
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

            job_title = _safe("h2, h3, div.title") or link.text.strip()
            company = _safe("h3.company, a.company, div.company-name")
            location = _safe("span.location, div.location")
            salary = _safe("span.salary, div.salary")

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
                )
            )

        self.logger.info(
            "[wellfound] %d matches for title=%r, city=%r.", len(collected), title, city
        )
        return collected
