from __future__ import annotations

import io
import json
import time
import zipfile
from pathlib import Path

import py7zr
from fastapi.testclient import TestClient

from app.main import app
from fingerprint import cluster_errors, normalize_message
from parse_logs import filter_errors, parse_log_file


def _make_log_file(path: Path, lines: list[str]) -> None:
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def test_valid_log_lines_and_error_filtering(tmp_path: Path) -> None:
    log_path = tmp_path / "sample.log"
    _make_log_file(
        log_path,
        [
            "28-03-2026_10:00:00.000 | 0.123 | 1 | INFO | Car::Move - started",
            "28-03-2026_10:00:00.100 | 0.123 | 1 | ERROR | Car::SetSpeed - Speed is 120",
            "28-03-2026_10:00:00.200 | 0.123 | 1 | WARN | Car::SetSpeed - Speed is 220",
            "28-03-2026_10:00:00.300 | 0.123 | 1 | ERROR | Car::SetSpeed - Speed is 130",
        ],
    )

    metadata, records, report = parse_log_file(log_path)
    assert metadata["source_file"] == "sample.log"
    assert len(records) == 4
    assert report["malformed_line_count"] == 0
    assert len(filter_errors(records)) == 2


def test_component_and_thread_id_formats_are_parsed(tmp_path: Path) -> None:
    log_path = tmp_path / "dwrapper.log"
    _make_log_file(
        log_path,
        [
            "[2026-03-16 12:56:58.369] [DWrapper] [info] || ThreadID: 37808 || Car::SetSpeed - Speed is 120",
            "[2026-03-16 12:57:00.087] [DServer] [error] || ThreadID: 37808 || Source=Primary || Car::SetSpeed - Speed is 120",
        ],
    )

    _, records, report = parse_log_file(log_path)
    assert len(records) == 2
    assert report["malformed_line_count"] == 0
    assert records[0].component == "DWrapper"
    assert records[0].thread_id == "37808"
    assert records[0].level == "INFO"
    assert records[0].qualified_function == "Car::SetSpeed"
    assert records[0].message == "Speed is 120"
    assert records[1].component == "DServer"
    assert records[1].raw_message == "Source=Primary || Car::SetSpeed - Speed is 120"
    assert records[1].message == "Speed is 120"
    assert len(filter_errors(records)) == 1


def test_lambda_function_and_malformed_line(tmp_path: Path) -> None:
    log_path = tmp_path / "lambda.log"
    _make_log_file(
        log_path,
        [
            "28-03-2026_10:00:00.000 | 0.123 | 1 | ERROR | MyClass::operator() - removed 1 item",
            "BROKEN LINE",
        ],
    )

    _, records, report = parse_log_file(log_path)
    assert len(records) == 1
    assert records[0].qualified_function == "MyClass::operator()"
    assert report["malformed_line_count"] == 1


def test_fingerprint_normalization_and_global_aggregation() -> None:
    records = [
        {
            "level": "ERROR",
            "qualified_function": "Car::SetSpeed",
            "function_name": "SetSpeed",
            "class_scope": "Car",
            "message": "Speed is 120",
            "thread_id": "1",
            "timestamp_iso": "2026-03-28T10:00:00.000+00:00",
            "source_file": "driver.log",
            "source_line": 10,
            "raw_message": "Speed is 120",
            "component": "DriverWrapper",
        },
        {
            "level": "ERROR",
            "qualified_function": "Car::SetSpeed",
            "function_name": "SetSpeed",
            "class_scope": "Car",
            "message": "Speed is 150",
            "thread_id": "2",
            "timestamp_iso": "2026-03-28T10:00:10.000+00:00",
            "source_file": "otx.log",
            "source_line": 20,
            "raw_message": "Speed is 150",
            "component": "OTX",
        },
        {
            "level": "ERROR",
            "qualified_function": "Car::Park",
            "function_name": "Park",
            "class_scope": "Car",
            "message": "Vehicle is 2",
            "thread_id": "3",
            "timestamp_iso": "2026-03-28T10:00:20.000+00:00",
            "source_file": "dserver.log",
            "source_line": 30,
            "raw_message": "Vehicle is 2",
            "component": "DServer",
        },
    ]

    clusters = cluster_errors(records)
    assert len(clusters) == 2
    assert clusters[0]["occurrence_count"] == 2
    assert clusters[0]["components"] == ["DriverWrapper", "OTX"]
    assert normalize_message("Speed is 120") == "Speed is <N>"


