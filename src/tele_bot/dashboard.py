from __future__ import annotations

import html
import json
import os
import sqlite3
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from dotenv import load_dotenv

from .config import load_download_dir


NON_BOT_SOURCE_CASE = """
CASE
    WHEN tfa.telegram_file_unique_id LIKE 'saved:%'
      OR tfa.telegram_file_unique_id LIKE 'saved-text:%'
    THEN 'saved'
    WHEN tfa.telegram_file_unique_id LIKE 'channel\\_%:%' ESCAPE '\\'
      OR tfa.telegram_file_unique_id LIKE 'channel\\_%-text:%' ESCAPE '\\'
    THEN 'channels'
    ELSE 'unknown'
END
"""


@dataclass(frozen=True)
class DashboardSettings:
    host: str
    port: int
    db_path: Path
    download_dir: Path
    refresh_seconds: int
    active_partial_seconds: int
    stale_partial_days: int


def load_dashboard_settings() -> DashboardSettings:
    load_dotenv()
    db_path = Path(os.getenv("DB_PATH", "./data/bot.db")).expanduser().resolve()
    return DashboardSettings(
        host=os.getenv("DASHBOARD_HOST", "127.0.0.1").strip() or "127.0.0.1",
        port=max(1, int(os.getenv("DASHBOARD_PORT", "8765"))),
        db_path=db_path,
        download_dir=load_download_dir(),
        refresh_seconds=max(1, int(os.getenv("DASHBOARD_REFRESH_SECONDS", "10"))),
        active_partial_seconds=max(1, int(os.getenv("DASHBOARD_ACTIVE_PARTIAL_SECONDS", "600"))),
        stale_partial_days=max(0, int(os.getenv("DASHBOARD_STALE_PARTIAL_DAYS", "30"))),
    )


def run_dashboard() -> None:
    settings = load_dashboard_settings()
    server = build_dashboard_server(settings)
    print(f"Dashboard listening on http://{settings.host}:{settings.port}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


def build_dashboard_server(settings: DashboardSettings) -> ThreadingHTTPServer:
    class DashboardHandler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802 - stdlib hook name.
            parsed = urlparse(self.path)
            if parsed.path == "/":
                self.send_html(render_dashboard_html(settings))
                return
            if parsed.path == "/api/status":
                self.send_json(collect_dashboard_status(settings))
                return
            self.send_error(404)

        def log_message(self, format: str, *args: Any) -> None:
            return

        def send_html(self, body: str) -> None:
            payload = body.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def send_json(self, body: dict[str, Any]) -> None:
            payload = json.dumps(body, ensure_ascii=False, sort_keys=True).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

    return ThreadingHTTPServer((settings.host, settings.port), DashboardHandler)


def collect_dashboard_status(settings: DashboardSettings) -> dict[str, Any]:
    now = time.time()
    db_summary = collect_database_summary(settings.db_path)
    partials = collect_partial_summary(
        settings.download_dir / ".tmp",
        now=now,
        active_partial_seconds=settings.active_partial_seconds,
        stale_partial_days=settings.stale_partial_days,
    )
    latest_archive = db_summary.get("latest_archive_at")
    failures = db_summary.get("failures", {"unresolved_count": 0})
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "settings": {
            "db_path": str(settings.db_path),
            "download_dir": str(settings.download_dir),
            "refresh_seconds": settings.refresh_seconds,
            "active_partial_seconds": settings.active_partial_seconds,
            "stale_partial_days": settings.stale_partial_days,
        },
        "health": classify_health(latest_archive, partials, failures),
        "database": db_summary,
        "partials": partials,
    }


def run_health() -> int:
    settings = load_dashboard_settings()
    status = collect_dashboard_status(settings)
    print(format_health_report(status))
    if not status["database"]["available"]:
        return 1
    if not Path(status["settings"]["download_dir"]).exists():
        return 1
    return 0


