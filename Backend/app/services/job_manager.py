from __future__ import annotations

import io
import json
import logging
import sqlite3
import tempfile
import time
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

    def _update_job_timed(self, job_id: str, timing: dict[str, float], **fields) -> dict:
        assignments = ", ".join(f"{key} = ?" for key in fields)
        values = list(fields.values()) + [job_id]
        with self._db_lock:
            write_started = time.perf_counter()
            self.conn.execute(f"UPDATE jobs SET {assignments} WHERE job_id = ?", values)
            timing["sqlite_write_seconds"] = timing.get("sqlite_write_seconds", 0.0) + time.perf_counter() - write_started
            commit_started = time.perf_counter()
            self.conn.commit()
            timing["sqlite_commit_seconds"] = timing.get("sqlite_commit_seconds", 0.0) + time.perf_counter() - commit_started
            return self._get_job_locked(job_id)

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
        job_started = time.perf_counter()
        discovery_seconds = 0.0
        extraction_seconds = 0.0
        parsing_seconds = 0.0
        fingerprinting_seconds = 0.0
        sqlite_write_seconds = 0.0
        sqlite_commit_seconds = 0.0
        occurrence_write_seconds = 0.0
        occurrence_commit_seconds = 0.0
        job_db_timing: dict[str, float] = {"sqlite_write_seconds": 0.0, "sqlite_commit_seconds": 0.0}
        processed_files = parsed_records = error_records = 0
        distinct_clusters = 0
        try:
            with tempfile.TemporaryDirectory(prefix="log-job-") as working_dir:
                discovery_metrics: dict[str, float] = {}
                stage_started = time.perf_counter()
                discovery = discover_zip_archive(upload_path, temp_root=working_dir, metrics=discovery_metrics)
                discovery_seconds = time.perf_counter() - stage_started
                extraction_seconds = discovery_metrics.get("extraction_seconds", 0.0)
                warnings = discovery.get("warnings") or []
                total_files = len(discovery.get("log_files", []))
                if total_files == 0:
                    self._update_job_timed(
                        job_id,
                        job_db_timing,
                        status="NO_SUPPORTED_FILES",
                        total_files=0,
                        warnings=json.dumps(warnings, ensure_ascii=False),
                        current_component=None,
                        current_file=None,
                        completed_at=_utc_now(),
                    )
                    return

                self._update_job_timed(
                    job_id,
                    job_db_timing,
                    status="DISCOVERED",
                    total_files=total_files,
                    warnings=json.dumps(warnings, ensure_ascii=False),
                    current_component=None,
                    current_file=None,
                )
                self._update_job_timed(job_id, job_db_timing, status="PROCESSING")

                cluster_store = SQLiteClusterStore()
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
                    self._update_job_timed(job_id, job_db_timing, current_component=component, current_file=entry.get("path"))
                    try:
                        with file_path.open("r", encoding="utf-8-sig", errors="replace") as text_stream:
                            malformed: list[dict[str, object]] = []
                            records = iter_log_records(text_stream, on_malformed=malformed.append)
                            while True:
                                parse_started = time.perf_counter()
                                try:
                                    record = next(records)
                                except StopIteration:
                                    parsing_seconds += time.perf_counter() - parse_started
                                    break
                                parsing_seconds += time.perf_counter() - parse_started
                                parsed_records += 1
                                if record.level != "ERROR":
                                    continue
                                error_records += 1
                                fingerprint_started = time.perf_counter()
                                sqlite_before = cluster_store.write_seconds
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
                                sqlite_write_seconds += cluster_store.write_seconds - sqlite_before
                                fingerprint = fingerprint_id(make_fingerprint({
                                    "qualified_function": record.qualified_function,
                                    "message": record.message,
                                }))
                                fingerprinting_seconds += time.perf_counter() - fingerprint_started
                                with self._db_lock:
                                    execute_started = time.perf_counter()
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
                                    occurrence_write_seconds += time.perf_counter() - execute_started
                                    commit_started = time.perf_counter()
                                    self.conn.commit()
                                    occurrence_commit_seconds += time.perf_counter() - commit_started
                            malformed_lines += len(malformed)
                    except Exception as exc:  # pragma: no cover - safety branch
                        self._update_job_timed(job_id, job_db_timing, status="FAILED", failure_reason=f"Corrupt log entry: {entry.get('path')} ({exc})")
                        raise
                    processed_files += 1
                    self._update_job_timed(
                        job_id,
                        job_db_timing,
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
                distinct_clusters = len(clusters)
                sqlite_commit_seconds += cluster_store.commit_seconds
                status = "COMPLETED_WITH_WARNINGS" if warnings else "COMPLETED"
                self._update_job_timed(
                    job_id,
                    job_db_timing,
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
                sqlite_commit_seconds = cluster_store.commit_seconds + occurrence_commit_seconds + job_db_timing["sqlite_commit_seconds"]
                sqlite_write_seconds += occurrence_write_seconds + job_db_timing["sqlite_write_seconds"]
                total_seconds = time.perf_counter() - job_started
                logger.info(
                    "JOB_PERFORMANCE %s",
                    json.dumps({
                        "job_id": job_id,
                        "discovery_seconds": round(discovery_seconds, 6),
                        "extraction_seconds": round(extraction_seconds, 6),
                        "parsing_seconds": round(parsing_seconds, 6),
                        "fingerprinting_aggregation_seconds": round(fingerprinting_seconds, 6),
                        "sqlite_write_seconds": round(sqlite_write_seconds, 6),
                        "sqlite_commit_seconds": round(sqlite_commit_seconds, 6),
                        "jobs_db_occurrence_write_seconds": round(occurrence_write_seconds, 6),
                        "jobs_db_occurrence_commit_seconds": round(occurrence_commit_seconds, 6),
                        "jobs_db_status_write_seconds": round(job_db_timing["sqlite_write_seconds"], 6),
                        "jobs_db_status_commit_seconds": round(job_db_timing["sqlite_commit_seconds"], 6),
                        "total_job_seconds": round(total_seconds, 6),
                        "files_processed": processed_files,
                        "records_parsed": parsed_records,
                        "error_occurrences": error_records,
                        "unique_clusters": distinct_clusters,
                        "records_per_second": round(parsed_records / total_seconds, 2) if total_seconds else 0,
                    }, ensure_ascii=False, sort_keys=True),
                )
        except Exception as exc:  # pragma: no cover - limited local runtime safety
            logger.exception("JOB_FAILED job_id=%s", job_id)
            self._update_job_timed(job_id, job_db_timing, status="FAILED", failure_reason=str(exc), completed_at=_utc_now())

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
