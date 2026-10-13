FROM python:3.11-alpine

# Instalar dependencias del sistema y crear usuario no privilegiado
RUN apk add --no-cache ffmpeg curl && \
    addgroup -g 1000 spotifysaver && \
    adduser -D -s /bin/sh -u 1000 -G spotifysaver spotifysaver

ENV PYTHONUNBUFFERED=1
ENV PIP_NO_CACHE_DIR=1

WORKDIR /app

# Copiar solo archivos necesarios primero (mejor cache de layers)
COPY requirements.txt pyproject.toml ./

# Instalar dependencias Python
RUN pip install --no-cache-dir -r requirements.txt && rm -rf ~/.cache/pip

# Copiar código fuente
COPY --chown=spotifysaver:spotifysaver . .

# Instalar la aplicación
RUN pip install . && \
    mkdir -p /music /videos /config /app/logs && \
    touch /config/cookies.txt && \
    chown -R spotifysaver:spotifysaver /music /videos /config /app/logs

ENV SPOTIFYSAVER_OUTPUT_DIR="/music" \
    SPOTIFYSAVER_VIDEO_DIR="/videos" \
    SPOTIFYSAVER_CHANNEL_WATCHER_DB="/config/channel-watcher.sqlite3" \
    YTDLP_COOKIES_PATH="/config/cookies.txt" \
    SPOTIFY_AUTH_CACHE_PATH="/config/.spotify-saver/.spotify-cache" \
    SPOTIFYSAVER_AUTO_OPEN_BROWSER="false" \
    SPOTIFYSAVER_UI_HOST="0.0.0.0"

# Cambiar a usuario no privilegiado
USER spotifysaver

# Healthcheck
HEALTHCHECK --interval=30s --timeout=10s --start-period=60s --retries=3 \
    CMD curl -f http://localhost:8000/health || exit 1

# Exponer puertos
EXPOSE 8000

# Run without the development reloader in the container.
CMD ["uvicorn", "spotifysaver.api.main:app", "--host", "0.0.0.0", "--port", "8000"]