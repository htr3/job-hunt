"""Exporters to external systems.

- **JSON** - always available; writes `exports/jobs_YYYYMMDD_HHMMSS.json`.
- **Google Sheets** - requires `gspread` + `google-auth`. Credentials file path
  comes from env var `GOOGLE_CREDENTIALS_JSON`; target sheet name from
  `GOOGLE_SHEET_NAME` (default: "Job Hunt Agent").
- **Notion** - requires `notion-client`. Token in `NOTION_TOKEN`,
  database id in `NOTION_DATABASE_ID`.

All exporters are best-effort: missing libraries or credentials log a warning
and silently skip. None of them raises; the pipeline always continues.
"""
from __future__ import annotations

import json
import logging
import os
from datetime import datetime
from pathlib import Path
from typing import Any

from report_generator import _job_to_dict  # reuse the same canonical shape

logger = logging.getLogger("exporters")

PROJECT_ROOT = Path(__file__).resolve().parent
EXPORTS_DIR = PROJECT_ROOT / "exports"


class Exporter:
    def __init__(self, config: dict[str, Any]) -> None:
        export_cfg = config.get("export", {}) or {}
        self.json_on = bool(export_cfg.get("json"))
        self.sheets_on = bool(export_cfg.get("google_sheets"))
        self.notion_on = bool(export_cfg.get("notion"))
        self._config = config

    def export_all(self, jobs: list) -> dict[str, str | None]:
        """Run every enabled exporter. Returns {name: output-spec or None}."""
        if not jobs:
            logger.info("Exporter: no jobs to export.")
            return {}

        out: dict[str, str | None] = {}
        if self.json_on:
            out["json"] = self._to_json(jobs)
        if self.sheets_on:
            out["google_sheets"] = self._to_sheets(jobs)
        if self.notion_on:
            out["notion"] = self._to_notion(jobs)
        return out

    # ------------------------------------------------------------------ json
    def _to_json(self, jobs: list) -> str | None:
        try:
            EXPORTS_DIR.mkdir(parents=True, exist_ok=True)
            stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            path = EXPORTS_DIR / f"jobs_{stamp}.json"
            payload = [_job_to_dict(j) for j in jobs]
            path.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2, default=str),
                encoding="utf-8",
            )
            logger.info("Exported %d jobs to %s", len(jobs), path)
            return str(path)
        except Exception as e:
            logger.warning("JSON export failed: %s", e)
            return None

    # ------------------------------------------------------------------ sheets
    def _to_sheets(self, jobs: list) -> str | None:
        try:
            import gspread  # type: ignore
            from google.oauth2.service_account import Credentials  # type: ignore
        except ImportError:
            logger.warning(
                "google_sheets enabled but `gspread` / `google-auth` not installed. "
                "Run: pip install gspread google-auth"
            )
            return None

        creds_path = os.environ.get("GOOGLE_CREDENTIALS_JSON", "")
        if not creds_path or not Path(creds_path).exists():
            logger.warning(
                "google_sheets enabled but GOOGLE_CREDENTIALS_JSON env var is empty "
                "or points to a non-existent file."
            )
            return None

        sheet_name = os.environ.get("GOOGLE_SHEET_NAME", "Job Hunt Agent")
        try:
            scopes = [
                "https://www.googleapis.com/auth/spreadsheets",
                "https://www.googleapis.com/auth/drive",
            ]
            creds = Credentials.from_service_account_file(creds_path, scopes=scopes)
            client = gspread.authorize(creds)
            try:
                sh = client.open(sheet_name)
            except Exception:
                sh = client.create(sheet_name)
            ws = sh.sheet1
            rows = [_job_to_dict(j) for j in jobs]
            headers = list(rows[0].keys()) if rows else []
            ws.clear()
            ws.update([headers] + [[str(r.get(h, "")) for h in headers] for r in rows])
            logger.info("Exported %d jobs to Google Sheet %r", len(jobs), sheet_name)
            return sheet_name
        except Exception as e:
            logger.warning("Google Sheets export failed: %s", e)
            return None

    # ------------------------------------------------------------------ notion
    def _to_notion(self, jobs: list) -> str | None:
        try:
            from notion_client import Client  # type: ignore
        except ImportError:
            logger.warning(
                "notion enabled but `notion-client` not installed. "
                "Run: pip install notion-client"
            )
            return None

        token = os.environ.get("NOTION_TOKEN")
        db_id = os.environ.get("NOTION_DATABASE_ID")
        if not (token and db_id):
            logger.warning(
                "notion enabled but NOTION_TOKEN / NOTION_DATABASE_ID env vars are missing."
            )
            return None

        try:
            client = Client(auth=token)
            written = 0
            for j in jobs:
                d = _job_to_dict(j)
                client.pages.create(
                    parent={"database_id": db_id},
                    properties={
                        "Title": {"title": [{"text": {"content": str(d.get("title", ""))[:200]}}]},
                        "Company": {"rich_text": [{"text": {"content": str(d.get("company", ""))}}]},
                        "Platform": {"rich_text": [{"text": {"content": str(d.get("platform", ""))}}]},
                        "Location": {"rich_text": [{"text": {"content": str(d.get("location", ""))}}]},
                        "Score": {"number": float(d.get("match_score") or 0)},
                        "URL": {"url": d.get("url") or None},
                    },
                )
                written += 1
            logger.info("Exported %d jobs to Notion database %s", written, db_id)
            return db_id
        except Exception as e:
            logger.warning("Notion export failed: %s", e)
            return None


def export_jobs(config: dict[str, Any], jobs: list) -> dict[str, str | None]:
    return Exporter(config).export_all(jobs)
