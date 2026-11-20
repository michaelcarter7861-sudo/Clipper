#!/usr/bin/env bash
# Same question as probe_upstream.py, but with curl: some instances sit behind
# Cloudflare, which refuses python's TLS fingerprint and lets a normal client through.
set -uo pipefail
url="${1:-https://youtu.be/DENbjnmSems}"
id="$url"
case "$url" in
  *youtu.be/*) id="${url#*youtu.be/}"; id="${id%%[?&]*}" ;;
  *v=*)        id="${url#*v=}"; id="${id%%[?&]*}" ;;
esac
UA="Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0 Safari/537.36"

probe_json() {
  local base="$1" path="$2"
  local code
  code=$(curl -sS -o /tmp/body.json -w '%{http_code}' -m 15 -A "$UA" \
    -H 'Accept: application/json,*/*' --compressed "$base$path" 2>/dev/null)
  local size
  size=$(wc -c < /tmp/body.json 2>/dev/null || echo 0)
  local media
  media=$(python3 - "$base" "$id" <<'PY' 2>/dev/null || true
import json, sys
try:
    data = json.load(open("/tmp/body.json"))
except Exception:
    print(""); raise SystemExit
urls = []
if isinstance(data, dict):
    proxy = str(data.get("proxyUrl") or "")
    for key in ("videoStreams", "audioStreams"):
        for s in data.get(key) or []:
            if isinstance(s, dict) and s.get("url"):
                urls.append(str(s["url"]))
    for key in ("formatStreams", "adaptiveFormats"):
        for s in data.get(key) or []:
            if isinstance(s, dict) and s.get("url"):
                urls.append(str(s["url"]))
if urls:
    print(urls[0])
PY
)
  echo "HTTP $base  code=$code bytes=$size"
  if [ -n "$media" ]; then
    local mcode
    mcode=$(curl -sS -o /tmp/media.bin -w '%{http_code}' -m 20 -A "$UA" -r 0-2047 \
      -H 'Origin: https://www.youtube.com' -H 'Referer: https://www.youtube.com/' "$media" 2>/dev/null)
    echo "      media range: code=$mcode bytes=$(wc -c < /tmp/media.bin 2>/dev/null || echo 0)"
  else
    head -c 120 /tmp/body.json | tr '\n' ' '; echo
  fi
}

for base in https://inv.nadeko.net https://yewtu.be https://invidious.nerdvpn.de https://iv.duti.dev https://invidious.materialio.us https://invidious.f5.si; do
  probe_json "$base" "/api/v1/videos/$id?fields=title,formatStreams,adaptiveFormats"
done
for base in https://pipedapi.ducks.party https://api.piped.private.coffee https://pipedapi.kavin.rocks https://pipedapi.adminforge.de; do
  probe_json "$base" "/streams/$id"
done
