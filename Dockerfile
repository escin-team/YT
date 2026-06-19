# Dockerfile untuk YouTube Music Streaming API
# Compatible dengan SnapDeploy, Defang.io, Hugging Face Spaces

FROM python:3.12-slim

WORKDIR /app

# Install system dependencies (ffmpeg WAJIB untuk konversi MP3)
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    ffmpeg \
    git \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/*

# Install Bun (JavaScript runtime untuk yt-dlp, lebih ringan dari Node.js)
RUN curl -fsSL https://bun.sh/install | bash
ENV PATH="/root/.bun/bin:${PATH}"

# Install uv (modern Python package manager)
RUN curl -LsSf https://astral.sh/uv/install.sh | sh
ENV PATH="/root/.local/bin:${PATH}"

# Copy dependency files
COPY pyproject.toml uv.lock ./

# Install dependencies
RUN uv sync --frozen --no-dev

# Copy application source
COPY src/ ./src/
COPY main.py ./

# Create necessary directories
RUN mkdir -p /tmp/youtube_audio logs

# Expose port (SnapDeploy default: 8000)
EXPOSE 8000

# Health check untuk monitoring
HEALTHCHECK --interval=30s --timeout=10s --start-period=60s --retries=3 \
    CMD curl -f http://localhost:8000/health || exit 1

# Run application dengan support PORT environment variable
CMD ["sh", "-c", "uv run uvicorn main:app --host 0.0.0.0 --port ${PORT:-8000}"]
