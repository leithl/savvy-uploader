# Savvy Upload

Automatically upload engine monitor CSV files to [SavvyAviation.com](https://savvyaviation.com) using browser automation (Playwright).

Designed to run unattended on a headless Linux box. Drop CSV files into a directory and they get uploaded and verified automatically. You get an email summary of what happened.

## Features

- Batch uploads all CSVs in a directory, one at a time
- Tracks what's already been uploaded (watermark in `.env`) so files aren't re-processed
- Detects upload status: Success, File Duplicated, File Too Small, etc.
- Scrapes rejected flights from Savvy's upload page and attributes them per-file
- Verifies uploaded files appear on the flights page
- Sends an email report via `msmtp` (or prints to terminal if unavailable)
- File watcher mode using `inotifywait` for instant uploads when new files appear
- Files stay in `CSV_DIR` forever — no archive shuffling, friendly to downstream consumers that may read the same dir

## Requirements

- Python 3.10+
- [Playwright](https://playwright.dev/python/) (Chromium)
- `msmtp` (optional, for email on Linux)
- `inotify-tools` (optional, for file watcher mode on Linux)

## Setup

### 1. Clone and create a virtual environment

```bash
git clone <repo-url> savvy-upload
cd savvy-upload
python3 -m venv venv
venv/bin/pip install playwright
venv/bin/playwright install chromium
```

### 2. Configure

Create a `.env` file in the project directory:

```
SAVVY_EMAIL=you@example.com
SAVVY_PASSWORD=yourpassword
SAVVY_AIRCRAFT_ID=12345
CSV_DIR=/path/to/engine/csv/files
```

| Variable | Required | Description |
|---|---|---|
| `SAVVY_EMAIL` | Yes | Your SavvyAviation login email |
| `SAVVY_PASSWORD` | Yes | Your SavvyAviation password |
| `SAVVY_AIRCRAFT_ID` | Yes | Your aircraft's numeric ID from the Savvy URL |
| `CSV_DIR` | Yes | Directory holding engine monitor CSVs. Files arrive here (via SCP from flashair-sync or manual drop) and stay forever — savvy-uploader does not move them. |
| `USER_AGENT` | No | Custom browser user agent (defaults to Chrome 131 on Linux x86_64) |
| `SUMMARY_DIR` | No | If set, each run appends a JSON-line summary to `{SUMMARY_DIR}/savvy-YYYY-MM-DD.jsonl` and **no email is sent**. Useful when an external process aggregates upload outcomes from multiple sources. Leave unset to keep the per-batch email behaviour. |
| `GRAPHQL_SETTLE_SECONDS` | No | Seconds to wait after the upload phase before the authoritative GraphQL flight-count query (default 60). Savvy parses CSVs asynchronously in multiple phases — flight attribution can land seconds-to-minutes after the upload page first reports "Success". Raise this if the log warns about 0-flight files whose status wasn't terminal-no-flight. |
| `LAST_UPLOADED` | Auto | Managed by the script — tracks the most recent uploaded file (the only state distinguishing uploaded vs. pending files in `CSV_DIR`). |

To find your aircraft ID, go to your aircraft's page on SavvyAviation and look at the URL:
`https://apps.savvyaviation.com/flights/aircraft/12345` - the number at the end is your ID.

### 3. Make scripts executable

```bash
chmod +x savvy_cron.sh savvy_watch.sh
```

### 4. Test it

```bash
# Headed mode (visible browser) to verify everything works:
./savvy_cron.sh --headed

# Headless:
./savvy_cron.sh
```

## Usage

### Manual run

```bash
./savvy_cron.sh                     # upload new CSVs from CSV_DIR
./savvy_cron.sh --headed            # visible browser for debugging
./savvy_cron.sh /other/dir          # override CSV_DIR for this run
./savvy_cron.sh /path/to/file.csv   # upload a single file
./savvy_cron.sh --reupload          # ignore watermark, re-process all files
```

### File watcher (recommended for always-on machines)

Uses `inotifywait` to detect new CSV files and upload them automatically. Includes a 60-second debounce to handle slow transfers (e.g. cellular connections) and batches of files.

```bash
# Install inotify-tools:
sudo apt install inotify-tools

# Quick test:
./savvy_watch.sh
```

#### Run as a systemd service

Create `/etc/systemd/system/savvy-upload.service`:

```ini
[Unit]
Description=Savvy Aviation CSV uploader
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=youruser
ExecStart=/path/to/savvy-upload/savvy_watch.sh
Restart=on-failure
RestartSec=30

[Install]
WantedBy=multi-user.target
```

Then enable and start it:

```bash
sudo systemctl daemon-reload
sudo systemctl enable savvy-upload
sudo systemctl start savvy-upload

# Watch logs:
journalctl -u savvy-upload -f
```

### Cron (alternative to file watcher)

If you prefer polling over file watching:

```bash
# Run every 10 minutes:
*/10 * * * * /path/to/savvy-upload/savvy_cron.sh

# Run daily at 8pm:
0 20 * * * /path/to/savvy-upload/savvy_cron.sh
```

## Email setup (optional)

On Linux with `msmtp` configured, the script sends an email summary to `SAVVY_EMAIL` after each run. On macOS or when `msmtp` isn't available, it prints the email to the terminal instead.

To set up `msmtp`:

```bash
sudo apt install msmtp
```

Configure `~/.msmtprc` with your email provider's SMTP settings. See the [msmtp documentation](https://marlam.de/msmtp/) for details.

## How it works

1. Reads config from `.env`
2. Scans `CSV_DIR` for files matching `log_*_*.csv`, skipping any already uploaded (based on `LAST_UPLOADED` watermark)
3. Launches a headless Chromium browser and logs into SavvyAviation
4. Uploads each file one at a time, polling for status (Success, Duplicated, Too Small, etc.)
5. After each successful upload, checks for newly rejected flights
6. Navigates to the flights page to verify uploads appeared
7. Updates the `LAST_UPLOADED` watermark in `.env`
8. Sends an email summary (or prints to terminal)

Files stay in `CSV_DIR` permanently — the watermark is the only state distinguishing uploaded from pending. Any downstream consumer reading the same directory does so safely (savvy-uploader is read-only on each CSV after upload).

## Deploying updates

Once the system is running on a remote host (e.g. an always-on Linux box), run `deploy.sh` from your laptop to pull main on the host in one command:

```bash
export SAVVY_DEPLOY_HOST=your-ssh-alias    # set once in your shell profile
./deploy.sh                                # pulls main on the host, verifies the service
./deploy.sh another-host                   # one-off override
```

The script prints exactly one final line — `[deploy] DEPLOY OK …` on success, `[deploy] DEPLOY FAILED step="…"` on a known failure, or `[deploy] DEPLOY DID NOT COMPLETE …` if it died silently mid-run. Don't trust a deploy report (yours or an agent's) that doesn't quote one of those back at you.

## Related projects

- [flashair-sync](https://github.com/leithl/flashair-sync) — Raspberry Pi that pulls CSVs off the FlashAir SD card in the engine monitor and SCPs them to this project's `CSV_DIR`.

## CSV file naming

The script expects engine monitor CSV files named `log_YYYYMMDD_HHMMSS_AIRPORT.csv` (e.g. `log_20260213_113501_KANK.csv`). This naming convention is used for:

- Chronological sorting and watermark comparison
- Matching files to their status on Savvy's upload page

## Files

| File | Description |
|---|---|
| `savvy_upload.py` | Main upload script |
| `savvy_cron.sh` | Shell wrapper for manual/cron runs |
| `savvy_watch.sh` | File watcher using inotifywait |
| `savvy_download.py` | Backfill: pulls every CSV Savvy has for the aircraft into `CSV_DIR` |
| `deploy.sh` | One-command deploy from your laptop (pulls latest on the host, verifies the service). |
| `.env` | Configuration (not checked into git) |
