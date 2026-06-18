"""YouTube 音檔下載 API 端點。

本模組實作單一影片和批次下載端點，支援直接連結返回或 MP3 串流。
"""

from __future__ import annotations

import logging
import time
from typing import Optional

from fastapi import APIRouter, Header, Request
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse

from youtube_search.config import get_settings
from youtube_search.models.download import (
    BatchDownloadItem,
    BatchDownloadRequest,
    BatchDownloadResponse,
    DownloadAudioResponse,
    DownloadFormat,
)
from youtube_search.services.audio_downloader import AudioDownloaderService
from youtube_search.services.cache_manager import CacheManagerService
from youtube_search.utils.errors import (
    AppError,
    DownloadFailedError,
    DurationExceededError,
    LiveStreamError,
    StorageFullError,
    VideoNotFoundError,
)
from youtube_search.utils.validators import (
    generate_download_url,
    sanitize_filename,
    validate_video_id,
)

logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/api/v1/download",
    tags=["audio-download"],
)

# 初始化服務
config = get_settings()
downloader_service = AudioDownloaderService()
cache_service = CacheManagerService()


# 速率限制裝飾器（需從 main.py 的 limiter 注入）
def get_limiter():
    """取得全局速率限制器。"""
    from main import limiter

    return limiter


@router.post(
    "/audio",
    response_model=DownloadAudioResponse,
    summary="下載單一 YouTube 影片為 MP3 音檔",
    responses={
        200: {
            "description": "下載成功，返回下載連結或音檔串流",
            "content": {
                "application/json": {
                    "example": {
                        "video_id": "dQw4w9WgXcQ",
                        "title": "Rick Astley - Never Gonna Give You Up",
                        "duration": 212,
                        "download_url": "http://localhost:8000/downloads/dQw4w9WgXcQ_Rick_Astley_Never_Gonna_Give_You_Up.mp3",
                        "cached": False,
                        "file_size": 3400000,
                    }
                },
                "audio/mpeg": {"example": "<binary MP3 data>"},
            },
        },
        400: {
            "description": "無效的請求參數",
            "example": {
                "code": "INVALID_VIDEO_ID",
                "message": "video_id 格式無效（應為 11 個英數字符），",
                "status": 400,
                "trace_id": "uuid",
            },
        },
        403: {
            "description": "影片受限制（長度超限、直播等）",
            "example": {
                "code": "DURATION_EXCEEDED",
                "message": "影片長度超過允許限制",
                "status": 403,
                "trace_id": "uuid",
            },
        },
        404: {
            "description": "影片不存在或已刪除",
            "example": {
                "code": "VIDEO_NOT_FOUND",
                "message": "影片不存在或已刪除",
                "status": 404,
                "trace_id": "uuid",
            },
        },
        503: {
            "description": "下載失敗或服務不可用",
            "example": {
                "code": "DOWNLOAD_FAILED",
                "message": "影片下載失敗",
                "status": 503,
                "trace_id": "uuid",
            },
        },
        507: {
            "description": "儲存空間不足",
            "example": {
                "code": "STORAGE_FULL",
                "message": "伺服器儲存空間不足",
                "status": 507,
                "trace_id": "uuid",
            },
        },
        429: {"description": "超過速率限制"},
    },
)
async def download_audio(
    request: Request,
    video_id: str,
    format: DownloadFormat = DownloadFormat.LINK,
    x_forwarded_for: Optional[str] = Header(None),
) -> DownloadAudioResponse | FileResponse:
    """
    下載 YouTube 影片並轉換為 MP3 音檔。

    ### 功能
    - 驗證影片存在性和長度限制（最長 10 分鐘）
    - 檢查 Redis 快取，避免重複下載
    - 使用 yt-dlp 下載並轉換為 128kbps MP3
    - 支援返回下載連結 (link) 或直接串流 MP3 (stream)
    - 記錄所有下載活動

    ### 參數
    - `video_id`: YouTube 影片 ID（必須）
    - `format`: 返回格式 (link 或 stream，預設: link)

    ### 限制
    - 每 IP 每小時最多 20 次下載
    - 支援的影片長度: 1 秒 ~ 600 秒（10 分鐘）
    - 不支援直播和受限影片

    ### 範例

    ```bash
    # 返回下載連結
    curl -X POST "http://localhost:8000/api/v1/download/audio?video_id=dQw4w9WgXcQ&format=link"

    # 直接串流 MP3
    curl -X POST "http://localhost:8000/api/v1/download/audio?video_id=dQw4w9WgXcQ&format=stream" \\
      -o output.mp3
    ```
    """
    start_time = time.time()
    client_ip = x_forwarded_for or request.client.host if request.client else "unknown"

    try:
        # 驗證影片 ID
        validated_video_id = validate_video_id(video_id)
        logger.info(f"下載請求: video_id={validated_video_id}, format={format}, IP={client_ip}")

        # 檢查快取
        cached_audio = await cache_service.get_cached_audio(validated_video_id)
        if cached_audio:
            logger.info(f"快取命中: {validated_video_id}")
            is_cloudinary = cached_audio.file_path.startswith("http")

            if format == DownloadFormat.STREAM:
                if is_cloudinary:
                    # Cloudinary URL — redirect the client to stream directly
                    return RedirectResponse(url=cached_audio.file_path, status_code=302)
                safe_filename = sanitize_filename(cached_audio.title)
                return FileResponse(
                    path=cached_audio.file_path,
                    media_type="audio/mpeg",
                    filename=f"{validated_video_id}_{safe_filename}.mp3",
                )
            else:
                # 返回 JSON 回應（快取）
                download_url = (
                    cached_audio.file_path
                    if is_cloudinary
                    else generate_download_url(
                        config.download_base_url,
                        validated_video_id,
                        cached_audio.title,
                    )
                )
                return DownloadAudioResponse(
                    video_id=validated_video_id,
                    title=cached_audio.title,
                    duration=int(cached_audio.duration),
                    download_url=download_url,
                    cached=True,
                    file_size=cached_audio.file_size,
                )

        # 下載新檔案
        logger.info(f"開始下載新檔案: {validated_video_id}")
        audio_file = await downloader_service.download_and_convert(validated_video_id)

        # 快取音檔信息
        await cache_service.set_cached_audio(audio_file)

        is_cloudinary = audio_file.file_path.startswith("http")

        if format == DownloadFormat.STREAM:
            if is_cloudinary:
                # Cloudinary URL — redirect the client to stream directly
                return RedirectResponse(url=audio_file.file_path, status_code=302)
            safe_filename = sanitize_filename(audio_file.title)
            return FileResponse(
                path=audio_file.file_path,
                media_type="audio/mpeg",
                filename=f"{validated_video_id}_{safe_filename}.mp3",
            )
        else:
            # 返回 JSON 回應
            download_url = (
                audio_file.file_path
                if is_cloudinary
                else generate_download_url(
                    config.download_base_url,
                    validated_video_id,
                    audio_file.title,
                )
            )
            return DownloadAudioResponse(
                video_id=validated_video_id,
                title=audio_file.title,
                duration=audio_file.duration,
                download_url=download_url,
                cached=False,
                file_size=audio_file.file_size,
            )

    except VideoNotFoundError as e:
        duration = time.time() - start_time
        logger.error(f"影片不存在: {video_id} ({duration:.2f}s)")
        return JSONResponse(
            status_code=404,
            content=e.to_response(),
        )

    except DurationExceededError as e:
        duration = time.time() - start_time
        logger.error(f"影片長度超限: {video_id} ({duration:.2f}s)")
        return JSONResponse(
            status_code=403,
            content=e.to_response(),
        )

    except LiveStreamError as e:
        duration = time.time() - start_time
        logger.error(f"不支援直播: {video_id} ({duration:.2f}s)")
        return JSONResponse(
            status_code=403,
            content=e.to_response(),
        )

    except DownloadFailedError as e:
        duration = time.time() - start_time
        logger.error(f"下載失敗: {video_id} ({duration:.2f}s) - {e.reason}")
        return JSONResponse(
            status_code=503,
            content=e.to_response(),
        )

    except StorageFullError as e:
        duration = time.time() - start_time
        logger.error(f"儲存空間不足: {video_id} ({duration:.2f}s)")
        return JSONResponse(
            status_code=507,
            content=e.to_response(),
        )

    except AppError as e:
        duration = time.time() - start_time
        logger.error(f"應用錯誤: {video_id} ({duration:.2f}s) - {e.message}")
        return JSONResponse(
            status_code=e.status_code,
            content=e.to_response(),
        )

    except Exception as e:
        duration = time.time() - start_time
        logger.exception(f"未預期的錯誤: {video_id} ({duration:.2f}s)")
        error = DownloadFailedError(
            message="未預期的伺服器錯誤",
            reason=str(e),
        )
        return JSONResponse(
            status_code=503,
            content=error.to_response(),
        )


