FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    ODOO_MCP_HOST=0.0.0.0 \
    ODOO_MCP_PORT=8000

WORKDIR /app
COPY pyproject.toml ./
COPY wansoft_mcp_cliente ./wansoft_mcp_cliente
RUN pip install --no-cache-dir . && useradd --create-home --uid 1000 mcp

USER mcp
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=4)"

CMD ["wansoft-mcp-cliente"]
