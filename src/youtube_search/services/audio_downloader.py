"""Audio downloader service using yt-dlp with Cloudinary multi-account failover
and smart cache (check Cloudinary before downloading from YouTube)."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
import zipfile
from datetime import datetime
from pathlib import Path
from typing import Optional

# Detect Bun for yt-dlp's JS runtime (lighter than Node.js, ~20 MB RAM).
# Falls back to None when Bun is not installed — yt-dlp will warn but
# still work for formats that don't need JS decryption.
_BUN_BIN: Optional[str] = (
    shutil.which("bun")
    or (
        os.path.expanduser("~/.bun/bin/bun")
        if os.path.isfile(os.path.expanduser("~/.bun/bin/bun"))
        else None
    )
)
if _BUN_BIN:
    logger_init = logging.getLogger(__name__)
    logger_init.info(f"Bun detected at {_BUN_BIN} — will pass --js-runtimes to yt-dlp")

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
        cached_hit = await self._check_cloudinary_cache(video_id)
        if cached_hit:
            logger.info(
                f"Smart cache HIT for {video_id} via {cached_hit.storage_account} "
                "— skipping download."
            )
            return cached_hit

        # ── Step 2: Download and convert locally via yt-dlp ──
        mp3_path, title, duration = await self._run_ytdlp(video_id)
        file_size = mp3_path.stat().st_size

        # ── Step 3: Upload to Cloudinary (with failover) ──
        cloudinary_url, used_account = await self._upload_to_cloudinary(
            video_id, title, duration, mp3_path
        )

        # ── Step 4: Delete local temp file on success ──
        if cloudinary_url:
            try:
                mp3_path.unlink()
                logger.info(f"Deleted local temp file after Cloudinary upload: {mp3_path}")
            except OSError as exc:
                logger.warning(f"Could not delete temp file {mp3_path}: {exc}")
            file_path = cloudinary_url
            storage_source = "cloudinary"
        else:
            logger.warning(
                f"All Cloudinary accounts failed — serving {video_id} from local storage."
            )
            file_path = str(mp3_path)
            storage_source = "local"
            used_account = ""

        return AudioFile(
            video_id=video_id,
            file_name=mp3_path.name,
            file_path=file_path,
            file_size=file_size,
            duration=duration,
            title=title,
            cached=False,
            storage_source=storage_source,
            storage_account=used_account,
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
                    f"Cloudinary smart cache HIT on {account_name} for {video_id}: {secure_url}"
                )
                return AudioFile(
                    video_id=video_id,
                    file_name=f"{video_id}.mp3",
                    file_path=secure_url,
                    file_size=file_size,
                    duration=duration,
                    title=title,
                    cached=True,
                    storage_source="cloudinary",
                    storage_account=account_name,
                )

            except CloudinaryError:
                # NotFound or any other Cloudinary error — try next account
                logger.debug(f"Not found on {account_name}, checking next account...")
            except Exception as exc:
                logger.warning(f"Cloudinary cache check failed on {account_name}: {exc}")

        return None

    async def _fetch_video_metadata(
        self, video_id: str, url: str, max_duration: int
    ) -> tuple[str, int]:
        """Phase 1: Fetch title and duration without downloading.

        Uses --dump-single-json --skip-download which is not affected by the
        --print simulation bug present in yt-dlp ≥ 2025.x.

        Raises DurationExceededError early if the video is too long.
        Returns (title, duration_seconds).
        """
        meta_cmd = [
            "yt-dlp",
            "--no-playlist",
            "--no-update",
            "--dump-single-json",
            "--skip-download",
        ]
        if _BUN_BIN:
            meta_cmd += [
                "--js-runtimes", f"bun:{_BUN_BIN}",
                "--remote-components", "ejs:github",
            ]
        meta_cmd.append(url)
        try:
            meta_proc = await asyncio.create_subprocess_exec(
                *meta_cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            meta_stdout, meta_stderr = await asyncio.wait_for(
                meta_proc.communicate(),
                timeout=30.0,
            )
        except asyncio.TimeoutError:
            logger.warning(f"Metadata fetch timed out for {video_id} — using fallback values")
            return video_id, 0

        if meta_proc.returncode != 0:
            err = meta_stderr.decode("utf-8", errors="replace").lower()
            if "video unavailable" in err or "not available" in err:
                raise VideoNotFoundError(video_id=video_id)
            if "is a live event" in err or "live stream" in err:
                raise LiveStreamError(video_id=video_id)
            # Non-fatal: proceed with fallback values
            logger.warning(
                f"Metadata fetch failed for {video_id} (rc={meta_proc.returncode}) — "
                "continuing with fallback title/duration"
            )
            return video_id, 0

        try:
            import json as _json
            info = _json.loads(meta_stdout.decode("utf-8", errors="replace"))
            title: str = info.get("title") or video_id
            duration: int = int(info.get("duration") or 0)
        except Exception as exc:
            logger.warning(f"Could not parse metadata JSON for {video_id}: {exc}")
            return video_id, 0

        if duration > max_duration:
            raise DurationExceededError(video_id=video_id, max_duration=max_duration)

        logger.info(f"Metadata fetched: title={title!r} duration={duration}s")
        return title, duration

    async def _run_ytdlp(self, video_id: str) -> tuple[Path, str, int]:
        """Run yt-dlp to download and convert to MP3.

        Two-phase approach to work around the yt-dlp ≥ 2025.x bug where
        --print causes simulation mode (no file written):
          Phase 1 — _fetch_video_metadata(): dump JSON without downloading
          Phase 2 — actual download (no --print flag)

        Returns:
            (mp3_path, title, duration_seconds)
        """
        url = f"https://www.youtube.com/watch?v={video_id}"
        max_duration = self.config.max_video_duration  # 420 s (env: MAX_VIDEO_DURATION)
        bitrate = self.config.audio_bitrate            # 128 kbps (env: AUDIO_BITRATE)
        output_template = str(self.download_dir / f"{video_id}.%(ext)s")

        # ── Phase 1: fetch metadata (fast, no download) ───────────────────
        title, duration = await self._fetch_video_metadata(video_id, url, max_duration)

        # ── Phase 2: download + convert (no --print to avoid simulation) ──
        cmd = [
            "yt-dlp",
            "--no-playlist",
            "--no-update",
            "-x",
            "--audio-format", "mp3",
            "--audio-quality", f"{bitrate}K",
            "--match-filter", f"duration <= {max_duration}",
            "--no-progress",
            "--output", output_template,
        ]
        if _BUN_BIN:
            cmd += [
                "--js-runtimes", f"bun:{_BUN_BIN}",
                "--remote-components", "ejs:github",
            ]
        cmd.append(url)

        logger.info(
            f"yt-dlp download starting: video_id={video_id} "
            f"title={title!r} max_duration={max_duration}s bitrate={bitrate}kbps\n"
            f"  cmd: {' '.join(cmd)}"
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

        stderr_text = stderr.decode("utf-8", errors="replace")

        if process.returncode != 0:
            logger.error(f"yt-dlp download error for {video_id}: {stderr_text[:500]}")
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

        # ── Locate the output MP3 file ────────────────────────────────────
        expected_path = self.download_dir / f"{video_id}.mp3"
        dir_contents = list(self.download_dir.iterdir())
        logger.debug(
            f"yt-dlp finished (rc=0). Expected: {expected_path}. "
            f"Download dir contents: {[f.name for f in dir_contents]}"
        )

        mp3_path = expected_path
        if not mp3_path.exists():
            matches = [f for f in dir_contents if f.name.startswith(video_id) and f.suffix == ".mp3"]
            if matches:
                mp3_path = matches[0]
                logger.info(f"MP3 found via glob fallback: {mp3_path.name}")
            else:
                logger.error(
                    f"MP3 not found after yt-dlp completed for {video_id}. "
                    f"Dir contents: {[f.name for f in dir_contents]}"
                )
                raise DownloadFailedError(
                    message="下載後找不到音檔",
                    video_id=video_id,
                    reason=(
                        f"MP3 file not found after yt-dlp completed. "
                        f"Download dir contains: {[f.name for f in dir_contents]}"
                    ),
                )

        logger.info(f"yt-dlp complete: {mp3_path.name} ({mp3_path.stat().st_size:,} bytes)")
        return mp3_path, title, duration

    async def _upload_to_cloudinary(
        self, video_id: str, title: str, duration: int, mp3_path: Path
    ) -> tuple[Optional[str], str]:
        """Upload an MP3 to Cloudinary using multi-account auto-failover.

        - public_id  = video_id  (deterministic; enables smart cache lookups)
        - resource_type = "video"  (required for MP3 streaming on Cloudinary)
        - context stores title + duration for retrieval on cache hits
        - Iterates accounts in order; returns on first success.

        Returns:
            (secure_url, account_name) on success, or (None, "") if all fail.
        """
        if not self.cloudinary_accounts:
            return None, ""

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
                return secure_url, account_name

            except (CloudinaryError, KeyError, Exception) as exc:
                logger.warning(f"Cloudinary upload failed on {account_name}: {exc}")
                if i < len(self.cloudinary_accounts) - 1:
                    logger.info("Failing over to next Cloudinary account...")
                else:
                    logger.error("All Cloudinary accounts exhausted — upload failed.")

        return None, ""

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
