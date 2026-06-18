"""FastAPI application entrypoint for YouTube Search API."""

from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from slowapi import Limiter
from slowapi.errors import RateLimitExceeded
from slowapi.util import get_remote_address

from youtube_search.api.v1.docs import router as docs_router
from youtube_search.api.v1.download import router as download_router
from youtube_search.api.v1.playlist import router as playlist_router
from youtube_search.api.v1.search import router as search_router
from youtube_search.api.v1.search_prefetch import router as search_prefetch_router
from youtube_search.config import get_settings
from youtube_search.mcp.router import router as mcp_router
from youtube_search.services.cache import get_cache_service
from youtube_search.utils.logger import configure_logging, get_logger

configure_logging()
logger = get_logger(__name__)

# 初始化速率限制器
limiter = Limiter(
    key_func=get_remote_address,
    default_limits=["200 per day", "50 per hour"],
)

app = FastAPI(
    title="YouTube 搜尋 API",
    version="1.0.0",
    description="提供 YouTube 搜尋、播放列表元數據提取與音檔下載的 REST API 服務",
    docs_url="/api/docs",
    openapi_url="/api/openapi.json",
    redoc_url="/api/redoc",
)

# 添加速率限制中間件
app.state.limiter = limiter


# 註冊速率限制異常處理器
@app.exception_handler(RateLimitExceeded)
async def ratelimit_handler(request, exc):
    """Rate limit exceeded handler."""
    return {
        "error": "rate_limit_exceeded",
        "message": "您的請求過於頻繁，請稍後再試",
    }


# 添加 CORS 支援
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# 包含所有路由
app.include_router(docs_router)
app.include_router(search_router)
app.include_router(search_prefetch_router)
app.include_router(download_router)
app.include_router(playlist_router)
app.include_router(mcp_router)

# 配置靜態檔案掛載（用於下載的音檔）
config = get_settings()
download_dir = Path(config.download_dir)
if download_dir.exists():
    app.mount("/downloads", StaticFiles(directory=str(download_dir)), name="downloads")
    logger.info(f"靜態檔案掛載: /downloads -> {download_dir}")
else:
    logger.warning(f"下載目錄不存在: {download_dir}")


@app.on_event("startup")
async def startup_event() -> None:
    """Initialize services on application startup."""

    logger.info("Starting YouTube Search API...")

    # Initialize and test Redis connection
    cache_service = get_cache_service()
    if cache_service.client:
        logger.info("Redis cache service initialized and ready")
    else:
        logger.warning("Running without Redis cache - all searches will be live")

    # Initialize MCP server
    try:
        from youtube_search.mcp.server import get_mcp_server_manager  # noqa: F401
        get_mcp_server_manager()
        logger.info("MCP server initialized successfully")
    except Exception as e:
        logger.error(f"Failed to initialize MCP server: {str(e)}", exc_info=True)


@app.get("/health", tags=["health"])
def health() -> dict[str, str]:
    """Simple health check endpoint."""

    logger.debug("health check")
    return {"status": "ok"}


if __name__ == "__main__":  # pragma: no cover
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8000)
