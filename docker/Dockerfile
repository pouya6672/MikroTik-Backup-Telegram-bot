# syntax=docker/dockerfile:1
ARG PYTHON_VERSION=3.12
FROM python:${PYTHON_VERSION}-slim

# ---- Python / pip -------------------------------------------------------------
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# ---- Bot settings (defaults = same as bot.py; override with -e / --env-file / compose) ----
# Required at runtime, deliberately NOT set here (never bake secrets into an image):
#   BOT_TOKEN   : token from @BotFather
#   FERNET_KEY  : optional; if empty a key is generated in DATA_DIR/.secret.key
ENV DATA_DIR=/app/data \
    ALLOWED_USERS="" \
    TRUSTED_USERS="" \
    MAX_JOBS_PER_USER=5 \
    MIN_INTERVAL_MINUTES=10 \
    MAX_PARALLEL_BACKUPS=3 \
    ALLOW_PRIVATE_IPS=false \
    RETRY_ATTEMPTS=3 \
    RETRY_DELAY_MINUTES=5 \
    TEST_LIMIT_PER_HOUR=10 \
    TARGET_LIMIT_PER_10MIN=5 \
    CONN_FAIL_MIN_SECONDS=20 \
    HIDE_CONN_ERRORS=auto

# gosu: entrypoint fixes volume permissions as root, then drops to the "bot" user
RUN apt-get update \
    && apt-get install -y --no-install-recommends gosu \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --system --uid 10001 --create-home --shell /usr/sbin/nologin bot

WORKDIR /app

COPY requirements.txt .
RUN pip install -r requirements.txt

COPY bot.py i18n.py ./
COPY docker/entrypoint.sh /usr/local/bin/entrypoint.sh
RUN chmod +x /usr/local/bin/entrypoint.sh \
    && mkdir -p /app/data && chown -R bot:bot /app

# SQLite DB + .secret.key live here: always mount a volume!
VOLUME ["/app/data"]

ENTRYPOINT ["entrypoint.sh"]
CMD ["python", "bot.py"]
