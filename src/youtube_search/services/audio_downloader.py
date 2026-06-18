"""Audio downloader service using yt-dlp with Cloudinary multi-account failover
and smart cache (check Cloudinary before downloading from YouTube)."""

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
import cloudinary.api
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
    then uploading to Cloudinary with multi-account auto-failover.

    On every request the service first checks whether the video already
    exists on any configured Cloudinary account (Smart Cache).  If found,
    the Cloudinary URL is returned immediately — no yt-dlp download needed.
    """

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
        """Smart-cache entry point.

        Order of operations:
        1. Check every Cloudinary account for an existing resource matching
           the video_id — if found, return immediately (no yt-dlp needed).
        2. Run yt-dlp to download + convert to MP3.
        3. Upload the MP3 to Cloudinary (multi-account failover).
        4. Delete the local temp file after a successful upload.
        5. Return AudioFile with Cloudinary URL (or local path as fallback).

        Args:
            video_id: YouTube video ID (11 characters)

        Returns:
            AudioFile: Metadata including URL (Cloudinary or local fallback)

        Raises:
            VideoNotFoundError, DurationExceededError, LiveStreamError,
            DownloadFailedError, StorageFullError
        """
        # ── Step 1: Smart Cache — check Cloudinary before downloading ──
        cached = await self._check_cloudinary_cache(video_id)
        if cached:
            logger.info(f"Smart cache hit for {video_id} — skipping download.")
            return cached

        # ── Step 2: Download and convert locally via yt-dlp ──
        mp3_path, title, duration = await self._run_ytdlp(video_id)
        file_size = mp3_path.stat().st_size

        # ── Step 3: Upload to Cloudinary (with failover) ──
        cloudinary_url = await self._upload_to_cloudinary(video_id, title, duration, mp3_path)

        # ── Step 4: Delete local temp file on success ──
        if cloudinary_url:
            try:
                mp3_path.unlink()
                logger.info(f"Deleted local temp file after Cloudinary upload: {mp3_path}")
            except OSError as exc:
                logger.warning(f"Could not delete temp file {mp3_path}: {exc}")
            file_path = cloudinary_url
        else:
            logger.warning(f"All Cloudinary accounts failed — serving {video_id} from local storage.")
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
        """Download multiple YouTube videos, upload each to Cloudinary,
        then package local files (fallback) into a ZIP.

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
                    # Only zip actual local files, not Cloudinary URLs
                    if local_path.exists():
                        zf.write(local_path, local_path.name)

        logger.info(f"Batch ZIP created: {zip_path} ({zip_path.stat().st_size} bytes)")
        return zip_path, results

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    async def _check_cloudinary_cache(self, video_id: str) -> Optional[AudioFile]:
        """Check every configured Cloudinary account for an existing resource.

        Uses ``video_id`` as the Cloudinary ``public_id`` (resource_type="video").
        Retrieves title and duration from the resource's context metadata if
        they were stored during the original upload.

        Returns:
            AudioFile if found on any account, otherwise None.
        """
        if not self.cloudinary_accounts:
            return None

        for i, account in enumerate(self.cloudinary_accounts):
            account_name = account.get("name", f"account-{i + 1}")
            try:
                cloudinary.config(
                    cloud_name=account["cloud_name"],
                    api_key=account["api_key"],
                    api_secret=account["api_secret"],
                    secure=True,
                )
                # This raises cloudinary.exceptions.NotFound (404) if absent
                resource = await asyncio.get_event_loop().run_in_executor(
                    None,
                    lambda: cloudinary.api.resource(
                        video_id,
                        resource_type="video",
                    ),
                )

                secure_url: str = resource["secure_url"]

                # Recover title and duration stored in context during upload
                context = resource.get("context", {}).get("custom", {})
                title = context.get("title", video_id)
                try:
                    duration = int(context.get("duration", 0))
                except (ValueError, TypeError):
                    duration = 0

                file_size = resource.get("bytes", 0)

                logger.info(
                    f"Cloudinary smart cache hit on {account_name} for {video_id}: {secure_url}"
                )
                return AudioFile(
                    video_id=video_id,
                    file_name=f"{video_id}.mp3",
                    file_path=secure_url,
                    file_size=file_size,
                    duration=duration,
                    title=title,
                )

            except CloudinaryError:
                # NotFound or any other Cloudinary error — try next account
                logger.debug(f"Not found on {account_name}, checking next account...")
            except Exception as exc:
                logger.warning(f"Cloudinary cache check failed on {account_name}: {exc}")

        return None

    async def _run_ytdlp(self, video_id: str) -> tuple[Path, str, int]:
        """Run yt-dlp to download and convert to MP3.

        Returns:
            (mp3_path, title, duration_seconds)
        """
        url = f"https://www.youtube.com/watch?v={video_id}"
        max_duration = self.config.max_video_duration  # 420 s (env: MAX_VIDEO_DURATION)
        bitrate = self.config.audio_bitrate            # 128 kbps (env: AUDIO_BITRATE)
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

        logger.info(
            f"yt-dlp starting: video_id={video_id} "
            f"max_duration={max_duration}s bitrate={bitrate}kbps"
        )

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
        self, video_id: str, title: str, duration: int, mp3_path: Path
    ) -> Optional[str]:
        """Upload an MP3 to Cloudinary using multi-account auto-failover.

        - public_id  = video_id  (deterministic; enables smart cache lookups)
        - resource_type = "video"  (required for MP3 streaming on Cloudinary)
        - context stores title + duration for retrieval on cache hits
        - Iterates accounts in order; returns the secure URL on first success.

        Returns:
            Cloudinary secure_url on success, or None if all accounts fail.
        """
        if not self.cloudinary_accounts:
            return None

        for i, account in enumerate(self.cloudinary_accounts):
            account_name = account.get("name", f"account-{i + 1}")
            logger.info(
                f"Cloudinary upload attempt — {account_name} "
                f"({i + 1}/{len(self.cloudinary_accounts)})"
            )
            try:
                cloudinary.config(
                    cloud_name=account["cloud_name"],
                    api_key=account["api_key"],
                    api_secret=account["api_secret"],
                    secure=True,
                )

                response = await asyncio.get_event_loop().run_in_executor(
                    None,
                    lambda: cloudinary.uploader.upload(
                        str(mp3_path),
                        resource_type="video",       # required for MP3 streaming
                        public_id=video_id,          # use video_id for deterministic lookup
                        format="mp3",
                        overwrite=True,
                        context=f"title={title}|duration={duration}",
                    ),
                )

                secure_url: str = response["secure_url"]
                logger.info(f"Cloudinary upload success via {account_name}: {secure_url}")
                return secure_url

            except (CloudinaryError, KeyError, Exception) as exc:
                logger.warning(f"Cloudinary upload failed on {account_name}: {exc}")
                if i < len(self.cloudinary_accounts) - 1:
                    logger.info("Failing over to next Cloudinary account...")
                else:
                    logger.error("All Cloudinary accounts exhausted — upload failed.")

        return None

    async def _safe_download(
        self, video_id: str
    ) -> tuple[bool, Optional[AudioFile], Optional[str]]:
        """Wraps download_and_convert to silently catch errors for batch downloads."""
        try:
            audio_file = await self.download_and_convert(video_id)
            return (True, audio_file, None)
        except Exception as exc:
            logger.warning(f"Batch download failed for {video_id}: {exc}")
            return (False, None, str(exc))
