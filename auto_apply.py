"""Selenium auto-apply (Naukri-focused).

Given a scored shortlist of jobs, the auto-applier will:
1. Boot a Chrome driver (headless by default — **strongly recommended**, since
   a visible window that loses focus mid-run will get killed by the OS).
2. Log in to Naukri with `.env` credentials (`NAUKRI_EMAIL`, `NAUKRI_PASSWORD`).
3. For each eligible job (platform in `auto_apply.platforms`, status != applied):
   - Open the job URL.
   - Click the Naukri apply button.
   - If a chatbot screening panel appears, answer what it can from
     `config.auto_apply.screening_answers` (the rest are left for you — the
     run is not blocked on unanswered questions; it just gives up on that job).
   - On success mark `status=applied` in the DB.
4. Honor `auto_apply.daily_limit` and `auto_apply.rate_limit.naukri` (seconds
   between applies).

Non-Naukri jobs are deliberately skipped — auto-apply across LinkedIn / Indeed
routinely trips anti-bot protection or needs Workday/Greenhouse detours that
don't belong in a first cut.

Every per-job failure is caught and logged so one weird posting can't stop the
whole batch. Returns a summary: {attempted, succeeded, skipped, failed}.
"""
from __future__ import annotations

import logging
import re
import time
from datetime import datetime
from typing import Any

try:
    from selenium.common.exceptions import (
        NoSuchElementException,
        TimeoutException,
        WebDriverException,
    )
    from selenium.webdriver.common.by import By
    from selenium.webdriver.support import expected_conditions as EC
    from selenium.webdriver.support.ui import WebDriverWait
except ImportError:  # pragma: no cover
    # selenium is a required dep; if it's missing the module still imports but
    # AutoApplier.apply() will no-op.
    WebDriverWait = None  # type: ignore
    By = None  # type: ignore
    EC = None  # type: ignore

    class WebDriverException(Exception):  # type: ignore
        pass

    class TimeoutException(Exception):  # type: ignore
        pass

    class NoSuchElementException(Exception):  # type: ignore
        pass

from job_db import JobDatabase, resolve_db_path
from naukri_auth import naukri_login
from scrapers.base_scraper import BaseScraper, handle_driver_error

logger = logging.getLogger("auto_apply")

_APPLY_BUTTON_SELECTORS = [
    (By.ID, "apply-button"),
    (By.CSS_SELECTOR, "button#apply-button"),
    (By.CSS_SELECTOR, "button.apply-button"),
    (By.XPATH, "//button[contains(translate(., 'APLY', 'aply'), 'apply')]"),
    (By.XPATH, "//button[contains(., 'Apply Now')]"),
]
_SUCCESS_HINTS = [
    "application has been successfully submitted",
    "successfully applied",
    "you have already applied",
    "applied successfully",
    "you've applied",
    "you have applied",
    "your application has been sent",
    "application sent",
    "your profile has been shared",
    "we have shared your profile",
    "we have received your application",
    "thanks for applying",
    "thank you for applying",
    "thank you for your responses",
    "thank you for your response",
    "thanks for your responses",
    'applied to "',
    "next step",
    "start your interview preparation",
    "send me jobs like this",
]


def _year_chip_matches(chip_text: str, target_year: str) -> bool:
    """Return True if a year-range chip label covers `target_year`.

    Handles common formats Naukri uses:
      "3"          -> exact
      "3 yrs"      -> exact
      "1-3"        -> 1 <= 3 <= 3
      "3-5 years"  -> 3 <= 3 <= 5
      "5+"         -> 3 >= 5  (False)
      "0-1"        -> False
    """
    try:
        target = int(str(target_year).strip())
    except (TypeError, ValueError):
        return False
    text = chip_text.lower()
    # "5+" or "5 +"
    m_plus = re.search(r"(\d+)\s*\+", text)
    if m_plus:
        return target >= int(m_plus.group(1))
    # "1-3" or "3 - 5"
    m_range = re.search(r"(\d+)\s*-\s*(\d+)", text)
    if m_range:
        lo, hi = int(m_range.group(1)), int(m_range.group(2))
        return lo <= target <= hi
    # bare "3" or "3 yrs"
    m_one = re.search(r"\b(\d+)\b", text)
    if m_one:
        return int(m_one.group(1)) == target
    return False


class _ApplyDriver(BaseScraper):
    """Reuses BaseScraper's lazy driver (headless default, user-agent, etc.)."""

    PLATFORM_NAME = "auto_apply"

    def search_one(self, title: str, city: str) -> list:  # pragma: no cover - unused
        return []


