#!/usr/bin/env python3
"""
MikroTik Backup Telegram Bot
----------------------------
Users add their MikroTik (SSH), choose what to back up (binary backup with/without
password, User Manager DB, or both), optionally a ZIP archive (with or without a password),
the folder on the router used for the temporary files, a destination (PV / group / channel), an interval and their time zone.
The bot connects over SSH, creates the backup on the router, downloads it over SFTP,
deletes it from the router and sends it to Telegram. Every job can be edited later.
"""
import asyncio
import functools
import html
import io
import ipaddress
import logging
import math
import os
import re
import socket
import sqlite3
import sys
import time
import zipfile
from collections import deque
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo, available_timezones

import asyncssh
import pyzipper
from cryptography.fernet import Fernet
from dotenv import load_dotenv
from telegram import InlineKeyboardButton as Btn
from telegram import InlineKeyboardMarkup, Update
from telegram.constants import ChatMemberStatus, ParseMode
from telegram.error import (
    BadRequest,
    ChatMigrated,
    Forbidden,
    InvalidToken,
    NetworkError,
    RetryAfter,
    TelegramError,
)
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    Defaults,
    MessageHandler,
    filters,
)

from i18n import LANGS, t

if sys.version_info < (3, 10):
    raise SystemExit("Python 3.10 or newer is required")

load_dotenv()
logging.basicConfig(format="%(asctime)s %(levelname)s %(name)s: %(message)s", level=logging.INFO)
logging.getLogger("httpx").setLevel(logging.WARNING)  # httpx logs URLs that contain the bot token
logging.getLogger("asyncssh").setLevel(logging.WARNING)
log = logging.getLogger("mtbackup")

# --------------------------------------------------------------------------- config
BOT_TOKEN = os.environ["BOT_TOKEN"]
DATA_DIR = Path(os.getenv("DATA_DIR", "data"))
DATA_DIR.mkdir(parents=True, exist_ok=True)
ALLOWED = {int(x) for x in os.getenv("ALLOWED_USERS", "").replace(" ", "").split(",") if x}
# Whitelist: these users are exempt from the abuse protection below (connection-test rate limits
# and the uniform error message / delay). They still must pass ALLOWED_USERS when that is set.
TRUSTED = {int(x) for x in os.getenv("TRUSTED_USERS", "").replace(" ", "").split(",") if x}
MAX_JOBS = int(os.getenv("MAX_JOBS_PER_USER", "5"))
MIN_INTERVAL_MIN = int(os.getenv("MIN_INTERVAL_MINUTES", "10"))
MAX_INTERVAL_MIN = 60 * 24 * 30
ALLOW_PRIVATE = os.getenv("ALLOW_PRIVATE_IPS", "false").lower() == "true"
MAX_PARALLEL = int(os.getenv("MAX_PARALLEL_BACKUPS", "3"))
MAX_FAILS = 5
# --- automatic retry of a failed scheduled backup (0 attempts = off)
RETRY_ATTEMPTS = max(0, int(os.getenv("RETRY_ATTEMPTS", "3")))        # extra tries after the first failure
RETRY_DELAY_MIN = max(1, int(os.getenv("RETRY_DELAY_MINUTES", "5")))  # wait between tries
# --- abuse protection for connection tests (the bot must not become a port scanner / brute-forcer)
TEST_LIMIT_PER_HOUR = int(os.getenv("TEST_LIMIT_PER_HOUR", "10"))        # per user, 0 = off
TARGET_LIMIT_PER_10MIN = int(os.getenv("TARGET_LIMIT_PER_10MIN", "5"))   # per host:port, all users, 0 = off
CONN_FAIL_MIN_SECONDS = float(os.getenv("CONN_FAIL_MIN_SECONDS", "20"))  # every failed test takes at least this long
_hide = os.getenv("HIDE_CONN_ERRORS", "auto").lower()  # auto = on when ALLOWED_USERS is empty (public bot)
UNIFORM_ERRORS = _hide == "true" or (_hide == "auto" and not ALLOWED)
TG_FILE_LIMIT = 49 * 1024 * 1024  # Telegram bots can upload up to 50 MB
MAX_CMD_OUTPUT = 64 * 1024  # never keep more than this from a router command
TG_UPLOAD_TIMEOUT = 120  # seconds; PTB's default write timeout (20 s) is too short for big backups

POPULAR_TZ = [
    "UTC", "Asia/Tehran", "Asia/Dubai", "Europe/Istanbul", "Europe/Moscow", "Europe/Madrid",
    "Europe/London", "Europe/Berlin", "Asia/Shanghai", "Asia/Kolkata", "America/New_York",
    "America/Mexico_City", "America/Los_Angeles", "America/Sao_Paulo",
]

SEM = asyncio.Semaphore(MAX_PARALLEL)
esc = html.escape


# --------------------------------------------------------------------------- crypto
def _load_fernet() -> Fernet:
    env = os.getenv("FERNET_KEY")
    if env:
        return Fernet(env.encode())
    p = DATA_DIR / ".secret.key"
    if not p.exists():
        try:  # O_EXCL + mode 0600: the key is never world-readable, not even for a moment
            fd = os.open(p, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "wb") as fh:
                fh.write(Fernet.generate_key())
        except FileExistsError:  # another process created it first
            pass
    return Fernet(p.read_bytes().strip())


FERNET = _load_fernet()


def enc(s: str) -> str:
    return FERNET.encrypt(s.encode()).decode()


def dec(s: str) -> str:
    return FERNET.decrypt(s.encode()).decode()


# --------------------------------------------------------------------------- database
db = sqlite3.connect(DATA_DIR / "bot.db", check_same_thread=False)
db.row_factory = sqlite3.Row
db.executescript(
    """
CREATE TABLE IF NOT EXISTS users(
    user_id INTEGER PRIMARY KEY,
    lang TEXT NOT NULL,
    tz TEXT NOT NULL DEFAULT 'UTC'
);
CREATE TABLE IF NOT EXISTS jobs(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    owner_id INTEGER NOT NULL,
    host TEXT NOT NULL,
    port INTEGER NOT NULL,
    username TEXT NOT NULL,
    password_enc TEXT NOT NULL,
    host_key TEXT,
    identity TEXT,
    kind TEXT NOT NULL,            -- backup | um | both
    bk_password_enc TEXT,          -- NULL = unencrypted backup
    zip_enabled INTEGER NOT NULL DEFAULT 0,  -- 1 = send the files inside a ZIP archive
    zip_password_enc TEXT,         -- NULL = ZIP without password (AES-256 when set)
    remote_dir TEXT NOT NULL DEFAULT '',   -- folder on the router ('' = root)
    dest_chat_id INTEGER NOT NULL,
    dest_title TEXT,
    interval_min INTEGER NOT NULL,
    active INTEGER NOT NULL DEFAULT 1,
    fails INTEGER NOT NULL DEFAULT 0,
    last_run INTEGER
);
"""
)


def _ensure_column(table: str, col: str, ddl: str) -> None:
    """Tiny migration helper for databases created by the previous version."""
    cols = {r["name"] for r in db.execute(f"PRAGMA table_info({table})")}
    if col not in cols:
        db.execute(f"ALTER TABLE {table} ADD COLUMN {col} {ddl}")


_ensure_column("users", "tz", "TEXT NOT NULL DEFAULT 'UTC'")
_ensure_column("jobs", "remote_dir", "TEXT NOT NULL DEFAULT ''")
_ensure_column("jobs", "zip_enabled", "INTEGER NOT NULL DEFAULT 0")
_ensure_column("jobs", "zip_password_enc", "TEXT")
db.commit()


def get_lang(uid: int) -> str:
    r = db.execute("SELECT lang FROM users WHERE user_id=?", (uid,)).fetchone()
    return r["lang"] if r else "en"


def has_user(uid: int) -> bool:
    return db.execute("SELECT 1 FROM users WHERE user_id=?", (uid,)).fetchone() is not None


def set_lang(uid: int, lang: str) -> None:
    db.execute(
        "INSERT INTO users(user_id, lang) VALUES(?,?) ON CONFLICT(user_id) DO UPDATE SET lang=excluded.lang",
        (uid, lang),
    )
    db.commit()


def set_tz(uid: int, tzname: str) -> None:
    db.execute(
        "INSERT INTO users(user_id, lang, tz) VALUES(?, 'en', ?) "
        "ON CONFLICT(user_id) DO UPDATE SET tz=excluded.tz",
        (uid, tzname),
    )
    db.commit()


def get_job(jid: int):
    return db.execute("SELECT * FROM jobs WHERE id=?", (jid,)).fetchone()


def user_jobs(uid: int):
    return db.execute("SELECT * FROM jobs WHERE owner_id=? ORDER BY id", (uid,)).fetchall()


def update_job(jid: int, **fields) -> None:
    cols = ", ".join(f"{k}=?" for k in fields)
    db.execute(f"UPDATE jobs SET {cols} WHERE id=?", (*fields.values(), jid))
    db.commit()


