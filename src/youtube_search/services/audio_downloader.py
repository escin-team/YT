"""Audio downloader service using yt-dlp."""

from __future__ import annotations

import asyncio
import logging
import zipfile
from datetime import datetime
from pathlib import Path
from typing import Optional

from youtube_search.config import get_settings
from youtube_search.models.download import AudioFile
from youtube_search.utils.errors import (
    DownloadFailedError,
    DurationExceededError,
    LiveStreamError,
    StorageFullError,
    VideoNotFoundError,
)

logger = logging.getLogger(__name__)


class AudioDownloaderService:
    """Service for downloading YouTube audio as MP3 using yt-dlp."""

    def __init__(self) -> None:
        self.config = get_settings()
        self.download_dir = Path(self.config.download_dir)
        self.download_dir.mkdir(parents=True, exist_ok=True)

    async def download_and_convert(self, video_id: str) -> AudioFile:
        """
        Download a YouTube video and convert to MP3.

        Args:
            video_id: YouTube video ID (11 characters)

        Returns:
            AudioFile: Downloaded audio file metadata

        Raises:
            VideoNotFoundError: Video does not exist or is unavailable
            DurationExceededError: Video exceeds max duration limit
            LiveStreamError: Video is a live stream
            DownloadFailedError: Download or conversion failed
            StorageFullError: Not enough disk space
        """
        url = f"https://www.youtube.com/watch?v={video_id}"
        max_duration = self.config.max_video_duration
        bitrate = self.config.audio_bitrate
        output_template = str(self.download_dir / f"{video_id}.%(ext)s")

        cmd = [
            "yt-dlp",
            "--no-playlist",
            "-x",
            "--audio-format", "mp3",
            "--audio-quality", f"{bitrate}K",
            "--match-filter", f"duration <= {max_duration}",
            "--print", "%(title)s|||%(duration)s",
            "--no-progress",
            "--output", output_template,
            url,
        ]

        logger.info(f"Starting download: {video_id}")

        try:
            process = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await asyncio.wait_for(
                process.communicate(),
                timeout=float(self.config.download_timeout),
            )
        except asyncio.TimeoutError:
            raise DownloadFailedError(
                message="下載超時",
                video_id=video_id,
                reason=f"Download exceeded {self.config.download_timeout}s timeout",
            )

        stdout_text = stdout.decode("utf-8", errors="replace").strip()
        stderr_text = stderr.decode("utf-8", errors="replace")

        if process.returncode != 0:
            logger.error(f"yt-dlp failed for {video_id}: {stderr_text}")
            err_lower = stderr_text.lower()

            if "video unavailable" in err_lower or "not available" in err_lower:
                raise VideoNotFoundError(video_id=video_id)
            if "is a live event" in err_lower or "live stream" in err_lower:
                raise LiveStreamError(video_id=video_id)
            if "does not pass filter" in err_lower:
                raise DurationExceededError(video_id=video_id, max_duration=max_duration)
            if "no space left" in err_lower:
                raise StorageFullError()
            raise DownloadFailedError(
                message="影片下載失敗",
                video_id=video_id,
                reason=stderr_text[:500],
            )

        title = video_id
        duration = 0

        if stdout_text:
            lines = [line for line in stdout_text.splitlines() if "|||" in line]
            if lines:
                parts = lines[-1].split("|||")
                if len(parts) >= 2:
                    title = parts[0].strip()
                    try:
                        duration = int(float(parts[1].strip()))
                    except (ValueError, TypeError):
                        duration = 0

        mp3_path = self.download_dir / f"{video_id}.mp3"
        if not mp3_path.exists():
            matching = list(self.download_dir.glob(f"{video_id}*.mp3"))
            if matching:
                mp3_path = matching[0]
            else:
                raise DownloadFailedError(
                    message="下載後找不到音檔",
                    video_id=video_id,
                    reason="MP3 file not found after yt-dlp completed",
                )

        file_size = mp3_path.stat().st_size
        logger.info(f"Download complete: {video_id} ({file_size} bytes)")

        return AudioFile(
            video_id=video_id,
            file_name=mp3_path.name,
            file_path=str(mp3_path),
            file_size=file_size,
            duration=duration,
            title=title,
        )

    async def batch_download_as_zip(
        self,
        video_ids: list[str],
    ) -> tuple[Path, dict[str, tuple[bool, Optional[AudioFile], Optional[str]]]]:
        """
        Download multiple YouTube videos as MP3 and package into a ZIP.

        Args:
            video_ids: List of YouTube video IDs

        Returns:
            Tuple of (zip_path, results_dict) where results_dict maps
            video_id -> (success, audio_file_or_None, error_message_or_None)
        """
        tasks = [self._safe_download(video_id) for video_id in video_ids]
        download_results = await asyncio.gather(*tasks)

        results: dict[str, tuple[bool, Optional[AudioFile], Optional[str]]] = {}
        for video_id, result in zip(video_ids, download_results):
            results[video_id] = result

        timestamp = datetime.utcnow().strftime("%Y%m%d_%H%M%S")
        zip_filename = f"youtube_batch_download_{timestamp}.zip"
        zip_path = self.download_dir / zip_filename

        with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
            for _video_id, (success, audio_file, _) in results.items():
                if success and audio_file:
                    mp3_path = Path(audio_file.file_path)
                    if mp3_path.exists():
                        zf.write(mp3_path, mp3_path.name)

        logger.info(f"Batch ZIP created: {zip_path} ({zip_path.stat().st_size} bytes)")
        return zip_path, results

    async def _safe_download(
        self, video_id: str
    ) -> tuple[bool, Optional[AudioFile], Optional[str]]:
        """Wrapper that catches errors for batch downloads."""
        try:
            audio_file = await self.download_and_convert(video_id)
            return (True, audio_file, None)
        except Exception as e:
            logger.warning(f"Batch download failed for {video_id}: {e}")
            return (False, None, str(e))
