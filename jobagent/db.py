"""SQLite store for discovered jobs and their scores.

Job status flow: new -> scored -> (approved | skipped); approved -> tailored -> applied
                 (`add` puts hand-picked jobs in as new; `apply` marks a job applied once you submit)
                 new -> filtered  (no longer passes the search filters after they were tightened)
"""

import json
import sqlite3
from contextlib import contextmanager

from .sources import Job

DB_PATH = "jobs.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    id          TEXT PRIMARY KEY,
    source      TEXT NOT NULL,
    company     TEXT NOT NULL,
    title       TEXT NOT NULL,
    location    TEXT,
    url         TEXT NOT NULL,
    description TEXT,
    status      TEXT NOT NULL DEFAULT 'new',
    score       INTEGER,
    verdict     TEXT,          -- JSON from the SCREEN pass
    analysis    TEXT,          -- JSON from the FULL pass (on demand)
    tailoring   TEXT,          -- JSON report from the tailoring stage
    applied_at  TIMESTAMP,
    posted_at   TEXT,          -- date the employer first posted it, if the board says
    first_seen  TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    scored_at   TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_jobs_status ON jobs(status);
"""


@contextmanager
def connect(path: str = DB_PATH):
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    try:
        conn.executescript(SCHEMA)
        columns = {r["name"] for r in conn.execute("PRAGMA table_info(jobs)")}
        if "analysis" not in columns:  # databases created before the FULL pass existed
            conn.execute("ALTER TABLE jobs ADD COLUMN analysis TEXT")
        if "tailoring" not in columns:  # databases created before the tailoring stage existed
            conn.execute("ALTER TABLE jobs ADD COLUMN tailoring TEXT")
        if "posted_at" not in columns:  # databases created before posting dates were kept
            conn.execute("ALTER TABLE jobs ADD COLUMN posted_at TEXT")
        if "applied_at" not in columns:  # databases created before the applying stage existed
            conn.execute("ALTER TABLE jobs ADD COLUMN applied_at TIMESTAMP")
        yield conn
        conn.commit()
    finally:
        conn.close()


def insert_job(conn, job: Job) -> bool:
    """Insert a job if unseen. Returns True if it was new."""
    cur = conn.execute(
        """INSERT OR IGNORE INTO jobs (id, source, company, title, location, url, description, posted_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        (job.id, job.source, job.company, job.title, job.location, job.url, job.description, job.posted or None),
    )
    if cur.rowcount == 0 and job.posted:  # already known: fill in the date for jobs saved before dates were kept
        conn.execute("UPDATE jobs SET posted_at = ? WHERE id = ? AND posted_at IS NULL", (job.posted, job.id))
    return cur.rowcount == 1


def jobs_with_status(conn, status: str, limit: int | None = None):
    sql = "SELECT * FROM jobs WHERE status = ? ORDER BY first_seen"
    params: tuple = (status,)
    if limit:
        sql += " LIMIT ?"
        params += (limit,)
    return conn.execute(sql, params).fetchall()


def save_score(conn, job_id: str, score: int, verdict: dict):
    conn.execute(
        """UPDATE jobs SET status = 'scored', score = ?, verdict = ?, scored_at = CURRENT_TIMESTAMP
           WHERE id = ?""",
        (score, json.dumps(verdict), job_id),
    )


def scored_jobs(conn, min_score: int):
    return conn.execute(
        "SELECT * FROM jobs WHERE status = 'scored' AND score >= ? ORDER BY score DESC",
        (min_score,),
    ).fetchall()


def set_status(conn, job_id: str, status: str) -> bool:
    cur = conn.execute("UPDATE jobs SET status = ? WHERE id = ?", (status, job_id))
    return cur.rowcount == 1


def save_analysis(conn, job_id: str, score: int, analysis: dict):
    """Store a FULL analysis; its score replaces the screening score."""
    conn.execute(
        "UPDATE jobs SET score = ?, analysis = ?, scored_at = CURRENT_TIMESTAMP WHERE id = ?",
        (score, json.dumps(analysis), job_id),
    )


def approved_without_analysis(conn):
    return conn.execute(
        "SELECT * FROM jobs WHERE status = 'approved' AND analysis IS NULL ORDER BY score DESC"
    ).fetchall()


def save_tailoring(conn, job_id: str, report: dict):
    status = "tailored" if report["decision"] == "tailor" else "approved"
    conn.execute("UPDATE jobs SET tailoring = ?, status = ? WHERE id = ?", (json.dumps(report), status, job_id))


def mark_applied(conn, job_id: str) -> bool:
    cur = conn.execute("UPDATE jobs SET status = 'applied', applied_at = CURRENT_TIMESTAMP WHERE id = ?", (job_id,))
    return cur.rowcount == 1
