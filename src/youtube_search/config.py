"""Configuration management with auto-detection."""

import os
import json
import logging
from typing import List, Dict, Any
from functools import lru_cache

from pydantic_settings import BaseSettings
from pydantic import Field

logger = logging.getLogger(__name__)


class Settings(BaseSettings):
    """Application settings with sensible defaults."""
    
    # Server
    port: int = Field(default=8080, env="PORT")
    api_host: str = Field(default="0.0.0.0", env="API_HOST")
    api_port: int = Field(default=8080, env="API_PORT")
    log_level: str = Field(default="INFO", env="LOG_LEVEL")
    
    # Cloudinary
    cloudinary_accounts_json: str = Field(default="[]", env="CLOUDINARY_ACCOUNTS_JSON")
    
    # Redis
    redis_enabled: bool = Field(default=False, env="REDIS_ENABLED")
    redis_host: str = Field(default="localhost", env="REDIS_HOST")
    redis_port: int = Field(default=6379, env="REDIS_PORT")
    redis_db: int = Field(default=0, env="REDIS_DB")
    redis_password: str = Field(default="", env="REDIS_PASSWORD")
    
    # Application
    download_dir: str = Field(default="/tmp/youtube_audio", env="DOWNLOAD_DIR")
    max_video_duration: int = Field(default=600, env="MAX_VIDEO_DURATION")
    audio_bitrate: int = Field(default=128, env="AUDIO_BITRATE")
    cache_ttl_hours: int = Field(default=24, env="CACHE_TTL_HOURS")
    
    class Config:
        env_file = ".env"
        case_sensitive = False
    
    @property
    def cloudinary_accounts(self) -> List[Dict[str, Any]]:
        """Parse Cloudinary accounts from JSON."""
        try:
            accounts = json.loads(self.cloudinary_accounts_json)
            if isinstance(accounts, list) and len(accounts) > 0:
                logger.info(f"Loaded {len(accounts)} Cloudinary accounts")
                return accounts
        except json.JSONDecodeError as e:
            logger.error(f"Failed to parse CLOUDINARY_ACCOUNTS_JSON: {e}")
        except Exception as e:
            logger.error(f"Error loading Cloudinary accounts: {e}")
        
        logger.warning("No valid Cloudinary accounts found")
        return []
    
    def validate(self) -> bool:
        """Validate critical settings."""
        if not self.cloudinary_accounts:
            logger.error("CLOUDINARY_ACCOUNTS_JSON is required but not set")
            return False
        
        logger.info("Configuration validated successfully")
        return True


@lru_cache()
def get_settings() -> Settings:
    """Get cached settings instance."""
    settings = Settings()
    
    # Auto-detect PORT from common platform env vars
    for env_var in ["PORT", "SERVER_PORT", "APPLICATION_PORT"]:
        if env_var in os.environ:
            try:
                settings.port = int(os.environ[env_var])
                settings.api_port = settings.port
                logger.info(f"Auto-detected port: {settings.port} from {env_var}")
                break
            except (ValueError, TypeError):
                pass
    
    return settings
