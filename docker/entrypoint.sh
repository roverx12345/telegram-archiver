#!/usr/bin/env sh
set -eu

if [ -n "${LOCAL_BOT_API_URL:-}" ]; then
  echo "Waiting for local Bot API server at ${LOCAL_BOT_API_URL}"
  python - <<'PY'
import os
import sys
import time
import urllib.request

url = os.environ["LOCAL_BOT_API_URL"].rstrip("/") + "/"
deadline = time.time() + 120
last_error = None

while time.time() < deadline:
    try:
        with urllib.request.urlopen(url, timeout=5) as response:
            if response.status < 500:
                sys.exit(0)
    except Exception as exc:  # pragma: no cover
        last_error = exc
    time.sleep(2)

print(f"Timed out waiting for {url}: {last_error}", file=sys.stderr)
sys.exit(1)
PY
fi

exec "$@"
