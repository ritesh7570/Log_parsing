# Log Analysis Platform

## Objective
This project implements a ZIP-based log analysis platform for discovering uploaded archives, scanning component log files, parsing ERROR records, generating deterministic fingerprints, and surfacing grouped error clusters in a React dashboard.

## Technology stack
- Python 3
- FastAPI
- SQLite for bounded-memory aggregation
- React + Vite
- Axios for API communication

## Project structure
- Backend/ — Python backend, existing log-parsing engine, and FastAPI app
- frontend/ — React application
- docs/ — architecture and implementation documentation
- Backend/storage/ — upload and analysis storage

## Backend setup
From the backend directory:

```bash
cd Backend
. .venv\Scripts\activate
python -m uvicorn app.main:app --host 127.0.0.1 --port 8000 --reload
```

## Frontend setup
From the frontend directory:

```bash
cd frontend
npm install
npm run dev
```

## Verified Phase 1 capabilities
- ZIP upload through the FastAPI service
- ZIP archive validation and storage
- Archive metadata discovery without extracting the full archive
- Supported component classification for DriverWrapper, DServer, and OTX paths
- Line-by-line parsing for DWrapper/DServer formats and the existing legacy pipe-delimited format
- Exact ERROR-only filtering and malformed-line accounting
- Deterministic SHA-256 fingerprint generation keyed by qualified function and normalized message
- SQLite-backed job tracking, cluster aggregation, and paginated cluster reads
- Frontend production build validation with Vite

## Current development phase
The verified Phase 1 state is parser compatibility, component exclusions, and SQLite-safe job processing. Nested ZIP extraction, CONXApp parsing, ServerAPIConnector parsing, Neptune integration, and vendor report generation remain out of scope.

## Local validation
ZIP and 7z upload and processing workflows have been exercised locally. Sample archives and customer data are not included in this repository.
