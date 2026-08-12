"""
SQLite Database Manager for Local Color Service Job Queuing and Status Storage.
"""

import sqlite3
import json
import datetime
import uuid
from pathlib import Path
from typing import Optional, Dict, Any, List
from app.config import settings


class DatabaseManager:
    """
    Manages SQLite connection pool and job table operations for background tasks.
    """

    def __init__(self, db_path: Optional[str] = None):
        if db_path:
            self.db_file = Path(db_path)
        else:
            self.db_file = settings.DATA_DIR / "local_color.db"

        self.db_file.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def get_connection(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.db_file), timeout=15.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL;")
        return conn

    def _init_db(self):
        with self.get_connection() as conn:
            conn.execute("""
            CREATE TABLE IF NOT EXISTS jobs (
                job_id TEXT PRIMARY KEY,
                job_type TEXT NOT NULL,
                status TEXT NOT NULL,
                progress INTEGER DEFAULT 0,
                input_params TEXT,
                result_data TEXT,
                error_message TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            """)
            columns = {row["name"] for row in conn.execute("PRAGMA table_info(jobs)")}
            for name, definition in (
                ("worker_id", "TEXT"),
                ("lease_expires_at", "TEXT"),
            ):
                if name not in columns:
                    conn.execute(f"ALTER TABLE jobs ADD COLUMN {name} {definition}")
            conn.commit()

    def create_job(self, job_id: str, job_type: str, input_params: Dict[str, Any]) -> Dict[str, Any]:
        now = datetime.datetime.now(datetime.timezone.utc).isoformat()
        with self.get_connection() as conn:
            conn.execute(
                """
                INSERT INTO jobs (job_id, job_type, status, progress, input_params, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (job_id, job_type, "pending", 0, json.dumps(input_params), now, now)
            )
            conn.commit()
        return self.get_job(job_id)

    def delete_job(self, job_id: str) -> None:
        with self.get_connection() as conn:
            conn.execute("DELETE FROM jobs WHERE job_id = ?", (job_id,))
            conn.commit()

    def update_job_progress(self, job_id: str, progress: int, worker_id: Optional[str] = None) -> None:
        """Advance a running job's progress without touching its status.

        Long renders used to report 5% for their entire duration, which is
        indistinguishable from being stuck.
        """
        now = datetime.datetime.now(datetime.timezone.utc)
        lease = (now + datetime.timedelta(seconds=30)).isoformat()
        with self.get_connection() as conn:
            owner_clause = " AND worker_id = ?" if worker_id else ""
            params: list[Any] = [int(max(0, min(99, progress))), now.isoformat(), lease, job_id, int(max(0, min(99, progress)))]
            if worker_id:
                params.append(worker_id)
            conn.execute(
                "UPDATE jobs SET progress = ?, updated_at = ?, lease_expires_at = ? "
                f"WHERE job_id = ? AND status IN ('processing', 'cancelling') AND progress < ?{owner_clause}",
                params,
            )
            conn.commit()

    def heartbeat_job(self, job_id: str, worker_id: str, lease_seconds: float = 30.0) -> bool:
        """Renew a worker lease without faking progress."""
        now = datetime.datetime.now(datetime.timezone.utc)
        lease = (now + datetime.timedelta(seconds=lease_seconds)).isoformat()
        with self.get_connection() as conn:
            updated = conn.execute(
                "UPDATE jobs SET updated_at=?, lease_expires_at=? "
                "WHERE job_id=? AND status IN ('processing', 'cancelling') AND worker_id=?",
                (now.isoformat(), lease, job_id, worker_id),
            )
            conn.commit()
            return updated.rowcount == 1

    def requeue_owned_job(self, job_id: str, worker_id: str, reason: str) -> bool:
        """Release a live claim during controlled shutdown without stealing another worker's job."""
        now = datetime.datetime.now(datetime.timezone.utc).isoformat()
        with self.get_connection() as conn:
            updated = conn.execute(
                "UPDATE jobs SET status='pending', progress=0, error_message=?, updated_at=?, "
                "worker_id=NULL, lease_expires_at=NULL "
                "WHERE job_id=? AND status='processing' AND worker_id=?",
                (reason, now, job_id, worker_id),
            )
            conn.commit()
            return updated.rowcount == 1

    def cancel_job(self, job_id: str) -> Dict[str, Any]:
        """Cancel a job, killing its subprocesses if it is already running.

        A pending job is settled here and now - no worker will ever pick it up.
        A running job is signalled: its ffmpeg processes are terminated and the
        worker thread records the final status when JobCancelled surfaces.
        """
        from color_core.cancellation import request_cancel

        now = datetime.datetime.now(datetime.timezone.utc).isoformat()
        with self.get_connection() as conn:
            row = conn.execute("SELECT status FROM jobs WHERE job_id = ?", (job_id,)).fetchone()
            if row is None:
                return {"job_id": job_id, "found": False}
            status = row["status"]
            if status in ("completed", "failed", "cancelled"):
                return {"job_id": job_id, "found": True, "status": status, "changed": False}
            if status == "cancelling":
                return {"job_id": job_id, "found": True, "status": status, "changed": False}
            if status == "pending":
                conn.execute(
                    "UPDATE jobs SET status='cancelled', progress=100, error_message=?, updated_at=? "
                    "WHERE job_id=? AND status='pending'",
                    ("Cancelled before it started", now, job_id),
                )
                conn.commit()
                return {"job_id": job_id, "found": True, "status": "cancelled", "changed": True,
                        "was": "pending", "terminated_processes": 0}
            conn.execute(
                "UPDATE jobs SET status='cancelling', error_message=?, updated_at=? "
                "WHERE job_id=? AND status='processing'",
                ("Cancellation requested; waiting for the current operation to stop", now, job_id),
            )
            conn.commit()
        result = request_cancel(job_id)
        return {"job_id": job_id, "found": True, "status": "cancelling", "changed": True,
                "was": "processing", **result}

    def cancel_all_active(self) -> Dict[str, Any]:
        with self.get_connection() as conn:
            rows = conn.execute(
                "SELECT job_id FROM jobs WHERE status IN ('pending', 'processing', 'cancelling')"
            ).fetchall()
        return {"requested": [self.cancel_job(row["job_id"]) for row in rows]}

    def purge_jobs(
        self,
        statuses: Optional[List[str]] = None,
        older_than_days: Optional[float] = None,
    ) -> Dict[str, Any]:
        """Delete finished job records and their directories.

        Active jobs are never purged; cancel them first.
        """
        import shutil

        statuses = statuses or ["completed", "failed", "cancelled"]
        active = {"pending", "processing", "cancelling"}
        if active & set(statuses):
            raise ValueError("Cancel a job before purging it; active jobs cannot be purged")
        cutoff = None
        if older_than_days is not None:
            cutoff = (
                datetime.datetime.now(datetime.timezone.utc)
                - datetime.timedelta(days=float(older_than_days))
            ).isoformat()
        query = f"SELECT job_id FROM jobs WHERE status IN ({','.join('?' * len(statuses))})"
        params: List[Any] = list(statuses)
        if cutoff:
            query += " AND updated_at < ?"
            params.append(cutoff)
        with self.get_connection() as conn:
            rows = [row["job_id"] for row in conn.execute(query, params).fetchall()]
            for job_id in rows:
                conn.execute("DELETE FROM jobs WHERE job_id = ?", (job_id,))
            conn.commit()
        removed_bytes = 0
        for job_id in rows:
            directory = self.db_file.parent / "jobs" / job_id
            if directory.is_dir():
                removed_bytes += sum(
                    item.stat().st_size for item in directory.rglob("*") if item.is_file()
                )
                shutil.rmtree(directory, ignore_errors=True)
        return {"purged": len(rows), "job_ids": rows, "freed_bytes": removed_bytes}

    def update_job_status(
        self,
        job_id: str,
        status: str,
        progress: int = 0,
        result_data: Optional[Dict[str, Any]] = None,
        error_message: Optional[str] = None,
        worker_id: Optional[str] = None,
    ):
        now = datetime.datetime.now(datetime.timezone.utc).isoformat()
        result_str = json.dumps(result_data) if result_data is not None else None

        with self.get_connection() as conn:
            owner_clause = " AND worker_id = ?" if worker_id else ""
            params: list[Any] = [status, progress, result_str, error_message, now, job_id]
            if worker_id:
                params.append(worker_id)
            conn.execute(
                """
                UPDATE jobs
                SET status = ?, progress = ?, result_data = COALESCE(?, result_data),
                    error_message = ?, updated_at = ?, worker_id = NULL, lease_expires_at = NULL
                WHERE job_id = ?
                """ + owner_clause,
                params,
            )
            conn.commit()

    def get_job(self, job_id: str) -> Optional[Dict[str, Any]]:
        with self.get_connection() as conn:
            cur = conn.execute("SELECT * FROM jobs WHERE job_id = ?", (job_id,))
            row = cur.fetchone()
            if row:
                d = dict(row)
                d["input_params"] = json.loads(d["input_params"]) if d["input_params"] else {}
                d["result_data"] = json.loads(d["result_data"]) if d["result_data"] else {}
                return d
            return None

    def list_jobs(self, statuses: Optional[List[str]] = None, limit: int = 100) -> List[Dict[str, Any]]:
        """Return recent jobs for the local task board without their bulky results."""
        limit = max(1, min(int(limit), 500))
        query = (
            "SELECT job_id, job_type, status, progress, input_params, error_message, created_at, updated_at, "
            "worker_id, lease_expires_at FROM jobs"
        )
        params: list[Any] = []
        if statuses:
            query += f" WHERE status IN ({','.join('?' * len(statuses))})"
            params.extend(statuses)
        query += " ORDER BY CASE status WHEN 'processing' THEN 0 WHEN 'pending' THEN 1 ELSE 2 END, updated_at DESC LIMIT ?"
        params.append(limit)
        with self.get_connection() as conn:
            jobs = [dict(row) for row in conn.execute(query, params).fetchall()]
            for job in jobs:
                job["input_params"] = json.loads(job["input_params"]) if job["input_params"] else {}
            return jobs

    def purge_job(self, job_id: str) -> bool:
        """Delete exactly one terminal job and its artifacts; never sweep history."""
        import shutil

        with self.get_connection() as conn:
            row = conn.execute("SELECT status FROM jobs WHERE job_id=?", (job_id,)).fetchone()
            if row is None:
                return False
            if row["status"] in ("pending", "processing"):
                raise ValueError("Cancel an active job before deleting it")
            conn.execute("DELETE FROM jobs WHERE job_id=?", (job_id,))
            conn.commit()
        shutil.rmtree(self.db_file.parent / "jobs" / job_id, ignore_errors=True)
        return True

    def fetch_next_pending_job(
        self,
        include_types: Optional[List[str]] = None,
        exclude_types: Optional[List[str]] = None,
        worker_id: Optional[str] = None,
        lease_seconds: float = 30.0,
    ) -> Optional[Dict[str, Any]]:
        """Atomically claim one pending job so multiple workers cannot duplicate it.

        The type filters exist so a short interactive job (a reference preflight
        takes about a second) is not stuck behind a fifteen-minute render on a
        single queue. See workers/job_worker.py for the lane definitions.
        """
        now = datetime.datetime.now(datetime.timezone.utc)
        worker_id = worker_id or uuid.uuid4().hex
        lease = (now + datetime.timedelta(seconds=lease_seconds)).isoformat()
        clauses = ["status = 'pending'"]
        params: list[Any] = []
        if include_types:
            clauses.append(f"job_type IN ({','.join('?' * len(include_types))})")
            params.extend(include_types)
        if exclude_types:
            clauses.append(f"job_type NOT IN ({','.join('?' * len(exclude_types))})")
            params.extend(exclude_types)
        query = f"SELECT * FROM jobs WHERE {' AND '.join(clauses)} ORDER BY created_at ASC LIMIT 1"
        conn = self.get_connection()
        try:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(query, params).fetchone()
            if row is None:
                conn.commit()
                return None
            updated = conn.execute(
                "UPDATE jobs SET status='processing', progress=5, error_message=NULL, updated_at=?, "
                "worker_id=?, lease_expires_at=? WHERE job_id=? AND status='pending'",
                (now.isoformat(), worker_id, lease, row["job_id"]),
            )
            if updated.rowcount != 1:
                conn.rollback()
                return None
            conn.commit()
            data = dict(row)
            data["status"] = "processing"
            data["progress"] = 5
            data["worker_id"] = worker_id
            data["lease_expires_at"] = lease
            data["input_params"] = json.loads(data["input_params"]) if data["input_params"] else {}
            data["result_data"] = json.loads(data["result_data"]) if data["result_data"] else {}
            return data
        finally:
            conn.close()

    def queue_snapshot(self, job_id: str) -> Dict[str, Any]:
        """Where a pending job sits in its lane, so waiting never looks like failure.

        A queued job previously reported only 'pending', which is
        indistinguishable from a job that silently did nothing.
        """
        from workers.job_worker import lane_for_job_type

        with self.get_connection() as conn:
            row = conn.execute(
                "SELECT job_type, status, created_at FROM jobs WHERE job_id = ?", (job_id,)
            ).fetchone()
            if row is None:
                return {}
            lane = lane_for_job_type(row["job_type"])
            if row["status"] != "pending":
                running = conn.execute(
                    "SELECT job_id, job_type FROM jobs WHERE status = 'processing' AND job_id != ?",
                    (job_id,),
                ).fetchall()
                return {
                    "lane": lane,
                    "queue_position": 0,
                    "concurrent_jobs": [dict(item) for item in running],
                }
            ahead = conn.execute(
                "SELECT job_id, job_type, status FROM jobs "
                "WHERE status IN ('pending', 'processing') AND created_at < ?",
                (row["created_at"],),
            ).fetchall()
            same_lane = [item for item in ahead if lane_for_job_type(item["job_type"]) == lane]
            blocking = next(
                (dict(item) for item in same_lane if item["status"] == "processing"), None
            )
            return {
                "lane": lane,
                "queue_position": len(same_lane) + 1,
                "blocked_by": blocking,
            }

    def recover_interrupted_jobs(self) -> Dict[str, int]:
        """Requeue only expired worker leases; never steal live work."""
        recovered = failed = 0
        now = datetime.datetime.now(datetime.timezone.utc).isoformat()
        with self.get_connection() as conn:
            rows = conn.execute(
                "SELECT job_id FROM jobs WHERE status='processing' "
                "AND (lease_expires_at IS NULL OR lease_expires_at < ?)", (now,)
            ).fetchall()
            for row in rows:
                directory = self.db_file.parent / "jobs" / row["job_id"]
                if (directory / "request.json").is_file():
                    conn.execute(
                        "UPDATE jobs SET status='pending', progress=0, error_message=?, updated_at=?, "
                        "worker_id=NULL, lease_expires_at=NULL WHERE job_id=?",
                        ("Recovered after an interrupted worker process", now, row["job_id"]),
                    )
                    recovered += 1
                else:
                    conn.execute(
                        "UPDATE jobs SET status='failed', progress=100, error_message=?, updated_at=?, worker_id=NULL, lease_expires_at=NULL WHERE job_id=?",
                        ("Interrupted job cannot be recovered because its job directory or request.json is missing", now, row["job_id"]),
                    )
                    failed += 1
            # A cancellation request survives a process crash; never revive it
            # as a render on the following service startup.
            cancelled = conn.execute(
                "UPDATE jobs SET status='cancelled', progress=100, updated_at=?, worker_id=NULL, lease_expires_at=NULL "
                "WHERE status='cancelling'",
                (now,),
            ).rowcount
            conn.commit()
        return {"requeued": recovered, "failed": failed, "cancelled": cancelled}


db_manager = DatabaseManager()
