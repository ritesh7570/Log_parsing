# API Documentation

## GET /health
- Method: GET
- Purpose: returns backend health status
- Request: none
- Response:

```json
{
  "status": "ok"
}
```

## POST /api/v1/uploads
- Method: POST
- Purpose: uploads a ZIP archive and returns discovery metadata
- Request format: multipart form-data with a `file` field
- Response:

```json
{
  "upload_id": "uuid",
  "filename": "sample.zip",
  "stored_path": ".../storage/uploads/uuid.zip",
  "status": "ready",
  "discovery": {
    "archive_name": "sample.zip",
    "archive_size_bytes": 123,
    "total_entries": 10,
    "total_log_files": 3,
    "components": [
      {
        "name": "DServer",
        "file_count": 2,
        "files": [
          { "path": "...", "size": 456 }
        ]
      }
    ],
    "files": [
      { "path": "...", "size": 456, "component": "DServer", "is_log_candidate": true }
    ]
  }
}
```

## Error responses
- 400: invalid extension or invalid ZIP file
- 413: upload exceeds 20 GB limit
- 500: processing error while writing or validating the upload

## Processing job endpoints
The job endpoints are implemented as part of the processing pipeline and include:
- POST /api/v1/jobs
- GET /api/v1/jobs/{job_id}
- DELETE /api/v1/jobs/{job_id}
- GET /api/v1/jobs/{job_id}/clusters
- GET /api/v1/jobs/{job_id}/clusters/{fingerprint_id}/occurrences

These endpoints are documented in the code and drive the job lifecycle and cluster pagination. Only supported component logs are included in processing; excluded paths such as CONXApp and ServerAPIConnector are filtered before normal parser execution.
