from __future__ import annotations

import logging
import zipfile
from pathlib import Path
from uuid import uuid4

from fastapi import APIRouter, File, HTTPException, UploadFile

from app.services.zip_discovery import discover_zip_archive

logger = logging.getLogger(__name__)

router = APIRouter()
MAX_UPLOAD_BYTES = 20 * 1024 * 1024 * 1024
UPLOAD_ROOT = Path(__file__).resolve().parents[2] / "storage" / "uploads"
UPLOAD_ROOT.mkdir(parents=True, exist_ok=True)


@router.post("/api/v1/uploads")
async def upload_zip(file: UploadFile = File(...)) -> dict:
    logger.info(
        "UPLOAD_RECEIVED filename=%s",
        file.filename,
    )
    if file.filename is None:
        raise HTTPException(status_code=400, detail="Uploaded file is missing a name.")
    file_name = file.filename.lower()
    if not (file_name.endswith(".zip") or file_name.endswith(".7z")):
        raise HTTPException(status_code=400, detail="Only .zip and .7z files are supported.")

    upload_id = uuid4()
    suffix = ".7z" if file_name.endswith(".7z") else ".zip"
    temp_path = UPLOAD_ROOT / f"{upload_id}.part"
    final_path = UPLOAD_ROOT / f"{upload_id}{suffix}"
    total_bytes = 0

    try:
        with temp_path.open("wb") as buffer:
            while True:
                chunk = await file.read(1024 * 1024)
                if not chunk:
                    break
                total_bytes += len(chunk)
                if total_bytes > MAX_UPLOAD_BYTES:
                    raise HTTPException(status_code=413, detail="Upload exceeds the 20 GB limit.")
                buffer.write(chunk)

        if suffix == ".zip" and not zipfile.is_zipfile(temp_path):
            raise HTTPException(status_code=400, detail="Uploaded file is not a valid ZIP archive.")
        if suffix == ".7z":
            try:
                import py7zr
                with py7zr.SevenZipFile(temp_path, mode="r") as archive:
                    archive.getnames()
            except Exception as exc:  # pragma: no cover - archive validation path
                raise HTTPException(status_code=400, detail=f"Uploaded file is not a valid 7z archive: {exc}") from exc

        temp_path.replace(final_path)
        logger.info(
            "UPLOAD_SAVED upload_id=%s filename=%s bytes=%s",
            upload_id,
            file.filename,
            total_bytes,
        )
        discovery = discover_zip_archive(final_path)
        archives_discovered = discovery.get("archives_discovered", 0)
        total_entries = discovery.get("total_entries", 0)
        total_log_files = discovery.get("total_log_files", 0)
        excluded_files = discovery.get("excluded_files", 0)
        unsupported_files = discovery.get("unsupported_files", 0)
        logger.info(
            "DISCOVERY_COMPLETED upload_id=%s archives=%s entries=%s logs=%s excluded=%s unsupported=%s",
            upload_id,
            archives_discovered,
            total_entries,
            total_log_files,
            excluded_files,
            unsupported_files,
        )
        return {
            "upload_id": str(upload_id),
            "filename": file.filename,
            "stored_path": str(final_path),
            "status": "ready",
            "discovery": discovery,
        }
    except HTTPException:
        if temp_path.exists():
            temp_path.unlink()
        if final_path.exists():
            final_path.unlink()
        raise
    except Exception as exc:  # pragma: no cover - safety path for runtime errors
        if temp_path.exists():
            temp_path.unlink()
        if final_path.exists():
            final_path.unlink()
        raise HTTPException(status_code=500, detail=f"Error processing upload: {exc}") from exc