# --------------------------------------------------------------------------- time zones
OFFSET_RE = re.compile(r"^(?:UTC|GMT)?\s*([+-])\s*(\d{1,2})(?::?(\d{2}))?$", re.I)


@functools.lru_cache(maxsize=1)
def _tz_names() -> dict:
    """lower-case IANA name -> canonical name (listed once; the walk is slow)."""
    return {z.lower(): z for z in available_timezones() if z != "localtime"}


def parse_tz(text: str):
    """IANA name or UTC offset -> (canonical_name, tzinfo) or None."""
    s = (text or "").strip()
    if not s:
        return None
    if s.upper() in ("UTC", "GMT", "Z"):
        return "UTC", timezone.utc
    m = OFFSET_RE.match(s)
    if m:
        h, mi = int(m.group(2)), int(m.group(3) or 0)
        if h > 14 or mi > 59:
            return None
        delta = timedelta(hours=h, minutes=mi)
        if m.group(1) == "-":
            delta = -delta
        return f"UTC{m.group(1)}{h:02d}:{mi:02d}", timezone(delta)
    names = _tz_names()
    if names:
        z = names.get(s.lower())
        if z:
            try:
                return z, ZoneInfo(z)
            except (KeyError, ValueError, OSError):
                return None
        return None
    try:  # no tz database could be listed: fall back to a direct lookup
        return s, ZoneInfo(s)
    except (KeyError, ValueError, OSError):
        return None


def get_tz(uid: int):
    r = db.execute("SELECT tz FROM users WHERE user_id=?", (uid,)).fetchone()
    return (r and parse_tz(r["tz"])) or ("UTC", timezone.utc)


def fmt_now(tz) -> str:
    return datetime.now(tz).strftime("%Y-%m-%d %H:%M")


