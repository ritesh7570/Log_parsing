from __future__ import annotations

import io
import shutil
import tempfile
import time
import zipfile
from pathlib import Path, PurePosixPath

import py7zr

LOG_FILE_EXTENSIONS = {
    ".txt",
    ".log",
    ".out",
    ".err",
    ".trace",
    ".csv",
    ".jsonl",
    ".xml",
}

COMPONENT_ALIASES = {
    "driverwrapper": "DriverWrapper",
    "dwrapper": "DriverWrapper",
    "driver_wrapper": "DriverWrapper",
    "otx": "OTX",
    "otxcoreruntime": "OTX",
    "otxcore": "OTX",
    "dserver": "DServer",
    "conxapp": "CONXApp",
    "serverapiconnector": "ServerAPIConnector",
}

SUPPORTED_COMPONENTS = {"DriverWrapper", "DServer", "OTX"}
EXCLUDED_COMPONENTS = {"CONXApp", "ServerAPIConnector"}
CANONICAL_COMPONENT_ORDER = ["DriverWrapper", "OTX", "DServer", "CONXApp", "ServerAPIConnector"]
ARCHIVE_SUFFIXES = (".zip", ".7z")
MAX_ARCHIVE_DEPTH = 5
MAX_ARCHIVE_COUNT = 100
MAX_TOTAL_ENTRIES = 100_000
MAX_TOTAL_DECOMPRESSED_BYTES = 2 * 1024 * 1024 * 1024
MAX_INDIVIDUAL_FILE_SIZE = 256 * 1024 * 1024
MAX_TEMP_DISK_USAGE_BYTES = 512 * 1024 * 1024


def _normalize_component_token(value: str) -> str:
    return value.strip().lower().replace(" ", "").replace("-", "").replace("_", "")


def _normalize_relative_path(value: str) -> str:
    normalized = value.replace("\\", "/")
    while normalized.startswith("./"):
        normalized = normalized[2:]
    normalized = normalized.lstrip("/")
    if ".." in normalized.split("/"):
        raise ValueError(f"Path traversal blocked: {value!r}")
    if normalized in {"", "."}:
        raise ValueError(f"Empty archive member path: {value!r}")
    return normalized


def _is_directory_entry(path: str, info: object | None = None) -> bool:
    if info is not None and getattr(info, "is_directory", False):
        return True
    normalized = path.replace("\\", "/")
    return normalized.endswith("/") or normalized.endswith("\\")


def _is_archive_name(path: str) -> bool:
    lower_name = path.lower()
    return lower_name.endswith(".zip") or lower_name.endswith(".7z")


def _is_unsafe_file_size(size: int, *, allowed: int = MAX_INDIVIDUAL_FILE_SIZE) -> bool:
    return size > allowed


def detect_component_name(path: str) -> str | None:
    normalized_path = path.replace("\\", "/")
    parts = [part for part in PurePosixPath(normalized_path).parts if part and part not in (".", "/")]
    for part in reversed(parts):
        token = _normalize_component_token(part)
        for alias, canonical in COMPONENT_ALIASES.items():
            if alias in token:
                return canonical
    return None


def is_log_candidate(path: str) -> bool:
    lower_name = path.lower()
    if lower_name.endswith(tuple(sorted(LOG_FILE_EXTENSIONS))):
        return True
    if "log" in lower_name:
        return True
    return False


def is_supported_component(path: str) -> bool:
    component_name = detect_component_name(path)
    return component_name in SUPPORTED_COMPONENTS


def _register_component(state: dict, item: dict) -> None:
    component_name = item.get("component")
    if not component_name:
        return
    bucket = state["components"].setdefault(component_name, {"name": component_name, "files": []})
    bucket["files"].append({
        "path": item["path"],
        "size": item["size"],
        "source_path": item.get("source_path"),
    })