def test_zip_discovery_detects_components(tmp_path: Path) -> None:
    root = tmp_path / "bundle"
    nested = root / "CommonRuntime" / "DServer"
    nested.mkdir(parents=True)
    (root / "CommonRuntime" / "OTXCoreRunTime").mkdir(parents=True)
    (nested / "one.txt").write_text("hello", encoding="utf-8")
    (root / "CommonRuntime" / "OTXCoreRunTime" / "two.log").write_text("hello", encoding="utf-8")

    zip_path = tmp_path / "bundle.zip"
    with zipfile.ZipFile(zip_path, "w") as archive:
        archive.write(nested / "one.txt", arcname="CommonRuntime/DServer/one.txt")
        archive.write(root / "CommonRuntime" / "OTXCoreRunTime" / "two.log", arcname="CommonRuntime/OTXCoreRunTime/two.log")

    from app.services.zip_discovery import discover_zip_archive

    discovery = discover_zip_archive(zip_path)
    names = {component["name"] for component in discovery["components"]}
    assert {"DServer", "OTX"}.issubset(names)
    assert discovery["total_log_files"] >= 1


def test_excluded_components_are_not_processed(tmp_path: Path) -> None:
    zip_path = tmp_path / "mixed.zip"
    supported = "CommonRuntime/DServer/driver.log"
    unsupported = "CONXApp/CONX_2024.txt"
    with zipfile.ZipFile(zip_path, "w") as archive:
        archive.writestr(supported, "[2026-03-16 12:56:58.369] [DServer] [error] || ThreadID: 37808 || Car::SetSpeed - Speed is 120\n")
        archive.writestr(unsupported, "[2026-03-16 12:57:00.087] [CONXApp] [error] || ThreadID: 999 || Some::Ignored - never parsed\n")

    discovery = __import__("app.services.zip_discovery", fromlist=["discover_zip_archive"]).discover_zip_archive(zip_path)
    assert any(item["path"] == unsupported for item in discovery["files"])
    assert all(item["path"] != unsupported for item in discovery["log_files"])

    clusters = cluster_errors([
        {
            "level": "ERROR",
            "qualified_function": "Car::SetSpeed",
            "function_name": "SetSpeed",
            "class_scope": "Car",
            "message": "Speed is 120",
            "thread_id": "37808",
            "timestamp_iso": "2026-03-16T12:56:58.369+00:00",
            "source_file": supported,
            "source_line": 1,
            "raw_message": "Speed is 120",
            "component": "DServer",
        }
    ])
    assert len(clusters) == 1
    assert clusters[0]["components"] == ["DServer"]


def test_zip_containing_nested_7z_is_discovered(tmp_path: Path) -> None:
    inner_dir = tmp_path / "inner_logs"
    inner_dir.mkdir()
    log_path = inner_dir / "driver.log"
    log_path.write_text("[2026-03-16 12:56:58.369] [DWrapper] [error] || ThreadID: 37808 || Car::SetSpeed - Speed is 120\n", encoding="utf-8")

    inner_7z = tmp_path / "bundle.7z"
    with py7zr.SevenZipFile(inner_7z, mode="w") as archive:
        archive.write(log_path, arcname="CommonRuntime/DWrapper/driver.log")

    outer_zip = tmp_path / "outer.zip"
    with zipfile.ZipFile(outer_zip, "w") as archive:
        archive.write(inner_7z, arcname="bundle.7z")

    discovery = __import__("app.services.zip_discovery", fromlist=["discover_zip_archive"]).discover_zip_archive(outer_zip)
    assert discovery["total_log_files"] >= 1
    assert any(item["component"] == "DriverWrapper" for item in discovery["log_files"])


