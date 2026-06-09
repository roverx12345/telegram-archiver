# Telegram Archiver

这个项目是一个 Telegram 媒体归档器，用来把 Telegram 媒体自动下载、去重并保存到本地或 NAS。

它现在按“一个软件，两套 source”组织：

- `bot`：Telegram Bot API source，处理私聊里转发给 bot 的媒体。
- `saved`：Telethon Saved Messages source，扫描并监听你自己的 Saved Messages。

两套 source 共用同一个数据库、下载目录、文件命名和去重逻辑；现有 `.env`、数据库和下载目录会继续沿用，不需要重新配置。

## 已实现需求

1. 转发到 bot 的媒体会自动下载并保存。
2. Saved Messages 里的历史和新增媒体可以自动归档。
3. Saved Messages 媒体下载中断后，会从 `.part` 半成品继续下载。
4. 不按文件名去重，优先使用 Telegram 的文件标识，再用下载后的 `sha256` 做二次确认。
5. 支持 `/pair <code>` 配对授权，避免任意知道 bot 用户名的人都能直接使用。
6. 支持接入本地 Telegram Bot API server 来加速 bot source 下载。
7. bot 重启后会继续处理 Telegram 仍保留的积压消息，不会在启动时主动清空。
8. bot 下载任务会落库保存，失败后可自动重试，异常重启后会继续恢复未完成任务。
9. bot 启动时会自动注册 Telegram 命令菜单、简介和 `/help` 文档入口。

## 方案说明

### 去重逻辑

- 第一层：收到消息后先查 `file_unique_id`。命中则直接跳过，不重新下载。
- 第二层：若 `file_unique_id` 未命中，先下载到临时目录，计算 `sha256`。
- 若 `sha256` 已存在，说明虽然 Telegram 侧标识不同，但内容已经保存过，删除临时文件并记录别名映射。

### 任务恢复与重试

- 每条待下载的转发媒体都会先写入 `download_jobs` 表。
- 下载开始时状态会变成 `downloading`，成功后变成 `completed`，命中重复则记为 `duplicate`。
- 下载失败会记为 `failed` 并记录错误原因。
- bot 重启时会扫描 `pending / downloading / failed` 且未超过重试上限的任务，自动再次尝试下载。
- 当前恢复是“重新下载整文件”，不是断点续传。

### Saved Messages 断点续传

- `telegram-archiver saved` 下载媒体时会写入 `DOWNLOAD_DIR/.tmp/*.part`。
- 如果下载中断，`.part` 文件会保留。
- 下次处理同一条 Saved Messages 媒体时，会从 `.part` 当前大小继续下载。
- 下载完整后才会计算 `sha256`、去重、移动到最终分类目录。
- `telegram-archiver bot` 仍然是失败后整文件重试，不做断点续传。

### 两套 source 的关系

- `telegram-archiver bot` 使用 Bot API，只处理私聊中转发给 bot 的媒体。
- `telegram-archiver saved` 使用 Telethon 用户会话，处理 Saved Messages。
- `telegram-archiver all` 会用一个 supervisor 同时启动两套 source。
- 两套 source 都写入 `DB_PATH` 指向的同一个 SQLite 数据库，并保存到 `DOWNLOAD_DIR`。
- 文件保存会按媒体类型放入 `photos / videos / audio / documents / stickers / other` 等目录。

### 为什么还要做配对

Telegram bot 支持私聊，但“私聊 bot”不等于“只有你能用”。只要别人知道 bot 用户名并主动 `/start`，就能和它建立会话。因此这里默认建议启用 `PAIR_CODE`。

### 下载加速

项目支持通过 `LOCAL_BOT_API_URL` 接入本地 Bot API server。参考了 GitHub 上的两个思路：

- `avibn/telegram-downloader`：基于 `python-telegram-bot`，通过本地 Bot API server 下载。
- `rodriguezst/telethon_downloader`：基于 Telethon，并通过 `cryptg` 提升下载速度。

这两个项目都没有直接覆盖“只处理转发消息 + 双层去重 + 私聊配对授权”，所以这里没有直接 fork，而是单独实现。

## 配置

复制 `.env.example` 为 `.env`，至少填写：

```env
BOT_TOKEN=...
PAIR_CODE=...
DOWNLOAD_DIR=./downloads
DB_PATH=./data/bot.db
LOCAL_BOT_API_URL=http://telegram-bot-api:8081
MAX_DOWNLOAD_RETRIES=3
```

如果运行 `telegram-archiver saved` 下载 Saved Messages，可以让 Telethon 单独走 SOCKS5：

```env
TELETHON_PROXY=socks5h://用户名:密码@代理地址:端口
```

`TELETHON_PROXY` 依赖 `python-socks[asyncio]`，只影响 Saved Messages source。

如果你不想启用配对，可以删掉 `PAIR_CODE`，并设置：

```env
ALLOW_UNPAIRED_PRIVATE=true
```

## 本地运行

```bash
uv venv
source .venv/bin/activate
uv pip install -e '.[dev]'
telegram-archiver bot
```

