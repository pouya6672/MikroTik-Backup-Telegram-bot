#!/bin/sh
# Entrypoint for MikroTik Backup Bot. All configuration comes from environment variables.
set -eu

# Defaults (mirror the Dockerfile / bot.py, so the script also works outside this image)
: "${DATA_DIR:=/app/data}"
: "${ALLOWED_USERS:=}"
: "${TRUSTED_USERS:=}"
: "${MAX_JOBS_PER_USER:=5}"
: "${MIN_INTERVAL_MINUTES:=10}"
: "${MAX_PARALLEL_BACKUPS:=3}"
: "${ALLOW_PRIVATE_IPS:=false}"
: "${RETRY_ATTEMPTS:=3}"
: "${RETRY_DELAY_MINUTES:=5}"
: "${TEST_LIMIT_PER_HOUR:=10}"
: "${TARGET_LIMIT_PER_10MIN:=5}"
: "${CONN_FAIL_MIN_SECONDS:=20}"
: "${HIDE_CONN_ERRORS:=auto}"
export DATA_DIR ALLOWED_USERS TRUSTED_USERS MAX_JOBS_PER_USER MIN_INTERVAL_MINUTES \
       MAX_PARALLEL_BACKUPS ALLOW_PRIVATE_IPS TEST_LIMIT_PER_HOUR TARGET_LIMIT_PER_10MIN \
       CONN_FAIL_MIN_SECONDS HIDE_CONN_ERRORS RETRY_ATTEMPTS RETRY_DELAY_MINUTES

die()  { echo "[entrypoint] ERROR: $*" >&2; exit 1; }
warn() { echo "[entrypoint] WARNING: $*" >&2; }
info() { echo "[entrypoint] $*"; }

is_int()   { case "$2" in ''|*[!0-9]*) die "$1 must be a non-negative integer (got '$2')";; esac; }
is_num()   { case "$2" in ''|*[!0-9.]*|*.*.*) die "$1 must be a number (got '$2')";; esac; }
is_ids()   { v=$(printf '%s' "$2" | tr -d ' ')
             case "$v" in *[!0-9,]*) die "$1 must be comma-separated numeric Telegram IDs (got '$2')";; esac; }

# ---- required ----------------------------------------------------------------
[ -n "${BOT_TOKEN:-}" ] || die "BOT_TOKEN is not set (use -e BOT_TOKEN=... or --env-file .env)"

# ---- validate variables ------------------------------------------------------
is_ids ALLOWED_USERS "$ALLOWED_USERS"
is_ids TRUSTED_USERS "$TRUSTED_USERS"
is_int MAX_JOBS_PER_USER     "$MAX_JOBS_PER_USER"
is_int MIN_INTERVAL_MINUTES  "$MIN_INTERVAL_MINUTES"
is_int MAX_PARALLEL_BACKUPS  "$MAX_PARALLEL_BACKUPS"
is_int TEST_LIMIT_PER_HOUR   "$TEST_LIMIT_PER_HOUR"
is_int TARGET_LIMIT_PER_10MIN "$TARGET_LIMIT_PER_10MIN"
is_int RETRY_ATTEMPTS        "$RETRY_ATTEMPTS"
is_int RETRY_DELAY_MINUTES   "$RETRY_DELAY_MINUTES"
is_num CONN_FAIL_MIN_SECONDS "$CONN_FAIL_MIN_SECONDS"

case "$(printf '%s' "$ALLOW_PRIVATE_IPS" | tr 'A-Z' 'a-z')" in
    true|false) ;; *) die "ALLOW_PRIVATE_IPS must be true or false (got '$ALLOW_PRIVATE_IPS')";;
esac
case "$(printf '%s' "$HIDE_CONN_ERRORS" | tr 'A-Z' 'a-z')" in
    auto|true|false) ;; *) die "HIDE_CONN_ERRORS must be auto, true or false (got '$HIDE_CONN_ERRORS')";;
esac

if [ -n "${FERNET_KEY:-}" ]; then
    python - <<'PY' || die "FERNET_KEY is invalid. Generate one: python3 -c \"from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())\""
import os, sys
from cryptography.fernet import Fernet
try:
    Fernet(os.environ["FERNET_KEY"].encode())
except Exception:
    sys.exit(1)
PY
else
    warn "FERNET_KEY is empty: a key will be generated in ${DATA_DIR}/.secret.key. Back up the data volume, or saved passwords are lost."
fi

[ -n "$(printf '%s' "$ALLOWED_USERS" | tr -d ' ,')" ] \
    || warn "ALLOWED_USERS is empty: the bot is open to everyone. Set your numeric Telegram ID(s)."

# ---- data directory permissions ---------------------------------------------
mkdir -p "$DATA_DIR"

if [ "$(id -u)" = "0" ]; then
    chown -R bot:bot "$DATA_DIR" 2>/dev/null \
        || warn "could not chown ${DATA_DIR}; the bot may fail to write its database"
    chmod 700 "$DATA_DIR" 2>/dev/null || true
    info "starting as user 'bot'"
    exec gosu bot "$@"
fi

[ -w "$DATA_DIR" ] || die "${DATA_DIR} is not writable by uid $(id -u)"
info "starting as uid $(id -u)"
exec "$@"