def test_counter_semantics_ignore_directory_entries(tmp_path: Path) -> None:
    inner_dir = tmp_path / "inner_logs"
    inner_dir.mkdir()
    dwrapper = inner_dir / "dw.log"
    dserver = inner_dir / "ds.log"
    otx = inner_dir / "otx.log"
    excluded = inner_dir / "excluded.log"
    dwrapper.write_text("[2026-03-16 12:56:58.369] [DWrapper] [error] || ThreadID: 1 || Car::SetSpeed - bad\n", encoding="utf-8")
    dserver.write_text("[2026-03-16 12:56:58.370] [DServer] [error] || ThreadID: 2 || Car::SetSpeed - bad\n", encoding="utf-8")
    otx.write_text("[2026-03-16 12:56:58.371] [OTXCoreRunTime] [error] || ThreadID: 3 || OTX::Example - bad\n", encoding="utf-8")
    excluded.write_text("[2026-03-16 12:56:58.372] [CONXApp] [error] || ThreadID: 4 || Excluded::Ignored - bad\n", encoding="utf-8")

    inner_7z = tmp_path / "bundle.7z"
    with py7zr.SevenZipFile(inner_7z, mode="w") as archive:
        archive.write(dwrapper, arcname="CommonRuntime/DWrapper/dw.log")
        archive.write(dserver, arcname="CommonRuntime/DServer/ds.log")
        archive.write(otx, arcname="CommonRuntime/OTXCoreRunTime/otx.log")
        archive.write(excluded, arcname="CONXApp/excluded.log")

    outer_zip = tmp_path / "outer.zip"
    with zipfile.ZipFile(outer_zip, "w") as archive:
        archive.write(inner_7z, arcname="bundle.7z")

    discovery = __import__("app.services.zip_discovery", fromlist=["discover_zip_archive"]).discover_zip_archive(outer_zip)
    assert discovery["total_log_files"] == 3
    component_counts = {entry["name"]: entry["file_count"] for entry in discovery["components"]}
    assert component_counts["DriverWrapper"] == 1
    assert component_counts["DServer"] == 1
    assert component_counts["OTX"] == 1
    assert component_counts["CONXApp"] == 1
    assert discovery["excluded_files"] == 1
    assert discovery["unsupported_files"] == 0


def test_corrupt_7z_reports_warning(tmp_path: Path) -> None:
    bad_7z = tmp_path / "bad.7z"
    bad_7z.write_bytes(b"not a real 7z archive")
    discovery = __import__("app.services.zip_discovery", fromlist=["discover_zip_archive"]).discover_zip_archive(bad_7z)
    assert discovery["total_log_files"] == 0
    assert any("Archive read failure" in warning for warning in discovery["warnings"])


def test_matching_dwrapper_and_dserver_errors_share_cluster() -> None:
    records = [
        {
            "level": "ERROR",
            "qualified_function": "Car::SetSpeed",
            "function_name": "SetSpeed",
            "class_scope": "Car",
            "message": "Speed is 120",
            "thread_id": "1",
            "timestamp_iso": "2026-03-16T12:56:58.369+00:00",
            "source_file": "driver.log",
            "source_line": 10,
            "raw_message": "Speed is 120",
            "component": "DriverWrapper",
        },
        {
            "level": "ERROR",
            "qualified_function": "Car::SetSpeed",
            "function_name": "SetSpeed",
            "class_scope": "Car",
            "message": "Speed is 120",
            "thread_id": "2",
            "timestamp_iso": "2026-03-16T12:57:00.087+00:00",
            "source_file": "dserver.log",
            "source_line": 11,
            "raw_message": "Speed is 120",
            "component": "DServer",
        },
    ]

    clusters = cluster_errors(records)
    assert len(clusters) == 1
    assert set(clusters[0]["components"]) == {"DriverWrapper", "DServer"}
    assert clusters[0]["occurrence_count"] == 2


