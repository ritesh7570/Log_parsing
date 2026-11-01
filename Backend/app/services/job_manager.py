from __future__ import annotations

import io
import json
import logging
import sqlite3
import tempfile
import uuid
import zipfile
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from threading import Lock

from app.services.zip_discovery import SUPPORTED_COMPONENTS, detect_component_name, discover_zip_archive, is_log_candidate
from fingerprint import SQLiteClusterStore, fingerprint_id, make_fingerprint
from parse_logs import iter_log_records

logger = logging.getLogger(__name__)

UPLOAD_ROOT = Path(__file__).resolve().parents[2] / "storage" / "uploads"
JOB_DB_PATH = Path(__file__).resolve().parents[2] / "storage" / "jobs.db"
JOB_DB_PATH.parent.mkdir(parents=True, exist_ok=True)


def _utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


class JobManager:
    _instance: "JobManager | None" = None
    _instance_lock = Lock()

    def __init__(self, db_path: str | Path | None = None):
        self.db_path = Path(db_path or JOB_DB_PATH)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(self.db_path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self._db_lock = Lock()
        self._ensure_schema()
        self._executor = ThreadPoolExecutor(max_workers=4, thread_name_prefix="log-job-")

    @classmethod
    def get_instance(cls) -> "JobManager":
        with cls._instance_lock:
            if cls._instance is None:
                cls._instance = cls()
            return cls._instance

    def _ensure_schema(self) -> None:
        self.conn.execute(
            """
            CREATE TABLE IF NOT EXISTS jobs (
                job_id TEXT PRIMARY KEY,
                upload_id TEXT NOT NULL,
                status TEXT NOT NULL,
                current_component TEXT,
                current_file TEXT,
                processed_files INTEGER DEFAULT 0,
                total_files INTEGER DEFAULT 0,
                parsed_records INTEGER DEFAULT 0,
                error_records INTEGER DEFAULT 0,
                malformed_lines INTEGER DEFAULT 0,
                distinct_fingerprints INTEGER DEFAULT 0,
                started_at TEXT,
                completed_at TEXT,
                failure_reason TEXT,
                cluster_summary TEXT,
                created_at TEXT NOT NULL
            )
            """
        )
        job_columns = self.conn.execute("PRAGMA table_info(jobs)").fetchall()
        if not any(row[1] == "warnings" for row in job_columns):
            self.conn.execute("ALTER TABLE jobs ADD COLUMN warnings TEXT")

        self.conn.execute(
            """
            CREATE TABLE IF NOT EXISTS job_occurrences (
                job_id TEXT NOT NULL,
                fingerprint_id TEXT NOT NULL,
                component TEXT,
                source_file TEXT,
                source_line INTEGER,
                timestamp TEXT,
                thread_id TEXT,
                message TEXT,
                raw_message TEXT,
                PRIMARY KEY (job_id, fingerprint_id, source_file, source_line, timestamp)
            )
            """
        )
        self.conn.commit()

    def _get_job_locked(self, job_id: str) -> dict:
        row = self.conn.execute("SELECT * FROM jobs WHERE job_id = ?", (job_id,)).fetchone()
        if row is None:
            raise KeyError(f"Job not found: {job_id}")
        payload = dict(row)
        payload["cluster_summary"] = json.loads(payload.get("cluster_summary") or "[]")
        return payload

    def _update_job_locked(self, job_id: str, **fields) -> dict:
        if not fields:
            return self._get_job_locked(job_id)
        assignments = ", ".join(f"{key} = ?" for key in fields)
        values = list(fields.values())
        values.append(job_id)
        self.conn.execute(f"UPDATE jobs SET {assignments} WHERE job_id = ?", values)
        self.conn.commit()
        return self._get_job_locked(job_id)

    def create_job(self, upload_id: str) -> dict:
        upload_path = self._resolve_upload_path(upload_id)
        if not upload_path.exists():
            raise FileNotFoundError(f"Upload not found: {upload_path}")

        job_id = uuid.uuid4().hex
        now = _utc_now()
        with self._db_lock:
            self.conn.execute(
                """
                INSERT INTO jobs (
                    job_id, upload_id, status, processed_files, total_files,
                    parsed_records, error_records, malformed_lines, distinct_fingerprints,
                    started_at, completed_at, failure_reason, warnings, cluster_summary, created_at
                ) VALUES (?, ?, 'UPLOADED', 0, 0, 0, 0, 0, 0, ?, NULL, NULL, NULL, NULL, ?)
                """,
                (job_id, upload_id, now, now),
            )
            self.conn.commit()
            self._executor.submit(self._run_job, job_id, str(upload_path))
            self._update_job_locked(job_id, status="QUEUED")
            return self._get_job_locked(job_id)

    def update_job(self, job_id: str, **fields) -> dict:
        with self._db_lock:
            return self._update_job_locked(job_id, **fields)

    def get_job(self, job_id: str) -> dict:
        with self._db_lock:
            return self._get_job_locked(job_id)

    @staticmethod
    def _resolve_upload_path(upload_id: str) -> Path:
        for suffix in (".zip", ".7z"):
            candidate = UPLOAD_ROOT / f"{upload_id}{suffix}"
            if candidate.exists():
                return candidate
        return UPLOAD_ROOT / f"{upload_id}.zip"

    def cancel_job(self, job_id: str) -> dict:
        with self._db_lock:
            row = self.conn.execute("SELECT status FROM jobs WHERE job_id = ?", (job_id,)).fetchone()
            if row is None:
                raise KeyError(f"Job not found: {job_id}")
            if row["status"] in {"COMPLETED", "FAILED", "CANCELLED", "NO_SUPPORTED_FILES", "COMPLETED_WITH_WARNINGS"}:
                return self._get_job_locked(job_id)
            self._update_job_locked(job_id, status="CANCELLED", completed_at=_utc_now())
            return self._get_job_locked(job_id)

    def _run_job(self, job_id: str, upload_path: str) -> None:
        logger.info("JOB_STARTED job_id=%s", job_id)
        try:
            with tempfile.TemporaryDirectory(prefix="log-job-") as working_dir:
                discovery = discover_zip_archive(upload_path, temp_root=working_dir)
                warnings = discovery.get("warnings") or []
                total_files = len(discovery.get("log_files", []))
                if total_files == 0:
                    self.update_job(
                        job_id,
                        status="NO_SUPPORTED_FILES",
                        total_files=0,
                        warnings=json.dumps(warnings, ensure_ascii=False),
                        current_component=None,
                        current_file=None,
                        completed_at=_utc_now(),
                    )
                    return

                self.update_job(
                    job_id,
                    status="DISCOVERED",
                    total_files=total_files,
                    warnings=json.dumps(warnings, ensure_ascii=False),
                    current_component=None,
                    current_file=None,
                )
                self.update_job(job_id, status="PROCESSING")

                cluster_store = SQLiteClusterStore()
                processed_files = 0
                parsed_records = 0
                error_records = 0
                malformed_lines = 0

                for entry in discovery.get("log_files", []):
                    source_path = entry.get("source_path")
                    if not source_path:
                        continue
                    file_path = Path(source_path)
                    if not file_path.exists():
                        continue
                    component = entry.get("component")
                    if component not in SUPPORTED_COMPONENTS:
                        continue
                    self.update_job(job_id, current_component=component, current_file=entry.get("path"))
                    try:
                        with file_path.open("r", encoding="utf-8-sig", errors="replace") as text_stream:
                            malformed: list[dict[str, object]] = []
                            for record in iter_log_records(text_stream, on_malformed=malformed.append):
                                parsed_records += 1
                                if record.level != "ERROR":
                                    continue
                                error_records += 1
                                cluster_store.add_occurrence(
                                    {
                                        "level": record.level,
                                        "qualified_function": record.qualified_function,
                                        "message": record.message,
                                        "function_name": record.function_name,
                                        "class_scope": record.class_scope,
                                        "timestamp_iso": record.timestamp_iso,
                                        "timestamp": record.timestamp,
                                        "thread_id": record.thread_id,
                                        "source_file": entry.get("path"),
                                        "source_line": record.source_line,
                                        "raw_message": record.raw_line,
                                    },
                                    component=component,
                                )
                                fingerprint = fingerprint_id(make_fingerprint({
                                    "qualified_function": record.qualified_function,
                                    "message": record.message,
                                }))
                                with self._db_lock:
                                    self.conn.execute(
                                        """
                                        INSERT OR IGNORE INTO job_occurrences (
                                            job_id, fingerprint_id, component, source_file, source_line, timestamp, thread_id, message, raw_message
                                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                                        """,
                                        (
                                            job_id,
                                            fingerprint,
                                            component,
                                            entry.get("path"),
                                            record.source_line,
                                            record.timestamp_iso,
                                            record.thread_id,
                                            record.message,
                                            record.raw_line,
                                        ),
                                    )
                                    self.conn.commit()
                            malformed_lines += len(malformed)
                    except Exception as exc:  # pragma: no cover - safety branch
                        self.update_job(job_id, status="FAILED", failure_reason=f"Corrupt log entry: {entry.get('path')} ({exc})")
                        raise
                    processed_files += 1
                    self.update_job(
                        job_id,
                        processed_files=processed_files,
                        parsed_records=parsed_records,
                        error_records=error_records,
                        malformed_lines=malformed_lines,
                    )
                    if processed_files % 25 == 0:
                        logger.info(
                            "JOB_PROGRESS job_id=%s processed=%s total=%s records=%s errors=%s",
                            job_id,
                            processed_files,
                            total_files,
                            parsed_records,
                            error_records,
                        )

                clusters = cluster_store.export_clusters()
                status = "COMPLETED_WITH_WARNINGS" if warnings else "COMPLETED"
                self.update_job(
                    job_id,
                    cluster_summary=json.dumps(clusters, ensure_ascii=False),
                    distinct_fingerprints=len(clusters),
                    status=status,
                    completed_at=_utc_now(),
                    current_file=None,
                    current_component=None,
                    warnings=json.dumps(warnings, ensure_ascii=False),
                )
                logger.info(
                    "JOB_COMPLETED job_id=%s processed=%s records=%s errors=%s fingerprints=%s",
                    job_id,
                    processed_files,
                    parsed_records,
                    error_records,
                    len(clusters),
                )
                cluster_store.close()
        except Exception as exc:  # pragma: no cover - limited local runtime safety
            logger.exception("JOB_FAILED job_id=%s", job_id)
            self.update_job(job_id, status="FAILED", failure_reason=str(exc), completed_at=_utc_now())

    def get_clusters(self, job_id: str, page: int = 1, page_size: int = 20) -> dict:
        with self._db_lock:
            row = self.conn.execute("SELECT cluster_summary FROM jobs WHERE job_id = ?", (job_id,)).fetchone()
            if row is None:
                raise KeyError(f"Job not found: {job_id}")
            clusters = json.loads(row[0] or "[]")
            start = (page - 1) * page_size
            end = start + page_size
            return {
                "job_id": job_id,
                "page": page,
                "page_size": page_size,
                "total": len(clusters),
                "items": clusters[start:end],
            }

    def get_occurrences(self, job_id: str, fingerprint_id_value: str, page: int = 1, page_size: int = 20) -> dict:
        with self._db_lock:
            total = self.conn.execute(
                "SELECT COUNT(*) FROM job_occurrences WHERE job_id = ? AND fingerprint_id = ?",
                (job_id, fingerprint_id_value),
            ).fetchone()[0]
            rows = self.conn.execute(
                """
                SELECT component, source_file, source_line, timestamp, thread_id, message, raw_message
                FROM job_occurrences
                WHERE job_id = ? AND fingerprint_id = ?
                ORDER BY source_file ASC, source_line ASC
                LIMIT ? OFFSET ?
                """,
                (job_id, fingerprint_id_value, page_size, (page - 1) * page_size),
            ).fetchall()
            return {
                "job_id": job_id,
                "fingerprint_id": fingerprint_id_value,
                "page": page,
                "page_size": page_size,
                "total": total,
                "items": [dict(row) for row in rows],
            }


job_manager = JobManager.get_instance()
