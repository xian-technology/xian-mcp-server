# Use Python slim image
FROM python:3.14-slim AS base

# Set working directory
WORKDIR /app

COPY --from=ghcr.io/astral-sh/uv:0.13.7 /uv /uvx /bin/

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
COPY dex_tools.py .
COPY dex_plan_registry.py .
COPY xian_server.py xian_connection.py logging_setup.py ./
COPY http_server.py .
COPY scripts/smoke_stdio.py scripts/
COPY serialization.py .
COPY tool_policy.py .

FROM base AS local

# Local multi-repo builds use the sibling SDK sources before they are published.
COPY --from=xian_runtime_types pyproject.toml README.md /tmp/build/xian-runtime-types/
COPY --from=xian_runtime_types src /tmp/build/xian-runtime-types/src
COPY --from=xian_accounts pyproject.toml README.md /tmp/build/xian-accounts/
COPY --from=xian_accounts src /tmp/build/xian-accounts/src
COPY --from=xian_py pyproject.toml README.md /tmp/build/xian-py/
COPY --from=xian_py src /tmp/build/xian-py/src

RUN uv --no-config pip install --system --no-cache --no-sources \
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

# Reproduce the standalone CLI and CI environment, including the pinned SDK sources.
COPY uv.lock .
RUN apt-get update && apt-get install -y --no-install-recommends git \
    && rm -rf /var/lib/apt/lists/*
ENV PATH="/app/.venv/bin:${PATH}"
RUN uv sync --frozen --no-dev --no-editable --no-cache

# Create non-root user
RUN useradd -m -u 1000 mcpuser && \
    chown -R mcpuser:mcpuser /app

# Switch to non-root user
USER mcpuser

# Default: stdio MCP mode. Override with "xian-mcp-http" for HTTP.
CMD ["xian-mcp-server"]
