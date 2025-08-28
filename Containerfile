# Multi-stage build for smaller image
# Stage 1: Builder
FROM python:3.12-slim as builder

WORKDIR /opt/app-root

# Install build dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    g++ \
    && rm -rf /var/lib/apt/lists/*

# Copy dependency files
COPY pyproject.toml uv.lock README.md ./

# Install uv and create virtual environment with dependencies
RUN pip install --no-cache-dir uv && \
    uv venv .venv && \
    uv sync --frozen --no-dev

# Stage 2: Runtime
FROM python:3.12-slim

WORKDIR /opt/app-root

# Install only runtime dependencies (curl for health checks)
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    && rm -rf /var/lib/apt/lists/*

# Copy virtual environment from builder
COPY --from=builder /opt/app-root/.venv .venv

# Copy application code and config files
COPY app ./app
COPY pyproject.toml uv.lock README.md ./

# Set PATH to use venv
ENV PATH="/opt/app-root/.venv/bin:$PATH"
ENV PYTHONPATH="/opt/app-root:$PYTHONPATH"

# Create non-root user
RUN useradd -m -u 1001 appuser && \
    chown -R appuser:appuser /opt/app-root

USER 1001

EXPOSE 10000

# Health check
HEALTHCHECK --interval=30s --timeout=10s --start-period=40s --retries=3 \
    CMD curl -f http://localhost:10000/.well-known/agent.json || exit 1

# Run the agent directly with python
CMD ["python", "-m", "app", "--host", "0.0.0.0"]