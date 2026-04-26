"""Scraper registry.

Phase 2 ships all 8 scrapers. The registry is keyed by the same platform name
used in `config.yaml -> platforms`.
"""
from __future__ import annotations

from typing import Type

from .base_scraper import (
    BaseScraper,
    Job,
    handle_driver_error,
    is_dead_driver_error,
    log_selenium_navigation_error,
)
from .foundit_scraper import FounditScraper
from .glassdoor_scraper import GlassdoorScraper
from .indeed_scraper import IndeedScraper
from .instahyre_scraper import InstahyreScraper
from .linkedin_scraper import LinkedInScraper
from .naukri_scraper import NaukriScraper
from .remoteok_scraper import RemoteOKScraper
from .wellfound_scraper import WellfoundScraper

SCRAPERS: dict[str, Type[BaseScraper]] = {
    "remoteok": RemoteOKScraper,
    "linkedin": LinkedInScraper,
    "naukri": NaukriScraper,
    "indeed": IndeedScraper,
    "glassdoor": GlassdoorScraper,
    "instahyre": InstahyreScraper,
    "foundit": FounditScraper,
    "wellfound": WellfoundScraper,
}

__all__ = [
    "SCRAPERS",
    "BaseScraper",
    "Job",
    "RemoteOKScraper",
    "LinkedInScraper",
    "NaukriScraper",
    "IndeedScraper",
    "GlassdoorScraper",
    "InstahyreScraper",
    "FounditScraper",
    "WellfoundScraper",
    "handle_driver_error",
    "is_dead_driver_error",
    "log_selenium_navigation_error",
]
