# MikroTik Backup Bot

**English** | [Español](README.es.md) | [Русский](README.ru.md) | [中文](README.zh.md) | [فارسی](README.fa.md)

A Telegram bot that automatically backs up your MikroTik routers and delivers the files to a private chat, group or channel, on a schedule you choose.

The bot connects to the router over SSH, creates the backup, downloads it over SFTP, deletes it from the router and sends it to Telegram. Every job can be edited later, and the bot itself speaks 🇬🇧 English, 🇪🇸 Español, 🇷🇺 Русский, 🇨🇳 中文 and 🇮🇷 فارسی.

## Features

- **Backup types:** binary backup (`.backup`, with or without a password), User Manager database (`.umb`), or both
- **Optional ZIP:** plain ZIP or **AES-256** encrypted ZIP
- **Flexible destinations:** private chat, group or channel
- **Flexible schedule:** presets from 1 hour to 7 days, or a custom interval such as `30m`, `2h`, `1d` (up to 30 days)
- **Automatic retry** for transient failures, with clear, actionable error messages
- **Per-user time zone** for file names, captions and "last run"
- **Multi-user:** per-user job limits, global concurrency limit, access control
- **Secure by default:** encrypted credentials, SSH host key pinning, private-IP blocking, anti-abuse rate limits
- **Docker-ready**, runs as a non-root user

## Requirements