def fmt_offset(tz) -> str:
    minutes = int(datetime.now(tz).utcoffset().total_seconds() // 60)
    sign = "+" if minutes >= 0 else "-"
    h, m = divmod(abs(minutes), 60)
    return f"UTC{sign}{h}" + (f":{m:02d}" if m else "")


def fmt_ts(ts, tzpair) -> str:
    if not ts:
        return ""
    name, tz = tzpair
    return f"{datetime.fromtimestamp(ts, tz):%Y-%m-%d %H:%M} ({name})"


# --------------------------------------------------------------------------- RouterOS / SSH
class RouterError(Exception):
    pass


class NotRouterOS(RouterError):
    """Raised only AFTER a successful login, so it leaks nothing to someone without credentials."""


class SftpDenied(RouterError):
    """The router user has no ftp policy (SFTP refused)."""


class AddressNotAllowed(RouterError):
    """The target is a private/local address (blocked by the SSRF protection)."""


class RateLimited(Exception):
    def __init__(self, wait: float):
        super().__init__("rate limited")
        self.wait = wait


# --------------------------------------------------------------------------- rate limiting
_ATTEMPTS: dict[str, deque] = {}


def rate_check(uid: int, addr: str, port: int) -> None:
    """Record one connection test, or raise RateLimited(seconds_to_wait).

    Two sliding windows: per user (anti port-scan) and per target host:port across ALL users
    (anti brute-force / one target hammered by many accounts). Runs without awaiting, so it
    is atomic in the event loop. Scheduled backups never go through here.
    """
    if uid in TRUSTED:
        return
    rules = []
    if TEST_LIMIT_PER_HOUR > 0:
        rules.append((f"u:{uid}", TEST_LIMIT_PER_HOUR, 3600))
    if TARGET_LIMIT_PER_10MIN > 0:
        rules.append((f"t:{addr.lower()}:{port}", TARGET_LIMIT_PER_10MIN, 600))
    if not rules:
        return
    now = time.monotonic()
    if len(_ATTEMPTS) > 5000:  # drop expired keys so the dict cannot grow forever
        for k in [k for k, dq in _ATTEMPTS.items() if not dq or now - dq[-1] >= 3600]:
            del _ATTEMPTS[k]
    waits = []
    for key, limit, window in rules:
        dq = _ATTEMPTS.setdefault(key, deque())
        while dq and now - dq[0] >= window:
            dq.popleft()
        if len(dq) >= limit:
            waits.append(window - (now - dq[0]))
    if waits:
        raise RateLimited(max(waits))
    for key, _limit, _window in rules:
        _ATTEMPTS[key].append(now)


class LocalizedError(Exception):
    """User-facing error rendered in the viewer's language (see short_err)."""

    def __init__(self, key: str, **kw):
        super().__init__(key)
        self.key, self.kw = key, kw


STORAGE_RE = re.compile(r"^(flash|disk\d*|usb\d*|sd\d*|nvme\d*|raid\d*)$", re.I)
RDIR_PART_RE = re.compile(r"^[A-Za-z0-9_.\- ]+$")


def parse_rdir(text: str):
    """Validate a router folder path. '' = router root, None = invalid."""
    s = (text or "").strip().replace("\\", "/")
    if s in ("", "-", "/", "."):
        return ""
    parts = [p for p in s.split("/") if p.strip()]
    if len(s) > 100 or not parts:
        return None
    for p in parts:
        if p in (".", "..") or p != p.strip() or not RDIR_PART_RE.match(p):
            return None
    return "/".join(parts)


def ros_quote(s: str) -> str:
    """Quote a string for the RouterOS CLI."""
    return '"' + re.sub(r'(["\\$?])', r"\\\1", s) + '"'


async def resolve_target(host: str):
    """SSRF protection. Returns (address_to_connect_to, problem).

    problem is None, 'resolve' or 'private'. The caller must connect to the returned
    address (not to the hostname again), otherwise a DNS answer that changes between
    the check and the connection (DNS rebinding) would bypass the check.
    """
    if ALLOW_PRIVATE:
        return host, None
    try:
        infos = await asyncio.get_running_loop().getaddrinfo(host, None, type=socket.SOCK_STREAM)
    except socket.gaierror:
        return None, "resolve"
    addrs = [info[4][0].split("%")[0] for info in infos]
    if not addrs:
        return None, "resolve"
    for a in addrs:
        if not ipaddress.ip_address(a).is_global:
            return None, "private"
    return addrs[0], None


async def ensure_public(host: str):
    """Returns None if OK, otherwise 'resolve' or 'private'."""
    return (await resolve_target(host))[1]


def ssh_connect(host, port, user, password, host_key=None):
    known = asyncssh.import_known_hosts(f"* {host_key}") if host_key else None
    return asyncssh.connect(
        host,
        port=port,
        username=user,
        password=password,
        known_hosts=known,  # None only on first contact (trust-on-first-use)
        client_keys=None,
        agent_path=None,
        connect_timeout=15,
        login_timeout=20,
        keepalive_interval=15,
    )


async def run_cmd(conn, cmd: str, timeout: int = 90) -> str:
    """Run a command, wait for it to finish, return stdout+stderr (capped: a hostile
    server cannot make us buffer an unlimited amount of output)."""

    async def _go() -> str:
        async with conn.create_process(
            cmd, stderr=asyncssh.STDOUT, encoding="utf-8", errors="replace"
        ) as proc:
            kept, total = [], 0
            while True:
                chunk = await proc.stdout.read(4096)
                if not chunk:
                    break
                if total < MAX_CMD_OUTPUT:
                    kept.append(chunk)
                    total += len(chunk)
            await proc.wait(check=False)
        return "".join(kept)[:MAX_CMD_OUTPUT].strip()

    return await asyncio.wait_for(_go(), timeout)


async def get_info(conn):
    ident = await run_cmd(conn, "/system identity print")
    res = await run_cmd(conn, "/system resource print")
    v = re.search(r"version:\s*(\S+)", res)
    if not v:
        raise NotRouterOS("This does not look like a RouterOS device (or the user lacks permissions)")
    m = re.search(r"name:\s*(.+)", ident)
    return (m.group(1).strip() if m else "MikroTik"), v.group(1)


def ver_tuple(v: str):
    m = re.match(r"(\d+)\.(\d+)", v)
    return (int(m.group(1)), int(m.group(2))) if m else (7, 0)


async def probe(host, port, user, password, host_key=None, uid=None):
    """Test a connection. Returns (identity, version, host_key_line).

    Pass uid for tests started by a user: they are rate limited (see rate_check).
    """
    addr, problem = await resolve_target(host)
    if problem:
        raise RouterError("address not allowed")
    if uid is not None:
        rate_check(uid, addr, port)
    async with ssh_connect(addr, port, user, password, host_key) as conn:
        key = conn.get_server_host_key()
        key_line = key.export_public_key("openssh").decode().strip()
        identity, version = await get_info(conn)
    return identity, version, key_line


async def ensure_remote_dir(conn, path: str) -> None:
    """Create every missing folder of `path` on the router (best effort).

    RouterOS 7 supports `/file add type=directory`. On other versions/storages the
    folder is created implicitly when the file is saved into it; in every case the
    final existence check in pull() decides whether it really worked.
    """
    cur = ""
    for i, part in enumerate(path.split("/")):
        cur = f"{cur}/{part}" if cur else part
        out = await run_cmd(conn, f":put [:len [/file find where name={ros_quote(cur)}]]")
        if out.strip().isdigit() and int(out.strip()) > 0:
            continue  # already exists
        if i == 0 and STORAGE_RE.match(part):
            raise LocalizedError("err_storage", part=part)
        await run_cmd(conn, f"/file add name={ros_quote(cur)} type=directory")


async def pull(sftp, remote: str, cmd_out: str):
    """Wait for a file on the router, download it, delete it. Returns (ext, bytes)."""
    candidates = [remote, "/" + remote]
    path, size = None, 0
    denied = None
    try:
        for _ in range(15):
            for c in candidates:
                try:
                    size = (await sftp.stat(c)).size or 0
                except asyncssh.SFTPPermissionDenied as e:
                    denied, size = e, 0
                except asyncssh.SFTPError:
                    size = 0
                if size > 0:
                    path = c
                    break
            if path:
                break
            await asyncio.sleep(1)
        if not path:
            if denied is not None:  # e.g. the router user has no "ftp" policy
                raise SftpDenied(f"SFTP access denied on the router ({denied}). The user needs the ftp policy.")
            if cmd_out.strip():
                raise LocalizedError("err_not_created", remote=remote, out=cmd_out[:300])
            raise LocalizedError("err_not_created_empty", remote=remote)
        if size > TG_FILE_LIMIT:
            raise LocalizedError("err_too_big", mb=size // 1024 // 1024)
        async with sftp.open(path, "rb") as f:
            data = await f.read(TG_FILE_LIMIT + 1)  # never trust the size the server reported
        if len(data) > TG_FILE_LIMIT:
            raise LocalizedError("err_too_big", mb=len(data) // 1024 // 1024)
        return Path(remote).suffix, data
    finally:
        for c in [path] if path else candidates:
            try:
                await sftp.remove(c)
            except Exception:  # noqa: BLE001
                pass


async def cleanup_remote(sftp, names) -> None:
    """Best-effort removal of temp files left on the router (errors are ignored)."""
    for n in names:
        for c in (n, "/" + n):
            try:
                await asyncio.wait_for(sftp.remove(c), 10)
            except Exception:  # noqa: BLE001
                pass


async def remote_backup(job):
    """Returns (identity, version, files, step_errors).

    Each requested file is attempted on its own: if one of them fails (e.g. the
    user-manager package is missing) the others are still delivered and the failure is
    reported in step_errors. If nothing could be produced the first error is raised.
    """
    addr, problem = await resolve_target(job["host"])
    if problem == "resolve":  # DNS hiccup: worth a retry
        raise LocalizedError("err_dns")
    if problem:
        raise AddressNotAllowed("address not allowed")
    rdir = job["remote_dir"] or ""
    prefix = f"{rdir}/" if rdir else ""
    files, errors, pending = [], [], []
    async with ssh_connect(
        addr, job["port"], job["username"], dec(job["password_enc"]), job["host_key"]
    ) as conn:
        identity, version = await get_info(conn)
        major, minor = ver_tuple(version)
        if rdir:
            await ensure_remote_dir(conn, rdir)
        base = f"tgbk-{int(time.time())}-{job['id']}"
        async with conn.start_sftp_client() as sftp:
            try:
                if job["kind"] in ("backup", "both"):
                    if job["bk_password_enc"]:
                        args = f"password={ros_quote(dec(job['bk_password_enc']))}"
                    elif (major, minor) >= (6, 43):
                        args = "dont-encrypt=yes"
                    else:
                        args = ""
                    name = f"{prefix}{base}.backup"
                    pending.append(name)
                    try:
                        out = await run_cmd(conn, f"/system backup save name={ros_quote(prefix + base)} {args}".strip())
                        files.append(await pull(sftp, name, out))
                    except Exception as e:  # noqa: BLE001
                        errors.append(e)
                if job["kind"] in ("um", "both"):
                    cmd = "/user-manager" if major >= 7 else "/tool user-manager"
                    name = f"{prefix}{base}-um.umb"
                    pending.append(name)
                    try:
                        out = await run_cmd(conn, f"{cmd} database save name={ros_quote(prefix + base + '-um')}")
                        files.append(await pull(sftp, name, out))
                    except Exception as e:  # noqa: BLE001
                        errors.append(e)
            finally:
                # also runs on timeout/cancel: never leave tgbk-* files behind on the router
                await cleanup_remote(sftp, pending)
    if not files:
        raise errors[0] if errors else RouterError("nothing to back up")
    return identity, version, files, errors


# --------------------------------------------------------------------------- helpers
def kb(rows):
    return InlineKeyboardMarkup([[Btn(text, callback_data=data) for text, data in row] for row in rows])


def lang_kb():
    codes = list(LANGS)
    return kb([[(LANGS[c], f"lang:{c}") for c in codes[i : i + 2]] for i in range(0, len(codes), 2)])


def tz_kb(lang: str):
    buttons = []
    for name in POPULAR_TZ:
        p = parse_tz(name)
        if not p:
            continue
        city = name.split("/")[-1].replace("_", " ")
        buttons.append((f"{city} ({fmt_offset(p[1])})" if name != "UTC" else "UTC", f"tz:{name}"))
    rows = [buttons[i : i + 2] for i in range(0, len(buttons), 2)]
    rows.append([(t(lang, "btn_tz_custom"), "tzcustom")])
    return kb(rows)


def interval_kb(lang: str, prefix: str = "int"):
    return kb(
        [
            [(fmt_interval(lang, 60), f"{prefix}:60"), (fmt_interval(lang, 360), f"{prefix}:360"),
             (fmt_interval(lang, 720), f"{prefix}:720")],
            [(fmt_interval(lang, 1440), f"{prefix}:1440"), (fmt_interval(lang, 4320), f"{prefix}:4320"),
             (fmt_interval(lang, 10080), f"{prefix}:10080")],
            [(t(lang, "btn_custom"), f"{prefix}:custom")],
        ]
    )


def fmt_interval(lang: str, m: int) -> str:
    if m % 1440 == 0:
        return t(lang, "unit_d", n=m // 1440)
    if m % 60 == 0:
        return t(lang, "unit_h", n=m // 60)
    return t(lang, "unit_m", n=m)


def parse_interval(text: str):
    m = re.fullmatch(r"(\d{1,6})\s*([mhd]?)", (text or "").strip().lower())
    if not m:
        return None
    n, unit = int(m.group(1)), m.group(2) or "h"
    minutes = n * {"m": 1, "h": 60, "d": 1440}[unit]
    return minutes if MIN_INTERVAL_MIN <= minutes <= MAX_INTERVAL_MIN else None


HOST_RE = re.compile(
    r"^(?:\[(?P<v6>[0-9a-fA-F:]+)\]|(?P<h>[A-Za-z0-9](?:[A-Za-z0-9.-]{0,251}[A-Za-z0-9])?))(?::(?P<p>\d{1,5}))?$"
)


def parse_hostport(text: str):
    m = HOST_RE.match((text or "").strip())
    if not m:
        return None
    port = int(m.group("p") or 22)
    return (m.group("v6") or m.group("h")), port if 1 <= port <= 65535 else None


def kind_label(lang: str, kind: str, has_pw: bool = False) -> str:
    base = t(lang, {"backup": "btn_backup", "um": "btn_um", "both": "btn_both"}[kind])
    return base + (" 🔐" if has_pw and kind != "um" else "")


def short_err(e: Exception, lang=None) -> str:
    if isinstance(e, LocalizedError):
        txt = t(lang or "en", e.key, **e.kw)
    else:
        txt = str(e) or e.__class__.__name__
    return txt.replace("\n", " ")[:300]


WHY_KEYS = (  # (exception types, i18n key) - first match wins; order matters (subclasses first)
    ((asyncssh.PermissionDenied,), "why_auth"),
    ((asyncssh.HostKeyNotVerifiable,), "why_hostkey"),
    ((Forbidden, BadRequest, ChatMigrated, InvalidToken), "why_dest"),  # BadRequest is a NetworkError
    ((NetworkError, RetryAfter), "why_tg"),
    ((asyncio.TimeoutError, TimeoutError), "why_timeout"),
    ((ConnectionRefusedError,), "why_refused"),
    ((asyncssh.ConnectionLost, ConnectionError, asyncssh.DisconnectError), "why_lost"),
    ((OSError,), "why_network"),
)


def reason_text(e: Exception, lang: str) -> str:
    """Clear, localized reason for a failed backup run + the technical detail.

    Only used for jobs that already exist (their target was verified by a successful login
    when the job was created), so it cannot be abused to probe arbitrary hosts. Failures of
    the user-started connection test in /new and /edit keep the uniform error (conn_fail_text).
    """
    detail = short_err(e, lang)
    if isinstance(e, LocalizedError):
        return detail
    for types, key in WHY_KEYS:
        if isinstance(e, types):
            return f"{t(lang, key)}: {detail}"[:400]
    return detail


TRANSIENT_KEYS = {"err_dns"}  # LocalizedError keys that are worth retrying
HINT_BY_KEY = {  # LocalizedError key -> how to fix it
    "err_storage": "hint_folder",
    "err_not_created": "hint_folder",
    "err_not_created_empty": "hint_folder",
    "err_too_big": "hint_big",
    "err_zip": "hint_zip",
}


def classify(e: Exception):
    """-> (transient, hint_key). Only transient errors (network trouble, timeouts, a dropped
    connection, Telegram hiccups) are retried: waiting does not fix anything else (wrong
    credentials, changed SSH key, missing disk/folder, missing ftp policy, file too big,
    removed from the destination chat, ...). Unknown errors are NOT retried either."""
    if isinstance(e, asyncssh.PermissionDenied):
        return False, "hint_auth"
    if isinstance(e, asyncssh.HostKeyNotVerifiable):
        return False, "hint_hostkey"
    if isinstance(e, LocalizedError):
        if e.key in TRANSIENT_KEYS:
            return True, None
        return False, HINT_BY_KEY.get(e.key, "hint_unknown")
    if isinstance(e, NotRouterOS):
        return False, "hint_notros"
    if isinstance(e, (SftpDenied, asyncssh.SFTPError)):
        return False, "hint_ftp"
    if isinstance(e, AddressNotAllowed):
        return False, "hint_address"
    if isinstance(e, (Forbidden, BadRequest, ChatMigrated, InvalidToken)):
        return False, "hint_dest"
    if isinstance(e, (NetworkError, RetryAfter)):  # includes TimedOut
        return True, None
    if isinstance(e, (asyncio.TimeoutError, TimeoutError, OSError, asyncssh.DisconnectError)):
        return True, None  # ConnectionError/refused/reset/DNS are OSErrors; ConnectionLost is a DisconnectError
    return False, "hint_unknown"


async def conn_fail_text(e: Exception, lang: str, key: str, uid: int, host: str, port: int, started: float) -> str:
    """Text for a failed connection test.

    In uniform mode every failure looks the same (same message, same minimum duration),
    so the bot cannot be used to tell closed / filtered / non-SSH / wrong-password apart.
    The real reason goes to the server log only. Two reasons stay visible because they
    cannot be abused: a changed SSH host key of the user's OWN saved router, and
    "not a RouterOS device", which is only reported after a successful login.
    """
    if isinstance(e, RateLimited):
        return t(lang, "rate_limited", min=max(1, math.ceil(e.wait / 60)))
    log.info("connection test failed uid=%s target=%s:%s: %s: %s", uid, host, port, type(e).__name__, e)
    if not UNIFORM_ERRORS or uid in TRUSTED:
        return t(lang, key, err=esc(short_err(e)))
    await asyncio.sleep(max(0.0, CONN_FAIL_MIN_SECONDS - (time.monotonic() - started)))
    if isinstance(e, (asyncssh.HostKeyNotVerifiable, NotRouterOS)):
        return t(lang, key, err=esc(short_err(e)))
    return t(lang, key, err=esc(t(lang, "conn_generic")))


def safe_name(s: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "_", s).strip("_") or "mikrotik"


def build_zip(entries, password: str | None, when: datetime) -> bytes:
    """Pack [(filename, bytes), ...] into one ZIP archive and return its bytes.

    No password -> plain ZIP (standard library). With a password -> WinZip AES-256
    (pyzipper); open it with 7-Zip, WinRAR, Keka, The Unarchiver, ... (the Windows Explorer
    built-in extractor does not support AES). The legacy ZipCrypto scheme is deliberately
    not used: it is trivially breakable. CPU-bound: call it through asyncio.to_thread.
    """
    buf = io.BytesIO()
    date_time = when.timetuple()[:6]
    if password:
        zf = pyzipper.AESZipFile(
            buf, "w", compression=pyzipper.ZIP_DEFLATED, compresslevel=6, encryption=pyzipper.WZ_AES
        )
        zf.setpassword(password.encode("utf-8"))
        zf.setencryption(pyzipper.WZ_AES, nbits=256)
        info_cls, deflated = zf.zipinfo_cls, pyzipper.ZIP_DEFLATED  # AES needs its own ZipInfo class
    else:
        zf = zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6)
        info_cls, deflated = zipfile.ZipInfo, zipfile.ZIP_DEFLATED
    with zf:
        for name, data in entries:
            zi = info_cls(name, date_time=date_time)
            zi.compress_type = deflated
            zi.external_attr = 0o600 << 16  # rw------- after extraction on Unix
            zf.writestr(zi, data)
    return buf.getvalue()


def zip_suffix(enabled, has_pw) -> str:
    """Language-neutral marker appended to the 'kind' text of a job / caption."""
    return (" + 🗜 ZIP" + (" 🔐" if has_pw else "")) if enabled else ""


async def guard(update: Update) -> bool:
    uid = update.effective_user.id
    if ALLOWED and uid not in ALLOWED:
        await update.effective_message.reply_text(t(get_lang(uid), "not_allowed"))
        return False
    return True


async def drop_kb(q):
    try:
        await q.edit_message_reply_markup(reply_markup=None)
    except TelegramError:
        pass


def lg(update: Update) -> str:
    return get_lang(update.effective_user.id)


# --------------------------------------------------------------------------- scheduling
def cancel_retry(app: Application, jid: int) -> None:
    for j in app.job_queue.get_jobs_by_name(f"retry-{jid}"):
        j.schedule_removal()


def unschedule(app: Application, jid: int) -> None:
    for j in app.job_queue.get_jobs_by_name(f"job-{jid}"):
        j.schedule_removal()
    cancel_retry(app, jid)


def schedule(app: Application, row, first=None) -> None:
    unschedule(app, row["id"])
    if not row["active"]:
        return
    interval = row["interval_min"] * 60
    if first is None:
        first = max(5, interval - (time.time() - row["last_run"])) if row["last_run"] else 30
    app.job_queue.run_repeating(tick, interval=interval, first=first, name=f"job-{row['id']}", data=row["id"])


async def tick(context: ContextTypes.DEFAULT_TYPE) -> None:
    cancel_retry(context.application, context.job.data)  # a new scheduled run supersedes pending retries
    await execute_job(context.application, context.job.data, retry=True)


async def retry_tick(context: ContextTypes.DEFAULT_TYPE) -> None:
    jid, attempt = context.job.data
    await execute_job(context.application, jid, retry=True, attempt=attempt)


async def notify_owner(app: Application, uid: int, text: str) -> None:
    try:
        await app.bot.send_message(uid, text)
    except TelegramError:
        pass


RUNNING: set[int] = set()  # jobs with a backup in progress (one at a time per job)


async def execute_job(app: Application, jid: int, retry: bool = False, attempt: int = 0):
    """Run one backup and deliver it. Returns (ok, error_text).

    retry=True (scheduled runs only): a failure is retried up to RETRY_ATTEMPTS times,
    RETRY_DELAY_MIN minutes apart, and the owner is told the reason of every failed attempt and
    about the final give-up. Only transient errors are retried (see classify()). The first backup of a new job and manual "run now" never retry."""
    job = get_job(jid)
    if not job:
        return False, "gone"
    if jid in RUNNING:  # checked and set with no await in between => race-free
        return False, "busy"
    RUNNING.add(jid)
    try:
        return await _execute_job(app, job, retry, attempt)
    finally:
        RUNNING.discard(jid)


async def _execute_job(app: Application, job, retry: bool = False, attempt: int = 0):
    jid = job["id"]
    lang = get_lang(job["owner_id"])
    tzname, tz = get_tz(job["owner_id"])
    try:
        async with SEM:
            identity, _version, files, step_errors = await asyncio.wait_for(remote_backup(job), 300)
        now = datetime.now(tz)
        stamp = f"{safe_name(identity)}_{now:%Y-%m-%d_%H-%M}"
        labels = [
            t(lang, "btn_um")
            if ext == ".umb"
            else kind_label(lang, "backup", bool(job["bk_password_enc"]))
            for ext, _data in files
        ]
        if job["zip_enabled"]:
            # All files of this run go into ONE archive. If packing fails we raise (job failure)
            # instead of falling back to the raw files: a user who asked for a password-protected
            # ZIP must never get the unprotected files.
            zpw = dec(job["zip_password_enc"]) if job["zip_password_enc"] else None
            try:
                zdata = await asyncio.to_thread(
                    build_zip, [(f"{stamp}{ext}", data) for ext, data in files], zpw, now
                )
            except Exception as ze:  # noqa: BLE001
                raise LocalizedError("err_zip", err=str(ze)[:150] or type(ze).__name__) from ze
            if len(zdata) > TG_FILE_LIMIT:
                raise LocalizedError("err_too_big", mb=len(zdata) // 1024 // 1024)
            outgoing = [(f"{stamp}.zip", zdata, " + ".join(labels) + zip_suffix(True, bool(zpw)))]
        else:
            outgoing = [(f"{stamp}{ext}", data, label) for (ext, data), label in zip(files, labels)]
        for fname, data, label in outgoing:
            await app.bot.send_document(
                chat_id=job["dest_chat_id"],
                document=data,
                filename=fname,
                caption=t(
                    lang, "caption", identity=esc(identity), kind=label,
                    time=f"{now:%Y-%m-%d %H:%M} ({esc(tzname)})",
                ),
                write_timeout=TG_UPLOAD_TIMEOUT,
                read_timeout=TG_UPLOAD_TIMEOUT,
                connect_timeout=30,
            )
        update_job(jid, last_run=int(time.time()), fails=0, identity=identity)
        if attempt > 0:
            await notify_owner(
                app, job["owner_id"],
                t(lang, "retry_ok", id=jid, host=esc(job["host"]), n=attempt + 1, total=RETRY_ATTEMPTS + 1),
            )
        if step_errors:  # some files were delivered, the others failed: tell the owner
            err = "; ".join(reason_text(e, lang) for e in step_errors)[:400]
            log.warning("job %s partially failed: %s", jid, err)
            try:
                await app.bot.send_message(
                    job["owner_id"], t(lang, "fail_owner", id=jid, host=esc(job["host"]), err=esc(err))
                )
            except TelegramError:
                pass
            return False, err
        return True, ""
    except Exception as e:  # noqa: BLE001
        err = reason_text(e, lang)
        log.warning("job %s failed (attempt %s): %s: %s", jid, attempt + 1, type(e).__name__, e)
        transient, hint_key = classify(e)
        permanent = retry and RETRY_ATTEMPTS > 0 and not transient
        if retry and attempt < RETRY_ATTEMPTS and transient:  # more tries left: tell the owner, try again later
            app.job_queue.run_once(
                retry_tick, when=RETRY_DELAY_MIN * 60, name=f"retry-{jid}", data=(jid, attempt + 1)
            )
            await notify_owner(
                app, job["owner_id"],
                t(
                    lang, "retry_scheduled", id=jid, host=esc(job["host"]), err=esc(err),
                    n=attempt + 1, total=RETRY_ATTEMPTS + 1, delay=fmt_interval(lang, RETRY_DELAY_MIN),
                ),
            )
            return False, err
        # out of tries (or retries off / manual run): this counts as ONE failure towards MAX_FAILS
        fails = (job["fails"] or 0) + 1
        update_job(jid, fails=fails)
        gave_up = retry and attempt > 0 and not permanent
        if permanent:  # waiting cannot fix it: no retries, say why and how to fix it
            text = t(lang, "fail_no_retry", id=jid, host=esc(job["host"]), err=esc(err), hint=esc(t(lang, hint_key)))
        elif gave_up:
            text = t(lang, "retry_gave_up", id=jid, host=esc(job["host"]), err=esc(err), n=attempt + 1)
        else:
            text = t(lang, "fail_owner", id=jid, host=esc(job["host"]), err=esc(err))
        try:
            await app.bot.send_message(job["owner_id"], text)
            if fails >= MAX_FAILS and job["active"]:
                update_job(jid, active=0)
                unschedule(app, jid)
                await app.bot.send_message(job["owner_id"], t(lang, "paused_auto", id=jid, n=fails))
        except TelegramError:
            pass
        return False, err


# --------------------------------------------------------------------------- basic commands
async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not await guard(update):
        return
    uid = update.effective_user.id
    if not has_user(uid):
        await update.message.reply_text(t("en", "choose_lang"), reply_markup=lang_kb())
        return
    await update.message.reply_text(t(get_lang(uid), "welcome"))


async def cmd_language(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not await guard(update):
        return
    await update.message.reply_text(t("en", "choose_lang"), reply_markup=lang_kb())


async def on_lang(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    if not await guard(update):
        return
    code = q.data.split(":")[1]
    if code not in LANGS:
        return
    uid = update.effective_user.id
    first = not has_user(uid)
    set_lang(uid, code)
    await q.edit_message_text(t(code, "lang_set"))
    if first:
        await ctx.bot.send_message(q.message.chat_id, t(code, "welcome"))
        await ctx.bot.send_message(q.message.chat_id, t(code, "ask_tz"), reply_markup=tz_kb(code))


async def cmd_timezone(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not await guard(update):
        return
    uid = update.effective_user.id
    if not has_user(uid):
        await update.message.reply_text(t("en", "choose_lang"), reply_markup=lang_kb())
        return
    lang = get_lang(uid)
    await update.message.reply_text(t(lang, "ask_tz"), reply_markup=tz_kb(lang))


async def on_tz(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    if not await guard(update):
        return
    p = parse_tz(q.data[3:])
    if not p:
        return
    uid = update.effective_user.id
    set_tz(uid, p[0])
    await q.edit_message_text(t(get_lang(uid), "tz_set", tz=esc(p[0]), now=fmt_now(p[1])))


async def tz_custom(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    if not await guard(update):
        return ConversationHandler.END
    await ctx.bot.send_message(q.message.chat_id, t(lg(update), "ask_tz_custom"))
    return TZ_TEXT


async def tz_text(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    lang = lg(update)
    p = parse_tz(update.message.text)
    if not p:
        await update.message.reply_text(t(lang, "bad_tz"))
        return TZ_TEXT
    set_tz(update.effective_user.id, p[0])
    await update.message.reply_text(t(lang, "tz_set", tz=esc(p[0]), now=fmt_now(p[1])))
    return ConversationHandler.END


async def cmd_cancel(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    ctx.user_data.pop("d", None)
    ctx.user_data.pop("edit", None)
    await update.message.reply_text(t(lg(update), "cancelled"))
    return ConversationHandler.END


# --------------------------------------------------------------------------- shared wizard pieces
(
    HOST, USER, PASS, TYPE, BKPW, BKPW_TEXT, ZIP, ZIP_TEXT, RDIR, DEST, INTERVAL, INTERVAL_TEXT, TZ_TEXT,
    EMENU, E_HOST, E_USER, E_PASS, E_TYPE, E_BKPW, E_BKPW_TEXT, E_ZIP, E_ZIP_TEXT, E_RDIR, E_DEST, E_INT,
    E_INT_TEXT,
) = range(26)


async def ask_rdir(ctx, chat_id: int, lang: str, cb: str = "rd:default"):
    await ctx.bot.send_message(
        chat_id, t(lang, "ask_rdir"), reply_markup=kb([[(t(lang, "btn_rdir_default"), cb)]])
    )


async def ask_dest(ctx, chat_id: int, lang: str, cb: str = "dest:here"):
    await ctx.bot.send_message(chat_id, t(lang, "ask_dest"), reply_markup=kb([[(t(lang, "btn_here"), cb)]]))


async def validate_dest(bot, chat, user_id: int):
    if chat.type == "private":
        if chat.id != user_id:
            raise LocalizedError("dest_err_private")
        return
    member = await bot.get_chat_member(chat.id, user_id)
    if member.status not in (ChatMemberStatus.OWNER, ChatMemberStatus.ADMINISTRATOR):
        raise LocalizedError("dest_err_admin")


async def resolve_dest(update: Update, ctx: ContextTypes.DEFAULT_TYPE, lang: str):
    """Text/forward message -> (chat_id, title). Raises on any problem."""
    msg = update.message
    uid = update.effective_user.id
    chat = None
    fo = getattr(msg, "forward_origin", None)
    if fo is not None:
        chat = getattr(fo, "chat", None) or getattr(fo, "sender_chat", None)
    if chat is None:
        ref = (msg.text or "").strip()
        if ref.startswith("https://t.me/"):
            ref = "@" + ref.rstrip("/").rsplit("/", 1)[-1]
        if re.fullmatch(r"-?\d+", ref):
            ref = int(ref)
        elif re.fullmatch(r"@?[A-Za-z0-9_]{4,64}", ref):
            ref = "@" + ref.lstrip("@")
        else:
            raise LocalizedError("dest_err_format")
        chat = await ctx.bot.get_chat(ref)
    await validate_dest(ctx.bot, chat, uid)
    if chat.type != "private":
        await ctx.bot.send_message(chat.id, t(lang, "dest_ok"))  # also proves the bot can post
    title = t(lang, "dest_pv") if chat.type == "private" else (chat.title or str(chat.id))
    return chat.id, title


async def check_new_host(update: Update, lang: str):
    """Parse + validate an address message. Returns (host, port) or None (error already sent)."""
    hp = parse_hostport(update.message.text)
    if not hp or hp[1] is None:
        await update.message.reply_text(t(lang, "bad_host"))
        return None
    problem = await ensure_public(hp[0])
    if problem:
        await update.message.reply_text(t(lang, "host_resolve" if problem == "resolve" else "host_private"))
        return None
    return hp


async def delete_quietly(msg):
    try:
        await msg.delete()  # never leave passwords in the chat
    except TelegramError:
        pass


def type_kb(lang: str, prefix: str):
    return kb(
        [
            [(t(lang, "btn_backup"), f"{prefix}:backup")],
            [(t(lang, "btn_um"), f"{prefix}:um")],
            [(t(lang, "btn_both"), f"{prefix}:both")],
        ]
    )


def bkpw_kb(lang: str, prefix: str):
    return kb([[(t(lang, "btn_pw_none"), f"{prefix}:none"), (t(lang, "btn_pw_set"), f"{prefix}:set")]])


def zip_kb(lang: str, prefix: str):
    return kb(
        [
            [(t(lang, "btn_zip_no"), f"{prefix}:no")],
            [(t(lang, "btn_zip_plain"), f"{prefix}:plain")],
            [(t(lang, "btn_zip_pw"), f"{prefix}:pw")],
        ]
    )


async def ask_zip(ctx, chat_id: int, lang: str, prefix: str = "zip"):
    await ctx.bot.send_message(chat_id, t(lang, "ask_zip"), reply_markup=zip_kb(lang, prefix))


def valid_bk_password(pw: str) -> bool:
    return 1 <= len(pw) <= 64 and pw.isprintable()


# --------------------------------------------------------------------------- create wizard (/new)
async def cmd_new(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not await guard(update):
        return ConversationHandler.END
    uid = update.effective_user.id
    if not has_user(uid):
        await update.message.reply_text(t("en", "choose_lang"), reply_markup=lang_kb())
        return ConversationHandler.END
    lang = get_lang(uid)
    if len(user_jobs(uid)) >= MAX_JOBS:
        await update.message.reply_text(t(lang, "limit_reached", n=MAX_JOBS))
        return ConversationHandler.END
    ctx.user_data.pop("edit", None)
    ctx.user_data["d"] = {}
    await update.message.reply_text(t(lang, "ask_host"))
    return HOST


async def got_host(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    lang = lg(update)
    hp = await check_new_host(update, lang)
    if not hp:
        return HOST
    ctx.user_data["d"].update(host=hp[0], port=hp[1])
    await update.message.reply_text(t(lang, "ask_user"))
    return USER


async def got_user(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    lang = lg(update)
    name = update.message.text.strip()
    if not name or len(name) > 64 or re.search(r"\s", name):
        await update.message.reply_text(t(lang, "ask_user"))
        return USER
    ctx.user_data["d"]["user"] = name
    await update.message.reply_text(t(lang, "ask_pass"))
    return PASS


async def got_pass(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    lang = lg(update)
    d = ctx.user_data["d"]
    pw = update.message.text
    await delete_quietly(update.message)
    d["pw"] = pw
    chat_id = update.effective_chat.id
    wait = await ctx.bot.send_message(chat_id, t(lang, "testing"))
    uid, started = update.effective_user.id, time.monotonic()
    try:
        identity, version, key = await probe(d["host"], d["port"], d["user"], pw, uid=uid)
    except Exception as e:  # noqa: BLE001
        host, port = d["host"], d["port"]
        ctx.user_data.pop("d", None)
        await wait.edit_text(await conn_fail_text(e, lang, "test_fail", uid, host, port, started))
        return ConversationHandler.END
    d.update(identity=identity, host_key=key)
    await wait.edit_text(t(lang, "test_ok", identity=esc(identity), version=esc(version)))
    await ctx.bot.send_message(chat_id, t(lang, "ask_type"), reply_markup=type_kb(lang, "type"))
    return TYPE


async def on_type(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    await drop_kb(q)
    lang = lg(update)
    d = ctx.user_data["d"]
    d["kind"] = q.data.split(":")[1]
    if d["kind"] == "um":
        d["bk_pw"] = None
        await ask_zip(ctx, q.message.chat_id, lang)
        return ZIP
    await ctx.bot.send_message(q.message.chat_id, t(lang, "ask_bk_pw"), reply_markup=bkpw_kb(lang, "pw"))
    return BKPW


async def on_bkpw(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    await drop_kb(q)
    lang = lg(update)
    if q.data == "pw:set":
        await ctx.bot.send_message(q.message.chat_id, t(lang, "ask_bk_pw_text"))
        return BKPW_TEXT
    ctx.user_data["d"]["bk_pw"] = None
    await ask_zip(ctx, q.message.chat_id, lang)
    return ZIP


async def got_bkpw(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    lang = lg(update)
    pw = update.message.text
    await delete_quietly(update.message)
    chat_id = update.effective_chat.id
    if not valid_bk_password(pw):
        await ctx.bot.send_message(chat_id, t(lang, "bad_bk_pw"))
        return BKPW_TEXT
    ctx.user_data["d"]["bk_pw"] = pw
    await ask_zip(ctx, chat_id, lang)
    return ZIP


async def on_zip(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    await drop_kb(q)
    lang = lg(update)
    choice = q.data.split(":")[1]
    if choice == "pw":
        await ctx.bot.send_message(q.message.chat_id, t(lang, "ask_zip_pw_text"))
        return ZIP_TEXT
    d = ctx.user_data["d"]
    d["zip"], d["zip_pw"] = choice == "plain", None
    await ask_rdir(ctx, q.message.chat_id, lang)
    return RDIR


async def got_zip_pw(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    lang = lg(update)
    pw = update.message.text
    await delete_quietly(update.message)
    chat_id = update.effective_chat.id
    if not valid_bk_password(pw):
        await ctx.bot.send_message(chat_id, t(lang, "bad_bk_pw"))
        return ZIP_TEXT
    d = ctx.user_data["d"]
    d["zip"], d["zip_pw"] = True, pw
    await ask_rdir(ctx, chat_id, lang)
    return RDIR


async def rdir_default(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    await drop_kb(q)
    ctx.user_data["d"]["rdir"] = ""
    await ask_dest(ctx, q.message.chat_id, lg(update))
    return DEST


async def rdir_text(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    lang = lg(update)
    rdir = parse_rdir(update.message.text)
    if rdir is None:
        await update.message.reply_text(t(lang, "bad_rdir"))
        return RDIR
    ctx.user_data["d"]["rdir"] = rdir
    await ask_dest(ctx, update.effective_chat.id, lang)
    return DEST


async def dest_here(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    await drop_kb(q)
    lang = lg(update)
    d = ctx.user_data["d"]
    d["dest_id"] = q.message.chat_id
    d["dest_title"] = t(lang, "dest_pv")
    await ctx.bot.send_message(q.message.chat_id, t(lang, "ask_interval"), reply_markup=interval_kb(lang))
    return INTERVAL


async def dest_text(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    lang = lg(update)
    try:
        chat_id, title = await resolve_dest(update, ctx, lang)
    except Exception as e:  # noqa: BLE001
        await update.message.reply_text(t(lang, "dest_fail", err=esc(short_err(e, lang))))
        return DEST
    d = ctx.user_data["d"]
    d["dest_id"], d["dest_title"] = chat_id, title
    await update.message.reply_text(t(lang, "ask_interval"), reply_markup=interval_kb(lang))
    return INTERVAL


async def finalize(ctx: ContextTypes.DEFAULT_TYPE, chat_id: int, uid: int, minutes: int):
    lang = get_lang(uid)
    d = ctx.user_data.pop("d", None)
    if d is None:  # the same button was tapped twice
        return ConversationHandler.END
    cur = db.execute(
        """INSERT INTO jobs(owner_id, host, port, username, password_enc, host_key, identity, kind,
                            bk_password_enc, remote_dir, dest_chat_id, dest_title, interval_min,
                            zip_enabled, zip_password_enc)
           VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            uid, d["host"], d["port"], d["user"], enc(d["pw"]), d["host_key"], d["identity"], d["kind"],
            enc(d["bk_pw"]) if d.get("bk_pw") else None, d.get("rdir", ""), d["dest_id"], d["dest_title"],
            minutes, int(bool(d.get("zip"))), enc(d["zip_pw"]) if d.get("zip_pw") else None,
        ),
    )
    db.commit()
    jid = cur.lastrowid
    await ctx.bot.send_message(
        chat_id,
        t(
            lang, "job_saved", id=jid, host=esc(f"{d['host']}:{d['port']}"),
            kind=kind_label(lang, d["kind"], bool(d.get("bk_pw"))),
            zip=zip_suffix(d.get("zip"), d.get("zip_pw")), dest=esc(d["dest_title"]),
            interval=fmt_interval(lang, minutes),
        ),
    )
    schedule(ctx.application, get_job(jid), first=minutes * 60)
    ctx.application.create_task(execute_job(ctx.application, jid))  # first backup right away (no auto-retry)
    return ConversationHandler.END


async def on_interval(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    await drop_kb(q)
    val = q.data.split(":")[1]
    if val == "custom":
        await ctx.bot.send_message(
            q.message.chat_id, t(lg(update), "ask_interval_custom", min=MIN_INTERVAL_MIN)
        )
        return INTERVAL_TEXT
    return await finalize(ctx, q.message.chat_id, update.effective_user.id, int(val))


async def interval_text(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    minutes = parse_interval(update.message.text)
    if minutes is None:
        await update.message.reply_text(t(lg(update), "bad_interval"))
        return INTERVAL_TEXT
    return await finalize(ctx, update.effective_chat.id, update.effective_user.id, minutes)


# --------------------------------------------------------------------------- jobs list / detail
def dest_label(lang: str, row) -> str:
    """Destination name for display. 'Private chat' is localized at render time
    (it used to be stored in the DB in the language active at creation)."""
    if row["dest_chat_id"] == row["owner_id"]:
        return t(lang, "dest_pv")
    return row["dest_title"] or ""


def job_text(uid: int, row) -> str:
    lang = get_lang(uid)
    return t(
        lang, "job_detail", id=row["id"], host=esc(f"{row['host']}:{row['port']}"),
        user=esc(row["username"]), kind=kind_label(lang, row["kind"], bool(row["bk_password_enc"])),
        zip=zip_suffix(row["zip_enabled"], row["zip_password_enc"]), rdir=esc(row["remote_dir"] or "/"), dest=esc(dest_label(lang, row)),
        interval=fmt_interval(lang, row["interval_min"]),
        status=t(lang, "st_active" if row["active"] else "st_paused"),
        last=fmt_ts(row["last_run"], get_tz(uid)) or t(lang, "never"),
    )


def jobs_list_view(uid: int):
    lang = get_lang(uid)
    rows = user_jobs(uid)
    if not rows:
        return t(lang, "no_jobs"), None
    icons = {"backup": "💾", "um": "👥", "both": "💾👥"}
    buttons = [
        [(f"{'⏸ ' if not r['active'] else ''}#{r['id']} · {r['host']} · {icons[r['kind']]}{'🗜' if r['zip_enabled'] else ''}", f"job:{r['id']}")]
        for r in rows
    ]
    return t(lang, "jobs_title"), kb(buttons)


def job_view(uid: int, row):
    lang = get_lang(uid)
    toggle = ("btn_pause", "jpause") if row["active"] else ("btn_resume", "jres")
    markup = kb(
        [
            [(t(lang, "btn_run"), f"jrun:{row['id']}"), (t(lang, "btn_edit"), f"jedit:{row['id']}")],
            [(t(lang, toggle[0]), f"{toggle[1]}:{row['id']}"), (t(lang, "btn_delete"), f"jdel:{row['id']}")],
            [(t(lang, "btn_back"), "jback:0")],
        ]
    )
    return job_text(uid, row), markup


async def cmd_jobs(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not await guard(update):
        return
    text, markup = jobs_list_view(update.effective_user.id)
    await update.message.reply_text(text, reply_markup=markup)


async def on_job(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    if not await guard(update):
        return
    uid = update.effective_user.id
    lang = get_lang(uid)
    action, _, raw = q.data.partition(":")
    if action == "jback":
        text, markup = jobs_list_view(uid)
        await q.edit_message_text(text, reply_markup=markup)
        return
    row = get_job(int(raw))
    if not row or row["owner_id"] != uid:
        return
    jid = row["id"]
    if action == "jrun":
        await ctx.bot.send_message(q.message.chat_id, t(lang, "run_started"))
        if jid in RUNNING:  # a backup of this job is already in progress
            return

        async def _run():
            ok, _err = await execute_job(ctx.application, jid)
            if ok:
                await ctx.bot.send_message(q.message.chat_id, t(lang, "run_done"))
            # on failure execute_job already notified the owner

        ctx.application.create_task(_run())
        return
    if action == "jpause":
        update_job(jid, active=0)
        unschedule(ctx.application, jid)
    elif action == "jres":
        update_job(jid, active=1, fails=0)
        schedule(ctx.application, get_job(jid))
    elif action == "jdel":
        unschedule(ctx.application, jid)
        db.execute("DELETE FROM jobs WHERE id=?", (jid,))
        db.commit()
        await q.edit_message_text(t(lang, "deleted"))
        return
    text, markup = job_view(uid, get_job(jid))
    await q.edit_message_text(text, reply_markup=markup)


# --------------------------------------------------------------------------- edit flow
def cur_job(ctx, uid: int):
    jid = ctx.user_data.get("edit")
    row = get_job(jid) if jid else None
    return row if row and row["owner_id"] == uid else None


def edit_view(uid: int, row):
    lang = get_lang(uid)
    rows = [
        [(t(lang, "btn_e_host"), "ef:host"), (t(lang, "btn_e_user"), "ef:user")],
        [(t(lang, "btn_e_pass"), "ef:pass"), (t(lang, "btn_e_type"), "ef:type")],
    ]
    if row["kind"] != "um":
        rows.append([(t(lang, "btn_e_bkpw"), "ef:bkpw"), (t(lang, "btn_e_zip"), "ef:zip")])
    else:
        rows.append([(t(lang, "btn_e_zip"), "ef:zip")])
    rows += [
        [(t(lang, "btn_e_rdir"), "ef:rdir"), (t(lang, "btn_e_dest"), "ef:dest")],
        [(t(lang, "btn_e_int"), "ef:int")],
        [(t(lang, "btn_done"), "ef:done")],
    ]
    return f"{job_text(uid, row)}\n\n{t(lang, 'edit_hint')}", kb(rows)


async def edit_back(ctx, chat_id: int, uid: int):
    row = cur_job(ctx, uid)
    if not row:
        return ConversationHandler.END
    text, markup = edit_view(uid, row)
    await ctx.bot.send_message(chat_id, text, reply_markup=markup)
    return EMENU


async def save_and_back(update: Update, ctx: ContextTypes.DEFAULT_TYPE, **fields):
    uid, chat_id = update.effective_user.id, update.effective_chat.id
    row = cur_job(ctx, uid)
    if not row:
        return ConversationHandler.END
    update_job(row["id"], **fields)
    await ctx.bot.send_message(chat_id, t(get_lang(uid), "saved"))
    return await edit_back(ctx, chat_id, uid)


async def edit_entry(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    if not await guard(update):
        return ConversationHandler.END
    uid = update.effective_user.id
    row = get_job(int(q.data.split(":")[1]))
    if not row or row["owner_id"] != uid:
        return ConversationHandler.END
    ctx.user_data.pop("d", None)
    ctx.user_data["edit"] = row["id"]
    text, markup = edit_view(uid, row)
    await q.edit_message_text(text, reply_markup=markup)
    return EMENU


async def edit_choose(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    uid, chat_id = update.effective_user.id, q.message.chat_id
    lang = get_lang(uid)
    row = cur_job(ctx, uid)
    if not row:
        return ConversationHandler.END
    field = q.data.split(":")[1]
    if field == "done":
        text, markup = job_view(uid, row)
        await q.edit_message_text(text, reply_markup=markup)
        ctx.user_data.pop("edit", None)
        return ConversationHandler.END
    if field == "host":
        await ctx.bot.send_message(chat_id, f"{t(lang, 'ask_host')}\n\n{t(lang, 'edit_host_note')}")
        return E_HOST
    if field == "user":
        await ctx.bot.send_message(chat_id, t(lang, "ask_user"))
        return E_USER
    if field == "pass":
        await ctx.bot.send_message(chat_id, t(lang, "ask_pass"))
        return E_PASS
    if field == "type":
        await ctx.bot.send_message(chat_id, t(lang, "ask_type"), reply_markup=type_kb(lang, "et"))
        return E_TYPE
    if field == "bkpw":
        await ctx.bot.send_message(chat_id, t(lang, "ask_bk_pw"), reply_markup=bkpw_kb(lang, "epw"))
        return E_BKPW
    if field == "zip":
        await ask_zip(ctx, chat_id, lang, "ezip")
        return E_ZIP
    if field == "rdir":
        await ask_rdir(ctx, chat_id, lang, "erd:default")
        return E_RDIR
    if field == "dest":
        await ask_dest(ctx, chat_id, lang, "edest:here")
        return E_DEST
    if field == "int":
        await ctx.bot.send_message(chat_id, t(lang, "ask_interval"), reply_markup=interval_kb(lang, "eint"))
        return E_INT
    return EMENU


async def apply_conn_change(update: Update, ctx: ContextTypes.DEFAULT_TYPE, **new):
    """Re-test the connection with the changed credentials; save only if it works."""
    uid, chat_id = update.effective_user.id, update.effective_chat.id
    lang = get_lang(uid)
    row = cur_job(ctx, uid)
    if not row:
        return ConversationHandler.END
    host = new.get("host", row["host"])
    port = new.get("port", row["port"])
    user = new.get("username", row["username"])
    pw = new.get("password", dec(row["password_enc"]))
    pinned_key = None if "host" in new else row["host_key"]  # new address => trust-on-first-use again
    wait = await ctx.bot.send_message(chat_id, t(lang, "testing"))
    started = time.monotonic()
    try:
        identity, version, key = await probe(host, port, user, pw, pinned_key, uid=uid)
    except Exception as e:  # noqa: BLE001
        await wait.edit_text(await conn_fail_text(e, lang, "edit_test_fail", uid, host, port, started))
        return await edit_back(ctx, chat_id, uid)
    update_job(
        row["id"], host=host, port=port, username=user, password_enc=enc(pw),
        host_key=pinned_key or key, identity=identity, fails=0,
    )
    await wait.edit_text(f"{t(lang, 'test_ok', identity=esc(identity), version=esc(version))}\n{t(lang, 'saved')}")
    return await edit_back(ctx, chat_id, uid)


async def e_host(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    hp = await check_new_host(update, lg(update))
    if not hp:
        return E_HOST
    return await apply_conn_change(update, ctx, host=hp[0], port=hp[1])


async def e_user(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    name = update.message.text.strip()
    if not name or len(name) > 64 or re.search(r"\s", name):
        await update.message.reply_text(t(lg(update), "ask_user"))
        return E_USER
    return await apply_conn_change(update, ctx, username=name)


async def e_pass(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    pw = update.message.text
    await delete_quietly(update.message)
    return await apply_conn_change(update, ctx, password=pw)


async def e_type(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    await drop_kb(q)
    kind = q.data.split(":")[1]
    fields = {"kind": kind}
    if kind == "um":
        fields["bk_password_enc"] = None  # a backup password makes no sense without a binary backup
    return await save_and_back(update, ctx, **fields)


async def e_bkpw(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    await drop_kb(q)
    if q.data == "epw:set":
        await ctx.bot.send_message(q.message.chat_id, t(lg(update), "ask_bk_pw_text"))
        return E_BKPW_TEXT
    return await save_and_back(update, ctx, bk_password_enc=None)


async def e_bkpw_text(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    pw = update.message.text
    await delete_quietly(update.message)
    if not valid_bk_password(pw):
        await ctx.bot.send_message(update.effective_chat.id, t(lg(update), "bad_bk_pw"))
        return E_BKPW_TEXT
    return await save_and_back(update, ctx, bk_password_enc=enc(pw))


async def e_zip(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    await drop_kb(q)
    choice = q.data.split(":")[1]
    if choice == "pw":
        await ctx.bot.send_message(q.message.chat_id, t(lg(update), "ask_zip_pw_text"))
        return E_ZIP_TEXT
    return await save_and_back(update, ctx, zip_enabled=int(choice == "plain"), zip_password_enc=None)


async def e_zip_text(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    pw = update.message.text
    await delete_quietly(update.message)
    if not valid_bk_password(pw):
        await ctx.bot.send_message(update.effective_chat.id, t(lg(update), "bad_bk_pw"))
        return E_ZIP_TEXT
    return await save_and_back(update, ctx, zip_enabled=1, zip_password_enc=enc(pw))


async def e_rdir_default(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    await drop_kb(q)
    return await save_and_back(update, ctx, remote_dir="")


async def e_rdir_text(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    rdir = parse_rdir(update.message.text)
    if rdir is None:
        await update.message.reply_text(t(lg(update), "bad_rdir"))
        return E_RDIR
    return await save_and_back(update, ctx, remote_dir=rdir)


async def e_dest_here(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    await drop_kb(q)
    return await save_and_back(
        update, ctx, dest_chat_id=q.message.chat_id, dest_title=t(lg(update), "dest_pv")
    )


async def e_dest_text(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    lang = lg(update)
    try:
        chat_id, title = await resolve_dest(update, ctx, lang)
    except Exception as e:  # noqa: BLE001
        await update.message.reply_text(t(lang, "dest_fail", err=esc(short_err(e, lang))))
        return E_DEST
    return await save_and_back(update, ctx, dest_chat_id=chat_id, dest_title=title)


async def apply_interval(update: Update, ctx: ContextTypes.DEFAULT_TYPE, minutes: int):
    state = await save_and_back(update, ctx, interval_min=minutes)
    row = cur_job(ctx, update.effective_user.id)
    if row:
        schedule(ctx.application, row, first=minutes * 60)
    return state


async def e_interval(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    await drop_kb(q)
    val = q.data.split(":")[1]
    if val == "custom":
        await ctx.bot.send_message(
            q.message.chat_id, t(lg(update), "ask_interval_custom", min=MIN_INTERVAL_MIN)
        )
        return E_INT_TEXT
    return await apply_interval(update, ctx, int(val))


async def e_interval_text(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    minutes = parse_interval(update.message.text)
    if minutes is None:
        await update.message.reply_text(t(lg(update), "bad_interval"))
        return E_INT_TEXT
    return await apply_interval(update, ctx, minutes)


# --------------------------------------------------------------------------- app
async def post_init(app: Application):
    for row in db.execute("SELECT * FROM jobs WHERE active=1").fetchall():
        schedule(app, row)
    await app.bot.set_my_commands(
        [
            ("new", "New backup job"),
            ("jobs", "My jobs (run / edit / delete)"),
            ("timezone", "Time zone"),
            ("language", "Language"),
            ("cancel", "Cancel"),
        ]
    )
    log.info("Bot started.")


async def on_error(update, ctx: ContextTypes.DEFAULT_TYPE):
    log.error("Unhandled error", exc_info=ctx.error)


def main():
    app = (
        Application.builder()
        .token(BOT_TOKEN)
        .defaults(Defaults(parse_mode=ParseMode.HTML))
        .concurrent_updates(True)
        .post_init(post_init)
        .build()
    )
    private = filters.ChatType.PRIVATE
    text_only = filters.TEXT & ~filters.COMMAND & private
    anything = ~filters.COMMAND & private
    # Passwords may legitimately start with "/": accept them unless the text is one of OUR
    # commands (those must still reach the fallbacks / global handlers).
    secret_text = filters.TEXT & private & ~filters.Regex(
        r"^/(start|new|jobs|timezone|language|cancel)(@\w+)?(\s|$)"
    )

    # One conversation for every multi-step flow, so a user is only ever in one flow at a time.
    # These can be started (or restarted) at any moment:
    starters = [
        CommandHandler("new", cmd_new, filters=private),
        CallbackQueryHandler(edit_entry, pattern=r"^jedit:\d+$"),
        CallbackQueryHandler(tz_custom, pattern=r"^tzcustom$"),
    ]
    conv = ConversationHandler(
        entry_points=starters,
        states={
            # create
            HOST: [MessageHandler(text_only, got_host)],
            USER: [MessageHandler(text_only, got_user)],
            PASS: [MessageHandler(secret_text, got_pass)],
            TYPE: [CallbackQueryHandler(on_type, pattern=r"^type:")],
            BKPW: [CallbackQueryHandler(on_bkpw, pattern=r"^pw:")],
            BKPW_TEXT: [MessageHandler(secret_text, got_bkpw)],
            ZIP: [CallbackQueryHandler(on_zip, pattern=r"^zip:")],
            ZIP_TEXT: [MessageHandler(secret_text, got_zip_pw)],
            RDIR: [
                CallbackQueryHandler(rdir_default, pattern=r"^rd:default$"),
                MessageHandler(text_only, rdir_text),
            ],
            DEST: [
                CallbackQueryHandler(dest_here, pattern=r"^dest:here$"),
                MessageHandler(anything, dest_text),
            ],
            INTERVAL: [CallbackQueryHandler(on_interval, pattern=r"^int:")],
            INTERVAL_TEXT: [MessageHandler(text_only, interval_text)],
            # time zone
            TZ_TEXT: [MessageHandler(text_only, tz_text)],
            # edit
            EMENU: [CallbackQueryHandler(edit_choose, pattern=r"^ef:")],
            E_HOST: [MessageHandler(text_only, e_host)],
            E_USER: [MessageHandler(text_only, e_user)],
            E_PASS: [MessageHandler(secret_text, e_pass)],
            E_TYPE: [CallbackQueryHandler(e_type, pattern=r"^et:")],
            E_BKPW: [CallbackQueryHandler(e_bkpw, pattern=r"^epw:")],
            E_BKPW_TEXT: [MessageHandler(secret_text, e_bkpw_text)],
            E_ZIP: [CallbackQueryHandler(e_zip, pattern=r"^ezip:")],
            E_ZIP_TEXT: [MessageHandler(secret_text, e_zip_text)],
            E_RDIR: [
                CallbackQueryHandler(e_rdir_default, pattern=r"^erd:default$"),
                MessageHandler(text_only, e_rdir_text),
            ],
            E_DEST: [
                CallbackQueryHandler(e_dest_here, pattern=r"^edest:here$"),
                MessageHandler(anything, e_dest_text),
            ],
            E_INT: [CallbackQueryHandler(e_interval, pattern=r"^eint:")],
            E_INT_TEXT: [MessageHandler(text_only, e_interval_text)],
        },
        fallbacks=[CommandHandler("cancel", cmd_cancel, filters=private), *starters],
        conversation_timeout=900,
    )
    app.add_handler(conv)
    app.add_handler(CommandHandler("start", cmd_start, filters=private))
    app.add_handler(CommandHandler("language", cmd_language, filters=private))
    app.add_handler(CommandHandler("timezone", cmd_timezone, filters=private))
    app.add_handler(CommandHandler("jobs", cmd_jobs, filters=private))
    app.add_handler(CommandHandler("cancel", cmd_cancel, filters=private))
    app.add_handler(CallbackQueryHandler(on_lang, pattern=r"^lang:"))
    app.add_handler(CallbackQueryHandler(on_tz, pattern=r"^tz:"))
    app.add_handler(CallbackQueryHandler(on_job, pattern=r"^(job|jrun|jpause|jres|jdel|jback):"))
    app.add_error_handler(on_error)
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()