def format_health_report(status: dict[str, Any]) -> str:
    database = status["database"]
    failures = database.get("failures", {})
    partials = status["partials"]
    settings = status["settings"]
    lines = [
        "Telegram Archiver health",
        f"status={status['health']['level']}",
        f"message={status['health']['message']}",
        f"db_available={database['available']}",
        f"db_path={settings['db_path']}",
        f"download_dir={settings['download_dir']}",
        f"download_dir_exists={Path(settings['download_dir']).exists()}",
        f"non_bot_files={database['totals']['files']}",
        f"non_bot_bytes={database['totals']['bytes']}",
        f"latest_archive_at={database.get('latest_archive_at')}",
        f"partials={partials['count']}",
        f"partial_bytes={partials['bytes']}",
        f"active_partials={partials['active_count']}",
        f"stale_partials={partials['stale_count']}",
        f"unresolved_failures={failures.get('unresolved_count', 0)}",
        f"retryable_failures={failures.get('retryable_count', 0)}",
    ]
    if database.get("error"):
        lines.append(f"db_error={database['error']}")
    return "\n".join(lines)


def collect_database_summary(db_path: Path) -> dict[str, Any]:
    if not db_path.exists():
        return {
            "available": False,
            "error": f"database not found: {db_path}",
            "totals": {"files": 0, "bytes": 0},
            "sources": [],
            "media_types": [],
            "recent_archives": [],
            "failures": empty_failure_summary(),
            "latest_archive_at": None,
        }

    try:
        connection = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        connection.row_factory = sqlite3.Row
    except sqlite3.Error as exc:
        return {
            "available": False,
            "error": f"{exc.__class__.__name__}: {exc}",
            "totals": {"files": 0, "bytes": 0},
            "sources": [],
            "media_types": [],
            "recent_archives": [],
            "failures": empty_failure_summary(),
            "latest_archive_at": None,
        }

    try:
        sources = rows_to_dicts(
            connection.execute(
                f"""
                WITH non_bot AS (
                    SELECT DISTINCT sf.sha256, {NON_BOT_SOURCE_CASE} AS source
                    FROM saved_files sf
                    JOIN telegram_file_aliases tfa ON tfa.file_sha256 = sf.sha256
                    WHERE {NON_BOT_SOURCE_CASE} != 'unknown'
                )
                SELECT nb.source,
                       COUNT(*) AS files,
                       COALESCE(SUM(sf.file_size), 0) AS bytes,
                       MAX(sf.created_at) AS latest_archive_at
                FROM non_bot nb
                JOIN saved_files sf ON sf.sha256 = nb.sha256
                GROUP BY nb.source
                ORDER BY nb.source
                """
            )
        )
        media_types = rows_to_dicts(
            connection.execute(
                f"""
                WITH non_bot AS (
                    SELECT DISTINCT sf.sha256
                    FROM saved_files sf
                    JOIN telegram_file_aliases tfa ON tfa.file_sha256 = sf.sha256
                    WHERE {NON_BOT_SOURCE_CASE} != 'unknown'
                )
                SELECT sf.media_type,
                       COUNT(*) AS files,
                       COALESCE(SUM(sf.file_size), 0) AS bytes,
                       MAX(sf.created_at) AS latest_archive_at
                FROM non_bot nb
                JOIN saved_files sf ON sf.sha256 = nb.sha256
                GROUP BY sf.media_type
                ORDER BY files DESC, sf.media_type
                """
            )
        )
        recent_archives = rows_to_dicts(
            connection.execute(
                f"""
                WITH non_bot AS (
                    SELECT sf.sha256,
                           MIN({NON_BOT_SOURCE_CASE}) AS source
                    FROM saved_files sf
                    JOIN telegram_file_aliases tfa ON tfa.file_sha256 = sf.sha256
                    WHERE {NON_BOT_SOURCE_CASE} != 'unknown'
                    GROUP BY sf.sha256
                )
                SELECT nb.source,
                       sf.media_type,
                       sf.original_name,
                       sf.file_size,
                       sf.final_path,
                       sf.created_at
                FROM non_bot nb
                JOIN saved_files sf ON sf.sha256 = nb.sha256
                ORDER BY sf.created_at DESC
                LIMIT 20
                """
            )
        )
        totals = connection.execute(
            f"""
            SELECT COUNT(DISTINCT sf.sha256) AS files,
                   COALESCE(SUM(sf.file_size), 0) AS bytes
            FROM saved_files sf
            WHERE sf.sha256 IN (
                SELECT DISTINCT sf2.sha256
                FROM saved_files sf2
                JOIN telegram_file_aliases tfa ON tfa.file_sha256 = sf2.sha256
                WHERE {NON_BOT_SOURCE_CASE} != 'unknown'
            )
            """
        ).fetchone()
        latest_archive_at = query_scalar(
            connection,
            f"""
            SELECT MAX(sf.created_at)
            FROM saved_files sf
            JOIN telegram_file_aliases tfa ON tfa.file_sha256 = sf.sha256
            WHERE {NON_BOT_SOURCE_CASE} != 'unknown'
            """,
        )
        failures = collect_failure_summary(connection)
        total_files = int(totals["files"] or 0)
        total_bytes = int(totals["bytes"] or 0)
        return {
            "available": True,
            "error": None,
            "totals": {"files": total_files, "bytes": total_bytes},
            "sources": sources,
            "media_types": media_types,
            "recent_archives": recent_archives,
            "failures": failures,
            "latest_archive_at": latest_archive_at,
        }
    finally:
        connection.close()


