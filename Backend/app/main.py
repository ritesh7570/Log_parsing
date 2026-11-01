from __future__ import annotations

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.routes import health, jobs, uploads

app = FastAPI(title="Log Analysis Platform", version="0.1.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173", "http://127.0.0.1:5173", "http://localhost:3000"],
    allow_methods=["*"],
    allow_headers=["*"],
    allow_credentials=True,
)

app.include_router(health.router)
app.include_router(uploads.router)
app.include_router(jobs.router)


@app.get("/")
def root() -> dict[str, str]:
    return {"service": "Log Analysis Platform", "status": "ok"}
