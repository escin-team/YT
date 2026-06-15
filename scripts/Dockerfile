# Use Python 3.12 slim base image
FROM python:3.12-slim

# Set working directory
WORKDIR /app

# Install system dependencies (ffmpeg WAJIB untuk konversi ke MP3)
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
        ffmpeg \
            && apt-get clean \
                && rm -rf /var/lib/apt/lists/*

                # Install uv (package manager modern)
                RUN curl -LsSf https://astral.sh/uv/install.sh | sh
                ENV PATH="/root/.local/bin:${PATH}"

                # Copy dependency files
                COPY pyproject.toml uv.lock ./

                # Install dependencies
                RUN uv sync --frozen --no-dev

                # Copy application source
                COPY src/ ./src/
                COPY main.py ./

                # Expose API port (Render akan otomatis menimpa dengan $PORT)
                EXPOSE 8000

                # Health check (untuk Render agar tahu server sudah siap)
                HEALTHCHECK --interval=30s --timeout=10s --start-period=30s --retries=3 \
                    CMD curl -f http://localhost:8000/health || exit 1

                    # Run application
                    # Render akan otomatis men-set PORT, jadi kita gunakan variabel $PORT
                    CMD ["sh", "-c", "uv run uvicorn main:app --host 0.0.0.0 --port ${PORT:-8000}"]