"""Job Hunt AI Agent — CLI orchestrator.

Usage:
    python job_hunter.py --setup                       # interactive first-run wizard
    python job_hunter.py                               # run with defaults from config.yaml
    python job_hunter.py --platforms remoteok --no-apply
    python job_hunter.py --new-only --verbose
"""
from __future__ import annotations

import argparse
import concurrent.futures
import getpass
import logging
import os
import re
import signal
import sys
import threading
import time
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any

import yaml
from dotenv import load_dotenv

from ai_matcher import AIJobMatcher
from auto_apply import AutoApplier
from company_intel import CompanyIntel
from exporters import Exporter
from gap_analysis import GapAnalyzer
from interview_prep import InterviewPrep
from job_db import JobDatabase, resolve_db_path
from linkedin_network import LinkedInNetwork
from notifier import Notifier
from report_generator import generate_reports
from resume_tailor import ResumeTailor
from scrapers import SCRAPERS, Job

# --------------------------------------------------------------------------- #
# Paths                                                                       #
# --------------------------------------------------------------------------- #

PROJECT_ROOT = Path(__file__).resolve().parent
DEFAULT_CONFIG_PATH = PROJECT_ROOT / "config.yaml"
DEFAULT_ENV_PATH = PROJECT_ROOT / ".env"
LOG_DIR = PROJECT_ROOT / "logs"

logger = logging.getLogger("job_hunter")


# --------------------------------------------------------------------------- #
# Logging                                                                     #
# --------------------------------------------------------------------------- #


