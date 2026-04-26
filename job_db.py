"""SQLite wrapper for the Job Hunt AI Agent.

Single source of truth for scraped jobs and run history. Designed so the CLI,
Flask web app, and Streamlit dashboard can safely share the same DB file.
"""
from __future__ import annotations

import logging
import sqlite3
import threading
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Iterable

logger = logging.getLogger(__name__)

VALID_STATUSES: set[str] = {
    "new",
    "applied",
    "interview",
    "rejected",
    "offer",
    "saved",
    "hidden",
}

_PROJECT_ROOT = Path(__file__).resolve().parent


def resolve_db_path(results_dir: str = "results", filename: str = "jobs.db") -> Path:
    """Return the DB path anchored at the project root (cwd-safe)."""
    path = _PROJECT_ROOT / results_dir / filename
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


class JobDatabase:
    """Thin SQLite wrapper with upsert-by-URL semantics."""

    def __init__(self, db_path: str | Path | None = None, results_dir: str = "results"):
        self.db_path = Path(db_path) if db_path else resolve_db_path(results_dir)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._init_schema()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.db_path), timeout=30.0)
        conn.execute("PRAGMA busy_timeout = 30000")
        conn.execute("PRAGMA journal_mode = WAL")
        conn.row_factory = sqlite3.Row
        return conn

    def _init_schema(self) -> None:
        with self._connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS jobs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    url TEXT UNIQUE NOT NULL,
                    title TEXT NOT NULL,
                    company TEXT DEFAULT '',
                    location TEXT DEFAULT '',
                    platform TEXT DEFAULT '',
                    description TEXT DEFAULT '',
                    salary TEXT DEFAULT '',
                    experience TEXT DEFAULT '',
                    job_type TEXT DEFAULT '',
                    posted_date TEXT DEFAULT '',
                    skills TEXT DEFAULT '',
                    match_score REAL DEFAULT 0,
                    status TEXT DEFAULT 'new',
                    first_seen_at TEXT NOT NULL,
                    last_seen_at TEXT NOT NULL,
                    applied_at TEXT,
                    notes TEXT DEFAULT ''
                );

                CREATE INDEX IF NOT EXISTS idx_jobs_url ON jobs(url);
                CREATE INDEX IF NOT EXISTS idx_jobs_status ON jobs(status);
                CREATE INDEX IF NOT EXISTS idx_jobs_platform ON jobs(platform);
                CREATE INDEX IF NOT EXISTS idx_jobs_first_seen ON jobs(first_seen_at);

                CREATE TABLE IF NOT EXISTS run_history (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    run_at TEXT NOT NULL,
                    platforms TEXT DEFAULT '',
                    total_scraped INTEGER DEFAULT 0,
                    new_jobs INTEGER DEFAULT 0,
                    duration_seconds REAL DEFAULT 0
                );
                """
            )

    @staticmethod
    def _job_to_row(job: Any) -> dict[str, Any]:
        skills = getattr(job, "skills", []) or []
        if isinstance(skills, str):
            skills_str = skills
        else:
            skills_str = ",".join(str(s).strip() for s in skills if str(s).strip())
        return {
            "url": getattr(job, "url", "") or "",
            "title": getattr(job, "title", "") or "",
            "company": getattr(job, "company", "") or "",
            "location": getattr(job, "location", "") or "",
            "platform": getattr(job, "platform", "") or "",
            "description": getattr(job, "description", "") or "",
            "salary": getattr(job, "salary", "") or "",
            "experience": getattr(job, "experience", "") or "",
            "job_type": getattr(job, "job_type", "") or "",
            "posted_date": getattr(job, "posted_date", "") or "",
            "skills": skills_str,
            "match_score": float(getattr(job, "match_score", 0.0) or 0.0),
        }

    def upsert_job(self, job: Any) -> bool:
        """Insert or update a single job. Returns True if this URL was new."""
        new_count, _ = self.upsert_jobs([job])
        return new_count == 1

    def upsert_jobs(self, jobs: Iterable[Any]) -> tuple[int, int]:
        """Bulk upsert. Returns (new_count, updated_count)."""
        jobs_list = [j for j in jobs if getattr(j, "url", None)]
        if not jobs_list:
            return (0, 0)

        now = datetime.utcnow().isoformat(timespec="seconds")
        new_count = 0
        updated_count = 0

        with self._lock, self._connect() as conn:
            cur = conn.cursor()
            for job in jobs_list:
                row = self._job_to_row(job)
                cur.execute("SELECT id FROM jobs WHERE url = ?", (row["url"],))
                existing = cur.fetchone()
                if existing:
                    cur.execute(
                        """
                        UPDATE jobs SET
                            title = ?, company = ?, location = ?, platform = ?,
                            description = ?, salary = ?, experience = ?, job_type = ?,
                            posted_date = ?, skills = ?, match_score = ?,
                            last_seen_at = ?
                        WHERE url = ?
                        """,
                        (
                            row["title"], row["company"], row["location"], row["platform"],
                            row["description"], row["salary"], row["experience"], row["job_type"],
                            row["posted_date"], row["skills"], row["match_score"],
                            now, row["url"],
                        ),
                    )
                    updated_count += 1
                else:
                    cur.execute(
                        """
                        INSERT INTO jobs (
                            url, title, company, location, platform, description,
                            salary, experience, job_type, posted_date, skills,
                            match_score, status, first_seen_at, last_seen_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'new', ?, ?)
                        """,
                        (
                            row["url"], row["title"], row["company"], row["location"],
                            row["platform"], row["description"], row["salary"],
                            row["experience"], row["job_type"], row["posted_date"],
                            row["skills"], row["match_score"], now, now,
                        ),
                    )
                    new_count += 1
            conn.commit()
        return (new_count, updated_count)

    def get_new_only(self, jobs: Iterable[Any]) -> list[Any]:
        """Filter `jobs` to only those whose URL is not already in the DB."""
        existing = self.get_existing_urls()
        return [j for j in jobs if getattr(j, "url", None) and j.url not in existing]

    def get_existing_urls(self) -> set[str]:
        with self._connect() as conn:
            rows = conn.execute("SELECT url FROM jobs").fetchall()
        return {r["url"] for r in rows}

    def update_status(self, url: str, status: str, notes: str | None = None) -> int:
        """Update status (and optionally notes). Returns rowcount."""
        if status not in VALID_STATUSES:
            raise ValueError(f"Invalid status: {status!r}. Must be one of {sorted(VALID_STATUSES)}")

        applied_at = datetime.utcnow().isoformat(timespec="seconds") if status == "applied" else None

        with self._lock, self._connect() as conn:
            cur = conn.cursor()
            if notes is None:
                if applied_at:
                    cur.execute(
                        "UPDATE jobs SET status = ?, applied_at = COALESCE(applied_at, ?) WHERE url = ?",
                        (status, applied_at, url),
                    )
                else:
                    cur.execute("UPDATE jobs SET status = ? WHERE url = ?", (status, url))
            else:
                if applied_at:
                    cur.execute(
                        "UPDATE jobs SET status = ?, notes = ?, applied_at = COALESCE(applied_at, ?) WHERE url = ?",
                        (status, notes, applied_at, url),
                    )
                else:
                    cur.execute(
                        "UPDATE jobs SET status = ?, notes = ? WHERE url = ?",
                        (status, notes, url),
                    )
            rowcount = cur.rowcount
            conn.commit()
        return rowcount

    def get_stats(self) -> dict[str, Any]:
        with self._connect() as conn:
            total = conn.execute("SELECT COUNT(*) AS c FROM jobs").fetchone()["c"]
            by_status_rows = conn.execute(
                "SELECT status, COUNT(*) AS c FROM jobs GROUP BY status"
            ).fetchall()
            by_platform_rows = conn.execute(
                "SELECT platform, COUNT(*) AS c FROM jobs GROUP BY platform"
            ).fetchall()
            cutoff = (datetime.utcnow() - timedelta(hours=24)).isoformat(timespec="seconds")
            new_24h = conn.execute(
                "SELECT COUNT(*) AS c FROM jobs WHERE first_seen_at >= ?", (cutoff,)
            ).fetchone()["c"]

        return {
            "total_jobs": total,
            "by_status": {r["status"]: r["c"] for r in by_status_rows},
            "by_platform": {r["platform"]: r["c"] for r in by_platform_rows},
            "new_last_24h": new_24h,
        }

    def log_run(
        self,
        platforms: list[str] | str,
        total_scraped: int,
        new_jobs: int,
        duration_seconds: float,
    ) -> None:
        platforms_str = ",".join(platforms) if isinstance(platforms, list) else str(platforms)
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                INSERT INTO run_history (run_at, platforms, total_scraped, new_jobs, duration_seconds)
                VALUES (?, ?, ?, ?, ?)
                """,
                (
                    datetime.utcnow().isoformat(timespec="seconds"),
                    platforms_str,
                    int(total_scraped),
                    int(new_jobs),
                    float(duration_seconds),
                ),
            )
            conn.commit()

    def search_jobs(
        self,
        query: str = "",
        status: str | None = None,
        platform: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        clauses: list[str] = []
        params: list[Any] = []
        if query:
            clauses.append("(title LIKE ? OR company LIKE ? OR description LIKE ? OR skills LIKE ?)")
            like = f"%{query}%"
            params.extend([like, like, like, like])
        if status:
            clauses.append("status = ?")
            params.append(status)
        if platform:
            clauses.append("platform = ?")
            params.append(platform)

        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        sql = (
            f"SELECT * FROM jobs {where} "
            f"ORDER BY match_score DESC, first_seen_at DESC "
            f"LIMIT ? OFFSET ?"
        )
        params.extend([int(limit), int(offset)])
        with self._connect() as conn:
            rows = conn.execute(sql, params).fetchall()
        return [dict(r) for r in rows]

    def get_job_by_id(self, job_id: int) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM jobs WHERE id = ?", (int(job_id),)).fetchone()
        return dict(row) if row else None

    def update_status_by_id(
        self,
        job_id: int,
        status: str,
        notes: str | None = None,
    ) -> int:
        """Id-based variant of update_status. Returns rowcount."""
        if status not in VALID_STATUSES:
            raise ValueError(
                f"Invalid status: {status!r}. Must be one of {sorted(VALID_STATUSES)}"
            )
        applied_at = (
            datetime.utcnow().isoformat(timespec="seconds") if status == "applied" else None
        )
        with self._lock, self._connect() as conn:
            cur = conn.cursor()
            if notes is None:
                if applied_at:
                    cur.execute(
                        "UPDATE jobs SET status = ?, applied_at = COALESCE(applied_at, ?) WHERE id = ?",
                        (status, applied_at, int(job_id)),
                    )
                else:
                    cur.execute(
                        "UPDATE jobs SET status = ? WHERE id = ?", (status, int(job_id))
                    )
            else:
                if applied_at:
                    cur.execute(
                        "UPDATE jobs SET status = ?, notes = ?, applied_at = COALESCE(applied_at, ?) WHERE id = ?",
                        (status, notes, applied_at, int(job_id)),
                    )
                else:
                    cur.execute(
                        "UPDATE jobs SET status = ?, notes = ? WHERE id = ?",
                        (status, notes, int(job_id)),
                    )
            rowcount = cur.rowcount
            conn.commit()
        return rowcount

    def recent_runs(self, limit: int = 20) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM run_history ORDER BY run_at DESC LIMIT ?", (int(limit),)
            ).fetchall()
        return [dict(r) for r in rows]
