"""
Vera Bot — magicpin AI Challenge
Main FastAPI application entry point.
"""

import time
import uvicorn
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from routes.context import router as context_router
from routes.tick import router as tick_router
from routes.reply import router as reply_router
from routes.health import router as health_router
from routes.meta import router as meta_router
from utils.store import store

app = FastAPI(
    title="Vera Bot — magicpin AI Challenge",
    description="Merchant AI assistant for WhatsApp engagement",
    version="1.0.0",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# Register all routers
app.include_router(context_router, prefix="/v1")
app.include_router(tick_router, prefix="/v1")
app.include_router(reply_router, prefix="/v1")
app.include_router(health_router, prefix="/v1")
app.include_router(meta_router, prefix="/v1")

# Record boot time
store.boot_time = time.time()


@app.api_route("/", methods=["GET", "HEAD"])
def root():
    return {"service": "vera-bot", "status": "running", "version": "1.0.0"}


if __name__ == "__main__":
    uvicorn.run("main:app", host="0.0.0.0", port=8080, reload=False)
