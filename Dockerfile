# Use Python slim image
FROM python:3.14-slim AS base

# Set working directory
WORKDIR /app

COPY --from=ghcr.io/astral-sh/uv:0.6.11 /uv /uvx /bin/

# Set Python unbuffered mode
ENV PYTHONUNBUFFERED=1

# Install build dependencies for compilation
RUN apt-get update && apt-get install -y \
    gcc \
    g++ \
    make \
    libffi-dev \
    && rm -rf /var/lib/apt/lists/*

# Copy project metadata and source
COPY pyproject.toml .
COPY README.md .
COPY xian_server.py .
COPY http_server.py .
COPY serialization.py .

FROM base AS local

# Local multi-repo builds use the sibling SDK sources before they are published.
COPY --from=xian_runtime_types . /tmp/build/xian-runtime-types
COPY --from=xian_accounts . /tmp/build/xian-accounts
COPY --from=xian_py . /tmp/build/xian-py

RUN uv pip install --system --no-cache \
    /tmp/build/xian-runtime-types \
    /tmp/build/xian-accounts \
    "/tmp/build/xian-py[eth,hd]" \
    .

# Create non-root user
RUN useradd -m -u 1000 mcpuser && \
    chown -R mcpuser:mcpuser /app

# Switch to non-root user
USER mcpuser

# Default: stdio MCP mode. Override with "xian-mcp-http" for HTTP.
CMD ["xian-mcp-server"]

FROM base AS pypi

# Install dependencies and console entry points from published packages.
RUN uv pip install --system --no-cache .

# Create non-root user
RUN useradd -m -u 1000 mcpuser && \
    chown -R mcpuser:mcpuser /app

# Switch to non-root user
USER mcpuser

# Default: stdio MCP mode. Override with "xian-mcp-http" for HTTP.
CMD ["xian-mcp-server"]
