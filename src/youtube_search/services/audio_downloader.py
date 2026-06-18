"""Audio downloader service using yt-dlp with Cloudinary multi-account failover."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import zipfile
from datetime import datetime
from pathlib import Path
from typing import Optional

import cloudinary
import cloudinary.uploader
from cloudinary.exceptions import Error as CloudinaryError

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
    """Service for downloading YouTube audio as MP3 using yt-dlp,
    then uploading to Cloudinary with multi-account auto-failover."""

    def __init__(self) -> None:
        self.config = get_settings()
        self.download_dir = Path(self.config.download_dir)
        self.download_dir.mkdir(parents=True, exist_ok=True)

        # Load Cloudinary accounts from environment variable (JSON array).
        # Format: [{"name":"acct1","cloud_name":"...","api_key":"...","api_secret":"..."}, ...]
        self.cloudinary_accounts: list[dict] = []
        accounts_json = os.getenv("CLOUDINARY_ACCOUNTS_JSON", "[]")
        try:
            self.cloudinary_accounts = json.loads(accounts_json)
            if self.cloudinary_accounts:
                logger.info(
                    f"Loaded {len(self.cloudinary_accounts)} Cloudinary account(s) for failover."
                )
            else:
                logger.warning(
                    "CLOUDINARY_ACCOUNTS_JSON is empty or not set — "
                    "files will be served from local storage only."
                )
        except json.JSONDecodeError:
            logger.error(
                "CLOUDINARY_ACCOUNTS_JSON is not valid JSON — Cloudinary upload disabled."
            )

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def download_and_convert(self, video_id: str) -> AudioFile:
        """Download a YouTube video, convert to MP3, upload to Cloudinary,
        then delete the local file.

        Args:
            video_id: YouTube video ID (11 characters)

        Returns:
            AudioFile: Metadata including Cloudinary URL (or local path as fallback)

        Raises:
            VideoNotFoundError, DurationExceededError, LiveStreamError,
            DownloadFailedError, StorageFullError
        """
        # Step 1 — download and convert locally
        mp3_path, title, duration = await self._run_ytdlp(video_id)
        file_size = mp3_path.stat().st_size

        # Step 2 — upload to Cloudinary (with failover); delete local on success
        cloudinary_url = await self._upload_to_cloudinary(video_id, title, mp3_path)

        if cloudinary_url:
            # Successfully uploaded — remove temp file to free disk space
            try:
                mp3_path.unlink()
                logger.info(f"Deleted local temp file after Cloudinary upload: {mp3_path}")
            except OSError as exc:
                logger.warning(f"Could not delete temp file {mp3_path}: {exc}")

            file_path = cloudinary_url
        else:
            # All Cloudinary accounts failed (or none configured) — fall back to local
            logger.warning(f"Falling back to local storage for {video_id}")
            file_path = str(mp3_path)

        return AudioFile(
            video_id=video_id,
            file_name=mp3_path.name,
            file_path=file_path,
            file_size=file_size,
            duration=duration,
            title=title,
        )

    async def batch_download_as_zip(
        self,
        video_ids: list[str],
    ) -> tuple[Path, dict[str, tuple[bool, Optional[AudioFile], Optional[str]]]]:
        """Download multiple YouTube videos as MP3, upload each to Cloudinary,
        then package the Cloudinary URLs into a ZIP manifest (or the local files
        if Cloudinary is unavailable).

        Args:
            video_ids: List of YouTube video IDs

        Returns:
            Tuple of (zip_path, results_dict)
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
            for _vid, (success, audio_file, _) in results.items():
                if success and audio_file:
                    local_path = Path(audio_file.file_path)
                    # Only include if it's actually a local file (not a Cloudinary URL)
                    if local_path.exists():
                        zf.write(local_path, local_path.name)

        logger.info(f"Batch ZIP created: {zip_path} ({zip_path.stat().st_size} bytes)")
        return zip_path, results

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    async def _run_ytdlp(self, video_id: str) -> tuple[Path, str, int]:
        """Run yt-dlp to download and convert to MP3.

        Returns:
            (mp3_path, title, duration_seconds)
        """
        url = f"https://www.youtube.com/watch?v={video_id}"
        max_duration = self.config.max_video_duration  # 420 s by default (env override)
        bitrate = self.config.audio_bitrate            # 128 kbps by default
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

        logger.info(f"yt-dlp starting: video_id={video_id} max_duration={max_duration}s bitrate={bitrate}kbps")

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
            logger.error(f"yt-dlp error for {video_id}: {stderr_text[:300]}")
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

        # Parse title and duration from yt-dlp --print output
        title = video_id
        duration = 0
        if stdout_text:
            lines = [line for line in stdout_text.splitlines() if "|||" in line]
            if lines:
                parts = lines[-1].split("|||")
                if len(parts) >= 2:
                    title = parts[0].strip() or video_id
                    try:
                        duration = int(float(parts[1].strip()))
                    except (ValueError, TypeError):
                        duration = 0

        # Locate the output MP3 file
        mp3_path = self.download_dir / f"{video_id}.mp3"
        if not mp3_path.exists():
            matches = list(self.download_dir.glob(f"{video_id}*.mp3"))
            if matches:
                mp3_path = matches[0]
            else:
                raise DownloadFailedError(
                    message="下載後找不到音檔",
                    video_id=video_id,
                    reason="MP3 file not found after yt-dlp completed",
                )

        logger.info(f"yt-dlp complete: {mp3_path.name} ({mp3_path.stat().st_size} bytes)")
        return mp3_path, title, duration

    async def _upload_to_cloudinary(
        self, video_id: str, title: str, mp3_path: Path
    ) -> Optional[str]:
        """Upload an MP3 to Cloudinary using multi-account auto-failover.

        Iterates through self.cloudinary_accounts in order. Returns the
        secure Cloudinary URL on first success, or None if all accounts fail
        (or none are configured).

        Uses resource_type="video" so Cloudinary allows MP3 streaming.
        """
        if not self.cloudinary_accounts:
            return None

        safe_title = "".join(
            c for c in title if c.isalpha() or c.isdigit() or c == " "
        ).strip()
        public_id = f"{video_id}_{safe_title}" if safe_title else video_id

        for i, account in enumerate(self.cloudinary_accounts):
            account_name = account.get("name", f"account-{i + 1}")
            logger.info(f"Trying Cloudinary upload — {account_name} ({i + 1}/{len(self.cloudinary_accounts)})")
            try:
                # Configure Cloudinary for this specific account
                cloudinary.config(
                    cloud_name=account["cloud_name"],
                    api_key=account["api_key"],
                    api_secret=account["api_secret"],
                    secure=True,  # Always use HTTPS URLs
                )

                # Upload as resource_type="video" so the MP3 is streamable
                response = cloudinary.uploader.upload(
                    str(mp3_path),
                    resource_type="video",
                    public_id=public_id,
                    format="mp3",
                    overwrite=True,
                )

                secure_url: str = response["secure_url"]
                logger.info(
                    f"Cloudinary upload success via {account_name}: {secure_url}"
                )
                return secure_url

            except (CloudinaryError, KeyError, Exception) as exc:
                logger.warning(
                    f"Cloudinary upload failed for {account_name}: {exc}"
                )
                if i < len(self.cloudinary_accounts) - 1:
                    logger.info("Trying next Cloudinary account...")
                else:
                    logger.error("All Cloudinary accounts exhausted — no upload possible.")

        return None

    async def _safe_download(
        self, video_id: str
    ) -> tuple[bool, Optional[AudioFile], Optional[str]]:
        """Wrapper that silently catches errors for use in batch downloads."""
        try:
            audio_file = await self.download_and_convert(video_id)
            return (True, audio_file, None)
        except Exception as exc:
            logger.warning(f"Batch download failed for {video_id}: {exc}")
            return (False, None, str(exc))
