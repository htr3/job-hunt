"""Naukri.com scraper — Selenium.

Two modes (configured via `platforms.naukri.mode` in config.yaml):

  - "search"       (default) — Anonymous keyword/city search via the public
                                /<title>-jobs-in-<city> URL pattern. No login
                                needed. This is the original behavior.

  - "recommended"            — Logs in with NAUKRI_EMAIL / NAUKRI_PASSWORD,
                                then scrapes /mnjuser/recommendedjobs (the
                                personalized feed Naukri builds from your
                                profile). Higher Easy-Apply ratio, so the
                                downstream auto_apply step succeeds far more
                                often. Skips the title × city loop entirely
                                because Naukri's algorithm picks the matches.
"""
from __future__ import annotations

import re
import time
from typing import Any

from .base_scraper import BaseScraper, Job, handle_driver_error


def _slug(text: str) -> str:
    text = (text or "").lower().strip()
    text = re.sub(r"[^a-z0-9\s-]", "", text)
    text = re.sub(r"\s+", "-", text)
    return text


def _build_url(title: str, city: str, page: int = 1) -> str:
    t = _slug(title) or "software"
    c = _slug(city)
    base = "https://www.naukri.com"
    slug = f"{t}-jobs-in-{c}" if c else f"{t}-jobs"
    suffix = "" if page <= 1 else f"-{page}"
    return f"{base}/{slug}{suffix}"


_RECOMMENDED_URL = "https://www.naukri.com/mnjuser/recommendedjobs"


def _build_listing_url(title: str, company: str, location: str, job_id: str) -> str:
    """Construct a Naukri job-listing URL from card metadata.

    Naukri's URL router parses only the trailing numeric job-id; everything
    before it is decorative SEO slug. So as long as the URL ends in
    ``-<job_id>`` it resolves to the right job page.
    """
    slug_parts = [_slug(title), _slug(company), _slug(location)]
    slug_parts = [p for p in slug_parts if p]
    suffix = f"-{job_id}" if job_id else ""
    if not slug_parts:
        return f"https://www.naukri.com/job-listings{suffix}"
    return f"https://www.naukri.com/job-listings-{'-'.join(slug_parts)}{suffix}"


