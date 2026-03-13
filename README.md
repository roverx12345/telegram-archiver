# Telegram Forward Archiver Bot

这个项目实现了一个 Telegram bot，用来在私聊中接收“转发过来的媒体消息”，自动下载、去重并保存到本地。

## 已实现需求

1. 转发到 bot 的媒体会自动下载并保存。
2. 不按文件名去重，优先使用 Telegram 的 `file_unique_id`，再用下载后的 `sha256` 做二次确认。
3. 支持 `/pair <code>` 配对授权，避免任意知道 bot 用户名的人都能直接使用。
4. 支持接入本地 Telegram Bot API server 来加速下载。
5. bot 重启后会继续处理 Telegram 仍保留的积压消息，不会在启动时主动清空。

## 方案说明

### 去重逻辑

- 第一层：收到消息后先查 `file_unique_id`。命中则直接跳过，不重新下载。
- 第二层：若 `file_unique_id` 未命中，先下载到临时目录，计算 `sha256`。
- 若 `sha256` 已存在，说明虽然 Telegram 侧标识不同，但内容已经保存过，删除临时文件并记录别名映射。

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
```

如果你不想启用配对，可以删掉 `PAIR_CODE`，并设置：

```env
ALLOW_UNPAIRED_PRIVATE=true
```

## 本地运行

```bash
uv venv
source .venv/bin/activate
uv pip install -e '.[dev]'
tele-bot
```

## Windows 运行

在 Windows 上建议直接运行 Python 版，不必额外折腾本地 Bot API server。

```powershell
git clone https://github.com/roverx12345/tele_bot.git
cd tele_bot
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
```

启动命令：

```powershell
.venv\Scripts\tele-bot.exe
```

如果 PowerShell 默认禁止脚本执行，可以先运行：

```powershell
Set-ExecutionPolicy -Scope CurrentUser RemoteSigned
```

## Docker Compose

如果你想同时跑 bot 和本地 Bot API server：

```bash
docker compose up --build
```

还需要在 `.env` 里补充：

```env
TELEGRAM_API_ID=...
TELEGRAM_API_HASH=...
LOCAL_BOT_API_URL=http://telegram-bot-api:8081
```

## 使用方式

1. 在 Telegram 私聊中先发 `/pair <你的配对码>`。
2. 把媒体消息转发给 bot。
3. bot 会自动判断是否已保存；未保存则下载，已保存则直接跳过。

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
