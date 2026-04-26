"""LinkedIn "warm intro" finder.

For each of the top-N jobs, hit LinkedIn's public people search (authenticated
with the user's `li_at` cookie) and return a handful of employees at the target
company who may be worth a connection request. Optionally generate personalized
outreach message templates.

Writes one Markdown per company under `network/<company>__<timestamp>.md`.

Best-effort only: LinkedIn aggressively changes DOM / gates endpoints and can
rate-limit or soft-block requests. Every failure is caught and logged, never
raises into the main pipeline.

`auto_connect` is scaffolded but intentionally NOT executed — automated
connection requests are a fast path to account restriction. The scaffolding
lets you wire your own browser automation later if you choose.
"""
from __future__ import annotations

import json
import logging
import re
import time
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

import requests
from bs4 import BeautifulSoup

logger = logging.getLogger("linkedin_network")

PROJECT_ROOT = Path(__file__).resolve().parent
OUT_DIR = PROJECT_ROOT / "network"

_DEFAULT_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9",
}


def _safe_slug(s: str, max_len: int = 50) -> str:
    slug = re.sub(r"[^A-Za-z0-9._-]+", "_", (s or "").strip())
    return slug[:max_len].strip("_") or "company"


class LinkedInNetwork:
    def __init__(self, config: dict[str, Any]) -> None:
        self.config = config or {}
        ln = self.config.get("linkedin_network", {}) or {}
        self.enabled: bool = bool(ln.get("enabled"))
        self.top_n: int = int(ln.get("top_n") or 5)
        self.auto_connect: bool = bool(ln.get("auto_connect"))

        self.li_at = (
            (self.config.get("platforms", {}) or {}).get("linkedin", {}) or {}
        ).get("li_at_cookie", "") or ""
        self.li_at = (self.li_at or "").strip()

    def available(self) -> bool:
        return bool(self.li_at) and not self.li_at.startswith("${")

    def find(self, jobs: list) -> list[Path]:
        if not self.enabled:
            return []
        if not jobs:
            return []
        if not self.available():
            logger.warning(
                "linkedin_network enabled but LI_AT_COOKIE is missing — skipping."
            )
            return []

        # Pick unique companies from the top-N scored jobs, preserving order.
        seen: set[str] = set()
        unique_companies: list[tuple[str, Any]] = []
        for j in jobs:
            c = (getattr(j, "company", "") or "").strip()
            if not c or c.lower() in seen:
                continue
            seen.add(c.lower())
            unique_companies.append((c, j))
            if len(unique_companies) >= self.top_n:
                break

        if not unique_companies:
            return []

        OUT_DIR.mkdir(parents=True, exist_ok=True)
        session = requests.Session()
        session.headers.update(_DEFAULT_HEADERS)
        session.cookies.set("li_at", self.li_at, domain=".linkedin.com")

        written: list[Path] = []
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        for company, job in unique_companies:
            try:
                contacts = self._search_people(session, company)
            except Exception as e:
                logger.warning("LinkedIn search for %r failed: %s", company, e)
                contacts = []

            if not contacts:
                logger.info("LinkedIn: no contacts found for %s", company)

            md = self._render_packet(company, job, contacts)
            path = OUT_DIR / f"{_safe_slug(company)}__{stamp}.md"
            path.write_text(md, encoding="utf-8")
            written.append(path)
            time.sleep(1.5)  # polite gap between requests

        if self.auto_connect:
            logger.warning(
                "linkedin_network.auto_connect=true is intentionally a no-op in this build "
                "(automated connection requests get accounts restricted). "
                "Open the generated %s files and send connects manually.", OUT_DIR,
            )

        logger.info("LinkedIn network: wrote %d packet(s) to %s", len(written), OUT_DIR)
        return written

    # ------------------------------------------------------------------ internals

    def _search_people(self, session: requests.Session, company: str) -> list[dict[str, Any]]:
        """Scrape a handful of profile cards from LinkedIn's people search.

        LinkedIn's HTML changes frequently — this parser is deliberately
        forgiving and returns [] if the structure is unrecognizable.
        """
        q = urlencode({"keywords": company, "origin": "GLOBAL_SEARCH_HEADER"})
        url = f"https://www.linkedin.com/search/results/people/?{q}"
        r = session.get(url, timeout=20, allow_redirects=True)
        if r.status_code in (401, 403):
            logger.warning(
                "LinkedIn returned %s for people search — `li_at` cookie likely expired.",
                r.status_code,
            )
            return []
        if r.status_code != 200:
            logger.debug("LinkedIn returned %s for %s", r.status_code, url)
            return []

        soup = BeautifulSoup(r.text, "html.parser")
        out: list[dict[str, Any]] = []

        # Strategy 1: JSON-LD blobs (present on some logged-in pages).
        for script in soup.find_all("script", type="application/ld+json"):
            try:
                data = json.loads(script.string or "{}")
            except json.JSONDecodeError:
                continue
            items = data.get("itemListElement") if isinstance(data, dict) else None
            if not isinstance(items, list):
                continue
            for it in items:
                person = (it or {}).get("item") or {}
                name = person.get("name") or ""
                link = person.get("url") or ""
                if name and link and "linkedin.com/in/" in link:
                    out.append({"name": name.strip(), "headline": "", "profile": link.strip()})
            if out:
                break

        # Strategy 2: fall back to plain HTML anchors to /in/ profiles.
        if not out:
            anchors = soup.select("a[href*='/in/']")
            seen_profiles: set[str] = set()
            for a in anchors:
                href = (a.get("href") or "").split("?")[0]
                if not href.startswith(("/in/", "https://www.linkedin.com/in/")):
                    continue
                profile = "https://www.linkedin.com" + href if href.startswith("/in/") else href
                if profile in seen_profiles:
                    continue
                name = a.get_text(strip=True)
                if not name or len(name) > 80:
                    continue
                seen_profiles.add(profile)
                out.append({"name": name, "headline": "", "profile": profile})
                if len(out) >= 8:
                    break

        return out[:8]

    def _render_packet(
        self,
        company: str,
        job: Any,
        contacts: list[dict[str, Any]],
    ) -> str:
        profile = self.config.get("profile", {}) or {}
        my_name = profile.get("name") or ""
        my_role = profile.get("current_role") or ""

        md = [
            f"# Warm-intro targets at {company}",
            f"_Generated {datetime.now().isoformat(timespec='seconds')}_",
            "",
            f"**Target job:** {getattr(job, 'title', '')}",
            f"**Posting:** {getattr(job, 'url', '')}",
            "",
            "## People to reach out to",
            "",
        ]
        if not contacts:
            md += [
                "_No public profiles found. LinkedIn may have rate-limited the search or "
                "your `li_at` cookie has expired. Open LinkedIn manually and refresh it._",
                "",
            ]
        else:
            md += ["| Name | Headline | Profile |", "| --- | --- | --- |"]
            for p in contacts:
                md.append(
                    f"| {p.get('name','')} | {p.get('headline','')} | {p.get('profile','')} |"
                )
            md.append("")
            md.append("## Suggested outreach templates")
            md.append("")
            for p in contacts[:3]:
                name = (p.get("name") or "").split(" ")[0] or "there"
                md += [
                    f"### For {p.get('name','')}",
                    "",
                    "> Hi " + name + ",",
                    "> ",
                    f"> I'm {my_name or '[your name]'}, {my_role or '[your role]'} — "
                    f"I came across the {getattr(job,'title','')} role at {company} and would "
                    "love a 10-minute chat about what the team is solving before I apply. "
                    "Could I ask you a couple of questions about your experience there?",
                    "",
                ]
        return "\n".join(md)


def find_warm_intros(config: dict[str, Any], jobs: list) -> list[Path]:
    return LinkedInNetwork(config).find(jobs)