class NaukriScraper(BaseScraper):
    PLATFORM_NAME = "naukri"

    MAX_PAGES = 3
    # How many "scroll to bottom" cycles to do on the recommended page before
    # giving up on more lazy-loaded cards.
    RECOMMENDED_SCROLL_ROUNDS = 8

    # ------------------------------------------------------------------ entry point

    def search_all(self) -> list[Job]:
        """Override base search_all to dispatch on `mode`."""
        mode = self._mode()
        if mode == "recommended":
            return self._search_recommended()
        return super().search_all()

    def _mode(self) -> str:
        platforms = self.config.get("platforms", {}) or {}
        naukri = platforms.get("naukri", {}) or {}
        return str(naukri.get("mode", "search")).strip().lower() or "search"

    # ------------------------------------------------------------------ mode: search

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

        for page in range(1, self.MAX_PAGES + 1):
            if self._should_stop():
                break
            url = _build_url(title, city, page)
            try:
                self.driver.get(url)
            except WebDriverException as e:
                handle_driver_error(self, url, e)
                return collected

            try:
                WebDriverWait(self.driver, 12).until(
                    EC.presence_of_element_located((By.CSS_SELECTOR, "div.srp-jobtuple-wrapper, article.jobTuple"))
                )
            except TimeoutException:
                self.logger.info(
                    "[naukri] No cards appeared on page %d for %r / %r.", page, title, city
                )
                break

            try:
                cards = self.driver.find_elements(
                    By.CSS_SELECTOR, "div.srp-jobtuple-wrapper, article.jobTuple"
                )
            except WebDriverException as e:
                handle_driver_error(self, url, e)
                return collected

            if not cards:
                break

            for card in cards:
                if self._should_stop():
                    break
                try:
                    title_el = card.find_element(By.CSS_SELECTOR, "a.title, a.jobTitle")
                    job_title = title_el.text.strip()
                    job_url = (title_el.get_attribute("href") or "").split("?", 1)[0]
                except (NoSuchElementException, StaleElementReferenceException):
                    continue
                if not job_url or job_url in seen:
                    continue
                seen.add(job_url)

                def _safe(css: str) -> str:
                    try:
                        return card.find_element(By.CSS_SELECTOR, css).text.strip()
                    except (NoSuchElementException, StaleElementReferenceException):
                        return ""

                company = _safe("a.comp-name, a.subTitle")
                location = _safe("span.locWdth, li.location")
                exp = _safe("span.expwdth, li.experience")
                salary = _safe("span.sal-wrap, li.salary")
                posted = _safe("span.job-post-day, span.fleft.postedDate")

                try:
                    tags = card.find_elements(By.CSS_SELECTOR, "ul.tags-gt li, ul.tags li")
                    skills = [t.text.strip() for t in tags if t.text.strip()]
                except (NoSuchElementException, StaleElementReferenceException):
                    skills = []

                collected.append(
                    Job(
                        title=job_title,
                        company=company,
                        location=location,
                        url=job_url,
                        platform=self.PLATFORM_NAME,
                        salary=salary,
                        experience=exp,
                        posted_date=posted,
                        skills=skills,
                    )
                )

            self._polite_delay(1.0, 2.5)

        self.logger.info(
            "[naukri] %d matches for title=%r, city=%r.", len(collected), title, city
        )
        return collected

    # ------------------------------------------------------------------ mode: recommended

    def _search_recommended(self) -> list[Job]:
        """Login + scrape /mnjuser/recommendedjobs.

        Honors the same `output.max_results_per_platform`,
        `output.max_results_per_company`, `company_intel.blacklist/whitelist`,
        and `_runtime.skip_urls` knobs as the base flow.
        """
        from selenium.common.exceptions import (
            TimeoutException,
            WebDriverException,
        )
        from selenium.webdriver.common.by import By
        from selenium.webdriver.support import expected_conditions as EC
        from selenium.webdriver.support.ui import WebDriverWait

        if self._should_stop():
            return []

        # Credentials
        platforms = self.config.get("platforms", {}) or {}
        naukri = platforms.get("naukri", {}) or {}
        email = (naukri.get("email") or "").strip()
        password = (naukri.get("password") or "").strip()
        if not email or not password or email.startswith("${") or password.startswith("${"):
            self.logger.warning(
                "[naukri] mode=recommended needs NAUKRI_EMAIL / NAUKRI_PASSWORD in .env. "
                "Falling back to mode=search."
            )
            return super().search_all()

        # Login
        try:
            from naukri_auth import naukri_login
        except ImportError:
            self.logger.error("[naukri] naukri_auth helper missing; cannot use recommended mode.")
            return []

        try:
            driver = self.driver
        except WebDriverException as e:
            self.logger.error("[naukri] driver init failed: %s", e)
            return []

        if not naukri_login(driver, email, password, logger=self.logger):
            self.logger.error("[naukri] login failed; cannot scrape recommended jobs.")
            return []

        try:
            driver.get(_RECOMMENDED_URL)
        except WebDriverException as e:
            handle_driver_error(self, _RECOMMENDED_URL, e)
            return []

        # Wait for at least one card-like element. Recommended page DOM has
        # changed shape over the years, so try multiple selectors. As of
        # 2026, /mnjuser/recommendedjobs uses `article.jobTuple` with a
        # `data-job-id` attribute and NO inner anchor — the card is
        # JS-clickable. We construct the listing URL from data-job-id below.
        card_selectors = [
            "article.jobTuple",
            "div.srp-jobtuple-wrapper",
            "div.styles_jlc__main__VdwtF",
        ]
        try:
            WebDriverWait(driver, 15).until(
                EC.presence_of_element_located(
                    (By.CSS_SELECTOR, ", ".join(card_selectors))
                )
            )
        except TimeoutException:
            self.logger.warning("[naukri] No cards found on recommended page.")
            return []

        # Pagination on the recommended page is mostly infinite scroll.
        # Scroll a few times to trigger lazy-loads.
        last_count = -1
        for _ in range(self.RECOMMENDED_SCROLL_ROUNDS):
            if self._should_stop():
                break
            try:
                driver.execute_script("window.scrollTo(0, document.body.scrollHeight);")
            except WebDriverException:
                break
            time.sleep(1.5)
            try:
                cards = driver.find_elements(By.CSS_SELECTOR, ", ".join(card_selectors))
            except WebDriverException:
                cards = []
            if len(cards) == last_count:
                break
            last_count = len(cards)

        # Snapshot all card metadata in one atomic JavaScript call.
        # Iterating WebElements one-by-one in Python is fragile here because
        # Naukri's recommended feed re-renders cards as you scroll (React
        # virtualization), so half of any pre-collected `find_elements()`
        # list goes stale before we can read its sub-elements. A single
        # synchronous `execute_script` returns plain dicts immune to that.
        try:
            raw = driver.execute_script(
                """
                const sels = arguments[0];
                const cards = document.querySelectorAll(sels);
                const txt = (el) => (el && (el.innerText || el.textContent) || '').trim();
                return Array.from(cards).map(c => {
                    const titleEl = c.querySelector('p.title, a.title, a.jobTitle');
                    const titleAnchor = c.querySelector('a.title, a.jobTitle');
                    return {
                        id: c.getAttribute('data-job-id') || '',
                        title: txt(titleEl),
                        href: (titleAnchor && titleAnchor.href) || '',
                        company: txt(c.querySelector('span.subTitle, a.subTitle, a.comp-name, span.comp-name')),
                        location: txt(c.querySelector('li.location span, span.locWdth, span.loc')),
                        exp: txt(c.querySelector('li.experience span, span.expwdth, span.exp')),
                        salary: txt(c.querySelector('li.salary span, span.sal-wrap, span.sal')),
                        posted: txt(c.querySelector('div.type span, span.job-post-day, span.fleft.postedDate')),
                        skills: Array.from(c.querySelectorAll('ul.tags li, ul.tags-gt li'))
                            .map(t => (t.innerText || t.textContent || '').trim())
                            .filter(s => s),
                    };
                });
                """,
                ", ".join(card_selectors),
            ) or []
        except WebDriverException as e:
            handle_driver_error(self, _RECOMMENDED_URL, e)
            return []

        self.logger.info(
            "[naukri] Snapshotted %d card metadata records from recommended page.",
            len(raw),
        )

        # Apply the same caps and filters the base scraper applies in search_all.
        output = self.config.get("output", {}) or {}
        cap = int(output.get("max_results_per_platform", 50) or 0)
        per_company_cap = int(output.get("max_results_per_company", 0) or 0)
        company_intel = self.config.get("company_intel", {}) or {}
        bl_terms = [s.lower().strip() for s in (company_intel.get("blacklist") or []) if s]
        wl_terms = [s.lower().strip() for s in (company_intel.get("whitelist") or []) if s]
        runtime = self.config.get("_runtime") or {}
        skip_urls: set[str] = set(runtime.get("skip_urls") or set())

        collected: list[Job] = []
        seen: set[str] = set()
        per_company: dict[str, int] = {}
        blocked = 0
        stale_drops = 0
        no_url = 0

        for rec in raw:
            if self._should_stop():
                break
            if cap and len(collected) >= cap:
                break

            job_title = (rec.get("title") or "").strip()
            company = (rec.get("company") or "").strip()
            location = (rec.get("location") or "").strip()
            exp = (rec.get("exp") or "").strip()
            salary = (rec.get("salary") or "").strip()
            posted = (rec.get("posted") or "").strip()
            skills = list(rec.get("skills") or [])

            href = (rec.get("href") or "").split("?", 1)[0]
            job_id = (rec.get("id") or "").strip()

            if href:
                job_url = href
            elif job_id:
                job_url = _build_listing_url(job_title, company, location, job_id)
            else:
                no_url += 1
                continue

            if not job_url or job_url in seen:
                continue
            seen.add(job_url)

            if job_url in skip_urls:
                stale_drops += 1
                continue

            cmp_lower = (company or "").lower()
            if bl_terms and any(b in cmp_lower for b in bl_terms):
                blocked += 1
                continue
            if wl_terms and not any(w in cmp_lower for w in wl_terms):
                blocked += 1
                continue

            if per_company_cap > 0 and company:
                if per_company.get(cmp_lower, 0) >= per_company_cap:
                    continue
                per_company[cmp_lower] = per_company.get(cmp_lower, 0) + 1

            collected.append(
                Job(
                    title=job_title or "(unknown)",
                    company=company,
                    location=location,
                    url=job_url,
                    platform=self.PLATFORM_NAME,
                    salary=salary,
                    experience=exp,
                    posted_date=posted,
                    skills=skills,
                )
            )

        self.logger.info(
            "[naukri] recommended-mode results: %d kept, %d blocked, %d stale, %d no_url.",
            len(collected), blocked, stale_drops, no_url,
        )
        return collected
