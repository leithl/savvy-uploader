#!/usr/bin/env bash
# savvy_watch.sh - Watch for new engine monitor CSVs and upload them.
#
# Uses inotifywait to trigger uploads only when new files appear,
# instead of polling on a cron schedule.
#
# Install inotify-tools:
#   sudo apt install inotify-tools
#
# Run in the background (or via systemd, see below):
#   nohup ./savvy_watch.sh &
#
# --- systemd service (recommended) ---
# Create /etc/systemd/system/savvy-upload.service:
#
#   [Unit]
#   Description=Savvy Aviation CSV uploader
#   After=network-online.target
#   Wants=network-online.target
#
#   [Service]
#   Type=simple
#   User=leith
#   ExecStart=/home/leith/savvy/savvy_watch.sh
#   Restart=on-failure
#   RestartSec=30
#
#   [Install]
#   WantedBy=multi-user.target
#
# Then:
#   sudo systemctl daemon-reload
#   sudo systemctl enable savvy-upload
#   sudo systemctl start savvy-upload
#   journalctl -u savvy-upload -f    # watch logs

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

# Read CSV_DIR from .env
CSV_DIR=""
if [[ -f "$SCRIPT_DIR/.env" ]]; then
    CSV_DIR=$(grep -E '^CSV_DIR=' "$SCRIPT_DIR/.env" | cut -d= -f2-)
fi

if [[ -z "$CSV_DIR" || ! -d "$CSV_DIR" ]]; then
    echo "ERROR: CSV_DIR not set in .env or directory does not exist" >&2
    exit 1
fi

echo "$(date): Watching $CSV_DIR for new CSV files..."

# Debounce: wait after a file lands before uploading.
# 60s allows for slow transfers (e.g. cellular) and batches of files.
DEBOUNCE=60

inotifywait -m -e close_write -e moved_to --include 'log_.*\.csv$' "$CSV_DIR" |
while read -r dir event file; do
    echo "$(date): Detected $event: $file — waiting ${DEBOUNCE}s..."
    sleep "$DEBOUNCE"

    # Drain any events that queued up during the debounce/upload
    while read -r -t 0.1 dir event file; do
        echo "$(date): Collapsed $event: $file"
    done

    echo "$(date): Running upload..."
    "$SCRIPT_DIR/savvy_cron.sh"
done
