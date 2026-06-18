"""下載功能的資料模型定義。"""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Optional

from pydantic import BaseModel, Field


class DownloadStatus(str, Enum):
    """下載狀態列舉。"""

    PENDING = "pending"
    DOWNLOADING = "downloading"
    SUCCESS = "success"
    FAILED = "failed"
    CANCELLED = "cancelled"


class DownloadFormat(str, Enum):
    """下載格式列舉。"""

    LINK = "link"  # 返回下載連結
    STREAM = "stream"  # 直接串流 MP3


class DownloadErrorType(str, Enum):
    """下載錯誤類型列舉。"""

    VIDEO_NOT_FOUND = "video_not_found"
    DURATION_EXCEEDED = "duration_exceeded"
    LIVE_STREAM = "live_stream"
    DOWNLOAD_FAILED = "download_failed"
    STORAGE_FULL = "storage_full"
    TIMEOUT = "timeout"
    INVALID_VIDEO_ID = "invalid_video_id"
    UNKNOWN = "unknown"


class AudioFile(BaseModel):
    """音檔模型。"""

    video_id: str = Field(..., description="YouTube 影片 ID")
    file_name: str = Field(..., description="本地檔案名稱（無路徑）")
    file_path: str = Field(..., description="完整檔案路徑或 Cloudinary URL")
    file_size: int = Field(..., ge=0, description="檔案大小（字節）")
    duration: int = Field(..., ge=0, description="音檔長度（秒）")
    title: str = Field(..., description="影片標題")
    created_at: datetime = Field(default_factory=datetime.utcnow, description="建立時間")
    cached: bool = Field(default=False, description="True 表示由 Cloudinary Smart Cache 命中")
    storage_source: str = Field(default="local", description="儲存來源：'cloudinary' 或 'local'")
    storage_account: str = Field(default="", description="Cloudinary 帳號名稱（若適用）")


class DownloadLog(BaseModel):
    """下載日誌模型。"""

    video_id: str = Field(..., description="YouTube 影片 ID")
    status: DownloadStatus = Field(..., description="下載狀態")
    error_type: Optional[DownloadErrorType] = Field(
        default=None,
        description="錯誤類型（失敗時）",
    )
    error_message: Optional[str] = Field(
        default=None,
        description="錯誤訊息（失敗時）",
    )
    ip_address: Optional[str] = Field(default=None, description="請求者 IP 位址")
    timestamp: datetime = Field(default_factory=datetime.utcnow, description="時間戳記")
    duration: Optional[int] = Field(default=None, description="處理耗時（毫秒）")


class DownloadRequest(BaseModel):
    """下載請求基類。"""

    video_id: str = Field(..., min_length=10, max_length=20, description="YouTube 影片 ID")
    format: DownloadFormat = Field(
        default=DownloadFormat.LINK,
        description="返回格式 (link 或 stream)",
    )


class DownloadAudioRequest(DownloadRequest):
    """單一音檔下載 API 請求。"""

    pass


class DownloadAudioResponse(BaseModel):
    """單一音檔下載 API 回應。"""

    video_id: str = Field(..., description="YouTube 影片 ID")
    title: str = Field(..., description="影片標題")
    duration: int = Field(..., ge=0, description="音檔長度（秒）")
    download_url: Optional[str] = Field(
        default=None,
        description="下載連結（format=link 時返回）",
    )
    storage_source: str = Field(
        default="local",
        description="儲存來源：'cloudinary' 或 'local'",
    )
    storage_account: str = Field(
        default="",
        description="使用的 Cloudinary 帳號名稱（若適用）",
    )
    cached: bool = Field(
        default=False,
        description="True = Cloudinary Smart Cache 命中；False = 新下載",
    )
    file_size: Optional[int] = Field(
        default=None,
        description="音檔大小（字節）",
    )


class BatchDownloadItem(BaseModel):
    """批次下載項目（已廢棄，保留向後相容性）。"""

    video_id: str = Field(..., description="YouTube 影片 ID")
    status: str = Field(..., description="下載狀態 (success, failed)")
    download_url: Optional[str] = Field(
        default=None,
        description="下載連結（成功時）",
    )
    error_message: Optional[str] = Field(
        default=None,
        description="錯誤訊息（失敗時）",
    )
    duration: Optional[int] = Field(
        default=None,
        description="音檔長度（秒，成功時）",
    )
    cached: bool = Field(
        default=False,
        description="是否從快取直接返回",
    )


class BatchDownloadRequest(BaseModel):
    """批次下載 API 請求。"""

    video_ids: list[str] = Field(
        ...,
        min_items=1,
        max_items=20,
        description="YouTube 影片 ID 清單（最多 20 個）",
    )


class BatchDownloadResponse(BaseModel):
    """批次下載 API 回應（ZIP 壓縮檔）。"""

    total: int = Field(..., ge=0, description="請求總數")
    successful: int = Field(..., ge=0, description="成功下載數量")
    failed: int = Field(..., ge=0, description="失敗數量")
    zip_url: str = Field(
        ...,
        description="ZIP 壓縮檔下載連結（包含所有成功下載的音檔）",
    )
    zip_file_size: int = Field(
        ...,
        ge=0,
        description="ZIP 壓縮檔大小（字節）",
    )
    items: list[BatchDownloadItem] = Field(
        default=[],
        description="各影片下載詳細結果（僅包含失敗項目以供調試）",
    )


class PrefetchRequest(BaseModel):
    """預熱快取請求。

    接受 JSON 陣列或逗號分隔字串：
    - JSON:  {"video_ids": ["id1", "id2"]}
    - 字串:  {"video_ids": "id1,id2,id3"}
    """

    video_ids: list[str] | str = Field(
        ...,
        description="YouTube 影片 ID 清單（JSON 陣列或逗號分隔字串，最多 50 個）",
    )

    def parsed_ids(self) -> list[str]:
        """Normalise to a flat deduplicated list regardless of input format."""
        if isinstance(self.video_ids, str):
            ids = [v.strip() for v in self.video_ids.split(",") if v.strip()]
        else:
            ids = [v.strip() for v in self.video_ids if v.strip()]
        # Deduplicate while preserving order
        seen: set[str] = set()
        unique: list[str] = []
        for vid in ids:
            if vid not in seen:
                seen.add(vid)
                unique.append(vid)
        return unique[:50]  # hard cap


class PrefetchResponse(BaseModel):
    """預熱快取回應（立即返回，背景處理）。"""

    status: str = Field(..., description="Always 'prefetch_started'")
    count: int = Field(..., ge=0, description="已排入背景佇列的有效影片數量")
    video_ids: list[str] = Field(..., description="已排入佇列的影片 ID 清單")
    skipped: list[str] = Field(
        default=[],
        description="格式無效而略過的影片 ID",
    )


class SearchAndPrefetchResponse(BaseModel):
    """搜尋 + 自動預熱回應。

    回傳完整搜尋結果，同時在背景預熱排名最前的 2 支影片。
    """

    search_keyword: str = Field(..., description="搜尋關鍵字")
    result_count: int = Field(..., ge=0, description="返回影片數量")
    videos: list = Field(default_factory=list, description="影片清單（完整結果）")
    timestamp: str = Field(..., description="搜尋時間戳記（ISO 8601 UTC）")
    prefetch_queued: list[str] = Field(
        default=[],
        description="已排入背景預熱佇列的影片 ID（最多 2 個）",
    )
    prefetch_count: int = Field(
        default=0,
        ge=0,
        description="實際排入預熱佇列的數量",
    )
