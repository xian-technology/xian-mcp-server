# Use Python slim image
FROM python:3.11-slim

# Set working directory
WORKDIR /app

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

# Install dependencies and console entry points
RUN pip install --no-cache-dir .

# Create non-root user
RUN useradd -m -u 1000 mcpuser && \
    chown -R mcpuser:mcpuser /app

# Switch to non-root user
USER mcpuser

# Default: stdio MCP mode. Override with "xian-mcp-http" for HTTP.
CMD ["xian-mcp-server"]
