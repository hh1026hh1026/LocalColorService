"""Offline job maintenance.

The API endpoints (DELETE /v1/jobs/{id}, /v1/jobs/cancel-all, /v1/jobs/purge)
cover the running service. This script does the same against the database
directly, for when the service is stopped - for example clearing a stuck job
before restarting, so the interrupted-job recovery does not immediately start it
again.

Usage:
    python scripts/jobs_admin.py status
    python scripts/jobs_admin.py cancel-active
    python scripts/jobs_admin.py purge --older-than-days 7
    python scripts/jobs_admin.py purge --statuses failed cancelled
"""

from __future__ import annotations

import argparse
import datetime
import os
import shutil
import sqlite3
import sys
from pathlib import Path

REPOSITORY = Path(__file__).resolve().parent.parent


def data_dir() -> Path:
    configured = os.getenv("DATA_DIR")
    if configured:
        return Path(configured)
    env_file = REPOSITORY / ".env"
    if env_file.is_file():
        for line in env_file.read_text(encoding="utf-8", errors="replace").splitlines():
            if line.strip().startswith("DATA_DIR="):
                return Path(line.split("=", 1)[1].strip())
    return REPOSITORY / "data"


def connect(root: Path) -> sqlite3.Connection:
    database = root / "local_color.db"
    if not database.is_file():
        sys.exit(f"No job database at {database}")
    connection = sqlite3.connect(str(database), timeout=15.0)
    connection.row_factory = sqlite3.Row
    return connection


def command_status(root: Path) -> None:
    connection = connect(root)
    print(f"Database: {root / 'local_color.db'}")
    for row in connection.execute(
        "SELECT status, COUNT(*) AS total FROM jobs GROUP BY status ORDER BY total DESC"
    ):
        print(f"  {row['status']:<12} {row['total']}")
    active = connection.execute(
        "SELECT job_id, job_type, status, created_at FROM jobs "
        "WHERE status IN ('pending','processing') ORDER BY created_at"
    ).fetchall()
    if not active:
        print("\nNo pending or processing jobs.")
        return
    print("\nActive:")
    for row in active:
        print(f"  {row['job_id']}  {row['job_type']:<22} {row['status']:<11} {row['created_at']}")


def command_cancel_active(root: Path) -> None:
    connection = connect(root)
    now = datetime.datetime.now(datetime.timezone.utc).isoformat()
    rows = connection.execute(
        "SELECT job_id, job_type, status FROM jobs WHERE status IN ('pending','processing')"
    ).fetchall()
    if not rows:
        print("Nothing to cancel.")
        return
    connection.execute(
        "UPDATE jobs SET status='cancelled', progress=100, error_message=?, updated_at=? "
        "WHERE status IN ('pending','processing')",
        ("Cancelled from scripts/jobs_admin.py", now),
    )
    connection.commit()
    for row in rows:
        print(f"  cancelled {row['job_id']}  {row['job_type']} (was {row['status']})")
    print(f"\n{len(rows)} job(s) cancelled. They will not be restarted on the next launch.")


def command_purge(root: Path, statuses: list[str], older_than_days: float | None) -> None:
    if {"pending", "processing"} & set(statuses):
        sys.exit("Refusing to purge active jobs; run cancel-active first.")
    connection = connect(root)
    query = f"SELECT job_id FROM jobs WHERE status IN ({','.join('?' * len(statuses))})"
    params: list = list(statuses)
    if older_than_days is not None:
        cutoff = (
            datetime.datetime.now(datetime.timezone.utc)
            - datetime.timedelta(days=older_than_days)
        ).isoformat()
        query += " AND updated_at < ?"
        params.append(cutoff)
    job_ids = [row["job_id"] for row in connection.execute(query, params).fetchall()]
    if not job_ids:
        print("Nothing matched.")
        return
    freed = 0
    for job_id in job_ids:
        directory = root / "jobs" / job_id
        if directory.is_dir():
            freed += sum(item.stat().st_size for item in directory.rglob("*") if item.is_file())
            shutil.rmtree(directory, ignore_errors=True)
    connection.executemany("DELETE FROM jobs WHERE job_id = ?", [(item,) for item in job_ids])
    connection.commit()
    print(f"Purged {len(job_ids)} job(s), freed {freed / 1e9:.2f} GB from {root / 'jobs'}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Local Color Service job maintenance")
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("status", help="Show job counts and anything still active")
    subparsers.add_parser("cancel-active", help="Cancel every pending and processing job")
    purge = subparsers.add_parser("purge", help="Delete finished jobs and their directories")
    purge.add_argument("--statuses", nargs="+", default=["completed", "failed", "cancelled"])
    purge.add_argument("--older-than-days", type=float, default=None)
    args = parser.parse_args()

    root = data_dir()
    if args.command == "status":
        command_status(root)
    elif args.command == "cancel-active":
        command_cancel_active(root)
    else:
        command_purge(root, args.statuses, args.older_than_days)


if __name__ == "__main__":
    main()
