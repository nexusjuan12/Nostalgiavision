FROM python:3.13-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# ffmpeg turns the schedule into continuous streams for the Jellyfin/IPTV feed (see README).
RUN apt-get update \
 && apt-get install -y --no-install-recommends ffmpeg \
 && rm -rf /var/lib/apt/lists/*

# Run as an unprivileged user. The UID/GID default to 1000 so files written to the
# bind-mounted ./data stay owned by a typical host user; override with
# --build-arg APP_UID=... APP_GID=... if yours differs.
ARG APP_UID=1000
ARG APP_GID=1000
RUN groupadd --gid "${APP_GID}" app \
 && useradd --uid "${APP_UID}" --gid app --no-create-home --shell /usr/sbin/nologin app

WORKDIR /app

# Dependencies first so this layer is cached across code changes.
COPY requirements.txt .
RUN pip install -r requirements.txt

COPY --chown=app:app . .

# data/ holds all runtime state (SQLite DB, channel logos and config.json). Mount a
# volume there so it survives the container (see docker-compose.yml).
RUN mkdir data && chown app:app data
USER app

# Container contract: always listen on 0.0.0.0:5000 whatever config.json says, and keep
# config.json in the data volume. It is optional: PLEX_URL, PLEX_TOKEN, LIBRARY_IDS,
# SCHEDULE_DAYS_AHEAD and PUBLIC_URL can be passed as environment variables instead.
ENV NOSTALGIAVISION_HOST=0.0.0.0 \
    NOSTALGIAVISION_PORT=5000 \
    NOSTALGIAVISION_CONFIG=/app/data/config.json
EXPOSE 5000

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:5000/', timeout=4)"]

# start.py is the desktop launcher and tries to open a browser, so it isn't used here.
CMD ["python", "app.py"]