def setup_logging(verbose: bool = False) -> None:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    level = logging.DEBUG if verbose else logging.INFO

    root = logging.getLogger()
    root.setLevel(level)

    for handler in list(root.handlers):
        root.removeHandler(handler)

    fmt = logging.Formatter(
        "%(asctime)s %(levelname)-7s [%(name)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    stream = logging.StreamHandler(sys.stdout)
    stream.setLevel(level)
    stream.setFormatter(fmt)
    root.addHandler(stream)

    file_handler = RotatingFileHandler(
        LOG_DIR / "job_hunter.log", maxBytes=2_000_000, backupCount=3, encoding="utf-8"
    )
    file_handler.setLevel(logging.DEBUG)
    file_handler.setFormatter(fmt)
    root.addHandler(file_handler)


# --------------------------------------------------------------------------- #
# Config loading with ${VAR} expansion                                        #
# --------------------------------------------------------------------------- #

_VAR_RE = re.compile(r"\$\{([A-Z0-9_]+)\}")


def _expand(value: Any) -> Any:
    if isinstance(value, str):
        return _VAR_RE.sub(lambda m: os.environ.get(m.group(1), ""), value)
    if isinstance(value, dict):
        return {k: _expand(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_expand(v) for v in value]
    return value


def load_config(path: str | Path = DEFAULT_CONFIG_PATH) -> dict[str, Any]:
    """Load YAML config, populating `${VAR}` placeholders from `.env` / environ."""
    load_dotenv(DEFAULT_ENV_PATH, override=False)
    config_path = Path(path)
    if not config_path.exists():
        example = PROJECT_ROOT / "config.example.yaml"
        hint = (
            f"Config not found at {config_path}. "
            f"Run: python job_hunter.py --setup"
        )
        if example.exists():
            hint += (
                f"\nOr copy the template to get started: "
                f"`cp {example.name} {config_path.name}` "
                f"and fill in your details."
            )
        raise FileNotFoundError(hint)
    with open(config_path, "r", encoding="utf-8") as f:
        raw = yaml.safe_load(f) or {}
    return _expand(raw)


# --------------------------------------------------------------------------- #
# Setup wizard                                                                #
# --------------------------------------------------------------------------- #

_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")

PLATFORM_KEYS = [
    "linkedin",
    "naukri",
    "indeed",
    "glassdoor",
    "instahyre",
    "remoteok",
    "foundit",
    "wellfound",
]


def _prompt(text: str, default: str = "") -> str:
    suffix = f" [{default}]" if default else ""
    val = input(f"{text}{suffix}: ").strip()
    return val or default


def _prompt_bool(text: str, default: bool = False) -> bool:
    default_str = "Y/n" if default else "y/N"
    val = input(f"{text} [{default_str}]: ").strip().lower()
    if not val:
        return default
    return val in ("y", "yes", "true", "1")


def _prompt_int(text: str, default: int = 0, min_value: int | None = None) -> int:
    while True:
        raw = _prompt(text, str(default))
        try:
            val = int(raw)
        except ValueError:
            print(f"  Please enter a whole number (got {raw!r}).")
            continue
        if min_value is not None and val < min_value:
            print(f"  Must be >= {min_value}.")
            continue
        return val


def _prompt_list(text: str, default: list[str]) -> list[str]:
    shown = ", ".join(default)
    raw = _prompt(f"{text} (comma-separated)", shown)
    return [item.strip() for item in raw.split(",") if item.strip()]


def _prompt_secret(text: str) -> str:
    try:
        return getpass.getpass(f"{text} (hidden): ").strip()
    except (EOFError, KeyboardInterrupt):
        print()
        return ""


def _prompt_email(text: str, default: str = "") -> str:
    while True:
        val = _prompt(text, default)
        if not val or _EMAIL_RE.match(val):
            return val
        print("  That doesn't look like a valid email address.")


def _write_env_file(env_path: Path, values: dict[str, str]) -> None:
    """Write/merge `.env`, preserving any keys not in `values`."""
    existing: dict[str, str] = {}
    if env_path.exists():
        for line in env_path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, _, v = line.partition("=")
            existing[k.strip()] = v.strip()

    existing.update(values)

    lines = ["# Job Hunt AI Agent — secrets (DO NOT COMMIT)"]
    for key in sorted(existing):
        lines.append(f"{key}={existing[key]}")
    env_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    try:
        os.chmod(env_path, 0o600)
    except OSError:
        pass  # Windows may not support chmod; best effort only.


def _write_yaml(path: Path, data: dict[str, Any]) -> None:
    """Write YAML, preserving `${VAR}` placeholders (no quoting them)."""
    with open(path, "w", encoding="utf-8") as f:
        yaml.safe_dump(
            data,
            f,
            default_flow_style=False,
            sort_keys=False,
            allow_unicode=True,
            width=120,
        )


def run_setup_wizard(
    config_path: Path = DEFAULT_CONFIG_PATH,
    env_path: Path = DEFAULT_ENV_PATH,
) -> None:
    """Interactive wizard: prompts for everything and writes config.yaml + .env."""
    print()
    print("=" * 60)
    print("  Job Hunt AI Agent — Setup Wizard")
    print("=" * 60)
    print(
        "This will write `config.yaml` and `.env` in the project root.\n"
        "Secrets (passwords, API keys) go to `.env`; everything else to `config.yaml`.\n"
        "Press Enter to accept the [default] shown.\n"
    )

    if config_path.exists():
        if not _prompt_bool(
            f"{config_path.name} already exists. Overwrite?", default=False
        ):
            print("Aborted. Existing config left untouched.")
            return

    existing: dict[str, Any] = {}
    if config_path.exists():
        try:
            with open(config_path, "r", encoding="utf-8") as f:
                existing = yaml.safe_load(f) or {}
        except Exception:
            existing = {}

    exist_profile = existing.get("profile", {}) or {}
    exist_search = existing.get("search", {}) or {}
    exist_loc = existing.get("location", {}) or {}
    exist_filters = existing.get("filters", {}) or {}
    exist_ai = existing.get("ai_matching", {}) or {}
    exist_apply = existing.get("auto_apply", {}) or {}
    exist_notif = existing.get("notifications", {}) or {}
    exist_output = existing.get("output", {}) or {}

    # --- profile ---
    print("\n-- Profile --")
    profile = {
        "name": _prompt("Full name", exist_profile.get("name", "")),
        "email": _prompt_email("Email", exist_profile.get("email", "")),
        "phone": _prompt("Phone", exist_profile.get("phone", "")),
        "location": _prompt("Current city", exist_profile.get("location", "")),
        "experience_years": _prompt_int(
            "Years of experience",
            int(exist_profile.get("experience_years") or 0),
            min_value=0,
        ),
        "current_role": _prompt("Current role", exist_profile.get("current_role", "")),
        "resume_path": _prompt(
            "Path to resume (optional)", exist_profile.get("resume_path", "")
        ),
    }

    # --- search ---
    print("\n-- Search --")
    search = {
        "job_titles": _prompt_list(
            "Target job titles",
            exist_search.get("job_titles") or ["Software Engineer", "Backend Developer"],
        ),
        "skills": _prompt_list(
            "Key skills",
            exist_search.get("skills") or ["Python", "Java", "SQL"],
        ),
        "excluded_keywords": _prompt_list(
            "Excluded keywords",
            exist_search.get("excluded_keywords") or ["internship", "unpaid", "volunteer"],
        ),
    }
    while not search["job_titles"]:
        print("  At least one job title is required.")
        search["job_titles"] = _prompt_list("Target job titles", ["Software Engineer"])

    # --- location ---
    print("\n-- Location --")
    location = {
        "preferred_cities": _prompt_list(
            "Preferred cities",
            exist_loc.get("preferred_cities") or ["Pune", "Bangalore", "Remote"],
        ),
        "remote_only": _prompt_bool("Remote only?", bool(exist_loc.get("remote_only", False))),
        "willing_to_relocate": _prompt_bool(
            "Willing to relocate?", bool(exist_loc.get("willing_to_relocate", True))
        ),
    }

    # --- filters ---
    print("\n-- Filters --")
    filters = {
        "min_experience": _prompt_int(
            "Min experience (years)", int(exist_filters.get("min_experience") or 0), 0
        ),
        "max_experience": _prompt_int(
            "Max experience (years)", int(exist_filters.get("max_experience") or 10), 0
        ),
        "min_salary_lpa": _prompt_int(
            "Min salary (LPA)", int(exist_filters.get("min_salary_lpa") or 0), 0
        ),
        "max_salary_lpa": _prompt_int(
            "Max salary (LPA)", int(exist_filters.get("max_salary_lpa") or 100), 0
        ),
        "job_type": _prompt(
            "Job type (any | full_time | part_time | contract)",
            exist_filters.get("job_type", "any"),
        ),
        "posted_within_days": _prompt_int(
            "Posted within (days)", int(exist_filters.get("posted_within_days") or 30), 0
        ),
    }

    # --- platforms + secrets ---
    print("\n-- Platforms --  (answer y/N for each)")
    exist_platforms = existing.get("platforms", {}) or {}
    platforms_cfg: dict[str, dict[str, Any]] = {}
    env_values: dict[str, str] = {}

    for key in PLATFORM_KEYS:
        prev = exist_platforms.get(key, {}) or {}
        enabled = _prompt_bool(
            f"Enable {key}?", bool(prev.get("enabled", key == "remoteok"))
        )
        entry: dict[str, Any] = {"enabled": enabled}

        if key == "naukri" and enabled:
            naukri_email = _prompt_email("  Naukri email", "")
            naukri_pw = _prompt_secret("  Naukri password")
            if naukri_email:
                env_values["NAUKRI_EMAIL"] = naukri_email
            if naukri_pw:
                env_values["NAUKRI_PASSWORD"] = naukri_pw
            entry["email"] = "${NAUKRI_EMAIL}"
            entry["password"] = "${NAUKRI_PASSWORD}"
        elif key == "linkedin" and enabled:
            print(
                "  LinkedIn requires the `li_at` cookie (DevTools > Application > Cookies)."
            )
            cookie = _prompt_secret("  li_at cookie value")
            if cookie:
                env_values["LI_AT_COOKIE"] = cookie
            entry["li_at_cookie"] = "${LI_AT_COOKIE}"

        platforms_cfg[key] = entry

    # --- AI matching ---
    print("\n-- AI matching (OpenAI) --")
    ai_enabled = _prompt_bool("Enable AI job scoring?", bool(exist_ai.get("enabled", False)))
    ai: dict[str, Any] = {
        "enabled": ai_enabled,
        "openai_api_key": "${OPENAI_API_KEY}",
        "model": exist_ai.get("model", "gpt-4o-mini"),
        "min_match_score": int(exist_ai.get("min_match_score") or 30),
    }
    if ai_enabled:
        ai["model"] = _prompt("OpenAI model", ai["model"])
        ai["min_match_score"] = _prompt_int("Minimum match score (0-100)", ai["min_match_score"], 0)
        api_key = _prompt_secret("  OpenAI API key")
        if api_key:
            env_values["OPENAI_API_KEY"] = api_key

    # --- auto-apply ---
    print("\n-- Auto-apply --")
    apply_enabled = _prompt_bool(
        "Enable auto-apply (Selenium)?", bool(exist_apply.get("enabled", False))
    )
    auto_apply: dict[str, Any] = {
        "enabled": apply_enabled,
        "daily_limit": int(exist_apply.get("daily_limit") or 25),
        "headless": True,
        "chrome_profile_dir": exist_apply.get("chrome_profile_dir", ""),
        "platforms": exist_apply.get("platforms") or ["naukri"],
        "rate_limit": exist_apply.get("rate_limit")
        or {"naukri": 60, "linkedin": 90, "default": 60},
        "screening_answers": exist_apply.get("screening_answers")
        or {
            "current_ctc": "",
            "expected_ctc": "",
            "notice_period": "",
            "total_experience": "",
            "current_location": "",
            "willing_to_relocate": "Yes",
            "highest_qualification": "",
            "current_designation": "",
            "skills_yes": [],
            "default_skill_answer": "No",
            "custom_answers": {},
        },
    }
    if apply_enabled:
        auto_apply["daily_limit"] = _prompt_int(
            "Daily apply limit", auto_apply["daily_limit"], 0
        )
        auto_apply["headless"] = _prompt_bool(
            "Run Chrome headless? (recommended — visible windows can get killed)", True
        )
        sa = auto_apply["screening_answers"]
        print("  Screening answers (leave blank to skip):")
        sa["current_ctc"] = _prompt("    Current CTC", sa.get("current_ctc", ""))
        sa["expected_ctc"] = _prompt("    Expected CTC", sa.get("expected_ctc", ""))
        sa["notice_period"] = _prompt("    Notice period", sa.get("notice_period", ""))
        sa["total_experience"] = _prompt(
            "    Total experience", sa.get("total_experience", "") or str(profile["experience_years"])
        )
        sa["current_location"] = _prompt(
            "    Current location", sa.get("current_location", "") or profile["location"]
        )
        sa["highest_qualification"] = _prompt(
            "    Highest qualification", sa.get("highest_qualification", "")
        )
        sa["current_designation"] = _prompt(
            "    Current designation", sa.get("current_designation", "") or profile["current_role"]
        )

    # --- notifications ---
    print("\n-- Notifications --")
    notifications = {
        "desktop": _prompt_bool("Desktop notifications?", bool(exist_notif.get("desktop", True))),
        "email": _prompt_bool("Email?", bool(exist_notif.get("email", False))),
        "telegram": _prompt_bool("Telegram?", bool(exist_notif.get("telegram", False))),
        "slack": _prompt_bool("Slack?", bool(exist_notif.get("slack", False))),
        "whatsapp": _prompt_bool("WhatsApp?", bool(exist_notif.get("whatsapp", False))),
    }
    if notifications["telegram"]:
        token = _prompt_secret("  Telegram bot token")
        chat_id = _prompt("  Telegram chat id", "")
        if token:
            env_values["TELEGRAM_BOT_TOKEN"] = token
        if chat_id:
            env_values["TELEGRAM_CHAT_ID"] = chat_id
    if notifications["slack"]:
        hook = _prompt_secret("  Slack webhook URL")
        if hook:
            env_values["SLACK_WEBHOOK_URL"] = hook

    # --- output ---
    print("\n-- Output --")
    output = {
        "results_dir": _prompt("Results directory", exist_output.get("results_dir", "results")),
        "format": _prompt(
            "Report format (csv | html | json | both)", exist_output.get("format", "both")
        ),
        "max_results_per_platform": _prompt_int(
            "Max results per platform",
            int(exist_output.get("max_results_per_platform") or 50),
            1,
        ),
        "auto_open_report": _prompt_bool(
            "Auto-open report after run?", bool(exist_output.get("auto_open_report", True))
        ),
    }

    # --- assemble config ---
    cfg: dict[str, Any] = {
        "profile": profile,
        "search": search,
        "location": location,
        "filters": filters,
        "platforms": platforms_cfg,
        "ai_matching": ai,
        "company_intel": existing.get("company_intel")
        or {"enabled": False, "blacklist": [], "whitelist": []},
        "auto_apply": auto_apply,
        "resume_tailoring": existing.get("resume_tailoring") or {"enabled": False, "top_n": 5},
        "interview_prep": existing.get("interview_prep") or {"enabled": False, "top_n": 5},
        "gap_analysis": existing.get("gap_analysis") or {"enabled": False},
        "linkedin_network": existing.get("linkedin_network")
        or {"enabled": False, "auto_connect": False, "top_n": 5},
        "notifications": notifications,
        "export": existing.get("export") or {"json": True, "google_sheets": False, "notion": False},
        "scheduler": existing.get("scheduler") or {"run_time": "08:00"},
        "output": output,
    }

    _write_yaml(config_path, cfg)
    if env_values:
        _write_env_file(env_path, env_values)
        print(f"\nWrote secrets to {env_path}")
    print(f"Wrote configuration to {config_path}")
    print("\nNext steps:")
    print("  1. pip install -r requirements.txt")
    print("  2. python job_hunter.py --platforms remoteok --no-apply")
    print()


# --------------------------------------------------------------------------- #
# Agent run                                                                   #
# --------------------------------------------------------------------------- #


def _enabled_platforms(config: dict[str, Any]) -> list[str]:
    plats = config.get("platforms", {}) or {}
    return [name for name, cfg in plats.items() if (cfg or {}).get("enabled")]


def _print_jobs_table(jobs: list[Job], limit: int = 25) -> None:
    if not jobs:
        print("\n(No jobs to display.)")
        return

    print()
    header = f"{'#':<3} {'Score':>5}  {'Platform':<10} {'Title':<45} {'Company':<25} {'Location':<20}"
    print(header)
    print("-" * len(header))
    for i, j in enumerate(jobs[:limit], 1):
        title = (j.title or "")[:44]
        company = (j.company or "")[:24]
        loc = (j.location or "")[:19]
        plat = (j.platform or "")[:9]
        print(f"{i:<3} {j.match_score:>5.1f}  {plat:<10} {title:<45} {company:<25} {loc:<20}")
    if len(jobs) > limit:
        print(f"... and {len(jobs) - limit} more")
    print()


def _run_scraper(
    name: str,
    config: dict[str, Any],
    shutdown_event: threading.Event,
) -> tuple[str, list[Job]]:
    cls = SCRAPERS.get(name)
    if cls is None:
        logger.warning("[%s] No scraper registered — skipping.", name)
        return (name, [])

    scraper = cls(config)
    scraper.set_shutdown_event(shutdown_event)
    try:
        jobs = scraper.search_all()
        return (name, jobs)
    except Exception as e:
        logger.exception("[%s] scraper crashed: %s", name, e)
        return (name, [])
    finally:
        try:
            scraper.close()
        except Exception:
            pass


def run_agent(
    config: dict[str, Any],
    platforms: list[str] | None = None,
    new_only: bool = False,
    no_apply: bool = False,
) -> dict[str, Any]:
    """Main orchestration pipeline.

    Returns a summary dict: {total_scraped, new_jobs, duration_seconds, platforms}.
    """
    start = time.time()

    output = config.get("output", {}) or {}
    results_dir = output.get("results_dir", "results")
    db = JobDatabase(db_path=resolve_db_path(results_dir))

    all_enabled = _enabled_platforms(config)
    if platforms:
        unknown = [p for p in platforms if p not in SCRAPERS]
        if unknown:
            logger.warning("Unknown platform(s) ignored: %s", ", ".join(unknown))
        chosen = [p for p in platforms if p in SCRAPERS]
    else:
        chosen = [p for p in all_enabled if p in SCRAPERS]

    if not chosen:
        logger.error(
            "No runnable platforms selected. Enabled=%s, registered=%s",
            all_enabled, sorted(SCRAPERS.keys()),
        )
        return {"total_scraped": 0, "new_jobs": 0, "duration_seconds": 0.0, "platforms": []}

    logger.info("Running platforms: %s", ", ".join(chosen))
    pre_run_urls = db.get_existing_urls() if new_only else set()
    logger.info("Pre-run DB snapshot: %d existing URLs.", len(pre_run_urls))

    # When --new-only is on, push the URL set down into the scrapers via a
    # private runtime slot. This lets each scraper skip already-seen URLs
    # DURING the search loop instead of after — so the per-platform cap is
    # filled with genuinely new postings, not stale ones that get filtered out.
    runtime = dict(config.get("_runtime") or {})
    if new_only:
        runtime["skip_urls"] = pre_run_urls
    else:
        runtime.pop("skip_urls", None)
    config["_runtime"] = runtime

    shutdown_event = threading.Event()
    _install_signal_handler(shutdown_event)

    all_jobs: dict[str, Job] = {}
    total_new = 0
    total_updated = 0

    # Each scraper spawns its own headless Chrome. Running too many in parallel
    # on a single laptop overwhelms CPU/RAM/network and causes 120s chromedriver
    # RPC timeouts (see error.log). 2 is a safe default; users with a beefy
    # machine can raise via `scraper.workers` in config.yaml.
    workers_cfg = (config.get("scraper") or {}).get("workers", 2)
    try:
        workers = max(1, int(workers_cfg))
    except (TypeError, ValueError):
        workers = 2
    workers = min(len(chosen), workers)
    logger.info("Running %d scraper(s) with %d worker(s).", len(chosen), workers)

    # Build the company filter ONCE up-front so we can apply it before each
    # per-platform DB upsert. This way blacklisted companies (e.g. Accenture)
    # never get written to the DB at all — previously the filter ran only
    # after the upsert, so the rows still leaked through.
    intel = CompanyIntel(config)

    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {
            pool.submit(_run_scraper, name, config, shutdown_event): name
            for name in chosen
        }
        for fut in concurrent.futures.as_completed(futures):
            name = futures[fut]
            try:
                _, jobs = fut.result()
            except Exception as e:
                logger.exception("[%s] future failed: %s", name, e)
                continue

            jobs = intel.filter(jobs)

            for j in jobs:
                if j.url and j.url not in all_jobs:
                    all_jobs[j.url] = j

            # Incremental save so Ctrl+C doesn't lose work.
            if jobs:
                new_count, upd_count = db.upsert_jobs(jobs)
                total_new += new_count
                total_updated += upd_count
                logger.info(
                    "[%s] DB: +%d new, %d updated (%d scraped this platform).",
                    name, new_count, upd_count, len(jobs),
                )

    jobs_list = list(all_jobs.values())
    logger.info("Deduped total: %d unique jobs across platforms.", len(jobs_list))

    if new_only:
        filtered = [j for j in jobs_list if j.url not in pre_run_urls]
        logger.info("--new-only: filtered to %d jobs (was %d).", len(filtered), len(jobs_list))
        jobs_list = filtered

    matcher = AIJobMatcher(config)
    if matcher.enabled:
        matcher.score_jobs(jobs_list)
        jobs_list = matcher.filter_by_score(jobs_list)
        if jobs_list:
            db.upsert_jobs(jobs_list)

    jobs_list.sort(key=lambda j: (-(j.match_score or 0.0), j.title or ""))
    _print_jobs_table(jobs_list)

    try:
        written = generate_reports(jobs_list, config)
        if written:
            paths = ", ".join(f"{k}={v.name}" for k, v in written.items())
            logger.info("Reports written: %s", paths)
    except Exception as e:
        logger.exception("Report generation failed: %s", e)

    # --- Phase 4 post-processing (each gated by its own config block) --- #
    _run_phase4(config, jobs_list, no_apply=no_apply)

    duration = time.time() - start
    db.log_run(chosen, len(all_jobs), total_new, duration)
    logger.info(
        "Run complete in %.1fs — %d unique jobs (%d new, %d updated).",
        duration, len(all_jobs), total_new, total_updated,
    )

    summary = {
        "total_scraped": len(all_jobs),
        "new_jobs": total_new,
        "updated_jobs": total_updated,
        "duration_seconds": duration,
        "platforms": chosen,
        "final_jobs": len(jobs_list),
    }

    try:
        Notifier(config).send_run_summary(summary, top_jobs=jobs_list[:5])
    except Exception as e:
        logger.warning("Notifier failed: %s", e)

    return summary


def _run_phase4(
    config: dict[str, Any],
    jobs_list: list[Job],
    *,
    no_apply: bool,
) -> None:
    """Run every Phase 4 post-processing stage, each guarded so one failure
    never blocks the others. Each module internally checks its `enabled` flag
    and degrades gracefully when credentials are missing."""

    # 1. Exporters (JSON / Google Sheets / Notion)
    try:
        exported = Exporter(config).export_all(jobs_list)
        if exported:
            logger.info("Exports: %s", exported)
    except Exception as e:
        logger.warning("Exporter stage failed: %s", e)

    # 2. Gap analysis (no LLM, fast)
    try:
        GapAnalyzer(config).analyze(jobs_list)
    except Exception as e:
        logger.warning("Gap analysis stage failed: %s", e)

    # 3. Resume tailoring (LLM)
    try:
        ResumeTailor(config).tailor(jobs_list)
    except Exception as e:
        logger.warning("Resume tailor stage failed: %s", e)

    # 4. Interview prep (LLM)
    try:
        InterviewPrep(config).generate(jobs_list)
    except Exception as e:
        logger.warning("Interview prep stage failed: %s", e)

    # 5. LinkedIn warm-intro finder
    try:
        LinkedInNetwork(config).find(jobs_list)
    except Exception as e:
        logger.warning("LinkedIn network stage failed: %s", e)

    # 6. Auto-apply (Selenium, last because it's the heaviest & most fragile)
    if no_apply:
        logger.info("--no-apply set; skipping auto_apply stage.")
        return
    try:
        AutoApplier(config).apply_to_jobs(jobs_list)
    except Exception as e:
        logger.warning("Auto-apply stage failed: %s", e)


def _install_signal_handler(event: threading.Event) -> None:
    def _handler(signum, frame):  # noqa: ARG001
        logger.warning("Received signal %s — requesting graceful shutdown.", signum)
        event.set()

    try:
        signal.signal(signal.SIGINT, _handler)
    except (ValueError, OSError):
        # Only main thread on the main interpreter can install signal handlers.
        pass


# --------------------------------------------------------------------------- #
# CLI                                                                         #
# --------------------------------------------------------------------------- #


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="job_hunter",
        description="Job Hunt AI Agent — scrape, score, and track jobs.",
    )
    p.add_argument("--config", default=str(DEFAULT_CONFIG_PATH), help="Path to config.yaml")
    p.add_argument(
        "--platforms",
        nargs="+",
        metavar="NAME",
        help="Override enabled platforms (space-separated, e.g. --platforms remoteok linkedin)",
    )
    p.add_argument(
        "--new-only",
        action="store_true",
        help="Only show / report jobs whose URL wasn't in the DB before this run",
    )
    p.add_argument("--no-apply", action="store_true", help="Skip auto-apply stage")
    p.add_argument("--web", action="store_true", help="(Phase 3) Launch Flask dashboard after run")
    p.add_argument("--verbose", action="store_true", help="Verbose (DEBUG) logging")
    p.add_argument(
        "--setup",
        action="store_true",
        help="Run the interactive setup wizard and exit",
    )
    return p


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    setup_logging(verbose=args.verbose)

    if args.setup:
        run_setup_wizard(Path(args.config), DEFAULT_ENV_PATH)
        return 0

    try:
        config = load_config(args.config)
    except FileNotFoundError as e:
        logger.error(str(e))
        return 2

    summary = run_agent(
        config,
        platforms=args.platforms,
        new_only=args.new_only,
        no_apply=args.no_apply,
    )

    if args.web:
        _launch_web_dashboard()

    return 0 if summary["total_scraped"] >= 0 else 1


def _launch_web_dashboard(host: str = "127.0.0.1", port: int = 5000) -> None:
    """Launch Flask dashboard in the current process (blocks until Ctrl+C)."""
    try:
        from web_app import app as flask_app  # local import to avoid hard dep
    except ImportError as e:
        logger.error("Could not import web_app: %s", e)
        return
    import webbrowser

    url = f"http://{host}:{port}/"
    logger.info("Launching Flask dashboard at %s (Ctrl+C to stop)", url)
    try:
        webbrowser.open(url)
    except Exception:
        pass
    flask_app.run(host=host, port=port, threaded=True)


if __name__ == "__main__":
    sys.exit(main())
