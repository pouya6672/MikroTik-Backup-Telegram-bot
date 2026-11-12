# MikroTik Backup Bot

[English](README.md) | [Español](README.es.md) | [Русский](README.ru.md) | **中文** | [فارسی](README.fa.md)

一个 Telegram 机器人，可按你设定的计划自动备份 MikroTik 路由器，并把备份文件发送到私聊、群组或频道。

机器人通过 SSH 连接路由器，创建备份，经 SFTP 下载，从路由器上删除，再发送到 Telegram。每个任务之后都可以编辑，机器人本身支持 🇬🇧 English、🇪🇸 Español、🇷🇺 Русский、🇨🇳 中文 和 🇮🇷 فارسی。

## 功能特性

- **备份类型：** 二进制备份（`.backup`，可设密码或不设）、User Manager 数据库（`.umb`），或两者都备份
- **可选 ZIP：** 普通 ZIP 或 **AES-256** 加密 ZIP
- **灵活的接收位置：** 私聊、群组或频道
- **灵活的计划：** 从 1 小时到 7 天的预设，或自定义间隔，如 `30m`、`2h`、`1d`（最长 30 天）
- **自动重试：** 针对临时性故障，并给出清晰、可操作的错误提示
- **按用户设置时区：** 影响文件名、说明文字时间和“上次运行”
- **多用户：** 每用户任务数限制、全局并发限制、访问控制
- **默认安全：** 凭据加密、SSH 主机密钥固定、屏蔽内网 IP、防滥用限流
- **支持 Docker**，以非 root 用户运行

## 环境要求

