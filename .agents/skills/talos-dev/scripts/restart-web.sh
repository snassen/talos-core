#!/bin/zsh
# Restart Talos Web (launchd keeps it alive) and wait until it answers. The browser then needs a
# reload to pick up new JS and CSS. The label's prefix is owner.json's service_prefix (talos.personal).
cd "$(dirname "$0")/../../../.." || exit 1
prefix=$(uv run --quiet python -c 'from talos import personal; print(personal.owner()["service_prefix"])') || exit 1
launchctl kickstart -k "gui/$(id -u)/${prefix}.web"
for i in {1..30}; do
  if curl -s -o /dev/null -w '%{http_code}' -H 'Host: 127.0.0.1:7420' http://127.0.0.1:7420/auth/status | grep -q 200; then
    echo "Talos Web is up (reload the page)"; exit 0
  fi
  sleep 1
done
echo "Talos Web did not answer in 30 s: see ~/TalosData/logs/web.err" >&2; exit 1
