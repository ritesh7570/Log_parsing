from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from app.services.job_manager import job_manager

logger = logging.getLogger(__name__)

router = APIRouter()


class JobRequest(BaseModel):
    upload_id: str


@router.post("/api/v1/jobs")
def create_job(request: JobRequest) -> dict:
    try:
        job_data = job_manager.create_job(request.upload_id)
        logger.info(
            "JOB_CREATED job_id=%s upload_id=%s",
            job_data.get("job_id"),
            request.upload_id,
        )
        return job_data
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.get("/api/v1/jobs/{job_id}")
def get_job(job_id: str) -> dict:
    try:
        return job_manager.get_job(job_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.delete("/api/v1/jobs/{job_id}")
def cancel_job(job_id: str) -> dict:
    try:
        return job_manager.cancel_job(job_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.get("/api/v1/jobs/{job_id}/clusters")
def get_clusters(job_id: str, page: int = 1, page_size: int = 20) -> dict:
    try:
        return job_manager.get_clusters(job_id, page=page, page_size=page_size)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.get("/api/v1/jobs/{job_id}/clusters/{fingerprint_id}/occurrences")
def get_occurrences(job_id: str, fingerprint_id: str, page: int = 1, page_size: int = 20) -> dict:
    try:
        return job_manager.get_occurrences(job_id, fingerprint_id, page=page, page_size=page_size)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
