"""Shared Naukri login + profile helpers.

Used by:
  - `auto_apply.py` — login before applying; optional resume upload.
  - `scrapers/naukri_scraper.py` — when `mode: recommended` needs the
    `/mnjuser/recommendedjobs` page (login-gated).

`naukri_login(driver, email, password)` returns True on success.
`naukri_update_resume(driver, resume_path)` uploads a PDF to the profile.
"""
from __future__ import annotations

import logging
import time
from datetime import datetime
from pathlib import Path
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
    WebDriverWait = None  # type: ignore
    By = None  # type: ignore
    EC = None  # type: ignore

    class WebDriverException(Exception):  # type: ignore
        pass

    class TimeoutException(Exception):  # type: ignore
        pass

    class NoSuchElementException(Exception):  # type: ignore
        pass


NAUKRI_LOGIN_URL = "https://www.naukri.com/nlogin/login"
NAUKRI_PROFILE_URL = "https://www.naukri.com/mnjuser/profile"
_HOME_HINT = "naukri.com"


def naukri_login(
    driver: Any,
    email: str,
    password: str,
    logger: logging.Logger | None = None,
    timeout: int = 20,
) -> bool:
    """Drive Naukri's login form. Returns True if URL leaves /nlogin/login."""
    log = logger or logging.getLogger("naukri_auth")

    if not email or not password:
        log.warning("naukri_auth: email/password missing.")
        return False
    if email.startswith("${") or password.startswith("${"):
        log.warning("naukri_auth: credentials still contain placeholders.")
        return False

    try:
        driver.get(NAUKRI_LOGIN_URL)
    except WebDriverException as e:
        log.warning("naukri_auth: navigation to login page failed: %s", e)
        return False

    try:
        wait = WebDriverWait(driver, timeout)

        email_selectors = [
            (By.ID, "usernameField"),
            (By.CSS_SELECTOR, "input[placeholder*='Email' i]"),
            (By.CSS_SELECTOR, "input[placeholder*='Username' i]"),
            (By.CSS_SELECTOR, "form input[type='text']"),
        ]
        email_el = None
        last_err: Exception | None = None
        for by, sel in email_selectors:
            try:
                email_el = wait.until(EC.element_to_be_clickable((by, sel)))
                if email_el:
                    break
            except TimeoutException as e:
                last_err = e
                continue
        if email_el is None:
            log.warning("naukri_auth: could not find email field (last err: %s)", last_err)
            return False

        try:
            driver.execute_script("arguments[0].scrollIntoView({block:'center'});", email_el)
        except WebDriverException:
            pass
        email_el.clear()
        email_el.send_keys(email)

        pw_el = wait.until(EC.element_to_be_clickable((By.CSS_SELECTOR, "input[type='password']")))
        pw_el.clear()
        pw_el.send_keys(password)

        submit_selectors = [
            (By.XPATH, "//form//button[@type='submit']"),
            (By.XPATH, "//button[contains(translate(., 'LOGIN', 'login'), 'login')]"),
            (By.CSS_SELECTOR, "button.btn-primary"),
        ]
        clicked = False
        for by, sel in submit_selectors:
            try:
                btn = driver.find_element(by, sel)
                if btn.is_displayed() and btn.is_enabled():
                    btn.click()
                    clicked = True
                    break
            except NoSuchElementException:
                continue
        if not clicked:
            log.warning("naukri_auth: could not click submit button.")
            return False

        wait.until(lambda d: "nlogin/login" not in (d.current_url or ""))
        log.info("naukri_auth: login successful.")
        return True

    except (TimeoutException, NoSuchElementException) as e:
        log.warning("naukri_auth: login form handling failed: %s", e)
        return False
    except WebDriverException as e:
        log.warning("naukri_auth: webdriver error during login: %s", e)
        return False


def _click_if_present(driver: Any, by: Any, selector: str, log: logging.Logger) -> bool:
    try:
        el = driver.find_element(by, selector)
        if el.is_displayed():
            el.click()
            return True
    except (NoSuchElementException, WebDriverException):
        pass
    return False


def naukri_update_resume(
    driver: Any,
    resume_path: str | Path,
    logger: logging.Logger | None = None,
    timeout: int = 30,
) -> bool:
    """Upload `resume_path` to Naukri profile. Returns True if upload looks OK."""
    log = logger or logging.getLogger("naukri_auth")
    path = Path(resume_path).expanduser().resolve()
    if not path.is_file():
        log.warning("naukri_auth: resume not found at %s", path)
        return False

    try:
        driver.get(NAUKRI_PROFILE_URL)
    except WebDriverException as e:
        log.warning("naukri_auth: could not open profile page: %s", e)
        return False

    time.sleep(2)
    for close_sel in (
        "//*[contains(@class, 'crossIcon')]",
        "//*[contains(@class, 'cross-icon')]",
        "//*[@alt='cross-icon']",
    ):
        _click_if_present(driver, By.XPATH, close_sel, log)
        time.sleep(0.5)

    wait = WebDriverWait(driver, timeout)
    uploaded = False
    abs_path = str(path)

    file_input_selectors = [
        (By.ID, "attachCV"),
        (By.ID, "lazyAttachCV"),
        (By.XPATH, "//*[contains(@class, 'upload')]//input[@type='file']"),
        (By.XPATH, "//input[@type='file' and contains(@id, 'attach')]"),
        (By.CSS_SELECTOR, "input[type='file']"),
    ]
    for by, sel in file_input_selectors:
        try:
            el = wait.until(EC.presence_of_element_located((by, sel)))
            if el:
                try:
                    driver.execute_script(
                        "arguments[0].style.display='block';"
                        "arguments[0].style.visibility='visible';",
                        el,
                    )
                except WebDriverException:
                    pass
                el.send_keys(abs_path)
                uploaded = True
                log.info("naukri_auth: sent resume file via %s", sel)
                break
        except TimeoutException:
            continue
        except WebDriverException as e:
            log.debug("naukri_auth: file input %s failed: %s", sel, e)
            continue

    if not uploaded:
        log.warning("naukri_auth: could not find resume upload input on profile page.")
        return False

    for save_sel in (
        "//button[@type='button' and contains(translate(., 'SAVE', 'save'), 'save')]",
        "//button[contains(translate(., 'UPDATE', 'update'), 'update')]",
    ):
        if _click_if_present(driver, By.XPATH, save_sel, log):
            break

    try:
        checkpoint = wait.until(
            EC.presence_of_element_located((By.XPATH, "//*[contains(@class, 'updateOn')]"))
        )
        updated_text = (checkpoint.text or "").strip()
        today = datetime.today()
        today_variants = {
            today.strftime("%b %d, %Y"),
            f"{today.strftime('%b')} {today.day}, {today.strftime('%Y')}",
        }
        if any(d in updated_text for d in today_variants):
            log.info("naukri_auth: resume updated (last updated: %s).", updated_text)
            return True
        log.info(
            "naukri_auth: resume upload sent; profile shows: %s", updated_text or "(no date)"
        )
        return True
    except TimeoutException:
        log.warning("naukri_auth: resume upload sent but could not verify update date.")
        return uploaded
    except WebDriverException as e:
        log.warning("naukri_auth: resume verify failed: %s", e)
        return uploaded
