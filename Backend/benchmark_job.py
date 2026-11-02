from __future__ import annotations

import argparse
import json
import logging
import sys
import tempfile
import threading
import uuid
from pathlib import Path


def _current_working_set_bytes() -> int:
    import psutil
    return int(psutil.Process().memory_info().rss)


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the existing sequential job pipeline and save a benchmark result.")
    parser.add_argument("archive", type=Path)
    parser.add_argument("--output", type=Path, default=Path("benchmark_results/customer_sample_upload_baseline.json"))
    args = parser.parse_args()
    archive = args.archive.resolve()
    if not archive.is_file():
        raise FileNotFoundError(archive)

    backend_root = Path(__file__).resolve().parent
    sys.path.insert(0, str(backend_root))
    from app.services.job_manager import JobManager, _utc_now

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    performance_records: list[dict] = []

    class _PerformanceHandler(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            message = record.getMessage()
            prefix = "JOB_PERFORMANCE "
            if prefix in message:
                performance_records.append(json.loads(message.split(prefix, 1)[1]))

    performance_handler = _PerformanceHandler()
    logging.getLogger("app.services.job_manager").addHandler(performance_handler)
    with tempfile.TemporaryDirectory(prefix="log-benchmark-") as temp_dir:
        manager = JobManager(db_path=Path(temp_dir) / "jobs.db")
        job_id = uuid.uuid4().hex
        now = _utc_now()
        with manager._db_lock:
            manager.conn.execute(
                "INSERT INTO jobs (job_id, upload_id, status, created_at, started_at) VALUES (?, ?, 'UPLOADED', ?, ?)",
                (job_id, archive.stem, now, now),
            )
            manager.conn.commit()

        peak_bytes = _current_working_set_bytes()
        stop_sampling = threading.Event()

        def sample_memory() -> None:
            nonlocal peak_bytes
            while not stop_sampling.wait(0.05):
                try:
                    peak_bytes = max(peak_bytes, _current_working_set_bytes())
                except OSError:
                    pass

        sampler = threading.Thread(target=sample_memory, name="benchmark-memory-sampler", daemon=True)
        sampler.start()
        try:
            manager._run_job(job_id, str(archive))
            job = manager.get_job(job_id)
        finally:
            stop_sampling.set()
            sampler.join()
            manager._executor.shutdown(wait=True)
            manager.conn.close()


    performance = performance_records[-1] if performance_records else {}
    result = {
        "archive": str(archive),
        "processing_mode": "sequential files within one job; existing job-level concurrency retained",
        "job_id": job_id,
        "status": job["status"],
        "total_duration_seconds": performance.get("total_job_seconds"),
        "stage_durations_seconds": {
            key: performance[key]
            for key in (
                "discovery_seconds", "extraction_seconds", "parsing_seconds",
                "fingerprinting_aggregation_seconds", "sqlite_write_seconds", "sqlite_commit_seconds",
            )
            if key in performance
        },
        "peak_process_ram_bytes": peak_bytes,
        "peak_process_ram_mib": round(peak_bytes / (1024 * 1024), 2),
        "files_processed": job["processed_files"],
        "records_parsed": job["parsed_records"],
        "error_occurrences": job["error_records"],
        "unique_clusters": job["distinct_fingerprints"],
        "records_per_second": performance.get("records_per_second"),
        "measurement_notes": [
            "Peak RSS is sampled every 50 ms and may miss shorter peaks.",
            "Discovery duration includes extraction; extraction duration is a subset of discovery.",
            "Fingerprint aggregation and SQLite timings overlap because aggregation writes to SQLite.",
        ],
        "failure_reason": job["failure_reason"],
        "timing_detail": performance,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(json.dumps(result, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
