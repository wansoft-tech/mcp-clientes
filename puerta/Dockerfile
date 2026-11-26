FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app
COPY pyproject.toml ./
COPY puerta_wansoft ./puerta_wansoft
RUN pip install --no-cache-dir . && useradd --create-home --uid 1000 mcp && mkdir -p /data && chown mcp /data

USER mcp
EXPOSE 8000
# Chequeo liviano: solo abre el puerto, sin lanzar Python. Espaciado, con
# margen y varios reintentos: con la CPU del servidor saturada, un chequeo
# pesado y apurado marcaba sanos como caídos y los reiniciaba en cadena.
HEALTHCHECK --interval=5m --timeout=30s --start-period=2m --retries=3 \
    CMD ["bash", "-c", "exec 3<>/dev/tcp/127.0.0.1/8000"]

CMD ["puerta-wansoft"]