可用启动方式：

```bash
telegram-archiver bot
telegram-archiver saved
telegram-archiver saved-stats
telegram-archiver all
```

只统计 Saved Messages 待处理数量、不下载：

```bash
telegram-archiver saved-stats
```

如果 Docker 里的 `saved-archiver` 正在运行，要先停掉它再统计，因为 Telethon 同一个 session 文件不能被两个进程同时打开：

```bash
docker compose stop saved-archiver
docker compose run --rm saved-archiver telegram-archiver saved-stats
# 快速抽样：
docker compose run --rm saved-archiver telegram-archiver saved-stats --limit 1000
docker compose start saved-archiver
```

## Linux 部署

建议在 Linux 上直接用 `systemd + Python venv` 部署，简单且稳定。

```bash
git clone https://github.com/roverx12345/telegram-archiver.git /opt/telegram-archiver
cd /opt/telegram-archiver
cp .env.example .env
python3 -m venv .venv
source .venv/bin/activate
pip install -e '.[dev]'
```

把 `.env` 填好后，可以先手动启动验证：

```bash
source .venv/bin/activate
telegram-archiver bot
```

项目已提供 `systemd` 模板：[telegram-archiver-bot.service](./deploy/systemd/telegram-archiver-bot.service)

把它复制到 `/etc/systemd/system/telegram-archiver-bot.service` 后，根据你的实际 Linux 用户和部署目录修改这些字段：

- `User=telebot`
- `WorkingDirectory=/opt/telegram-archiver`
- `ExecStart=/opt/telegram-archiver/.venv/bin/telegram-archiver bot`

启用服务：

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now telegram-archiver-bot
sudo systemctl status telegram-archiver-bot
journalctl -u telegram-archiver-bot -f
```

## Windows 运行

在 Windows 上建议直接运行 Python 版，不必额外折腾本地 Bot API server。

```powershell
git clone https://github.com/roverx12345/telegram-archiver.git
cd telegram-archiver
py -m venv .venv
.venv\Scripts\Activate.ps1
pip install -e ".[dev]"
copy .env.example .env
```

然后编辑 `.env`，至少填写：

```env
BOT_TOKEN=...
PAIR_CODE=...
DOWNLOAD_DIR=./downloads
DB_PATH=./data/bot.db
LOCAL_BOT_API_URL=
LOG_LEVEL=INFO
MAX_DOWNLOAD_RETRIES=3
```

启动命令：

```powershell
.venv\Scripts\telegram-archiver.exe bot
```

如果 PowerShell 默认禁止脚本执行，可以先运行：

```powershell
Set-ExecutionPolicy -Scope CurrentUser RemoteSigned
```

## Docker Compose

项目现在默认启动两个 source：

- `bot`：转发给 bot 的媒体归档。
- `saved-archiver`：Saved Messages 归档。

另外还有两种 Docker 模式：

- 基础模式：使用官方云端 Bot API。
- 大文件模式：额外启动本地 Bot API server，适合超过官方云端下载上限的 bot source 文件。

基础模式：

```bash
docker compose up --build
```

大文件模式：

```bash
docker compose -f docker-compose.yml -f docker-compose.large-files.yml up --build
```

如果要启用大文件模式，还需要在 `.env` 里补充：

```env
TELEGRAM_API_ID=...
TELEGRAM_API_HASH=...
```

说明：

- `docker-compose.yml` 是基础版。
- `docker-compose.large-files.yml` 会给 bot 注入 `LOCAL_BOT_API_URL=http://telegram-bot-api:8081`。
- bot 容器会等待本地 Bot API server 就绪后再启动。

## CI

项目已提供 GitHub Actions 工作流：[ci.yml](/Users/roverx/Documents/app/tele_bot/.github/workflows/ci.yml)

默认会在以下场景自动执行：

- push 到 `main`
- pull request

执行内容包括：

- 安装依赖
- 运行 `pytest -q`
- 运行 `python -m compileall src tests`

## 使用方式

1. 在 Telegram 私聊中先发 `/pair <你的配对码>`。
2. 把媒体消息转发给 bot。
3. bot 会自动判断是否已保存；未保存则下载，已保存则直接跳过。

### 管理命令

- `/status`：查看当前会话的任务统计。
- `/jobs`：查看最近任务和状态摘要。
- `/failed`：查看失败任务，以及哪些任务还能重试。
- `/retry_failed`：手动重试当前会话内还没超过上限的失败任务。

## 离线补拉说明

当前版本启动时不会清空 Telegram 的 pending updates，因此如果 bot 短时间离线，重新上线后会继续处理 Telegram 仍然保留的未投递消息。

但这不是无限补拉：

- 能补到的前提是 Telegram 仍保留这些 pending updates。
- Telegram 官方通常不会保留超过 24 小时的未投递 update。
- 如果离线时间过长，超出 Telegram 的保留窗口，这部分消息仍然拿不回来。

## 当前支持的媒体类型

- document
- video
- audio
- voice
- animation
- photo
- sticker
- video_note
