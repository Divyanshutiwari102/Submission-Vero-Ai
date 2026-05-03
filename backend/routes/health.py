"""
routes/health.py — GET /v1/healthz
Liveness probe. Always returns 200.
"""

import time
from fastapi import APIRouter
from utils.store import store

router = APIRouter()


@router.get("/healthz")
def healthz():
    uptime_seconds = int(time.time() - store.boot_time)
    context_counts = store.count_by_scope()
    return {
        "status": "ok",
        "uptime_seconds": uptime_seconds,
        "contexts_loaded": context_counts,
    }
