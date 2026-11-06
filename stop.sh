#!/usr/bin/env bash
# Stop only the proxy previously started by start.sh. Verifies the process command line first.
set -euo pipefail

root="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
pidfile="$root/proxy.pid"

if [ ! -f "$pidfile" ]; then
  echo "No managed proxy process."
  exit 0
fi

pid="$(tr -dc '0-9' <"$pidfile")"
if [ -z "$pid" ] || ! kill -0 "$pid" 2>/dev/null; then
  echo "Proxy is already stopped."
  rm -f "$pidfile"
  exit 0
fi

cmdline="$(ps -p "$pid" -o command= 2>/dev/null || true)"
case "$cmdline" in
  *"$root/proxy.py"*)
    kill "$pid"
    echo "Proxy stopped."
    rm -f "$pidfile"
    ;;
  *)
    echo "PID $pid belongs to another process; refusing to stop it." >&2
    exit 1
    ;;
esac
