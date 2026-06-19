"""Main FastAPI application."""

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
import logging

from youtube_search.config import get_settings
from youtube_search.routes import search, download

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger(__name__)

# Get settings (auto-detects PORT)
settings = get_settings()

app = FastAPI(
    title="YouTube Music Streaming API",
    description="Zero-cost YouTube music streaming API with Cloudinary storage",
    version="1.0.0"
)

# CORS
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

@app.get("/health")
async def health_check():
    """Health check endpoint."""
    return {
        "status": "healthy",
        "port": settings.port,
        "cache": "in-memory" if not settings.redis_enabled else "redis",
        "cloudinary_accounts": len(settings.cloudinary_accounts)
    }

@app.get("/")
async def root():
    """Root endpoint."""
    return {
        "message": "YouTube Music Streaming API",
        "docs": "/api/docs",
        "health": "/health"
    }

# Include routers
app.include_router(search.router, prefix="/api/v1", tags=["Search"])
app.include_router(download.router, prefix="/api/v1", tags=["Download"])

if __name__ == "__main__":
    import uvicorn
    logger.info(f"Starting server on {settings.api_host}:{settings.port}")
    uvicorn.run(
        "main:app",
        host=settings.api_host,
        port=settings.port,
        reload=False
    )