def collect_partial_summary(
    tmp_dir: Path,
    *,
    now: float,
    active_partial_seconds: int,
    stale_partial_days: int,
) -> dict[str, Any]:
    partials: list[dict[str, Any]] = []
    if tmp_dir.exists():
        for path in sorted(tmp_dir.glob("*.part"), key=lambda item: item.stat().st_mtime, reverse=True):
            try:
                stat = path.stat()
            except FileNotFoundError:
                continue
            age_seconds = max(0, now - stat.st_mtime)
            partials.append(
                {
                    "name": path.name,
                    "path": str(path),
                    "size": stat.st_size,
                    "modified_at": datetime.fromtimestamp(stat.st_mtime, timezone.utc).isoformat(),
                    "age_seconds": int(age_seconds),
                    "source": partial_source(path.name),
                }
            )

    active_cutoff = active_partial_seconds
    stale_cutoff = stale_partial_days * 24 * 60 * 60
    active = [item for item in partials if item["age_seconds"] <= active_cutoff]
    stale = [item for item in partials if item["age_seconds"] >= stale_cutoff] if stale_partial_days > 0 else []
    total_bytes = sum(int(item["size"]) for item in partials)
    return {
        "tmp_dir": str(tmp_dir),
        "count": len(partials),
        "bytes": total_bytes,
        "active_count": len(active),
        "active_bytes": sum(int(item["size"]) for item in active),
        "stale_count": len(stale),
        "stale_bytes": sum(int(item["size"]) for item in stale),
        "by_source": count_partials_by_source(partials),
        "recent": partials[:20],
    }


def partial_source(name: str) -> str:
    if name.startswith("saved_"):
        return "saved"
    if name.startswith("channel_"):
        return "channels"
    return "unknown"


def count_partials_by_source(partials: list[dict[str, Any]]) -> list[dict[str, Any]]:
    buckets: dict[str, dict[str, int]] = {}
    for item in partials:
        source = str(item["source"])
        bucket = buckets.setdefault(source, {"files": 0, "bytes": 0})
        bucket["files"] += 1
        bucket["bytes"] += int(item["size"])
    return [
        {"source": source, "files": bucket["files"], "bytes": bucket["bytes"]}
        for source, bucket in sorted(buckets.items())
    ]