def _ordered_components(state: dict) -> list[dict]:
    ordered: list[dict] = []
    for canonical in CANONICAL_COMPONENT_ORDER:
        if canonical in state["components"]:
            ordered.append({
                "name": canonical,
                "file_count": len(state["components"][canonical]["files"]),
                "files": state["components"][canonical]["files"],
            })
    for name in sorted(state["components"]):
        if name not in {item["name"] for item in ordered}:
            ordered.append({
                "name": name,
                "file_count": len(state["components"][name]["files"]),
                "files": state["components"][name]["files"],
            })
    return ordered


def _extract_zip_member_to_disk(archive: zipfile.ZipFile, member_info: zipfile.ZipInfo, target_root: Path, metrics: dict | None = None) -> Path:
    extraction_started = time.perf_counter()
    file_bytes = archive.read(member_info.filename)
    if len(file_bytes) > MAX_INDIVIDUAL_FILE_SIZE:
        raise ValueError(f"archive member exceeds size limit: {member_info.filename}")
    relative_path = _normalize_relative_path(member_info.filename)
    target_path = target_root / relative_path
    target_path.parent.mkdir(parents=True, exist_ok=True)
    target_path.write_bytes(file_bytes)
    if metrics is not None:
        metrics["extraction_seconds"] = metrics.get("extraction_seconds", 0.0) + time.perf_counter() - extraction_started
    return target_path


def _extract_7z_member_to_disk(archive: py7zr.SevenZipFile, member_name: str, target_root: Path, metrics: dict | None = None) -> Path:
    extraction_started = time.perf_counter()
    relative_path = _normalize_relative_path(member_name)
    target_path = target_root / relative_path
    target_path.parent.mkdir(parents=True, exist_ok=True)
    member_info = archive.getinfo(member_name)
    if member_info.uncompressed > MAX_INDIVIDUAL_FILE_SIZE:
        raise ValueError(f"archive member exceeds size limit: {member_name}")
    archive.extract(path=target_root, targets=[member_name])
    candidate = target_root / relative_path
    if not candidate.exists():
        raise FileNotFoundError(f"Unable to extract archive member: {member_name}")
    if metrics is not None:
        metrics["extraction_seconds"] = metrics.get("extraction_seconds", 0.0) + time.perf_counter() - extraction_started
    return candidate


