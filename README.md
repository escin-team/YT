---
title: YouTube Music Streaming API
emoji: 🎵
colorFrom: purple
colorTo: blue
sdk: docker
pinned: false
license: mit
app_port: 7860
---

# 🎵 YouTube Music Streaming API

API backend untuk Music Player Streaming (YouTube to MP3).

## 🚀 Environment Variables (WAJIB di-set di HF Settings)

| Variable | Value | Deskripsi |
|----------|-------|-----------|
| `CLOUDINARY_ACCOUNTS_JSON` | JSON Array | 3 akun Cloudinary |
| `REDIS_ENABLED` | `false` | Disable Redis |
| `DOWNLOAD_DIR` | `/tmp/youtube_audio` | Temp directory |
| `MAX_VIDEO_DURATION` | `600` | Max 10 menit |
| `AUDIO_BITRATE` | `128` | 128kbps |

## 🔌 API Endpoints

- `POST /api/v1/search-and-prefetch` - Search & auto-cache
- `POST /api/v1/download/audio` - Download MP3
- `GET /health` - Health check
- `GET /api/docs` - Swagger UI
## API Usage Examples

### Basic Search

```bash
curl "http://localhost:8000/api/v1/search?keyword=Python tutorial"
```

### Specify Result Count

```bash
curl "http://localhost:8000/api/v1/search?keyword=Python&limit=5"
```

### Sort by Date

```bash
curl "http://localhost:8000/api/v1/search?keyword=Python&sort_by=date&limit=3"
```

## Audio Download Feature

Download YouTube videos as MP3 audio files with automatic caching and cleanup.

### Download Features

- ✅ Download YouTube videos as 128kbps MP3 files
- ✅ Support for single and batch downloads
- ✅ Redis-based caching to avoid duplicate downloads
- ✅ Automatic cleanup of expired files (24-hour TTL)
- ✅ Rate limiting: 20 downloads per IP per hour
- ✅ Video duration limit: maximum 10 minutes
- ✅ Direct streaming or download link format

### Download API Examples

#### Single Video Download (Link Format)

```bash
# Get download link
curl -X POST "http://localhost:8000/api/v1/download/audio?video_id=dQw4w9WgXcQ&format=link"

# Response
{
  "video_id": "dQw4w9WgXcQ",
  "title": "Rick Astley - Never Gonna Give You Up",
  "duration": 212,
  "download_url": "http://localhost:8000/downloads/dQw4w9WgXcQ_Rick_Astley.mp3",
  "cached": false,
  "file_size": 3400000
}
```

#### Single Video Download (Stream Format)

```bash
# Direct MP3 stream - returns binary audio data
curl -X POST "http://localhost:8000/api/v1/download/audio?video_id=dQw4w9WgXcQ&format=stream" -o output.mp3
```

#### Batch Download

```bash
# Download multiple videos at once (max 20)
curl -X POST "http://localhost:8000/api/v1/download/batch" \
  -H "Content-Type: application/json" \
  -d '{
    "video_ids": ["dQw4w9WgXcQ", "jNQXAC9IVRw"]
  }'

# Response
{
  "total": 2,
  "successful": 1,
  "failed": 1,
  "items": [
    {
      "video_id": "dQw4w9WgXcQ",
      "status": "success",
      "download_url": "http://localhost:8000/downloads/...",
      "duration": 212,
      "cached": false
    },
    {
      "video_id": "jNQXAC9IVRw",
      "status": "failed",
      "error_message": "Video duration exceeds limit"
    }
  ]
}
```

### Configuration

Set these environment variables to customize download behavior:

```bash
# Download storage directory
DOWNLOAD_DIR=/tmp/youtube_audio

# Base URL for serving downloads (used in download links)
DOWNLOAD_BASE_URL=http://localhost:8000/downloads

# Download timeout in seconds
DOWNLOAD_TIMEOUT=300

# Maximum video duration in seconds (default: 600 = 10 minutes)
MAX_VIDEO_DURATION=600

# Audio bitrate in kbps
AUDIO_BITRATE=128

# Cache TTL in hours
CACHE_TTL_HOURS=24

# Rate limit: downloads per IP per hour
RATE_LIMIT_DOWNLOAD_PER_HOUR=20

# Enable rate limiting
RATE_LIMIT_ENABLED=true
```

### Error Handling

