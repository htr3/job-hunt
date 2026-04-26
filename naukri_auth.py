"""Shared Naukri login helper.

Used by:
  - `auto_apply.py` — to log in before clicking Apply buttons.
  - `scrapers/naukri_scraper.py` — when `mode: recommended` needs the
    `/mnjuser/recommendedjobs` page (login-gated).

Both call `naukri_login(driver, email, password)`. Returns True on success,
False if the form couldn't be filled or login didn't take.
"""
from __future__ import annotations

import logging
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
