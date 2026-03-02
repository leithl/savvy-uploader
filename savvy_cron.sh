#!/usr/bin/env bash
# savvy_cron.sh - Upload all engine monitor CSVs to SavvyAviation.
#
# All configuration lives in .env (next to this script):
#   SAVVY_EMAIL, SAVVY_PASSWORD, SAVVY_AIRCRAFT_ID, CSV_DIR
#
# Usage (manual):
#   ./savvy_cron.sh                     # headless, uses CSV_DIR from .env
#   ./savvy_cron.sh --headed            # visible browser for debugging
#   ./savvy_cron.sh /path/to/dir        # override CSV_DIR for this run
#   ./savvy_cron.sh /path/to/file.csv   # upload a single file
#   ./savvy_cron.sh --reupload          # ignore watermark, re-process all
#
# Cron example (daily at 8pm):
#   0 20 * * * /path/to/savvy_cron.sh

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PYTHON="$SCRIPT_DIR/venv/bin/python"

echo "$(date): Starting savvy_upload"

"$PYTHON" "$SCRIPT_DIR/savvy_upload.py" "$@"