def collect_failure_summary(connection: sqlite3.Connection) -> dict[str, Any]:
    if not table_exists(connection, "source_archive_failures"):
        return empty_failure_summary()

    totals = connection.execute(
        """
        SELECT COUNT(*) AS unresolved_count,
               COALESCE(SUM(CASE WHEN retryable THEN 1 ELSE 0 END), 0) AS retryable_count,
               COALESCE(SUM(CASE WHEN NOT retryable THEN 1 ELSE 0 END), 0) AS terminal_count
        FROM source_archive_failures
        WHERE resolved_at IS NULL
        """
    ).fetchone()
    by_kind = rows_to_dicts(
        connection.execute(
            """
            SELECT error_kind,
                   COUNT(*) AS count,
                   MAX(last_seen_at) AS latest_seen_at
            FROM source_archive_failures
            WHERE resolved_at IS NULL
            GROUP BY error_kind
            ORDER BY count DESC, error_kind
            """
        )
    )
    recent = rows_to_dicts(
        connection.execute(
            """
            SELECT CASE
                       WHEN source LIKE 'channel\\_%' ESCAPE '\\' THEN 'channels'
                       ELSE source
                   END AS source,
                   source AS source_key,
                   source_message_id,
                   media_type,
                   original_name,
                   error_kind,
                   error_class,
                   error_message,
                   retryable,
                   temp_path,
                   attempt_count,
                   first_seen_at,
                   last_seen_at
            FROM source_archive_failures
            WHERE resolved_at IS NULL
            ORDER BY last_seen_at DESC, id DESC
            LIMIT 20
            """
        )
    )
    return {
        "unresolved_count": int(totals["unresolved_count"] or 0),
        "retryable_count": int(totals["retryable_count"] or 0),
        "terminal_count": int(totals["terminal_count"] or 0),
        "by_kind": by_kind,
        "recent": recent,
    }


def empty_failure_summary() -> dict[str, Any]:
    return {
        "unresolved_count": 0,
        "retryable_count": 0,
        "terminal_count": 0,
        "by_kind": [],
        "recent": [],
    }


def table_exists(connection: sqlite3.Connection, table: str) -> bool:
    row = connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
        (table,),
    ).fetchone()
    return row is not None


def classify_health(latest_archive_at: str | None, partials: dict[str, Any], failures: dict[str, Any]) -> dict[str, str]:
    if failures.get("terminal_count", 0) > 0:
        return {"level": "error", "message": "Some non-retryable archive failures need manual review."}
    if failures.get("unresolved_count", 0) > 0:
        return {"level": "warning", "message": "Some archive failures are waiting for retry or review."}
    if partials["active_count"] > 0:
        return {"level": "active", "message": "Downloads are currently making progress."}
    if latest_archive_at is None:
        return {"level": "warning", "message": "No non-bot archive records were found yet."}
    if partials["stale_count"] > 0:
        return {"level": "warning", "message": "Some partial downloads are stale and may need review."}
    return {"level": "idle", "message": "No active partial downloads right now."}


def rows_to_dicts(cursor: sqlite3.Cursor) -> list[dict[str, Any]]:
    return [dict(row) for row in cursor.fetchall()]


def query_scalar(connection: sqlite3.Connection, sql: str) -> Any:
    row = connection.execute(sql).fetchone()
    if row is None:
        return None
    return row[0]