def _scan_archive_path(archive_path: Path, temp_root: Path, *, state: dict, depth: int = 0) -> None:
    if depth > MAX_ARCHIVE_DEPTH:
        state["warnings"].append(f"Nested archive depth limit reached while processing {archive_path.name}")
        return
    if state["archive_count"] >= MAX_ARCHIVE_COUNT:
        state["warnings"].append("Archive count limit reached; remaining nested archives skipped.")
        return

    lower_name = archive_path.name.lower()
    if lower_name.endswith(".zip"):
        if not zipfile.is_zipfile(archive_path):
            state["warnings"].append(f"Skipping unreadable ZIP archive: {archive_path}")
            return
        try:
            with zipfile.ZipFile(archive_path) as archive:
                state["archive_count"] += 1
                state["archives"].append(str(archive_path))
                for info in archive.infolist():
                    if info.is_dir():
                        continue
                    try:
                        relative_path = _normalize_relative_path(info.filename)
                    except ValueError:
                        state["warnings"].append(f"Rejected unsafe path inside {archive_path.name}: {info.filename!r}")
                        continue
                    if _is_directory_entry(relative_path, info):
                        continue
                    if state["total_entries"] >= MAX_TOTAL_ENTRIES:
                        state["warnings"].append("Total archive entry limit reached.")
                        return
                    state["total_entries"] += 1
                    if info.file_size > MAX_INDIVIDUAL_FILE_SIZE:
                        state["warnings"].append(f"Skipped oversized file: {relative_path}")
                        continue
                    component_name = detect_component_name(relative_path)
                    is_log = is_log_candidate(relative_path)
                    is_nested_archive = _is_archive_name(relative_path)
                    item = {
                        "path": relative_path,
                        "size": info.file_size,
                        "component": component_name,
                        "is_log_candidate": is_log,
                    }
                    state["all_files"].append(item)
                    if component_name and not is_nested_archive:
                        if component_name in EXCLUDED_COMPONENTS:
                            state["excluded_files"] += 1
                        elif component_name not in SUPPORTED_COMPONENTS:
                            state["unsupported_files"] += 1
                        elif is_log and component_name is None:
                            state["unsupported_files"] += 1
                        if component_name:
                            _register_component(state, item)
                    elif is_log and component_name is None:
                        state["unsupported_files"] += 1
                    if is_log and component_name in SUPPORTED_COMPONENTS:
                        extracted_path = temp_root / relative_path
                        extracted_path.parent.mkdir(parents=True, exist_ok=True)
                        extraction_started = time.perf_counter()
                        extracted_path.write_bytes(archive.read(info.filename))
                        state["metrics"]["extraction_seconds"] += time.perf_counter() - extraction_started
                        item["source_path"] = str(extracted_path)
                        state["log_files"].append({
                            "path": relative_path,
                            "size": item["size"],
                            "component": component_name,
                            "source_path": str(extracted_path),
                        })
                    elif is_nested_archive:
                        nested_archive_path = _extract_zip_member_to_disk(archive, info, temp_root / "nested", state["metrics"])
                        if depth + 1 <= MAX_ARCHIVE_DEPTH:
                            _scan_archive_path(nested_archive_path, temp_root, state=state, depth=depth + 1)
                        else:
                            state["warnings"].append(f"Nested ZIP depth limit reached for {relative_path}")
        except (zipfile.BadZipFile, OSError, ValueError) as exc:
            state["warnings"].append(f"Archive read failure for {archive_path.name}: {exc}")
            return
        return

    if lower_name.endswith(".7z"):
        try:
            with py7zr.SevenZipFile(archive_path, mode="r") as archive:
                state["archive_count"] += 1
                state["archives"].append(str(archive_path))
                names = archive.getnames()
                extraction_targets: list[str] = []
                nested_targets: list[str] = []
                for name in names:
                    if not name:
                        continue
                    try:
                        relative_path = _normalize_relative_path(name)
                    except ValueError:
                        state["warnings"].append(f"Rejected unsafe path inside {archive_path.name}: {name!r}")
                        continue
                    member_info = archive.getinfo(name)
                    if _is_directory_entry(relative_path, member_info):
                        continue
                    if state["total_entries"] >= MAX_TOTAL_ENTRIES:
                        state["warnings"].append("Total archive entry limit reached.")
                        return
                    state["total_entries"] += 1
                    file_size = getattr(member_info, "uncompressed", 0)
                    if file_size > MAX_INDIVIDUAL_FILE_SIZE:
                        state["warnings"].append(f"Skipped oversized file: {relative_path}")
                        continue
                    component_name = detect_component_name(relative_path)
                    is_log = is_log_candidate(relative_path)
                    is_nested_archive = _is_archive_name(relative_path)
                    item = {
                        "path": relative_path,
                        "size": file_size,
                        "component": component_name,
                        "is_log_candidate": is_log,
                    }
                    state["all_files"].append(item)
                    if component_name and not is_nested_archive:
                        if component_name in EXCLUDED_COMPONENTS:
                            state["excluded_files"] += 1
                        elif component_name not in SUPPORTED_COMPONENTS:
                            state["unsupported_files"] += 1
                        elif is_log and component_name is None:
                            state["unsupported_files"] += 1
                        if component_name:
                            _register_component(state, item)
                    elif is_log and component_name is None:
                        state["unsupported_files"] += 1
                    if is_log and component_name in SUPPORTED_COMPONENTS:
                        extraction_targets.append(name)
                    elif is_nested_archive:
                        nested_targets.append(name)

                targets_to_extract = extraction_targets + nested_targets
                if targets_to_extract:
                    extraction_started = time.perf_counter()
                    archive.extract(path=temp_root, targets=targets_to_extract)
                    state["metrics"]["extraction_seconds"] += time.perf_counter() - extraction_started
                for name in extraction_targets:
                    relative_path = _normalize_relative_path(name)
                    extracted_path = temp_root / relative_path
                    if extracted_path.exists():
                        state["log_files"].append({
                            "path": relative_path,
                            "size": archive.getinfo(name).uncompressed,
                            "component": detect_component_name(relative_path),
                            "source_path": str(extracted_path),
                        })
                for name in nested_targets:
                    relative_path = _normalize_relative_path(name)
                    nested_archive_path = temp_root / relative_path
                    if depth + 1 <= MAX_ARCHIVE_DEPTH:
                        _scan_archive_path(nested_archive_path, temp_root, state=state, depth=depth + 1)
                    else:
                        state["warnings"].append(f"Nested 7z depth limit reached for {relative_path}")
        except (py7zr.exceptions.Bad7zFile, ValueError, OSError, RuntimeError) as exc:
            state["warnings"].append(f"Archive read failure for {archive_path.name}: {exc}")
            return
        return

    state["warnings"].append(f"Unsupported archive type ignored: {archive_path.name}")


