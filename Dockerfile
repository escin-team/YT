# ============================================
# UNIVERSAL DOCKERFILE
# Compatible: Back4App, Defang, HF Spaces, Railway, Render, Fly.io
# ============================================

FROM python:3.12-slim

WORKDIR /app

# Install system dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    ffmpeg \
    git \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/*

# Install Bun (untuk yt-dlp)
RUN curl -fsSL https://bun.sh/install | bash
ENV PATH="/root/.bun/bin:${PATH}"
ENV BUN_INSTALL="/root/.bun"

# Install uv (Python package manager)
RUN curl -LsSf https://astral.sh/uv/install.sh | sh
ENV PATH="/root/.local/bin:${PATH}"

# Copy dependencies
COPY pyproject.toml uv.lock ./

# Install Python dependencies
RUN uv sync --frozen --no-dev

# Copy application
COPY src/ ./src/
COPY main.py ./

# Create directories
RUN mkdir -p /tmp/youtube_audio logs

# Health check
HEALTHCHECK --interval=30s --timeout=10s --start-period=60s --retries=3 \
    CMD curl -f http://localhost:${PORT:-8080}/health || exit 1

# Expose port (akan di-override oleh environment variable)
EXPOSE 8080

# Run application (auto-detect PORT dari environment)
CMD ["sh", "-c", "uv run uvicorn main:app --host 0.0.0.0 --port ${PORT:-8080}"]
