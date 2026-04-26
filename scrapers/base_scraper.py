"""Base scraper contract for the Job Hunt AI Agent.

All platform scrapers inherit from `BaseScraper` and implement `search_one`.
`search_all` drives the cartesian product of configured titles x cities and
respects a cooperative shutdown event for graceful Ctrl+C.
"""
from __future__ import annotations

import abc
import logging
import os
import random
import shutil
import threading
import time
from dataclasses import dataclass, field
from typing import Any

try:
    import cloudscraper  # type: ignore
except ImportError:  # pragma: no cover
    cloudscraper = None  # type: ignore

import requests

logger = logging.getLogger(__name__)


# --- Dead-driver detection ---------------------------------------------------

_DEAD_DRIVER_HINTS = (
    "no such window",
    "invalid session id",
    "chrome not reachable",
    "target window already closed",
    "web view not found",
    "session deleted",
    "disconnected",
    "10054",
    "connection aborted",
    "remote end closed connection",
    "cannot determine loading status",
)


def is_dead_driver_error(exc: BaseException) -> bool:
    """Return True if `exc` looks like the Chrome window / driver died."""
    msg = str(exc).lower()
    return any(hint in msg for hint in _DEAD_DRIVER_HINTS)


def log_selenium_navigation_error(
    log: logging.Logger, url: str, exc: BaseException
) -> None:
    """One-line warning for Selenium `driver.get` failures."""
    log.warning("Selenium navigation failed for %s: %s", url, str(exc).splitlines()[0][:200])


def handle_driver_error(scraper: "BaseScraper", url: str, exc: BaseException) -> None:
    """If the Chrome driver appears dead, mark the scraper and log a hint.

    Call this from `driver.get(...)` except blocks instead of re-raising.
    """
    log_selenium_navigation_error(scraper.logger, url, exc)
    if is_dead_driver_error(exc):
        scraper._driver_dead = True
        scraper.logger.warning(
            "[%s] Chrome window appears closed/dead — aborting this scraper. "
            "Hint: set auto_apply.headless: true in config.yaml to avoid this.",
            scraper.PLATFORM_NAME,
        )


# --- Job model ---------------------------------------------------------------


@dataclass
class Job:
    title: str
    company: str
    location: str
    url: str
    platform: str
    description: str = ""
    salary: str = ""
    experience: str = ""
    job_type: str = ""
    posted_date: str = ""
    skills: list[str] = field(default_factory=list)
    match_score: float = 0.0


# --- BaseScraper -------------------------------------------------------------