- Python **3.10+**（或 Docker）
- 来自 [@BotFather](https://t.me/BotFather) 的机器人令牌
- 已启用 SSH 且机器人所在服务器可访问的 MikroTik 路由器
- Python 依赖：`python-telegram-bot[job-queue]`、`asyncssh`、`pyzipper`、`cryptography`、`python-dotenv`、`tzdata`

## 安装

### 方式一：Docker（推荐）

```bash
git clone https://github.com/pouya6672/MikroTik-Backup-Telegram-bot.git
cd MikroTik-Backup-Telegram-bot
cp .env.example .env        # 设置 BOT_TOKEN 和 ALLOWED_USERS
docker compose --env-file .env -f docker/docker-compose.yml up -d --build
docker compose -f docker/docker-compose.yml logs -f
```

- 数据库和加密密钥保存在 `mtbackup-data` 卷（`/app/data`）中。**请务必保留该卷。**
- 容器仅在修正卷权限时以 root 启动，随后通过 `gosu` 切换为无特权的 `bot` 用户（uid 10001）。
- `docker/entrypoint.sh` 会在启动时校验所有环境变量，出现问题时给出清晰的错误（或警告），例如缺少 `BOT_TOKEN`、`FERNET_KEY` 无效或 `ALLOWED_USERS` 为空。
- 机器人使用长轮询，因此**无需开放任何入站端口**。

不使用 Compose：

```bash
docker build -f docker/Dockerfile -t mikrotik-backup-bot .
docker run -d --name mtbackup --restart unless-stopped \
  --env-file .env -v mtbackup-data:/app/data mikrotik-backup-bot
```

### 方式二：直接使用 Python

```bash
python3 -m venv venv && source venv/bin/activate
pip install -r requirements.txt
cp .env.example .env        # 设置 BOT_TOKEN 和 ALLOWED_USERS
python bot.py
```

## MikroTik 端设置（每台路由器）

1. 确保 **SSH 服务**已启用，并且机器人所在服务器可以访问（默认端口 22，或任意自定义端口）：
   ```
   /ip service print
   ```
2. 创建专用用户，并只允许来自机器人服务器 IP 的登录：
   ```
   /user add name=tgbackup group=full password=STRONG_PASSWORD address=BOT_SERVER_IP/32
   ```
   `full` 组最简单。如果创建权限更受限的组，至少需要这些策略：`ssh, ftp, read, write, policy, test, sensitive`。
3. 如需备份 User Manager，路由器上必须安装 `user-manager` 软件包。

## 使用方法

| 命令 | 说明 |
|---|---|
| `/new` | 创建备份任务 |
| `/jobs` | 任务列表：立即运行、编辑、暂停/恢复、删除 |
| `/timezone` | 设置你的时区 |
| `/language` | 更改机器人语言 |
| `/cancel` | 取消当前步骤 |

### 创建任务（`/new`）

1. **地址：** IP 或域名，可附带 SSH 端口（`1.2.3.4` 或 `1.2.3.4:2222`）
2. **用户名**
3. **密码：** 你发送的消息读取后会立即被删除，并马上测试连接
4. **备份类型：** MikroTik / User Manager / 两者，可设或不设备份密码
5. **ZIP**（可选）：不压缩 / 无密码 ZIP / 有密码 ZIP
6. **路由器上的临时文件夹**
7. **接收位置：** 私聊、群组或频道
8. **间隔：** 1 小时 … 7 天，或自定义（`30m`、`2h`、`1d`）

### ZIP 压缩包（可选）

启用后，每次运行的所有文件会被打包进**一个 ZIP**，并发送该 ZIP，例如 `MyRouter_2026-01-31_12-00.zip`，其中包含 `.backup`，若选择“两者”则还包含 `.umb`。默认是“不压缩”，因此旧任务的行为不变。

- **无密码：** 普通 ZIP（Deflate）。
- **有密码：** **AES-256** 加密 ZIP（WinZip 标准）。可用 7-Zip、WinRAR、Keka、The Unarchiver 等工具打开。Windows 资源管理器内置的解压功能不支持 AES。出于安全考虑，刻意不使用旧的、较弱的 ZipCrypto。
- ZIP 密码与 MikroTik 备份密码相互独立，可以同时设置（加密的备份放进加密的 ZIP）。
- 如果创建 ZIP 失败，**不会**发送未压缩的文件，以免本应加密的文件被无保护地发出。错误会通知任务所有者。
- 最终的 ZIP 同样必须小于 Telegram 的 50 MB 限制。
- 之后可在 `/jobs` → 任务 → **✏️ 编辑** → **🗜 ZIP** 中修改。

### 路由器文件夹

路径形如 `flash/backups`、`disk1/mt-backup` 或 `backups`。发送 `-`（或点击默认按钮）即使用路由器根目录。

- 不存在的文件夹（包括嵌套子文件夹）会在每次备份前通过 `/file add type=directory` 创建。在不支持该命令的版本上，文件夹会在保存文件时创建。
- 如果磁盘（`flash`、`disk1`、`usb1` 等）不存在，机器人会给出清晰的错误。
- 文件下载后会从路由器删除，文件夹保留。

### 编辑任务

`/jobs` → 选择任务 → **✏️ 编辑**。可修改：地址/端口、用户名、密码、备份类型、备份密码、ZIP、路由器文件夹、接收位置和间隔。

修改地址/用户名/密码时，保存前会先用测试连接验证；若失败则不做任何更改。保存地址（即使与原来相同）会重新接受路由器的 SSH 主机密钥，重置设备后就需要这样做。

### 自动重试

如果**定时**备份失败（创建任务后的首次备份和“立即运行”不会重试），机器人不会等到下一个计划时间，而是最多再重试 `RETRY_ATTEMPTS` 次，每次间隔 `RETRY_DELAY_MINUTES` 分钟。任务所有者会收到全部通知：

- 每次失败：包含**清晰原因**的消息（如“认证失败”“连接被拒绝”“连接超时”及技术细节）、尝试次数（“第 2 次，共 4 次”）以及下次尝试的时间
- 重试成功：“第 n 次尝试成功”
- 所有尝试都失败：附带最后一个错误的最终失败消息；下一次运行按正常计划进行

**只有临时性错误会重试：** 超时、连接被拒绝或中断、网络/DNS 错误以及 Telegram 网络错误。其他错误靠等待无法解决，所以**不会重试**；失败消息会立即送达，并附带具体的解决办法：

| 错误 | 建议的解决办法 |
|---|---|
| 用户名/密码错误 | 编辑用户名/密码 |
| SSH 主机密钥已变化 | 重新保存地址 |
| 磁盘/文件夹不存在，或存储已满 | 编辑路由器文件夹并检查剩余空间 |
| 路由器用户缺少 `ftp` 策略 | 添加所需权限 |
| 不是 RouterOS 设备 / 权限不足 | 检查地址和权限 |
| 文件超过 50 MB | 只备份一种类型或减少数据 |
| ZIP 创建失败 | 关闭 ZIP 或更换密码 |
| 机器人已被移出接收位置 / 没有权限 | 重新设为管理员或选择其他接收位置 |
| 内网/不允许的地址 | 使用公网地址 |
| 未知错误 | 检查设置和服务器日志 |

以上每一种都会计为一次失败，用于自动暂停的计数。

**安全说明：** 详细原因只对**已有任务**显示（其接收位置在创建时已验证）。`/new` 和编辑中的连接测试遵循防滥用设置，并在相应配置下返回统一的错误。

**说明**

- 到达下一个计划时间时，等待中的重试会被取消，由新的运行取代。暂停或删除任务同样会取消其等待中的重试。
- 等待中的重试保存在内存中，重启后会丢失（正常计划不受影响）。
- `RETRY_ATTEMPTS=0` 可关闭重试。

### 其他行为

- **时区：** `/timezone` 提供常用时区或“其他”（IANA 名称，如 `Asia/Tehran`，或偏移量，如 `+03:30`）。该设置按用户生效，影响文件名、说明文字时间和“上次运行”。
- 备份类型为“两者”时，如果其中一个文件无法创建（例如未安装 `user-manager` 软件包），另一个文件仍会发送，失败部分的错误会通知所有者。任务不会因此被暂停。
- 每个任务同一时间最多运行一个备份；在备份运行期间点击“立即运行”不会启动第二个。
- 如果备份中途被中断（超时），残留的 `tgbk-*` 临时文件会从路由器上清除。
- 在路由器上，机器人执行 `/system backup save`（以及 `user-manager database save`），通过 SFTP 下载文件，从路由器删除，再发送到 Telegram。
- **连续 5 次失败**后，任务会自动暂停并通知所有者（一次“失败”指整次运行的所有重试都失败）。
- 对于**频道或群组**，机器人必须是该聊天的管理员，用户也必须是。

## 安全

- **加密保存密钥信息：** 路由器密码、备份密码和 ZIP 密码使用 Fernet 加密后存入 SQLite。密钥来自 `FERNET_KEY` 或 `data/.secret.key`（以 `0600` 权限创建）。**请备份并妥善保护它。**
- **SSH 主机密钥固定（TOFU）：** 首次连接时保存主机密钥，之后每次连接都会验证。重置路由器后，打开 `/jobs` → 任务 → **编辑** → **地址 / 端口**，重新保存同一个地址即可接受新密钥，无需删除后重建任务。
- **SSRF 防护：** 默认屏蔽内网/本地地址（防止机器人扫描服务器所在网络）。地址只解析一次，并直接连接到该 IP，从而防止 DNS rebinding。
- **请务必设置 `ALLOWED_USERS`。** 如果为空，任何人都能使用机器人，机器人会向任意公网主机和端口发起 SSH 连接。
- **防滥用（端口扫描 / 暴力破解）：**
  - 每次连接测试都有限额：每用户每小时 `TEST_LIMIT_PER_HOUR` 次，整个机器人对每个 `host:port` 每 10 分钟 `TARGET_LIMIT_PER_10MIN` 次。编辑地址/用户名/密码也算一次测试；定时备份不算。
  - 在公开机器人中（`ALLOWED_USERS` 为空），所有连接失败都返回同一条消息（“无法连接”），并且至少耗时 `CONN_FAIL_MIN_SECONDS` 秒，这样既无法从错误文本、也无法从响应时间判断端口是关闭、被过滤，还是密码错误。真实原因只记录在服务器日志中（`INFO`）。有两个无法被利用、且对用户本人很重要的例外：用户*自己*已保存路由器的 SSH 密钥发生变化，以及“这不是 RouterOS”（仅在成功登录之后）。
  - **白名单：** 将可信用户 ID 填入 `TRUSTED_USERS`。他们不受连接测试限额以及统一错误/耗时的限制（可看到完整错误且没有延迟）。其他限制（内网地址、任务数上限、最小间隔）对他们依然有效。该列表不能替代 `ALLOWED_USERS`：如果设置了 `ALLOWED_USERS`，可信用户也必须在其中。
  - 在设置了 `ALLOWED_USERS` 的私人服务器上，会显示完整的错误消息（可用 `HIDE_CONN_ERRORS=true` 强制启用统一行为）。
- **输入有上限：** 路由器命令输出和下载文件的大小都有上限，恶意 SSH 服务器无法耗尽内存。
- **敏感文件：** 备份包含敏感数据。只发送到私聊，并始终设置备份密码。
- **Telegram 限制：** 超过 50 MB 的文件无法发送。

## 配置

所有设置都是环境变量，从 `.env` 加载（或传给容器）。

| 变量 | 默认值 | 说明 |
|---|---|---|
| `BOT_TOKEN` | — | 机器人令牌（**必填**） |
| `ALLOWED_USERS` | 空 | 允许使用机器人的 Telegram 数字 ID，用逗号分隔。空 = 所有人（不推荐） |
| `TRUSTED_USERS` | 空 | 用逗号分隔的 ID，免除连接测试限额和统一错误 |
| `FERNET_KEY` | — | 加密密钥。未设置时会生成 `data/.secret.key` |
| `DATA_DIR` | `data`（Docker 中为 `/app/data`） | 数据库和密钥的位置 |
| `MAX_JOBS_PER_USER` | `5` | 每用户任务数上限 |
| `MIN_INTERVAL_MINUTES` | `10` | 允许的最短自定义间隔 |
| `MAX_PARALLEL_BACKUPS` | `3` | 整个机器人的并发备份数 |
| `ALLOW_PRIVATE_IPS` | `false` | 允许内网/本地地址（仅限隔离的可信网络） |
| `RETRY_ATTEMPTS` | `3` | 定时备份失败后的重试次数（`0` = 关闭） |
| `RETRY_DELAY_MINUTES` | `5` | 重试间隔（分钟） |
| `TEST_LIMIT_PER_HOUR` | `10` | 每用户每小时的连接测试次数（`0` = 关闭） |
| `TARGET_LIMIT_PER_10MIN` | `5` | 每个 `host:port` 每 10 分钟的连接测试次数，所有用户合计（`0` = 关闭） |
| `CONN_FAIL_MIN_SECONDS` | `20` | 失败测试的最短耗时（用于统一响应时间） |
| `HIDE_CONN_ERRORS` | `auto` | `auto` = 仅当 `ALLOWED_USERS` 为空时使用统一错误；可设为 `true` / `false` 强制 |

生成密钥：

```bash
python3 -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
```

要查询自己的 Telegram 数字 ID，可给 [@userinfobot](https://t.me/userinfobot) 之类的机器人发消息。

## 项目结构

```
.
├── bot.py               # 机器人：处理器、调度器、SSH/SFTP、加密、数据库
├── i18n.py              # 翻译（en、es、ru、zh、fa）
├── requirements.txt
├── .env.example         # 配置模板
├── README.md            # English
├── README.es.md
├── README.ru.md
├── README.zh.md
├── README.fa.md
└── docker/
    ├── Dockerfile
    ├── docker-compose.yml
    ├── entrypoint.sh    # 校验变量、修正权限、降低权限
    └── Dockerfile.dockerignore
```

## 升级

替换文件并运行 `pip install -r requirements.txt`（加密 ZIP 需要 `pyzipper` 包）。使用 Docker 时请重新构建：`docker compose --env-file .env -f docker/docker-compose.yml up -d --build`。新的数据库列会自动添加，现有任务继续正常工作（文件夹 = 根目录，时区 = UTC，ZIP = 关闭）。

## 许可证

基于 [MIT 许可证](LICENSE)发布。
