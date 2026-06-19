"""Universal cache manager with Redis and in-memory fallback."""

from __future__ import annotations

import json
import logging
from typing import Optional, Dict, Any
from datetime import datetime, timedelta

from youtube_search.config import get_settings
from youtube_search.models.download import AudioFile

logger = logging.getLogger(__name__)


class InMemoryCache:
    """In-memory cache fallback (no Redis required)."""
    
    def __init__(self):
        self._cache: Dict[str, tuple[Any, datetime]] = {}
        logger.info("In-memory cache initialized")
    
    def get(self, key: str) -> Optional[str]:
        if key not in self._cache:
            return None
        
        value, expires_at = self._cache[key]
        if datetime.now() > expires_at:
            del self._cache[key]
            return None
        return value
    
    def setex(self, key: str, ttl_seconds: int, value: str) -> bool:
        expires_at = datetime.now() + timedelta(seconds=ttl_seconds)
        self._cache[key] = (value, expires_at)
        return True
    
    def exists(self, key: str) -> int:
        return 1 if key in self._cache and datetime.now() <= self._cache[key][1] else 0
    
    def ttl(self, key: str) -> int:
        if key not in self._cache:
            return -2
        value, expires_at = self._cache[key]
        if datetime.now() > expires_at:
            del self._cache[key]
            return -2
        return int((expires_at - datetime.now()).total_seconds())
    
    def delete(self, key: str) -> int:
        if key in self._cache:
            del self._cache[key]
            return 1
        return 0
    
    def scan(self, cursor: int, match: str):
        pattern = match.replace('*', '')
        keys = [k for k in self._cache.keys() if pattern in k]
        return 0, keys


class CacheManagerService:
    """Universal cache manager - works with or without Redis."""

    def __init__(self, redis_client=None):
        self.config = get_settings()
        
        # Try Redis first, fallback to in-memory
        if redis_client:
            self.redis = redis_client
            self.cache_type = "redis"
        elif self.config.redis_enabled:
            try:
                from redis import Redis
                self.redis = Redis(
                    host=self.config.redis_host,
                    port=self.config.redis_port,
                    db=self.config.redis_db,
                    password=self.config.redis_password,
                    decode_responses=True,
                    socket_connect_timeout=2,
                    socket_timeout=2,
                )
                self.redis.ping()
                self.cache_type = "redis"
                logger.info("✅ Redis cache connected")
            except Exception as e:
                logger.warning(f"⚠️ Redis unavailable, using in-memory cache: {e}")
                self.redis = InMemoryCache()
                self.cache_type = "in-memory"
        else:
            logger.info("ℹ️ Redis disabled, using in-memory cache")
            self.redis = InMemoryCache()
            self.cache_type = "in-memory"
        
        self.cache_key_prefix = "download:audio:"
        logger.info(f"Cache service initialized ({self.cache_type})")

    def _get_cache_key(self, video_id: str) -> str:
        return f"{self.cache_key_prefix}{video_id}"

    async def get_cached_audio(self, video_id: str) -> Optional[AudioFile]:
        try:
            cache_key = self._get_cache_key(video_id)
            cached_data = self.redis.get(cache_key)

            if not cached_data:
                return None

            audio_dict = json.loads(cached_data)
            audio_file = AudioFile(**audio_dict)
            logger.debug(f"Cache HIT: {video_id}")
            return audio_file

        except Exception as e:
            logger.error(f"Cache get error: {e}")
            return None

    async def set_cached_audio(self, audio_file: AudioFile) -> bool:
        try:
            cache_key = self._get_cache_key(audio_file.video_id)
            audio_dict = audio_file.model_dump(mode="json")
            cached_data = json.dumps(audio_dict)
            ttl_seconds = self.config.cache_ttl_hours * 3600

            result = self.redis.setex(cache_key, ttl_seconds, cached_data)
            logger.debug(f"Cache SET: {audio_file.video_id}")
            return result

        except Exception as e:
            logger.error(f"Cache set error: {e}")
            return False

    async def is_cached(self, video_id: str) -> bool:
        try:
            cache_key = self._get_cache_key(video_id)
            return self.redis.exists(cache_key) > 0
        except Exception as e:
            logger.error(f"Cache exists check error: {e}")
            return False

    async def get_cache_ttl(self, video_id: str) -> int:
        try:
            cache_key = self._get_cache_key(video_id)
            return self.redis.ttl(cache_key)
        except Exception as e:
            logger.error(f"Cache TTL error: {e}")
            return -2

    async def delete_cache(self, video_id: str) -> bool:
        try:
            cache_key = self._get_cache_key(video_id)
            result = self.redis.delete(cache_key)
            logger.debug(f"Cache DELETE: {video_id}")
            return result > 0
        except Exception as e:
            logger.error(f"Cache delete error: {e}")
            return False

    async def get_all_cached_video_ids(self) -> list[str]:
        try:
            video_ids = []
            cursor = 0

            while True:
                cursor, keys = self.redis.scan(cursor, match=f"{self.cache_key_prefix}*")
                for key in keys:
                    video_id = key.replace(self.cache_key_prefix, "")
                    video_ids.append(video_id)

                if cursor == 0:
                    break

            return video_ids

        except Exception as e:
            logger.error(f"Cache scan error: {e}")
            return []

    async def cleanup_expired_cache(self) -> int:
        logger.info("Starting cache cleanup...")
        cleaned_count = 0

        try:
            video_ids = await self.get_all_cached_video_ids()

            for video_id in video_ids:
                ttl = await self.get_cache_ttl(video_id)
                if ttl == -2:
                    await self.delete_cache(video_id)
                    cleaned_count += 1

            logger.info(f"Cache cleanup completed: {cleaned_count} items removed")
            return cleaned_count

        except Exception as e:
            logger.error(f"Cache cleanup error: {e}")
            return 0