class AutoApplier:
    def __init__(self, config: dict[str, Any]) -> None:
        self.config = config or {}
        aa = self.config.get("auto_apply", {}) or {}
        self.enabled: bool = bool(aa.get("enabled"))
        self.daily_limit: int = int(aa.get("daily_limit") or 0)
        self.platforms: list[str] = [p.lower() for p in (aa.get("platforms") or ["naukri"])]
        self.rate_limits: dict[str, int] = aa.get("rate_limit") or {"naukri": 60, "default": 60}
        self.screening: dict[str, Any] = aa.get("screening_answers") or {}

        output = self.config.get("output", {}) or {}
        self._db = JobDatabase(db_path=resolve_db_path(output.get("results_dir", "results")))

        naukri_cfg = (self.config.get("platforms", {}) or {}).get("naukri", {}) or {}
        self._naukri_email = (naukri_cfg.get("email") or "").strip()
        self._naukri_password = (naukri_cfg.get("password") or "").strip()

        self._scraper = _ApplyDriver(self.config)
        # Set by _apply_one to signal that the last "success" was actually just
        # an already-applied detection (so the loop can skip the polite sleep).
        self._last_was_already_applied: bool = False
        # Set by _apply_one when the click handed off to a non-Naukri portal.
        # The loop treats these as "skipped" (not "applied") so the user's
        # applied-count reflects only real Naukri-native submissions.
        self._last_was_external: bool = False

    # ------------------------------------------------------------------ public
    def apply_to_jobs(self, jobs: list) -> dict[str, int]:
        summary = {"attempted": 0, "succeeded": 0, "skipped": 0, "failed": 0}
        if not self.enabled:
            logger.info("auto_apply disabled; skipping.")
            return summary
        if self.daily_limit <= 0:
            logger.info("auto_apply.daily_limit <= 0; skipping.")
            return summary

        eligible = [
            j for j in jobs
            if (getattr(j, "platform", "") or "").lower() in self.platforms
            and (getattr(j, "url", "") or "")
        ]
        if not eligible:
            logger.info(
                "auto_apply: no eligible jobs (enabled platforms: %s)", self.platforms
            )
            return summary

        # Apply status filter so we don't re-apply to things already marked.
        already_urls: set[str] = {
            r["url"] for r in self._db.search_jobs(status="applied", limit=10_000)
            if r.get("url")
        }

        applied_this_run = 0
        driver_ready = False
        wait_naukri = int(self.rate_limits.get("naukri", self.rate_limits.get("default", 60)))
        # Cap total attempts so a string of failures can't burn through the whole
        # shortlist. We never try more than max(3, daily_limit*3) jobs.
        max_attempts = max(3, self.daily_limit * 3)

        for job in eligible:
            if applied_this_run >= self.daily_limit:
                logger.info("auto_apply: hit daily_limit=%d, stopping.", self.daily_limit)
                break
            if summary["attempted"] >= max_attempts:
                logger.warning(
                    "auto_apply: %d attempts with only %d success — stopping to avoid runaway.",
                    summary["attempted"], summary["succeeded"],
                )
                break
            url = getattr(job, "url", "")
            if url in already_urls:
                summary["skipped"] += 1
                continue

            if not driver_ready:
                if not self._ensure_logged_in():
                    logger.error("auto_apply: Naukri login failed; aborting batch.")
                    summary["failed"] += 1
                    break
                driver_ready = True

            summary["attempted"] += 1
            ok = self._apply_one(job)
            if ok and self._last_was_external:
                # Click handed off to a non-Naukri portal. Don't count as
                # "applied" — auto-apply can't drive arbitrary employer ATSs.
                summary["skipped"] += 1
                try:
                    self._db.update_status(
                        url,
                        "hidden",
                        notes=(
                            f"external_redirect at "
                            f"{datetime.utcnow().isoformat(timespec='seconds')}Z"
                        ),
                    )
                except Exception as e:
                    logger.warning("auto_apply: DB update failed for %s: %s", url, e)
            elif ok:
                summary["succeeded"] += 1
                applied_this_run += 1
                try:
                    self._db.update_status(
                        url,
                        "applied",
                        notes=f"auto_applied at {datetime.utcnow().isoformat(timespec='seconds')}Z",
                    )
                except Exception as e:
                    logger.warning("auto_apply: DB update failed for %s: %s", url, e)
            else:
                summary["failed"] += 1

            # Polite gap before the NEXT apply. Three cases:
            #   - last was a real successful apply  → wait the full rate-limit
            #   - last was already-applied sync      → skip (no apply was sent)
            #   - last was a failed apply            → short 5s breather only
            #     (no submission hit Naukri, so no need to wait 60s)
            keep_going = (
                applied_this_run < self.daily_limit
                and summary["attempted"] < max_attempts
            )
            if keep_going and not self._last_was_already_applied:
                if ok and not self._last_was_external:
                    logger.info("auto_apply: sleeping %ds before next apply...", wait_naukri)
                    time.sleep(wait_naukri)
                else:
                    # External redirect or failure → no submission hit Naukri,
                    # so a short breather is enough.
                    time.sleep(5)

        try:
            self._scraper.close()
        except Exception:
            pass

        logger.info("auto_apply: %s", summary)
        return summary

    # ------------------------------------------------------------------ login
    def _ensure_logged_in(self) -> bool:
        if "naukri" not in self.platforms:
            return False
        try:
            driver = self._scraper.driver
        except WebDriverException as e:
            logger.error("auto_apply: could not start Chrome: %s", e)
            return False
        return naukri_login(
            driver, self._naukri_email, self._naukri_password, logger=logger
        )

    # ------------------------------------------------------------------ apply
    def _apply_one(self, job: Any) -> bool:
        url = getattr(job, "url", "")
        title = getattr(job, "title", "?")
        company = getattr(job, "company", "?")
        if self._scraper._driver_dead:
            logger.warning("auto_apply: driver dead, skipping %s", url)
            return False

        driver = self._scraper.driver
        logger.info("auto_apply: applying to %s @ %s", title, company)

        try:
            driver.get(url)
        except WebDriverException as e:
            handle_driver_error(self._scraper, url, e)
            return False

        # Page may take a moment to hydrate.
        time.sleep(3)

        # Quick check — already applied? Try both text patterns and the
        # "Applied" button state that Naukri uses once the apply goes through.
        if self._is_already_applied(driver):
            logger.info("auto_apply: %s — already applied (detected), marking status only.", url)
            self._last_was_already_applied = True
            self._last_was_external = False
            return True
        self._last_was_already_applied = False
        self._last_was_external = False

        btn = None
        for by, selector in _APPLY_BUTTON_SELECTORS:
            try:
                btn = driver.find_element(by, selector)
                if btn and btn.is_displayed() and btn.is_enabled():
                    # Don't re-click an "Applied" button masquerading as Apply.
                    label = (btn.text or "").strip().lower()
                    if label in ("applied", "withdraw application"):
                        logger.info(
                            "auto_apply: %s — button shows '%s'; treating as already applied.",
                            url, label,
                        )
                        self._last_was_already_applied = True
                        return True
                    break
                btn = None
            except NoSuchElementException:
                continue
            except WebDriverException as e:
                handle_driver_error(self._scraper, url, e)
                return False

        if not btn:
            # Log what buttons ARE on the page to help diagnose next time.
            try:
                sample = []
                for el in driver.find_elements(By.XPATH, "//button")[:10]:
                    try:
                        txt = (el.text or "").strip()
                        if txt:
                            sample.append(txt[:40])
                    except Exception:
                        continue
                if sample:
                    logger.info("auto_apply: %s — buttons on page: %s", url, sample)
            except WebDriverException:
                pass
            logger.info("auto_apply: %s — no apply button found (external or not eligible).", url)
            return False

        # Many Naukri "Apply" buttons for Remote / external roles open the
        # employer's ATS in a NEW TAB rather than navigating in-place. Capture
        # the window handle count BEFORE the click so we can detect that.
        try:
            handles_before = list(driver.window_handles or [])
        except WebDriverException:
            handles_before = []
        btn_label = ((btn.text or "").strip().lower())
        looks_external = any(
            phrase in btn_label
            for phrase in ("company site", "company website", "on company")
        )

        try:
            try:
                driver.execute_script("arguments[0].scrollIntoView({block:'center'});", btn)
            except WebDriverException:
                pass
            try:
                btn.click()
            except WebDriverException as e:
                # Common case: a "Create Job Alert" or sticky banner overlays the
                # apply button. Fall back to a JS-dispatched click which ignores
                # the click-intercept layer.
                logger.info(
                    "auto_apply: %s — native click intercepted (%s); retrying via JS.",
                    url, type(e).__name__,
                )
                driver.execute_script("arguments[0].click();", btn)
        except WebDriverException as e:
            logger.warning("auto_apply: %s — click failed: %s", url, e)
            return False

        # Give the chatbot panel time to render. Naukri's chatbot usually slides
        # in from the bottom-right 1-4s after the Apply click.
        time.sleep(4)

        # New-tab handoff detection: if the click spawned an additional window,
        # Naukri kicked us over to the employer's ATS. We can't drive a random
        # ATS automatically, but the listing is now marked "applied" on Naukri,
        # which is what the user cares about for tracking. Close the new tab
        # and call it a success.
        try:
            handles_after = list(driver.window_handles or [])
        except WebDriverException:
            handles_after = []
        if len(handles_after) > len(handles_before):
            new_tabs = [h for h in handles_after if h not in handles_before]
            try:
                for h in new_tabs:
                    driver.switch_to.window(h)
                    ext_url = ""
                    try:
                        ext_url = driver.current_url or ""
                    except WebDriverException:
                        pass
                    driver.close()
                    if ext_url:
                        logger.info(
                            "auto_apply: %s opened external ATS (%s) — skipping (not auto-applyable).",
                            url, ext_url,
                        )
                if handles_before:
                    driver.switch_to.window(handles_before[0])
            except WebDriverException:
                pass
            self._last_was_external = True
            return True

        # If the button itself was labelled "Company Site" / "On company site",
        # treat it as an external handoff and skip — we can't drive a random ATS.
        if looks_external:
            logger.info(
                "auto_apply: %s — Apply button was external ('%s'); skipping.",
                url, btn_label,
            )
            self._last_was_external = True
            return True

        # If a chatbot appeared, answer up to N rounds of questions.
        chatbot_progress = self._drive_chatbot(driver)

        # Final polish: one more round of text-input filling + a "submit" button push.
        self._answer_screening(driver)
        self._click_save_and_continue(driver)

        # Give the confirmation toast time to appear, then check.
        time.sleep(4)

        # Strongest signal: Naukri redirects to /myapply/saveApply on success
        # and renders an "Applied to <job>" page with a Next-step CTA.
        try:
            current_url = (driver.current_url or "").lower()
        except WebDriverException:
            current_url = ""
        if "/myapply/saveapply" in current_url or "myapply/saveapply" in current_url:
            logger.info("auto_apply: %s — APPLIED (Naukri confirmation page reached).", url)
            return True

        page = (driver.page_source or "").lower()
        if any(hint in page for hint in _SUCCESS_HINTS):
            logger.info("auto_apply: %s — APPLIED (confirmation text detected).", url)
            return True

        # Naukri's chatbot frequently posts a final "Thank you for your
        # responses." bubble inside the drawer instead of a page-level toast.
        # Scan visible chatbot text directly so we catch that case.
        if self._chatbot_shows_success(driver):
            logger.info("auto_apply: %s — APPLIED (chatbot thank-you bubble).", url)
            return True

        # Soft-success: the chatbot answered ≥1 questions AND the panel is now
        # gone. Naukri sometimes silently closes the drawer (no toast) once
        # everything's submitted, so this is the most reliable post-flow check.
        if chatbot_progress >= 1 and not self._is_chatbot_open(driver):
            logger.info(
                "auto_apply: %s — APPLIED (%d chatbot round(s) completed, drawer closed).",
                url, chatbot_progress,
            )
            return True

        # Same-tab redirect to a non-naukri host = external handoff.
        try:
            current = (driver.current_url or "").lower()
        except WebDriverException:
            current = ""
        if current and "naukri.com" not in current:
            logger.info(
                "auto_apply: %s redirected to external portal (%s) — skipping.", url, current
            )
            self._last_was_external = True
            return True

        # Re-check applied state — sometimes the Apply button morphs into
        # "Applied" without showing a toast.
        if self._is_already_applied(driver):
            logger.info("auto_apply: %s — page now shows applied state.", url)
            return True

        logger.info("auto_apply: %s — no confirmation detected; marking as not-applied.", url)
        return False

    # ------------------------------------------------------------------ already-applied detection
    def _is_already_applied(self, driver: Any) -> bool:
        """Best-effort detect whether the current job page shows an 'already
        applied' state — covers text hints, CSS class states, and the common
        'Applied' button text."""
        # 1. Page-source text hints.
        try:
            page = (driver.page_source or "").lower()
        except WebDriverException:
            page = ""
        text_hints = (
            "already applied",
            "you've applied",
            "you have applied",
            "you applied",
            "application has been successfully submitted",
        )
        if any(h in page for h in text_hints):
            return True

        # 2. CSS class hints — Naukri often toggles the main CTA to show "Applied".
        class_selectors = [
            "button.applied",
            "button[class*='Applied']",
            "button.styles_already-applied",
            "div[class*='applied'] button",
        ]
        for sel in class_selectors:
            try:
                els = driver.find_elements(By.CSS_SELECTOR, sel)
            except WebDriverException:
                els = []
            for el in els:
                try:
                    if el.is_displayed():
                        return True
                except Exception:
                    continue

        # 3. Any visible button whose text is exactly "Applied".
        try:
            btns = driver.find_elements(By.XPATH, "//button")
        except WebDriverException:
            btns = []
        for b in btns:
            try:
                if not b.is_displayed():
                    continue
                label = (b.text or "").strip().lower()
                if label == "applied":
                    return True
            except Exception:
                continue
        return False

    # ------------------------------------------------------------------ chatbot
    def _is_chatbot_open(self, driver: Any) -> bool:
        """Return True if a Naukri chatbot drawer is currently visible."""
        for sel in (
            "div.chatbot_DrawerContentWrapper",
            "div[class*='chatbot']",
            "div[class*='Chatbot']",
            "section[class*='chatbot']",
        ):
            try:
                els = driver.find_elements(By.CSS_SELECTOR, sel)
            except WebDriverException:
                els = []
            for el in els:
                try:
                    if el.is_displayed():
                        return True
                except Exception:
                    continue
        return False

    def _chatbot_shows_success(self, driver: Any) -> bool:
        """Return True if the chatbot drawer contains any success/thank-you
        bubble (e.g. "Thank you for your responses.").

        Naukri renders this as a final bot message with no follow-up Save —
        seeing it means the application was accepted on their side even when
        no page-level toast appears.
        """
        for sel in (
            "div.chatbot_DrawerContentWrapper",
            "div[class*='chatbot']",
            "div[class*='Chatbot']",
        ):
            try:
                els = driver.find_elements(By.CSS_SELECTOR, sel)
            except WebDriverException:
                els = []
            for el in els:
                try:
                    if not el.is_displayed():
                        continue
                    txt = (el.text or "").lower()
                except Exception:
                    continue
                if any(hint in txt for hint in _SUCCESS_HINTS):
                    return True
        return False

    def _drive_chatbot(self, driver: Any, max_rounds: int = 15) -> int:
        """Drive Naukri's screening chatbot panel for up to `max_rounds` Q/A cycles.

        Each round: fill any visible text input using screening answers, click any
        matching chip/option, then click "Save and Continue" (or equivalent).
        Stops early if the chatbot closes (panel no longer visible) or the same
        question repeats too many times (the click isn't actually selecting).

        Returns the number of rounds in which we actually moved forward (chip
        click / text answer / save click). Callers use this as a soft success
        signal — if we drove >=1 round AND the panel is now gone, the apply
        almost certainly went through even when no toast text was found.
        """
        last_question = ""
        same_question_streak = 0
        debug_dumped = False
        progress_rounds = 0
        for _round in range(max_rounds):
            # Is a chatbot panel on screen?
            panel = None
            for sel in (
                "div.chatbot_DrawerContentWrapper",
                "div[class*='chatbot']",
                "div[class*='Chatbot']",
                "section[class*='chatbot']",
            ):
                try:
                    els = driver.find_elements(By.CSS_SELECTOR, sel)
                except WebDriverException:
                    els = []
                for el in els:
                    try:
                        if el.is_displayed():
                            panel = el
                            break
                    except Exception:
                        continue
                if panel:
                    break
            if panel is None:
                return progress_rounds  # no chatbot / already closed

            # 0. Dedicated handler for the 3-input DOB widget (DD/MM/YYYY).
            dob_filled = self._fill_dob_inputs(driver, panel)
            # 1. Click any chip/option that matches our answers (Yes/No,
            #    skill multi-select, year-range buttons).
            chip_clicked = False if dob_filled else self._click_matching_chips(driver, panel)
            # 2. If no chip matched, try the chat-bubble + "Type message here"
            #    pattern (e.g. "How many years of experience in Java?").
            text_answered = False
            if not (dob_filled or chip_clicked):
                text_answered = self._answer_chat_text_question(driver, panel)
            # 3. Old-style label-and-input forms (rare in chatbot, common in
            #    legacy Naukri screening drawers).
            if not (dob_filled or chip_clicked or text_answered):
                self._answer_screening(driver, root=panel)
            # 4. Advance.
            advanced = self._click_save_and_continue(driver, root=panel)
            round_did_progress = bool(dob_filled or chip_clicked or text_answered or advanced)
            if round_did_progress:
                progress_rounds += 1
            if not round_did_progress:
                # Nothing moved forward — question we can't answer.
                return progress_rounds

            # Bail-out: if the same question repeats >2x, our click isn't
            # actually selecting (e.g., radio onChange not firing). Dump the
            # panel HTML once for offline inspection, then stop spamming.
            current_q = self._latest_bot_question(panel) or ""
            if current_q and current_q == last_question:
                same_question_streak += 1
            else:
                same_question_streak = 0
                last_question = current_q
            if same_question_streak >= 2:
                if not debug_dumped:
                    try:
                        from pathlib import Path
                        out = Path("results") / f"chatbot_debug_{int(time.time())}.html"
                        out.parent.mkdir(parents=True, exist_ok=True)
                        out.write_text(
                            panel.get_attribute("outerHTML") or "",
                            encoding="utf-8",
                        )
                        logger.info(
                            "auto_apply: chatbot stuck on Q=%r; dumped DOM to %s",
                            current_q[:80], out,
                        )
                    except Exception:
                        pass
                    debug_dumped = True
                return progress_rounds
            time.sleep(1.8)
        return progress_rounds

    def _click_matching_chips(self, driver: Any, root: Any) -> bool:
        """Click a chip/button/radio-option in the chatbot whose text matches
        our answers.

        Recognized option element shapes (across Naukri's various chatbot
        skins):
          - <button>, <li role='button'>, <div role='radio'>, <div role='option'>
          - <div class='chip|option|radio'>, <span class='chip'>
          - <label class='option|radio'>

        Question-aware matching: we read the latest bot question inside the
        panel, derive the target answer via _answer_for_question_text(), and
        if the answer is numeric (e.g. years_default=3, notice_period=15)
        we click the option whose label numerically covers it
        (e.g. "2-3 years", "15 days or less").

        Returns True iff an option was clicked.
        """
        answers = self.screening or {}
        yes_keywords = {str(s).strip().lower() for s in (answers.get("skills_yes") or []) if s}
        default_skill = str(answers.get("default_skill_answer") or "").strip().lower()
        willing = str(answers.get("willing_to_relocate") or "").strip().lower()
        years_default = str(answers.get("years_default") or "").strip()

        try:
            chips = root.find_elements(
                By.XPATH,
                ".//button | .//li[@role='button'] | .//div[@role='radio']"
                " | .//div[@role='option']"
                " | .//div[contains(@class,'chip')] | .//span[contains(@class,'chip')]"
                " | .//div[contains(@class,'option')] | .//div[contains(@class,'radio')]"
                " | .//label[contains(@class,'option')] | .//label[contains(@class,'radio')]"
                " | .//label[contains(@class,'mcc__label')] | .//label[@for]",
            )
        except WebDriverException:
            return False

        def _resolve_clickable(chip: Any) -> Any:
            """Walk up the DOM until we hit the actual radio/option container.

            Naukri renders each radio row roughly as
              <wrapper class="..radio..">
                <span class="circle"/>
                <span>2-3 years</span>     <-- our text match lands here
              </wrapper>
            Clicking the inner <span> doesn't toggle React's state. We walk up
            (max 4 hops) until we find an element that is itself a radio
            container (role / class) or holds an <input type=radio>.

            Special case: a <label> element (especially with `for=` or class
            containing "label") IS already the right click target — clicking
            it natively toggles the linked input — so we return it as-is.
            """
            try:
                if (chip.tag_name or "").lower() == "label":
                    return chip
            except WebDriverException:
                pass
            el = chip
            for _ in range(4):
                try:
                    if (el.tag_name or "").lower() == "label":
                        return el
                    role = (el.get_attribute("role") or "").lower()
                    cls = (el.get_attribute("class") or "").lower()
                    if role in ("radio", "option", "button"):
                        return el
                    if "chatbot_drawer" in cls or "chatbot" in cls:
                        return chip
                    try:
                        if el.find_elements(By.XPATH, "./input[@type='radio' or @type='checkbox']"):
                            return el
                    except WebDriverException:
                        pass
                    if any(k in cls for k in ("ssrdrop", "row", "option", "radio", "chip", "btn", "list", "mcc")):
                        return el
                    el = el.find_element(By.XPATH, "..")
                except WebDriverException:
                    break
            return chip

        def _click(chip: Any) -> bool:
            target = _resolve_clickable(chip)
            ok = False
            try:
                target.click()
                ok = True
            except WebDriverException:
                try:
                    driver.execute_script("arguments[0].click();", target)
                    ok = True
                except WebDriverException:
                    ok = False
            # Belt-and-suspenders: also toggle any nested radio/checkbox input
            # OR the input referenced by a <label for="...">, and fire a change
            # event, so React-based UIs that ignore the outer click still
            # register the selection.
            try:
                driver.execute_script(
                    "var el=arguments[0];"
                    "var inp=el.querySelector && (el.querySelector('input[type=\"radio\"]')"
                    " || el.querySelector('input[type=\"checkbox\"]'));"
                    "if(!inp && el.tagName==='LABEL' && el.htmlFor){"
                    "  inp=document.getElementById(el.htmlFor);"
                    "}"
                    "if(inp){inp.checked=true;"
                    "inp.dispatchEvent(new Event('input',{bubbles:true}));"
                    "inp.dispatchEvent(new Event('change',{bubbles:true}));"
                    "inp.dispatchEvent(new MouseEvent('click',{bubbles:true}));}",
                    target,
                )
            except WebDriverException:
                pass
            return ok

        def _leaf_text(chip: Any) -> str:
            """Return chip.text only if it's a single short label (an actual
            option), not a wrapper containing several stacked options.

            Naukri's chatbot renders the options list as nested divs; a wrapper
            element's `.text` returns all option labels concatenated by `\n`,
            and the previous matcher would happily click that wrapper because
            its combined text still contains a matching range. Real option
            labels are single-line and short ("1 year", "2-3 years", "Yes").
            """
            try:
                raw = (chip.text or "").strip()
            except Exception:
                return ""
            if not raw:
                return ""
            lines = [ln.strip() for ln in raw.splitlines() if ln.strip()]
            if len(lines) != 1:
                return ""
            single = lines[0]
            # Hard length cap: a real option rarely exceeds ~50 chars.
            if len(single) > 60:
                return ""
            return single

        # Read the current bot question to drive context-aware option matching.
        question = self._latest_bot_question(root)
        question_lower = (question or "").lower()
        target_val = self._answer_for_question_text(question_lower) if question else ""
        try:
            target_int: int | None = int(str(target_val).strip()) if target_val else None
        except ValueError:
            target_int = None

        # PASS 0: question-driven numeric match.
        if target_int is not None:
            for chip in chips:
                try:
                    if not chip.is_displayed() or not chip.is_enabled():
                        continue
                except Exception:
                    continue
                text = _leaf_text(chip)
                if not text or "skip" in text.lower():
                    continue
                if _year_chip_matches(text, str(target_int)):
                    if _click(chip):
                        logger.info(
                            "auto_apply: chatbot picked %r for Q=%r",
                            text[:60], (question or "")[:80],
                        )
                        return True

        # PASS 1: years_default fallback (year-range chips only).
        if target_int is None and years_default:
            year_pattern = re.compile(r"\d+\s*[-+]?\s*\d*\s*(?:yr|year)?", re.IGNORECASE)
            for chip in chips:
                try:
                    if not chip.is_displayed() or not chip.is_enabled():
                        continue
                except Exception:
                    continue
                text = _leaf_text(chip)
                if not text or not year_pattern.search(text):
                    continue
                if _year_chip_matches(text, years_default):
                    if _click(chip):
                        return True

        # PASS 2: yes/no, skills_yes, default_skill.
        # Pick the right Yes/No source: prefer question-derived answer (e.g.
        # have_resigned → "No") over the generic willing_to_relocate fallback.
        target_yn = ""
        if isinstance(target_val, str) and target_val.strip().lower() in ("yes", "no"):
            target_yn = target_val.strip().lower()
        elif willing:
            target_yn = willing

        for chip in chips:
            try:
                if not chip.is_displayed() or not chip.is_enabled():
                    continue
            except Exception:
                continue
            text = _leaf_text(chip).lower()
            if not text:
                continue
            if text in ("yes", "no") and target_yn:
                if text == target_yn:
                    if _click(chip):
                        logger.info(
                            "auto_apply: chatbot picked %r for Q=%r",
                            text[:60], (question or "")[:80],
                        )
                        return True
            if text in yes_keywords:
                if _click(chip):
                    return True
            if default_skill and text == default_skill:
                if _click(chip):
                    return True
        return False

    # ------------------------------------------------------------------ shared helpers
    def _latest_bot_question(self, panel: Any) -> str:
        """Return the most recent bot question text inside the chatbot panel.

        Tries common bubble selectors first; falls back to "the last
        non-placeholder text line" when class names are unrecognized.
        """
        bot_selectors = (
            "li.botMsg", "div.botMsg",
            "li[class*='botMsg']", "div[class*='botMsg']",
            "div[class*='Bot']", "li[class*='Bot']",
        )
        for sel in bot_selectors:
            try:
                els = panel.find_elements(By.CSS_SELECTOR, sel)
            except WebDriverException:
                els = []
            visible = []
            for el in els:
                try:
                    if el.is_displayed():
                        visible.append(el)
                except Exception:
                    continue
            if visible:
                try:
                    text = (visible[-1].text or "").strip()
                except Exception:
                    text = ""
                if text:
                    return text
        # Heuristic fallback: take the last non-placeholder text line.
        try:
            lines = [
                ln.strip() for ln in (panel.text or "").splitlines() if ln.strip()
            ]
            lines = [
                ln for ln in lines
                if "type message" not in ln.lower()
                and ln.lower() not in ("save", "submit", "continue", "skip")
            ]
            for ln in reversed(lines):
                # Skip option labels like "1 year", "Yes", "No"
                low = ln.lower()
                if low.endswith("?") or "what" in low or "how" in low or "which" in low:
                    return ln
            if lines:
                return lines[0]  # first line is often the question
        except Exception:
            pass
        return ""

    # ------------------------------------------------------------------ DOB widget
    def _fill_dob_inputs(self, driver: Any, panel: Any) -> bool:
        """Fill Naukri's 3-input Date-Of-Birth widget if present.

        Naukri's DOB chatbot question renders three separate <input type=number>
        fields (day, month, year) inside a `dob__container`, not a single text
        input — so the regular chat-text path can't fill it.

        Reads `screening_answers.date_of_birth` (DD/MM/YYYY or DD-MM-YYYY) from
        config and pushes each segment into the corresponding input via JS so
        React-based onChange handlers fire reliably.
        """
        answers = self.screening or {}
        raw = str(answers.get("date_of_birth") or "").strip()
        if not raw:
            return False
        parts = re.split(r"[/\-\s]+", raw)
        if len(parts) != 3:
            return False
        day, month, year = parts[0], parts[1], parts[2]
        try:
            day_el = panel.find_element(By.CSS_SELECTOR, "input.dob__input.day, input[name='day']")
            month_el = panel.find_element(By.CSS_SELECTOR, "input.dob__input.month, input[name='month']")
            year_el = panel.find_element(By.CSS_SELECTOR, "input.dob__input.year, input[name='year']")
        except (WebDriverException, NoSuchElementException):
            return False

        def _set(el: Any, val: str) -> None:
            driver.execute_script(
                "var e=arguments[0],v=arguments[1];"
                "var p=Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype,'value').set;"
                "p.call(e,v);"
                "e.dispatchEvent(new Event('input',{bubbles:true}));"
                "e.dispatchEvent(new Event('change',{bubbles:true}));"
                "e.dispatchEvent(new Event('blur',{bubbles:true}));",
                el, val,
            )

        try:
            _set(day_el, day)
            _set(month_el, month)
            _set(year_el, year)
        except WebDriverException:
            return False
        logger.info("auto_apply: chatbot DOB filled %s/%s/%s", day, month, year)
        return True

    # ------------------------------------------------------------------ chat-bubble text question
    def _answer_chat_text_question(self, driver: Any, panel: Any) -> bool:
        """Answer a chatbot question that uses the chat-bubble + text-input
        pattern (e.g. *"How many years of experience in Java?"* with a
        "Type message here..." textarea below).

        Steps:
          1. Locate the message input inside the panel.
          2. Read the most recent bot question text.
          3. Map the question to a screening answer using the same keyword
             logic as _answer_screening (with a years-default fallback).
          4. Type the answer and submit (Enter + click send-arrow).

        Returns True iff an answer was typed and submitted.
        """
        from selenium.webdriver.common.keys import Keys

        # 1. Find the input element (textarea / contenteditable / input).
        input_el = None
        input_selectors = (
            "textarea",
            "div[contenteditable='true']",
            "input[type='text']",
            "input:not([type])",
        )
        for sel in input_selectors:
            try:
                els = panel.find_elements(By.CSS_SELECTOR, sel)
            except WebDriverException:
                els = []
            for el in els:
                try:
                    if el.is_displayed() and el.is_enabled():
                        input_el = el
                        break
                except Exception:
                    continue
            if input_el:
                break
        if input_el is None:
            return False

        # 2. Find the latest bot question.
        question = self._latest_bot_question(panel)
        if not question:
            return False

        # 3. Determine answer.
        val = self._answer_for_question_text(question.lower())
        if not val:
            logger.info("auto_apply: chatbot question (no answer mapped): %r", question[:120])
            return False

        # 4. Type + submit.
        try:
            try:
                input_el.click()
            except WebDriverException:
                pass
            try:
                input_el.clear()
            except WebDriverException:
                pass
            input_el.send_keys(val)
        except WebDriverException as e:
            logger.warning("auto_apply: chatbot input typing failed: %s", e)
            return False

        logger.info("auto_apply: chatbot Q=%r A=%r", question[:80], val)

        # Try Enter, then click any send-arrow as backup.
        try:
            input_el.send_keys(Keys.RETURN)
        except WebDriverException:
            pass
        time.sleep(0.6)
        self._click_save_and_continue(driver, root=panel)
        return True

    def _answer_for_question_text(self, text: str) -> str:
        """Map a lowercased chatbot question to an answer string.

        Mirrors _answer_screening's keyword logic but works on a free-form
        question string rather than a <label> element.
        """
        answers = self.screening or {}
        if "expected" in text and "ctc" in text:
            v = answers.get("expected_ctc")
            return str(v) if v else ""
        if "current" in text and "ctc" in text:
            v = answers.get("current_ctc")
            return str(v) if v else ""

        # Date of birth — supports phrasings like "DOB", "date of birth", "birth date".
        if "birth" in text or re.search(r"\bdob\b", text):
            v = answers.get("date_of_birth")
            if v:
                return str(v)

        # Resignation status — must be checked before generic "current" matchers
        # below so we don't fall through into something else.
        if "resign" in text:
            v = answers.get("have_resigned")
            if v:
                return str(v)

        # English / language proficiency.
        if "english" in text or "communicat" in text or "fluent" in text:
            v = answers.get("english_proficient")
            if v:
                return str(v)

        # "If selected, in how many days can you join?" — numeric days answer.
        if "join" in text and ("day" in text or "how many" in text):
            v = answers.get("days_to_join") or answers.get("notice_period")
            if v:
                return str(v)

        # Immediate joining (yes/no flavor).
        if "immediate" in text and ("join" in text or "available" in text):
            v = answers.get("can_join_immediately")
            if v:
                return str(v)

        # Generic Yes/No questions ("Do you have ...", "Are you ...", "Have you
        # worked with ...", "Can you ..."). Resolve to default_skill_answer so
        # skill / hands-on experience questions get the configured answer
        # (typically "Yes") rather than falling through to the "experience"
        # keyword route which would return a year count.
        if re.match(r"^\s*(do|are|have|can|is|will)\s+you\b", text):
            v = answers.get("default_skill_answer") or answers.get("willing_to_relocate")
            if v:
                return str(v)

        keyword_map = {
            "notice": "notice_period",
            "salary": "expected_ctc",
            "location": "current_location",
            "relocate": "willing_to_relocate",
            "experience": "total_experience",
            "qualification": "highest_qualification",
            "designation": "current_designation",
        }
        for kw, key in keyword_map.items():
            if kw in text:
                v = answers.get(key)
                if v:
                    return str(v)

        # Years-default fallback ("How many years of Java?").
        if "year" in text or "yrs" in text:
            v = answers.get("years_default")
            if v:
                return str(v)
        return ""

    def _click_save_and_continue(self, driver: Any, root: Any | None = None) -> bool:
        """Click Naukri's "Save" / "Save and Continue" / "Submit" / send-arrow button.

        Naukri renders this control as either a <button> or a styled <div>
        depending on which chatbot skin you land in, so we scan both.
        """
        scope = root if root is not None else driver
        # XPath match works on any element (button OR div). Translate folds case.
        selectors = [
            (By.XPATH,
                ".//*[(self::button or self::div or self::a or self::span)"
                " and ("
                "contains(translate(normalize-space(.), 'SAVE', 'save'), 'save')"
                " or contains(translate(normalize-space(.), 'SUBMIT', 'submit'), 'submit')"
                " or contains(translate(normalize-space(.), 'CONTINUE', 'continue'), 'continue')"
                ")]"),
            (By.CSS_SELECTOR, "div.sendMsg"),
            (By.CSS_SELECTOR, "div[class*='send']"),
        ]
        for by, sel in selectors:
            try:
                els = scope.find_elements(by, sel)
            except WebDriverException:
                els = []
            for el in els:
                try:
                    if not el.is_displayed():
                        continue
                    # Skip if visibly disabled (greyed out) — clicking would no-op.
                    aria_disabled = (el.get_attribute("aria-disabled") or "").lower()
                    if aria_disabled == "true":
                        continue
                    cls = (el.get_attribute("class") or "").lower()
                    if "disabled" in cls:
                        continue
                    txt = (el.text or "").strip().lower()
                    # Only click the actual control, not a parent div whose
                    # text contains "save" because of nested option labels.
                    if txt and txt not in (
                        "save", "submit", "continue", "save and continue",
                    ):
                        # Allow longer phrases that *start* with one of these
                        # (e.g., "Save and Continue Application").
                        if not any(
                            txt.startswith(w)
                            for w in ("save", "submit", "continue")
                        ):
                            continue
                    try:
                        el.click()
                    except WebDriverException:
                        driver.execute_script("arguments[0].click();", el)
                    return True
                except WebDriverException:
                    continue
        return False

    # ------------------------------------------------------------------ screening
    def _answer_screening(self, driver: Any, root: Any | None = None) -> None:
        """Best-effort: fill visible text inputs with matching screening answers.

        If `root` is provided, only elements inside it are searched (useful for
        the Naukri chatbot drawer).
        """
        answers = self.screening or {}
        if not answers:
            return
        # Map of (keywords to match in the question text) -> answer key in config.
        keyword_map = {
            "notice": "notice_period",
            "ctc": None,  # disambiguated below
            "current ctc": "current_ctc",
            "expected ctc": "expected_ctc",
            "salary": "expected_ctc",
            "location": "current_location",
            "relocate": "willing_to_relocate",
            "experience": "total_experience",
            "qualification": "highest_qualification",
            "designation": "current_designation",
        }
        scope = root if root is not None else driver
        # When scoped to an element, XPath must be relative (".//").
        label_xpath = (
            ".//label | .//*[self::div or self::span][contains(@class,'label')]"
            if root is not None
            else "//label | //*[self::div or self::span][contains(@class,'label')]"
        )
        try:
            labels = scope.find_elements(By.XPATH, label_xpath)
        except WebDriverException:
            return

        for label in labels:
            try:
                text = (label.text or "").lower()
            except Exception:
                continue
            if not text:
                continue
            answer_key = None
            val: str | None = None
            if "expected" in text and "ctc" in text:
                answer_key = "expected_ctc"
            elif "current" in text and "ctc" in text:
                answer_key = "current_ctc"
            else:
                for kw, key in keyword_map.items():
                    if kw in text and key:
                        answer_key = key
                        break

            # Fallback: any "how many years / yrs" question that didn't match a
            # specific key (e.g., "Years of Java?", "Years of AWS experience?")
            # gets the configured default years value.
            if not answer_key and ("year" in text or "yrs" in text):
                yd = answers.get("years_default")
                if yd:
                    val = str(yd)

            if val is None:
                if not answer_key:
                    continue
                v = answers.get(answer_key)
                if not v:
                    continue
                val = str(v)
            try:
                input_el = label.find_element(By.XPATH, ".//following::input[1] | .//following::textarea[1]")
                input_el.clear()
                input_el.send_keys(val)
            except (NoSuchElementException, WebDriverException):
                continue


def auto_apply_jobs(config: dict[str, Any], jobs: list) -> dict[str, int]:
    return AutoApplier(config).apply_to_jobs(jobs)