The API returns appropriate HTTP status codes:

- `400` - Invalid video ID or parameters
- `403` - Video too long, live stream, or access restricted
- `404` - Video not found or deleted
- `503` - Download failed or YouTube unavailable
- `507` - Server storage full

### Cleanup

Expired audio files are automatically deleted after 24 hours. Manual cleanup can be triggered:

```bash
# Manual cleanup script
python scripts/cleanup_cron.py

# Schedule with cron (daily at 2 AM)
0 2 * * * /usr/bin/python /path/to/scripts/cleanup_cron.py >> /path/to/logs/cleanup.log 2>&1
```

## MCP Integration (Model Context Protocol)

This service provides MCP server functionality, allowing AI assistants (such as Claude Desktop) to directly invoke YouTube search tools via the MCP protocol.

### MCP Service Types

- **HTTP Service**: Provided via StreamableHTTPSessionManager, integrated with existing FastAPI application
- **Supported Transport Modes**: HTTP (MVP)
- **Future Iterations**: Consider supporting stdio and SSE modes

### REST API and MCP Coexistence

- **REST API Preservation**: Existing REST API endpoints (`/api/v1/search`, etc.) remain fully functional
- **Flexible Deployment**: Choose between integrated (same FastAPI app) or separate (independent process) deployment
- **Backward Compatibility**: MCP additions do not modify any existing REST API signatures or behaviors

### MCP Configuration

#### Claude Desktop Setup

Add the following to Claude Desktop's configuration file (`~/.config/claude/settings.json` or `%APPDATA%\Claude\settings.json`):

```json
{
  "servers": {
    "youtube-search-mcp": {
      "command": "uv",
      "args": ["run", "python", "mcp_stdio.py"],
      "env": {
        "MCP_SEARCH_TIMEOUT": "15",
        "MCP_SEARCH_RETRIES": "3",
        "PYTHONUNBUFFERED": "1"
      }
    }
  }
}
```

#### Environment Variables

- `MCP_SEARCH_TIMEOUT` (default: 15 seconds): Search operation timeout
- `MCP_SEARCH_RETRIES` (default: 3 times): Retry attempts on search failure
- `REDIS_HOST`, `REDIS_PORT`, `REDIS_DB`: Redis cache configuration (optional)

### Starting the MCP Server

```bash
# Start MCP server (stdio mode)
python mcp_stdio.py

# Or using uv
uv run python mcp_stdio.py
```

### MCP Tool: youtube_search

**Tool Name**: `youtube_search`

**Description**: Search YouTube videos and return complete metadata

**Parameters**:

| Parameter | Type | Required | Default | Description |
|-----------|------|----------|---------|-------------|
| `keyword` | string | ✅ | - | Search keyword (1-200 characters) |
| `limit` | integer | ❌ | 1 | Number of results to return (1-100) |
| `sort_by` | string | ❌ | relevance | Sort order: `relevance` or `date` |

**Response Format**:

```json
{
  "videos": [
    {
      "video_id": "mIF-nn_y2_8",
      "title": "Jackie Cheung - Farewell Kiss",
      "channel": "Music Without Boundaries",
      "url": "https://www.youtube.com/watch?v=mIF-nn_y2_8",
      "channel_url": "https://www.youtube.com/@channel_name",
      "publish_date": "2020-01-15",
      "view_count": "1500000",
      "description": "Classic Cantonese song..."
    }
  ],
  "message": "Successfully returned 1 result"
}
```

**Error Handling**:

- Empty keyword: Returns `{"error": "INVALID_KEYWORD", "message": "Search keyword cannot be empty..."}`
- Invalid limit: Returns `{"error": "INVALID_LIMIT", "message": "limit must be between 1-100..."}`
- YouTube service unavailable: Returns `{"error": "SERVICE_UNAVAILABLE", "message": "YouTube service is temporarily unavailable..."}`
- Cache service failure: Gracefully degrades to direct search and returns normal results

## Environment Configuration

Copy `.env.example` to `.env` and modify as needed:

```bash
cp .env.example .env
```

## Docker Deployment

```bash
docker-compose up -d
```

## Testing

```bash
pytest tests/
```

## Project Structure

```text
src/youtube_search/
├── models/          # Pydantic data models
├── services/        # Business logic layer
├── api/             # API routes
└── utils/           # Utility functions
```

## License

MIT License
