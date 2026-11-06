#!/usr/bin/env bash
# Start the proxy in the background. macOS / Linux equivalent of start.ps1.
set -euo pipefail

root="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
py="${PYTHON:-python3}"

if ! command -v "$py" >/dev/null 2>&1; then
  echo "Python 3 not found. Install it or run: PYTHON=/path/to/python3 $0" >&2
  exit 1
fi

"$py" "$root/proxy.py" --init

port="$("$py" -c 'import json,sys;print(json.load(open(sys.argv[1]))["port"])' "$root/config.json")"
base="http://127.0.0.1:${port}"

healthy() {
  body="$(curl -fsS --max-time 2 "$base/health" 2>/dev/null || true)"
  case "$body" in
    *'"service":"claude-codex-proxy"'*|*'"service": "claude-codex-proxy"'*) return 0 ;;
    *) return 1 ;;
  esac
}

if curl -fsS --max-time 2 "$base/health" >/dev/null 2>&1; then
  if healthy; then
    echo "Proxy is already running: $base"
    exit 0
  fi
  echo "Port $port is occupied by another service." >&2
  exit 1
fi

nohup "$py" "$root/proxy.py" >"$root/stdout.log" 2>"$root/proxy.log" &
echo $! >"$root/proxy.pid"

for _ in $(seq 1 30); do
  sleep 0.2
  if healthy; then
    echo "Proxy started: $base"
    exit 0
  fi
  kill -0 "$(cat "$root/proxy.pid")" 2>/dev/null || break
done

echo "Proxy did not start. Check $root/proxy.log" >&2
exit 1