def render_dashboard_html(settings: DashboardSettings) -> str:
    refresh = html.escape(str(settings.refresh_seconds))
    return f"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Telegram Archiver Monitor</title>
  <style>
    :root {{
      color-scheme: light dark;
      --bg: #f6f7f9;
      --panel: #ffffff;
      --text: #1f2933;
      --muted: #687383;
      --line: #d7dce2;
      --green: #1f8f55;
      --amber: #a06300;
      --blue: #2868a7;
      --red: #b42318;
    }}
    @media (prefers-color-scheme: dark) {{
      :root {{
        --bg: #121417;
        --panel: #1b1f24;
        --text: #edf1f5;
        --muted: #a8b0bc;
        --line: #343b44;
      }}
    }}
    * {{ box-sizing: border-box; }}
    body {{
      margin: 0;
      background: var(--bg);
      color: var(--text);
      font: 14px/1.5 system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
    }}
    header {{
      padding: 20px 24px 12px;
      border-bottom: 1px solid var(--line);
      background: var(--panel);
    }}
    h1 {{
      margin: 0;
      font-size: 22px;
      font-weight: 650;
      letter-spacing: 0;
    }}
    main {{ padding: 20px 24px 32px; }}
    .meta {{ color: var(--muted); margin-top: 4px; }}
    .grid {{
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(210px, 1fr));
      gap: 12px;
      margin-bottom: 18px;
    }}
    .panel {{
      background: var(--panel);
      border: 1px solid var(--line);
      border-radius: 8px;
      padding: 14px;
    }}
    .label {{ color: var(--muted); font-size: 12px; }}
    .value {{ font-size: 24px; font-weight: 700; margin-top: 4px; overflow-wrap: anywhere; }}
    .status {{ display: inline-flex; gap: 8px; align-items: center; font-weight: 650; }}
    .dot {{ width: 10px; height: 10px; border-radius: 50%; background: var(--blue); }}
    .active .dot {{ background: var(--green); }}
    .warning .dot {{ background: var(--amber); }}
    .error .dot {{ background: var(--red); }}
    .idle .dot {{ background: var(--blue); }}
    table {{ width: 100%; border-collapse: collapse; }}
    th, td {{ padding: 8px 6px; border-bottom: 1px solid var(--line); text-align: left; vertical-align: top; }}
    th {{ color: var(--muted); font-weight: 600; font-size: 12px; }}
    td.path {{ max-width: 420px; overflow-wrap: anywhere; }}
    .columns {{
      display: grid;
      grid-template-columns: minmax(0, 1fr) minmax(0, 1fr);
      gap: 12px;
      margin-bottom: 18px;
    }}
    @media (max-width: 850px) {{
      main, header {{ padding-left: 14px; padding-right: 14px; }}
      .columns {{ grid-template-columns: 1fr; }}
    }}
  </style>