- Python **3.10+** (or Docker)
- A bot token from [@BotFather](https://t.me/BotFather)
- A MikroTik router with SSH enabled and reachable from the bot's server
- Python dependencies: `python-telegram-bot[job-queue]`, `asyncssh`, `pyzipper`, `cryptography`, `python-dotenv`, `tzdata`

## Installation

### Option 1: Docker (recommended)

```bash
git clone https://github.com/pouya6672/MikroTik-Backup-Telegram-bot.git
cd MikroTik-Backup-Telegram-bot
cp .env.example .env        # set BOT_TOKEN and ALLOWED_USERS
docker compose --env-file .env -f docker/docker-compose.yml up -d --build
docker compose -f docker/docker-compose.yml logs -f
```

- The database and the encryption key live in the `mtbackup-data` volume (`/app/data`). **Always keep this volume.**
- The container starts as root only to fix volume permissions, then drops to the unprivileged `bot` user (uid 10001) via `gosu`.
- `docker/entrypoint.sh` validates every environment variable at startup and prints a clear error (or warning) if something is wrong, for example a missing `BOT_TOKEN`, an invalid `FERNET_KEY`, or an empty `ALLOWED_USERS`.
- The bot uses long polling, so **no inbound port needs to be opened**.

Without Compose:

```bash
docker build -f docker/Dockerfile -t mikrotik-backup-bot .
docker run -d --name mtbackup --restart unless-stopped \
  --env-file .env -v mtbackup-data:/app/data mikrotik-backup-bot
```

### Option 2: Plain Python

```bash
python3 -m venv venv && source venv/bin/activate
pip install -r requirements.txt
cp .env.example .env        # set BOT_TOKEN and ALLOWED_USERS
python bot.py
```

## MikroTik setup (per router)

1. Make sure the **SSH service** is enabled and reachable from the bot's server (default port 22, or any custom port):
   ```
   /ip service print
   ```
2. Create a dedicated user and allow logins only from the bot server's IP:
   ```
   /user add name=tgbackup group=full password=STRONG_PASSWORD address=BOT_SERVER_IP/32
   ```
   `full` is the simplest choice. If you create a more restricted group, it needs at least these policies: `ssh, ftp, read, write, policy, test, sensitive`.
3. For User Manager backups, the `user-manager` package must be installed on the router.

## Usage

| Command | Description |
|---|---|
| `/new` | Create a backup job |
| `/jobs` | List jobs: run now, edit, pause/resume, delete |
| `/timezone` | Set your time zone |
| `/language` | Change the bot's language |
| `/cancel` | Cancel the current step |

### Creating a job (`/new`)

1. **Address**: IP or domain, optionally with an SSH port (`1.2.3.4` or `1.2.3.4:2222`)
2. **Username**
3. **Password**: your message is deleted right after it is read, and the connection is tested immediately
4. **Backup type**: MikroTik backup / User Manager / both, with or without a backup password
5. **ZIP** (optional): no ZIP / ZIP without password / ZIP with password
6. **Temporary folder on the router**
7. **Destination**: private chat, group or channel
8. **Interval**: 1 hour … 7 days, or custom (`30m`, `2h`, `1d`)

### ZIP archives (optional)

If enabled, all files of a run are packed into **one ZIP** and that is what gets sent, for example `MyRouter_2026-01-31_12-00.zip` containing the `.backup` and, if you chose "both", the `.umb`. The default is "no ZIP", so older jobs keep working unchanged.

- **Without password:** regular ZIP (Deflate).
- **With password:** **AES-256** encrypted ZIP (WinZip standard). Opens with 7-Zip, WinRAR, Keka, The Unarchiver and similar tools. The built-in Windows Explorer extractor does not support AES. The weak legacy ZipCrypto method is deliberately not used.
- The ZIP password is independent from the MikroTik backup password. You can use both (an encrypted backup inside an encrypted ZIP).
- If ZIP creation fails, the un-zipped files are **not** sent, so a file you wanted encrypted is never delivered unprotected. The owner is notified of the error.
- The final ZIP must also stay under Telegram's 50 MB limit.
- Change it later in `/jobs` → job → **✏️ Edit** → **🗜 ZIP**.

### Router folder

A path such as `flash/backups`, `disk1/mt-backup` or `backups`. Send `-` (or press the default button) to use the router's root.

- Missing folders (including nested subfolders) are created before each backup with `/file add type=directory`. On versions without this command, the folder is created when the file is saved.
- If the disk (`flash`, `disk1`, `usb1`, …) does not exist, the bot reports a clear error.
- Files are deleted from the router after download; the folder itself stays.

### Editing a job

`/jobs` → pick a job → **✏️ Edit**. You can change: address/port, username, password, backup type, backup password, ZIP, router folder, destination and interval.

Address/username/password changes are verified with a test connection before saving; if it fails, nothing is changed. Saving an address (even the same one) re-accepts the router's SSH host key, which is what you need after resetting a device.

### Automatic retry

If a **scheduled** backup fails (the first backup after creating a job and "Run now" are never retried), the bot retries up to `RETRY_ATTEMPTS` more times, every `RETRY_DELAY_MINUTES` minutes, instead of waiting for the next scheduled run. The job owner is told about everything:

- every failed attempt: a message with a **clear cause** (e.g. "authentication failed", "connection refused", "connection timed out" plus technical details), the attempt number ("attempt 2 of 4") and the time of the next attempt
- success on a retry: "succeeded on attempt n"
- all attempts failed: a final failure message with the last error; the next run follows the normal schedule

**Only transient errors are retried:** timeouts, refused or dropped connections, network/DNS errors and Telegram network errors. Other errors will not fix themselves by waiting, so they are **not retried**. The failure message arrives immediately, with a specific fix:

| Error | Suggested fix |
|---|---|
| Wrong username/password | Edit the username/password |
| SSH host key changed | Save the address again |
| Disk/folder missing, or storage full | Edit the router folder and check free space |
| Router user lacks the `ftp` policy | Add the required permissions |
| Not a RouterOS device / insufficient permissions | Check the address and permissions |
| File larger than 50 MB | Back up a single type or reduce the data |
| ZIP creation failed | Disable ZIP or change the password |
| Bot removed from destination / no permission | Re-add the bot as admin or pick another destination |
| Private/disallowed address | Use a public address |
| Unknown error | Check settings and the server log |

Each of these counts as one failure toward the automatic pause.

**Security note:** the detailed cause is shown only for **existing jobs** (their destination was verified at creation). Connection tests in `/new` and in edit follow the anti-abuse settings and return a uniform error where configured.

**Notes**

- When the next scheduled run is due, pending retries are cancelled and replaced by the new run. Pausing or deleting a job also cancels its pending retries.
- Pending retries are kept in memory and are lost on restart (the normal schedule is unaffected).
- `RETRY_ATTEMPTS=0` disables retrying.

### More behavior

- **Time zone:** `/timezone` offers common zones or "Other" (an IANA name like `Asia/Tehran`, or an offset like `+03:30`). It is per user and affects file names, caption times and "last run".
- With the "both" backup type, if one file cannot be created (for example the `user-manager` package is missing), the other file is still sent and the error for the failed part is reported to the owner. The job is not paused because of it.
- Each job runs at most one backup at a time; "Run now" during a running backup does not start a second one.
- If a backup is interrupted (timeout), leftover `tgbk-*` temp files are removed from the router.
- On the router the bot runs `/system backup save` (and `user-manager database save`), downloads the file over SFTP, deletes it from the router and sends it to Telegram.
- After **5 consecutive failures** a job is paused automatically and the owner is notified (one "failure" is a whole run whose retries all failed).
- For a **channel or group**, the bot must be an admin of that chat, and so must the user.

## Security

- **Encrypted secrets:** router password, backup password and ZIP password are encrypted with Fernet and stored in SQLite. The key comes from `FERNET_KEY` or from `data/.secret.key` (created with mode `0600`). **Back it up and protect it.**
- **SSH host key pinning (TOFU):** the host key is stored on first connection and verified afterwards. After resetting a router, open `/jobs` → job → **Edit** → **Address / port** and save the same address again to accept the new key; there is no need to delete and recreate the job.
- **SSRF protection:** private/local addresses are blocked by default (so the bot cannot scan the server's own network). The address is resolved once and the connection goes directly to that IP, which prevents DNS rebinding.
- **Set `ALLOWED_USERS`.** If it is empty, anyone can use the bot and it will open SSH connections to any public host and port.
- **Abuse protection (port scanning / brute-force):**
  - Every connection test is rate-limited: `TEST_LIMIT_PER_HOUR` per user, and `TARGET_LIMIT_PER_10MIN` per `host:port` across the whole bot. Editing address/username/password counts as a test; scheduled backups do not.
  - On a public bot (`ALLOWED_USERS` empty), all connection failures return one identical message ("could not connect") and take at least `CONN_FAIL_MIN_SECONDS`, so neither the error text nor the response time reveals whether a port is closed, filtered or the password is wrong. The real cause is logged on the server only (`INFO`). Two exceptions are not exploitable and matter to the user: a changed SSH key of their *own* stored router, and "this is not RouterOS" (only after a successful login).
  - **Whitelist:** put trusted user IDs in `TRUSTED_USERS`. They are exempt from the test quotas and from the uniform error/timing (they see full errors and no delay). Other limits (private addresses, job cap, minimum interval) still apply. This list does not replace `ALLOWED_USERS`: if `ALLOWED_USERS` is set, a trusted user must be listed there too.
  - On a private server with `ALLOWED_USERS` set, full error messages are shown (force the uniform behavior with `HIDE_CONN_ERRORS=true`).
- **Bounded input:** router command output and downloaded file sizes are capped, so a malicious SSH server cannot exhaust memory.
- **Sensitive files:** backups contain sensitive data. Send them only to private chats and always set a backup password.
- **Telegram limit:** files over 50 MB cannot be sent.

## Configuration

All settings are environment variables, loaded from `.env` (or passed to the container).

| Variable | Default | Description |
|---|---|---|
| `BOT_TOKEN` | — | Bot token (**required**) |
| `ALLOWED_USERS` | empty | Comma-separated numeric Telegram IDs allowed to use the bot. Empty = everyone (not recommended) |
| `TRUSTED_USERS` | empty | Comma-separated IDs exempt from connection-test quotas and uniform errors |
| `FERNET_KEY` | — | Encryption key. If unset, `data/.secret.key` is generated |
| `DATA_DIR` | `data` (`/app/data` in Docker) | Location of the database and key |
| `MAX_JOBS_PER_USER` | `5` | Job limit per user |
| `MIN_INTERVAL_MINUTES` | `10` | Shortest allowed custom interval |
| `MAX_PARALLEL_BACKUPS` | `3` | Concurrent backups across the whole bot |
| `ALLOW_PRIVATE_IPS` | `false` | Allow private/local addresses (only on a trusted, isolated network) |
| `RETRY_ATTEMPTS` | `3` | Retries after a failed scheduled backup (`0` = off) |
| `RETRY_DELAY_MINUTES` | `5` | Minutes between retries |
| `TEST_LIMIT_PER_HOUR` | `10` | Connection tests per user per hour (`0` = off) |
| `TARGET_LIMIT_PER_10MIN` | `5` | Connection tests per `host:port` per 10 minutes, all users (`0` = off) |
| `CONN_FAIL_MIN_SECONDS` | `20` | Minimum duration of a failed test (response-time equalization) |
| `HIDE_CONN_ERRORS` | `auto` | `auto` = uniform errors only when `ALLOWED_USERS` is empty; `true` / `false` to force |

Generate a key:

```bash
python3 -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
```

To find your numeric Telegram ID, message a bot such as [@userinfobot](https://t.me/userinfobot).

## Project structure

```
.
├── bot.py               # the bot: handlers, scheduler, SSH/SFTP, crypto, database
├── i18n.py              # translations (en, es, ru, zh, fa)
├── requirements.txt
├── .env.example         # configuration template
├── README.md            # English
├── README.es.md
├── README.ru.md
├── README.zh.md
├── README.fa.md
└── docker/
    ├── Dockerfile
    ├── docker-compose.yml
    ├── entrypoint.sh    # env validation, permission fix, drops privileges
    └── Dockerfile.dockerignore
```

## Upgrading

Replace the files and run `pip install -r requirements.txt` (the `pyzipper` package is required for encrypted ZIP). With Docker, rebuild: `docker compose --env-file .env -f docker/docker-compose.yml up -d --build`. New database columns are added automatically, and existing jobs keep working (folder = root, time zone = UTC, ZIP = off).

## License

Released under the [MIT License](LICENSE).