@router.post(
    "/batch",
    response_model=BatchDownloadResponse,
    summary="批次下載多個 YouTube 影片為 MP3 音檔（ZIP 壓縮）",
    responses={
        200: {
            "description": "批次下載完成，返回 ZIP 壓縮檔下載連結",
            "example": {
                "total": 2,
                "successful": 2,
                "failed": 0,
                "zip_url": "http://localhost:8000/downloads/youtube_batch_download_20231215_120000.zip",
                "zip_file_size": 8500000,
                "items": [],
            },
        },
        400: {"description": "無效的請求（超過限制或格式錯誤）"},
        429: {"description": "超過批次速率限制"},
        503: {"description": "下載或壓縮失敗"},
    },
)
async def batch_download(
    request: Request,
    batch_request: BatchDownloadRequest,
    x_forwarded_for: Optional[str] = Header(None),
) -> BatchDownloadResponse:
    """
    批次下載多個 YouTube 影片為 MP3 音檔並打包為 ZIP 壓縮檔。

    ### 功能
    - 支援一次提交最多 20 個影片 ID
    - 並行下載，避免阻塞
    - 部分失敗不影響其他檔案
    - 自動打包為 ZIP 壓縮檔並返回下載連結
    - 返回詳細的下載結果（僅包含失敗項目）

    ### 參數
    - `video_ids`: 影片 ID 清單（陣列，必須）

    ### 限制
    - 每 IP 每小時最多 10 次批次請求
    - 每次最多 20 個影片

    ### 範例

    ```bash
    curl -X POST "http://localhost:8000/api/v1/download/batch" \\
      -H "Content-Type: application/json" \\
      -d '{"video_ids": ["dQw4w9WgXcQ", "jNQXAC9IVRw"]}'
    ```
    """
    start_time = time.time()
    client_ip = x_forwarded_for or request.client.host if request.client else "unknown"

    logger.info(
        f"批次下載請求（ZIP）: {len(batch_request.video_ids)} 個影片, IP={client_ip}",
    )

    try:
        # 執行批次下載並打包為 ZIP
        zip_path, batch_results = await downloader_service.batch_download_as_zip(
            batch_request.video_ids,
        )

        # 統計結果
        successful = sum(1 for success, _, _ in batch_results.values() if success)
        failed = sum(1 for success, _, _ in batch_results.values() if not success)

        # 收集失敗項目用於回應
        failed_items = []
        for vid, (success, _audio_file, error_msg) in batch_results.items():
            if not success:
                failed_items.append(
                    BatchDownloadItem(
                        video_id=vid,
                        status="failed",
                        error_message=error_msg or "未知錯誤",
                    ),
                )

        # 生成 ZIP 下載連結
        zip_url = generate_download_url(
            config.download_base_url,
            "",
            zip_path.name,
        )

        # 獲取 ZIP 檔案大小
        zip_file_size = zip_path.stat().st_size

        duration = time.time() - start_time
        logger.info(
            f"批次下載完成（ZIP）: {successful} 成功, {failed} 失敗 ({duration:.2f}s), "
            f"ZIP 大小: {zip_file_size} 字節"
        )

        return BatchDownloadResponse(
            total=len(batch_request.video_ids),
            successful=successful,
            failed=failed,
            zip_url=zip_url,
            zip_file_size=zip_file_size,
            items=failed_items,
        )

    except Exception as e:
        duration = time.time() - start_time
        logger.error(f"批次下載異常: ({duration:.2f}s) - {str(e)}", exc_info=True)
        error = DownloadFailedError(
            message="批次下載失敗",
            reason=str(e),
        )
        return JSONResponse(
            status_code=503,
            content=error.to_response(),
        )
