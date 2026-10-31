FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PROVISIONER_PORT=8000

WORKDIR /app
COPY pyproject.toml ./
COPY provisioner ./provisioner
RUN pip install --no-cache-dir . && useradd --create-home --uid 1000 provisioner

USER provisioner
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=4)"

CMD ["wansoft-mcp-provisioner"]
