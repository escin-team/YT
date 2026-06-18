"""POST /api/v1/search-and-prefetch — search YouTube and warm the top-2 results.

Resource-conscious design for low-RAM environments:
- Returns the full search result list immediately.
- Fires a single asyncio Task that processes the top-2 video IDs
  **strictly sequentially** (never in parallel).
- Each yt-dlp call is capped at PREFETCH_YTDLP_TIMEOUT seconds (default 60)
  so a slow video cannot block the queue indefinitely.
- Smart Cache (Cloudinary check) runs first; yt-dlp is skipped on a hit.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Depends, Header, Query, Request
from fastapi.responses import JSONResponse

from youtube_search.models.download import SearchAndPrefetchResponse
from youtube_search.models.video import Video
from youtube_search.services.audio_downloader import AudioDownloaderService
from youtube_search.services.cache_manager import CacheManagerService
from youtube_search.services.search import SearchService, get_search_service
from youtube_search.utils.errors import AppError

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1", tags=["search-and-prefetch"])

# Module-level singletons — same pattern as download.py
_downloader = AudioDownloaderService()
_cache = CacheManagerService()

# How long (seconds) yt-dlp may run per video during a prefetch job.
# Kept deliberately short to protect free-tier CPU/RAM.
PREFETCH_YTDLP_TIMEOUT = 60
# How many top results to prefetch (hard limit — do not raise without RAM headroom).
PREFETCH_TOP_N = 2


# ---------------------------------------------------------------------------
# Background worker
# ---------------------------------------------------------------------------

async def _prefetch_top_n(video_ids: list[str]) -> None:
    """Sequential, error-isolated background prefetch for the top-N results.

    Each video is processed one at a time:
      1. Smart Cache check (Cloudinary) — free, no yt-dlp.
      2. yt-dlp download + Cloudinary upload — capped at PREFETCH_YTDLP_TIMEOUT.
      3. Write to Redis cache (if available).

    Errors for one video never abort the others.
    """
    logger.info(
        f"[search-prefetch] Background prefetch starting for "
        f"{len(video_ids)} video(s): {video_ids}"
    )

    for video_id in video_ids:
        try:
            audio = await asyncio.wait_for(
                _downloader.download_and_convert(video_id),
                timeout=float(PREFETCH_YTDLP_TIMEOUT),
            )
            # Persist in Redis cache so the next request is sub-millisecond
            await _cache.set_cached_audio(audio)
            source = "cache-hit" if audio.cached else "downloaded"
            logger.info(
                f"[search-prefetch] ✓ {video_id} ({source}) → {audio.file_path[:80]}"
            )
        except asyncio.TimeoutError:
            logger.warning(
                f"[search-prefetch] ✗ {video_id}: exceeded {PREFETCH_YTDLP_TIMEOUT}s "
                "prefetch timeout — skipping"
            )
        except Exception as exc:
            logger.warning(f"[search-prefetch] ✗ {video_id}: {exc}")

    logger.info("[search-prefetch] Background prefetch complete.")


# ---------------------------------------------------------------------------
# Endpoint
# ---------------------------------------------------------------------------

@router.post(
    "/search-and-prefetch",
    response_model=SearchAndPrefetchResponse,
    summary="搜尋 YouTube 並自動預熱排名前 2 的結果",
    responses={
        200: {
            "description": "立即返回完整搜尋結果，背景預熱 Top-2",
            "content": {
                "application/json": {
                    "example": {
                        "search_keyword": "rickroll",
                        "result_count": 10,
                        "videos": [],
                        "timestamp": "2026-01-01T00:00:00Z",
                        "prefetch_queued": ["dQw4w9WgXcQ", "jNQXAC9IVRw"],
                        "prefetch_count": 2,
                    }
                }
            },
        },
        400: {"description": "搜尋關鍵字無效"},
        503: {"description": "YouTube 搜尋服務不可用"},
    },
)
async def search_and_prefetch(
    request: Request,
    keyword: str = Query(..., min_length=1, max_length=200, description="搜尋關鍵字"),
    limit: int = Query(10, ge=1, le=50, description="返回結果數量（預設 10）"),
    sort_by: str = Query(
        "relevance",
        pattern="^(relevance|date)$",
        description="排序方式（relevance | date）",
    ),
    service: SearchService = Depends(get_search_service),
    x_forwarded_for: Optional[str] = Header(None),
) -> SearchAndPrefetchResponse:
    """
    搜尋 YouTube 並在背景自動預熱排名最前的 2 支影片。

    ### 工作流程
    1. 執行 YouTube 搜尋（與 `/api/v1/search` 相同邏輯）
    2. **立即返回**完整搜尋結果（不阻塞）
    3. 背景排程預熱 Top-2 影片：
       - Smart Cache 檢查（Cloudinary） → 命中則跳過 yt-dlp
       - 未命中 → yt-dlp 下載（最多 60 秒）→ Cloudinary 上傳 → Redis 快取
       - **嚴格循序執行**（不並行），保護伺服器資源

    ### 安全限制（Free Tier）
    | 限制 | 值 |
    |------|-----|
    | 預熱影片上限 | 2 支（Top-2） |
    | 每支 yt-dlp 超時 | 60 秒 |
    | 處理模式 | 嚴格循序 |

    ### 範例

    ```bash
    curl -X POST "http://localhost:5000/api/v1/search-and-prefetch?keyword=rickroll&limit=10"
    ```
    """
    client_ip = x_forwarded_for or (request.client.host if request.client else "unknown")
    logger.info(f"[search-prefetch] keyword={keyword!r} limit={limit} ip={client_ip}")

    # ── Step 1: Perform the search (same as GET /search) ─────────────────
    try:
        search_result = await service.search(keyword=keyword, limit=limit, sort_by=sort_by)
    except AppError as exc:
        return JSONResponse(status_code=exc.status_code, content=exc.to_response())
    except Exception as exc:
        logger.error(f"[search-prefetch] Search failed: {exc}", exc_info=True)
        return JSONResponse(
            status_code=503,
            content={"error": "search_failed", "message": str(exc)},
        )

    # ── Step 2: Pick top-N valid video IDs for prefetch ──────────────────
    top_ids: list[str] = [
        v.video_id
        for v in search_result.videos[:PREFETCH_TOP_N]
        if v.video_id
    ]

    # ── Step 3: Fire-and-forget background task ───────────────────────────
    if top_ids:
        asyncio.create_task(_prefetch_top_n(top_ids))
        logger.info(f"[search-prefetch] Queued prefetch for: {top_ids}")

    # ── Step 4: Build and return the response immediately ─────────────────
    timestamp = (
        datetime.now(timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )

    return SearchAndPrefetchResponse(
        search_keyword=search_result.search_keyword,
        result_count=search_result.result_count,
        videos=search_result.videos,
        timestamp=timestamp,
        prefetch_queued=top_ids,
        prefetch_count=len(top_ids),
    )