def test_api_start_and_poll_job(tmp_path: Path) -> None:
    zip_path = tmp_path / "sample.zip"
    with zipfile.ZipFile(zip_path, "w") as archive:
        archive.writestr("CommonRuntime/DServer/sample.log", "28-03-2026_10:00:00.000 | 0.123 | 1 | ERROR | Car::SetSpeed - Speed is 120\n")

    with TestClient(app) as client:
        upload_response = client.post("/api/v1/uploads", files={"file": ("sample.zip", zip_path.read_bytes(), "application/zip")})
        assert upload_response.status_code == 200
        upload_id = upload_response.json()["upload_id"]

        job_response = client.post("/api/v1/jobs", json={"upload_id": upload_id})
        assert job_response.status_code == 200
        job_id = job_response.json()["job_id"]

        status_response = client.get(f"/api/v1/jobs/{job_id}")
        assert status_response.status_code == 200
        assert status_response.json()["status"] in {"QUEUED", "DISCOVERED", "PROCESSING", "COMPLETED"}

        cluster_response = client.get(f"/api/v1/jobs/{job_id}/clusters")
        assert cluster_response.status_code == 200
        assert isinstance(cluster_response.json()["items"], list)


def test_no_supported_files_reports_job_status(tmp_path: Path) -> None:
    zip_path = tmp_path / "unsupported.zip"
    with zipfile.ZipFile(zip_path, "w") as archive:
        archive.writestr("CONXApp/ignored.txt", "[2026-03-16 12:56:58.369] [CONXApp] [error] || ThreadID: 999 || Some::Ignored - should be excluded\n")
        archive.writestr("ServerAPIConnector/ignored.txt", "[2026-03-16 12:57:00.087] [ServerAPIConnector] [error] || ThreadID: 1000 || Another::Ignored - should be excluded\n")

    with TestClient(app) as client:
        upload_response = client.post("/api/v1/uploads", files={"file": ("unsupported.zip", zip_path.read_bytes(), "application/zip")})
        assert upload_response.status_code == 200
        upload_id = upload_response.json()["upload_id"]

        job_response = client.post("/api/v1/jobs", json={"upload_id": upload_id})
        assert job_response.status_code == 200
        job_id = job_response.json()["job_id"]

        status = "QUEUED"
        for _ in range(50):
            response = client.get(f"/api/v1/jobs/{job_id}")
            assert response.status_code == 200
            status = response.json()["status"]
            if status in {"NO_SUPPORTED_FILES", "FAILED", "COMPLETED"}:
                break
            time.sleep(0.05)

        assert status == "NO_SUPPORTED_FILES"


def test_concurrent_polling_and_cluster_reads(monkeypatch, tmp_path: Path) -> None:
    import parse_logs as parse_logs_module

    original_iter = parse_logs_module.iter_log_records

    def slow_iter(source, *, on_malformed=None):
        for record in original_iter(source, on_malformed=on_malformed):
            time.sleep(0.01)
            yield record

    monkeypatch.setattr(parse_logs_module, "iter_log_records", slow_iter)

    zip_path = tmp_path / "concurrency.zip"
    with zipfile.ZipFile(zip_path, "w") as archive:
        for idx in range(25):
            archive.writestr(
                f"CommonRuntime/DServer/sample_{idx}.log",
                "[2026-03-16 12:56:58.369] [DServer] [error] || ThreadID: 37808 || Car::SetSpeed - Speed is 120\n",
            )

    with TestClient(app) as client:
        upload_response = client.post("/api/v1/uploads", files={"file": ("concurrency.zip", zip_path.read_bytes(), "application/zip")})
        assert upload_response.status_code == 200
        upload_id = upload_response.json()["upload_id"]

        job_response = client.post("/api/v1/jobs", json={"upload_id": upload_id})
        assert job_response.status_code == 200
        job_id = job_response.json()["job_id"]

        statuses = set()
        for _ in range(30):
            status_response = client.get(f"/api/v1/jobs/{job_id}")
            assert status_response.status_code == 200
            status = status_response.json()["status"]
            statuses.add(status)
            cluster_response = client.get(f"/api/v1/jobs/{job_id}/clusters")
            assert cluster_response.status_code == 200
            assert "items" in cluster_response.json()
            time.sleep(0.05)
            if status == "COMPLETED":
                break

        assert status in {"COMPLETED", "PROCESSING", "DISCOVERED", "QUEUED"}
        assert "FAILED" not in statuses
