#!/bin/sh
# Jellyfin Live TV → rne.
#
# Jellyfin runs this after each recording finishes (Dashboard → Live TV →
# DVR → "Post-processing application", arguments: "{path}"). It asks rne to
# queue the recording for encoding. The recording itself is left alone.
#
# Set RNE_URL and RNE_TOKEN below (or in the environment). RNE_TOKEN must
# match [api] token in ~/.config/rne/config.toml on the machine running
# rne-dashboard.
#
# The jellyfin user must be able to read this file, and nobody else should
# (it holds the token):
#   sudo chown root:jellyfin rne-postprocess.sh && sudo chmod 750 rne-postprocess.sh

RNE_URL="${RNE_URL:-}"      # the rne dashboard, e.g. http://<host>:8500
RNE_TOKEN="${RNE_TOKEN:-}"
LOG="${RNE_LOG:-/var/log/jellyfin/rne-postprocess.log}"

log() {
    line="$(date '+%Y-%m-%d %H:%M:%S') $*"
    echo "$line"
    echo "$line" >> "$LOG" 2>/dev/null || true
}

path="$1"
if [ -z "$path" ]; then
    echo "usage: $0 /path/to/recording.ts" >&2
    exit 2
fi
if [ -z "$RNE_URL" ] || [ -z "$RNE_TOKEN" ]; then
    log "$path -> not sent: set RNE_URL and RNE_TOKEN at the top of $0"
    exit 2
fi

# --fail-with-body: exit non-zero on an HTTP error but still log rne's reason.
reply=$(curl -sS --fail-with-body --max-time 120 \
    -H "Authorization: Bearer $RNE_TOKEN" \
    --data-urlencode "path=$path" \
    -w ' [HTTP %{http_code}]' \
    "${RNE_URL%/}/api/v1/recordings" 2>&1)
status=$?

log "$path -> $(printf '%s' "$reply" | tr -d '\n')"
exit $status
