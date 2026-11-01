# Log Analysis Platform

A local web application for inspecting ZIP and 7z log archives, discovering supported component logs, processing log records, and exploring grouped errors by deterministic fingerprint.

## Features

- Upload ZIP or 7z archives through a React dashboard.
- Discover archive entries and supported components: DriverWrapper, DServer, and OTX.
- Process supported log files and report parsed records, errors, and processing progress.
- Group errors into deterministic fingerprint clusters backed by SQLite.
- Explore cluster occurrences with pagination and inspect their raw log lines and archive paths.
- View archive component distribution and the most frequent error fingerprints.

## Project layout

```text
Backend/   FastAPI service, archive discovery, log processing, and SQLite job storage
frontend/  React and Vite dashboard
docs/      Architecture, API, pipeline, and project notes
```

## Requirements

- Python 3.10 or newer
- Node.js and npm

## Run locally

Start the backend in one terminal:

```powershell
cd Backend
python -m venv .venv
.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
python -m uvicorn app.main:app --host 127.0.0.1 --port 8000 --reload
```

Start the dashboard in a second terminal:

```powershell
cd frontend
npm install
npm run dev
```

Open the Vite URL shown in the frontend terminal (usually `http://localhost:5173`). The dashboard connects to the API at `http://127.0.0.1:8000`.

## API overview

- `GET /health` — backend health check
- `POST /api/v1/uploads` — upload a ZIP or 7z archive
- `POST /api/v1/jobs` — create a processing job for an upload
- `GET /api/v1/jobs/{job_id}` — read job status and processing metrics
- `GET /api/v1/jobs/{job_id}/clusters` — list fingerprint clusters
- `GET /api/v1/jobs/{job_id}/clusters/{fingerprint_id}/occurrences?page=1&page_size=20` — read a page of occurrences

Interactive API documentation is available at `http://127.0.0.1:8000/docs` while the backend is running.

## Build the frontend

```powershell
cd frontend
npm run build
```

## Tests

The backend parsing and fingerprint tests are in `Backend/test_log_processing.py`. Run them from the `Backend` directory with:

```powershell
python -m pytest
```

## Data and local storage

Uploaded archives and job data are kept under `Backend/storage/` at runtime. Customer archives, extracted sample logs, virtual environments, databases, and generated output are excluded from Git by the root `.gitignore`; provide your own test archives locally.