def discover_zip_archive(zip_path: str | Path, temp_root: str | Path | None = None, *, metrics: dict | None = None) -> dict:
    archive_path = Path(zip_path)
    if not archive_path.exists():
        raise FileNotFoundError(f"ZIP file not found: {archive_path}")

    work_dir = Path(temp_root) if temp_root is not None else Path(tempfile.mkdtemp(prefix="logscan_"))
    cleanup = temp_root is None
    metrics = metrics if metrics is not None else {}
    metrics["extraction_seconds"] = 0.0
    state = {
        "metrics": metrics,
        "archive_count": 0,
        "total_entries": 0,
        "archives": [],
        "components": {},
        "all_files": [],
        "log_files": [],
        "excluded_files": 0,
        "unsupported_files": 0,
        "warnings": [],
    }

    try:
        lower_name = archive_path.name.lower()
        if lower_name.endswith(".zip"):
            if not zipfile.is_zipfile(archive_path):
                state["warnings"].append(f"Archive read failure for {archive_path.name}: Not a valid ZIP archive")
                return {
                    "archive_name": archive_path.name,
                    "archive_size_bytes": archive_path.stat().st_size,
                    "archives_discovered": 0,
                    "total_entries": 0,
                    "total_log_files": 0,
                    "excluded_files": 0,
                    "unsupported_files": 0,
                    "components": [],
                    "files": [],
                    "log_files": [],
                    "warnings": state["warnings"],
                }
        elif lower_name.endswith(".7z"):
            try:
                with py7zr.SevenZipFile(archive_path, mode="r") as archive:
                    archive.getnames()
            except Exception as exc:  # pragma: no cover - protection path
                state["warnings"].append(f"Archive read failure for {archive_path.name}: Not a valid 7z archive ({exc})")
                return {
                    "archive_name": archive_path.name,
                    "archive_size_bytes": archive_path.stat().st_size,
                    "archives_discovered": 0,
                    "total_entries": 0,
                    "total_log_files": 0,
                    "excluded_files": 0,
                    "unsupported_files": 0,
                    "components": [],
                    "files": [],
                    "log_files": [],
                    "warnings": state["warnings"],
                }
        else:
            state["warnings"].append(f"Unsupported archive type ignored: {archive_path.name}")
            return {
                "archive_name": archive_path.name,
                "archive_size_bytes": archive_path.stat().st_size,
                "archives_discovered": 0,
                "total_entries": 0,
                "total_log_files": 0,
                "excluded_files": 0,
                "unsupported_files": 0,
                "components": [],
                "files": [],
                "log_files": [],
                "warnings": state["warnings"],
            }

        _scan_archive_path(archive_path, work_dir, state=state)
        ordered_components = _ordered_components(state)
        return {
            "archive_name": archive_path.name,
            "archive_size_bytes": archive_path.stat().st_size,
            "archives_discovered": len(state["archives"]),
            "total_entries": state["total_entries"],
            "total_log_files": len(state["log_files"]),
            "excluded_files": state["excluded_files"],
            "unsupported_files": state["unsupported_files"],
            "components": ordered_components,
            "files": state["all_files"],
            "log_files": state["log_files"],
            "warnings": state["warnings"],
        }
    finally:
        if cleanup:
            shutil.rmtree(work_dir, ignore_errors=True)
