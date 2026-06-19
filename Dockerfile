services:
  yt-music-api:
    build:
      context: .
      dockerfile: Dockerfile
    container_name: yt-music-api
    ports:
      - "8000:8000"
    environment:
      - CLOUDINARY_ACCOUNTS_JSON=${CLOUDINARY_ACCOUNTS_JSON}
      - REDIS_ENABLED=false
      - DOWNLOAD_DIR=/tmp/youtube_audio
      - MAX_VIDEO_DURATION=600
      - AUDIO_BITRATE=128
      - API_HOST=0.0.0.0
      - API_PORT=8000
      - LOG_LEVEL=INFO
    volumes:
      - youtube_audio:/tmp/youtube_audio
      - logs:/app/logs
    healthcheck:
      test: ["CMD", "curl", "-f", "http://localhost:8000/health"]
      interval: 30s
      timeout: 10s
      retries: 3
      start_period: 60s
    restart: unless-stopped
    deploy:
      resources:
        limits:
          memory: 2G
        reservations:
          memory: 512M

volumes:
  youtube_audio:
    name: youtube_audio_cache
  logs:
    name: yt_music_logs
