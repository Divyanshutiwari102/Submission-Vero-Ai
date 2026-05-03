"""
routes/meta.py — GET /v1/metadata
Bot identity and version info.
"""

from fastapi import APIRouter

router = APIRouter()


@router.get("/metadata")
def metadata():
    return {
        "name": "Vera Smart Merchant Assistant",
        "version": "1.0",
        "description": "AI-powered, context-aware growth assistant using deterministic scoring + intelligent messaging for merchant performance optimization",
    }