</head>
<body>
  <header>
    <h1>Telegram Archiver Monitor</h1>
    <div class="meta">Non-bot sources · refresh <span id="refresh">{refresh}</span>s · <span id="generated">loading</span></div>
  </header>
  <main>
    <section class="grid">
      <div class="panel"><div class="label">Status</div><div id="health" class="value status idle"><span class="dot"></span><span>loading</span></div></div>
      <div class="panel"><div class="label">Archived files</div><div id="files" class="value">-</div></div>
      <div class="panel"><div class="label">Archived size</div><div id="bytes" class="value">-</div></div>
      <div class="panel"><div class="label">Partial files</div><div id="partials" class="value">-</div></div>
      <div class="panel"><div class="label">Active partials</div><div id="active" class="value">-</div></div>
      <div class="panel"><div class="label">Stale partials</div><div id="stale" class="value">-</div></div>
      <div class="panel"><div class="label">Unresolved failures</div><div id="failures" class="value">-</div></div>
    </section>
    <section class="columns">
      <div class="panel">
        <h2>Sources</h2>
        <table><thead><tr><th>Source</th><th>Files</th><th>Size</th><th>Latest</th></tr></thead><tbody id="sources"></tbody></table>
      </div>
      <div class="panel">
        <h2>Media Types</h2>
        <table><thead><tr><th>Type</th><th>Files</th><th>Size</th><th>Latest</th></tr></thead><tbody id="media"></tbody></table>
      </div>
    </section>
    <section class="panel">
      <h2>Recent Archives</h2>
      <table><thead><tr><th>Time</th><th>Source</th><th>Type</th><th>Size</th><th>Path</th></tr></thead><tbody id="recent"></tbody></table>
    </section>
    <section class="panel" style="margin-top:12px">
      <h2>Unresolved Failures</h2>
      <table><thead><tr><th>Last Seen</th><th>Source</th><th>Message</th><th>Kind</th><th>Attempts</th><th>Detail</th></tr></thead><tbody id="failureRows"></tbody></table>
    </section>
    <section class="panel" style="margin-top:12px">
      <h2>Recent Partials</h2>
      <table><thead><tr><th>Modified</th><th>Source</th><th>Size</th><th>Name</th></tr></thead><tbody id="partialRows"></tbody></table>
    </section>
  </main>
  <script>
    const refreshSeconds = Number(document.getElementById('refresh').textContent) || 10;
    const fmt = new Intl.NumberFormat();
    function size(bytes) {{
      const units = ['B', 'KiB', 'MiB', 'GiB', 'TiB'];
      let value = Number(bytes || 0);
      let index = 0;
      while (value >= 1024 && index < units.length - 1) {{ value /= 1024; index += 1; }}
      return `${{value.toFixed(index === 0 ? 0 : 1)}} ${{units[index]}}`;
    }}
    function shortTime(value) {{
      if (!value) return '-';
      const date = new Date(value);
      return Number.isNaN(date.getTime()) ? value : date.toLocaleString();
    }}
    function esc(value) {{
      return String(value ?? '').replace(/[&<>"']/g, c => ({{'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}}[c]));
    }}
    function row(cells) {{ return `<tr>${{cells.map(cell => `<td>${{cell}}</td>`).join('')}}</tr>`; }}
    async function load() {{
      const response = await fetch('/api/status', {{cache: 'no-store'}});
      const data = await response.json();
      document.getElementById('generated').textContent = `updated ${{shortTime(data.generated_at)}}`;
      const health = document.getElementById('health');
      health.className = `value status ${{data.health.level || 'idle'}}`;
      health.querySelector('span:last-child').textContent = data.health.message || data.health.level;
      document.getElementById('files').textContent = fmt.format(data.database.totals.files || 0);
      document.getElementById('bytes').textContent = size(data.database.totals.bytes);
      document.getElementById('partials').textContent = `${{fmt.format(data.partials.count)}} / ${{size(data.partials.bytes)}}`;
      document.getElementById('active').textContent = `${{fmt.format(data.partials.active_count)}} / ${{size(data.partials.active_bytes)}}`;
      document.getElementById('stale').textContent = `${{fmt.format(data.partials.stale_count)}} / ${{size(data.partials.stale_bytes)}}`;
      document.getElementById('failures').textContent = fmt.format(data.database.failures.unresolved_count || 0);
      document.getElementById('sources').innerHTML = (data.database.sources || []).map(item => row([
        esc(item.source), fmt.format(item.files || 0), size(item.bytes), shortTime(item.latest_archive_at)
      ])).join('') || row(['-', '-', '-', '-']);
      document.getElementById('media').innerHTML = (data.database.media_types || []).map(item => row([
        esc(item.media_type), fmt.format(item.files || 0), size(item.bytes), shortTime(item.latest_archive_at)
      ])).join('') || row(['-', '-', '-', '-']);
      document.getElementById('recent').innerHTML = (data.database.recent_archives || []).map(item => row([
        shortTime(item.created_at), esc(item.source), esc(item.media_type), size(item.file_size), `<span class="path">${{esc(item.final_path)}}</span>`
      ])).join('') || row(['-', '-', '-', '-', '-']);
      document.getElementById('failureRows').innerHTML = (data.database.failures.recent || []).map(item => row([
        shortTime(item.last_seen_at),
        esc(item.source),
        esc(item.source_message_id),
        esc(item.error_kind),
        fmt.format(item.attempt_count || 0),
        `<span class="path">${{esc(item.error_class)}}: ${{esc(item.error_message)}}</span>`
      ])).join('') || row(['-', '-', '-', '-', '-', '-']);
      document.getElementById('partialRows').innerHTML = (data.partials.recent || []).map(item => row([
        shortTime(item.modified_at), esc(item.source), size(item.size), `<span class="path">${{esc(item.name)}}</span>`
      ])).join('') || row(['-', '-', '-', '-']);
    }}
    load().catch(error => {{
      const health = document.getElementById('health');
      health.className = 'value status error';
      health.querySelector('span:last-child').textContent = error.message;
    }});
    setInterval(load, refreshSeconds * 1000);
  </script>
</body>
</html>"""
