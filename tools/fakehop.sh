#!/bin/sh
# Start/stop tools/fake_hopx.py in the background (needs ~/.venvs/hoppla-sim with paramiko).
#   tools/fakehop.sh start | stop      log: ~/.cache/fake_hopx/fake.log
D="$HOME/.cache/fake_hopx"; P="$D/fake.pid"; mkdir -p "$D"
case "$1" in
  start) [ -f "$P" ] && kill "$(cat "$P")" 2>/dev/null; sleep 1
         cd "$(dirname "$0")/.." && nohup "$HOME/.venvs/hoppla-sim/bin/python" tools/fake_hopx.py > "$D/fake.log" 2>&1 &
         echo $! > "$P"; sleep 3; head -1 "$D/fake.log" ;;
  stop)  [ -f "$P" ] && kill "$(cat "$P")" 2>/dev/null; rm -f "$P"; echo stopped ;;
  *)     echo "usage: $0 start|stop"; exit 2 ;;
esac