class BaseScraper(abc.ABC):
    """Abstract base class for all platform scrapers."""

    PLATFORM_NAME: str = "base"

    def __init__(self, config: dict[str, Any]):
        self.config = config
        self.logger = logging.getLogger(f"scraper.{self.PLATFORM_NAME}")
        self.jobs: list[Job] = []
        self.shutdown_event: threading.Event | None = None
        self._driver_dead: bool = False
        self._driver: Any = None  # lazy Selenium driver

        if cloudscraper is not None:
            try:
                self.session = cloudscraper.create_scraper()
            except Exception:  # pragma: no cover — fallback
                self.session = requests.Session()
        else:
            self.session = requests.Session()

    # --- lifecycle -----------------------------------------------------------

    def set_shutdown_event(self, event: threading.Event) -> None:
        self.shutdown_event = event

    def _should_stop(self) -> bool:
        if self._driver_dead:
            return True
        if self.shutdown_event is not None and self.shutdown_event.is_set():
            return True
        return False

    @property
    def driver(self) -> Any:
        """Lazy Selenium Chrome driver. Created on first access."""
        if self._driver is None:
            self._driver = self._build_driver()
        return self._driver

    def _build_driver(self) -> Any:
        """Build a headless-by-default Chrome/Chromium driver.

        Driver resolution order:
          1. ``CHROMEDRIVER_PATH`` env var (Docker / VPS deploys set this).
          2. System ``chromedriver`` on PATH (Linux package managers, Homebrew).
          3. ``webdriver_manager`` download (last resort, slow + race-prone).

        Same for the browser binary via ``CHROME_BIN`` (e.g. /usr/bin/chromium).
        """
        from selenium import webdriver
        from selenium.webdriver.chrome.options import Options
        from selenium.webdriver.chrome.service import Service

        options = Options()
        headless = self.config.get("auto_apply", {}).get("headless", True)
        if headless:
            options.add_argument("--headless=new")
        options.add_argument("--disable-gpu")
        options.add_argument("--no-sandbox")
        options.add_argument("--disable-dev-shm-usage")
        options.add_argument("--window-size=1920,1080")
        options.add_argument(
            "--user-agent=Mozilla/5.0 (X11; Linux x86_64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
        )
        profile_dir = self.config.get("auto_apply", {}).get("chrome_profile_dir", "")
        if profile_dir:
            options.add_argument(f"--user-data-dir={profile_dir}")

        chrome_bin = os.environ.get("CHROME_BIN", "").strip()
        if chrome_bin:
            options.binary_location = chrome_bin

        driver_path = self._resolve_chromedriver()
        if driver_path:
            service = Service(executable_path=driver_path)
            return webdriver.Chrome(service=service, options=options)

        # Selenium 4.6+ has its own driver autoresolver (Selenium Manager) baked
        # in. Try that before reaching for webdriver_manager.
        try:
            return webdriver.Chrome(options=options)
        except Exception:
            pass

        try:
            from webdriver_manager.chrome import ChromeDriverManager  # type: ignore
        except ImportError:
            raise RuntimeError(
                "No chromedriver found. Set CHROMEDRIVER_PATH, install chromedriver "
                "on your PATH, or `pip install webdriver-manager`."
            )
        service = Service(ChromeDriverManager().install())
        return webdriver.Chrome(service=service, options=options)

    @staticmethod
    def _resolve_chromedriver() -> str:
        """Return a usable chromedriver path or ''."""
        env_path = os.environ.get("CHROMEDRIVER_PATH", "").strip()
        if env_path and os.path.isfile(env_path):
            return env_path
        which = shutil.which("chromedriver")
        if which:
            return which
        return ""

    def close(self) -> None:
        """Clean up driver if one was created."""
        if self._driver is not None:
            try:
                self._driver.quit()
            except Exception:
                pass
            self._driver = None

    # --- helpers -------------------------------------------------------------

    def _polite_delay(self, min_seconds: float = 1.0, max_seconds: float = 3.0) -> None:
        """Randomized pause to mimic human behavior."""
        time.sleep(random.uniform(min_seconds, max_seconds))

    def _excluded(self, job: Job, excluded_keywords: list[str]) -> bool:
        if not excluded_keywords:
            return False
        haystack = f"{job.title} {job.description}".lower()
        return any(kw.lower() in haystack for kw in excluded_keywords if kw)

    # --- API to implement ----------------------------------------------------

    @abc.abstractmethod
    def search_one(self, title: str, city: str) -> list[Job]:
        """Scrape a single (title, city) combination and return jobs."""

    # --- driver --------------------------------------------------------------

    def search_all(self) -> list[Job]:
        """Iterate job_titles x preferred_cities and aggregate results."""
        search = self.config.get("search", {}) or {}
        loc = self.config.get("location", {}) or {}
        output = self.config.get("output", {}) or {}

        titles: list[str] = [t for t in (search.get("job_titles") or []) if t]
        cities: list[str] = [c for c in (loc.get("preferred_cities") or []) if c]
        if not titles:
            self.logger.warning("[%s] No job_titles configured; nothing to search.", self.PLATFORM_NAME)
            return []
        if not cities:
            cities = [""]  # allow location-less search

        excluded = [k for k in (search.get("excluded_keywords") or []) if k]
        cap = int(output.get("max_results_per_platform", 50))
        # Per-company cap prevents a single company (e.g. Accenture on Naukri,
        # which spam-posts the same role 100+ times) from filling the entire
        # result set. Configurable via output.max_results_per_company.
        # Missing / None → default 5. Explicit 0 (or negative) → disabled.
        per_company_cap_raw = output.get("max_results_per_company", 5)
        if per_company_cap_raw is None:
            per_company_cap = 5
        else:
            try:
                per_company_cap = max(0, int(per_company_cap_raw))
            except (TypeError, ValueError):
                per_company_cap = 5

        # Apply company blacklist / whitelist DURING scraping so they don't
        # eat into the per-platform cap. If a user blacklists Accenture and
        # asks for 50 results, they should get 50 non-Accenture results, not
        # 50 minus however many Accenture postings happened to show up first.
        company_intel = self.config.get("company_intel", {}) or {}
        bl_terms = [s.lower().strip() for s in (company_intel.get("blacklist") or []) if s]
        wl_terms = [s.lower().strip() for s in (company_intel.get("whitelist") or []) if s]

        # When --new-only is on, job_hunter.py stashes the set of URLs that
        # already exist in the DB here. We treat them like blacklisted entries
        # so they don't fill the per-platform cap. Result: cap=50 with all 50
        # current results stale → scraper keeps walking title×city combos
        # until it finds 50 brand-new postings (or runs out of searches).
        runtime = self.config.get("_runtime") or {}
        skip_urls: set[str] = set(runtime.get("skip_urls") or set())

        aggregated: list[Job] = []
        seen_urls: set[str] = set()
        company_counts: dict[str, int] = {}
        company_skips: dict[str, int] = {}
        bl_drops = 0
        wl_drops = 0
        stale_drops = 0

        total_combos = len(titles) * max(len(cities), 1)
        self.logger.info(
            "[%s] Starting: %d title(s) x %d city(ies) = %d searches (cap=%d).",
            self.PLATFORM_NAME, len(titles), len(cities), total_combos, cap,
        )
        done_combos = 0

        for title in titles:
            if self._should_stop():
                break
            for city in cities:
                if self._should_stop():
                    break
                done_combos += 1
                self.logger.info(
                    "[%s] (%d/%d) searching %r / %r ...",
                    self.PLATFORM_NAME, done_combos, total_combos, title, city or "any",
                )
                t0 = time.time()
                try:
                    results = self.search_one(title, city) or []
                except Exception as e:
                    if is_dead_driver_error(e):
                        self._driver_dead = True
                        self.logger.warning(
                            "[%s] Driver died during %r / %r — aborting scraper.",
                            self.PLATFORM_NAME, title, city,
                        )
                        break
                    self.logger.exception(
                        "[%s] search_one(%r, %r) raised: %s",
                        self.PLATFORM_NAME, title, city, e,
                    )
                    continue

                added = 0
                for job in results:
                    if not job.url or job.url in seen_urls:
                        continue
                    if self._excluded(job, excluded):
                        continue
                    if skip_urls and job.url in skip_urls:
                        stale_drops += 1
                        continue
                    company_key = (job.company or "").strip().lower()
                    if bl_terms and company_key and any(t in company_key for t in bl_terms):
                        bl_drops += 1
                        continue
                    if wl_terms and not (company_key and any(t in company_key for t in wl_terms)):
                        wl_drops += 1
                        continue
                    if per_company_cap > 0:
                        if company_key and company_counts.get(company_key, 0) >= per_company_cap:
                            company_skips[company_key] = company_skips.get(company_key, 0) + 1
                            continue
                        if company_key:
                            company_counts[company_key] = company_counts.get(company_key, 0) + 1
                    seen_urls.add(job.url)
                    aggregated.append(job)
                    added += 1
                    if len(aggregated) >= cap:
                        self.logger.info(
                            "[%s] (%d/%d) +%d jobs in %.1fs  --> total %d (cap hit, stopping).",
                            self.PLATFORM_NAME, done_combos, total_combos,
                            added, time.time() - t0, len(aggregated),
                        )
                        self.jobs = aggregated
                        return aggregated

                self.logger.info(
                    "[%s] (%d/%d) +%d jobs in %.1fs  --> total %d",
                    self.PLATFORM_NAME, done_combos, total_combos,
                    added, time.time() - t0, len(aggregated),
                )
                self._polite_delay(0.5, 1.5)

        self.jobs = aggregated
        if company_skips:
            top_skipped = sorted(company_skips.items(), key=lambda x: -x[1])[:3]
            summary = ", ".join(f"{c or '(blank)'}={n}" for c, n in top_skipped)
            self.logger.info(
                "[%s] Per-company cap (%d) skipped extras: %s",
                self.PLATFORM_NAME, per_company_cap, summary,
            )
        if bl_drops or wl_drops:
            self.logger.info(
                "[%s] Company filter dropped: blacklist=%d, whitelist=%d.",
                self.PLATFORM_NAME, bl_drops, wl_drops,
            )
        if stale_drops:
            self.logger.info(
                "[%s] --new-only skipped %d already-known URL(s) during search.",
                self.PLATFORM_NAME, stale_drops,
            )
        self.logger.info("[%s] Collected %d jobs.", self.PLATFORM_NAME, len(aggregated))
        return aggregated
