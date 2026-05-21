# Savvy Upload

Headless browser automation tool that uploads engine monitor CSV files to SavvyAviation.com. Runs unattended on Linux/macOS with email notifications.

## Relationship to flashair-sync

Second stage of a two-project pipeline. [flashair-sync](https://github.com/leithl/flashair-sync) runs on a Raspberry Pi, downloads CSVs from the engine monitor's FlashAir WiFi SD card, and SCPs them to `CSV_DIR`. This project picks up those files and uploads them to SavvyAviation.

**Files stay in CSV_DIR forever — savvy-uploader does not move them.** The `LAST_UPLOADED` watermark in `.env` is the only state distinguishing "uploaded" from "pending"; the filesystem layout doesn't carry that information. This also makes `CSV_DIR` safe for any other consumer to read concurrently: savvy-uploader is read-only on each CSV after upload, and read-only readers won't race against its processing.

Both projects use a filename-based watermark pattern (lexicographic comparison of `log_YYYYMMDD_HHMMSS_KXXX.csv` filenames) and near-identical `.env` read/write helpers.

## Project structure

```
savvy_upload.py    # All application logic (~1035 lines): browser automation, upload, email, state
savvy_cron.sh      # Shell wrapper for cron/manual runs, passes args to Python
savvy_watch.sh     # File watcher using inotifywait, 60s debounce, triggers uploads on new CSVs
.env               # Config + watermark state (not in git)
```

Persistent state files (generated at runtime, not in git):
- `pending_uploads.json` — files that timed out and need verification next run
- `unsent_email.json` — failed emails queued for retry
- `savvy_upload.log` — log file
- `debug_*.png` — screenshots captured on errors

## Dependencies

- **Python 3.10+** (stdlib only, no third-party packages beyond Playwright)
- **Playwright** (`pip install playwright && playwright install chromium`) — browser automation
- **msmtp** (optional) — email sending on Linux; falls back to terminal output
- **inotify-tools** (optional) — for `savvy_watch.sh`; alternative is cron via `savvy_cron.sh`

No `requirements.txt` or `pyproject.toml` — minimal by design.

## Configuration (.env)

| Variable | Required | Purpose |
|----------|----------|---------|
| `SAVVY_EMAIL` | Yes | SavvyAviation login email |
| `SAVVY_PASSWORD` | Yes | SavvyAviation login password |
| `SAVVY_AIRCRAFT_ID` | Yes | Numeric aircraft ID from Savvy URL |
| `CSV_DIR` | Yes | Path to engine monitor CSV directory |
| `USER_AGENT` | No | Custom browser user agent |
| `LAST_UPLOADED` | Auto | Watermark — managed by script, do not edit manually |

## Running

```bash
# Setup
python3 -m venv venv
venv/bin/pip install playwright
venv/bin/playwright install chromium

# Manual / cron
./savvy_cron.sh                     # Upload new files from CSV_DIR
./savvy_cron.sh --headed            # Visible browser for debugging
./savvy_cron.sh --slow-mo 500       # Slow down actions for debugging
./savvy_cron.sh /path/to/file.csv   # Upload a single file
./savvy_cron.sh --reupload          # Ignore watermark, reprocess all

# File watcher (runs continuously)
./savvy_watch.sh
```

Recommended production deployment is as a systemd service running `savvy_watch.sh`.

## Architecture and key patterns

**Entry flow:** Shell wrappers → `savvy_upload.py:main()` → launch Playwright → login → upload loop → verify → email. No file movement at any stage.

**Files never move.** `CSV_DIR` is the permanent home. The only state tracking "what's done" is the `LAST_UPLOADED` watermark in `.env`; files stay put. This also makes the dir safe for any other consumer to read concurrently — no storage-policy coordination needed.

**Watermark system:** `LAST_UPLOADED` in `.env` tracks the most recently uploaded filename. Files are sorted lexicographically; anything `<= LAST_UPLOADED` is skipped. Works because filenames follow `log_YYYYMMDD_HHMMSS_KXXX.csv` format (chronological = alphabetical).

**Three-layer retry for uploads:**
1. Initial poll — up to 5 minutes per file (`UPLOAD_TIMEOUT`)
2. Batch retry — extra 12 polling cycles for timed-out files (`RETRY_POLLS`)
3. Persistent pending — unresolved files saved to `pending_uploads.json`, verified on next run

**Email retry with backoff:** 3 attempts (0s, 10s, 20s delays), then persisted to `unsent_email.json` for retry next run.

**Network error handling:** If page reload fails between uploads, batch stops early. Un-watermarked files retry automatically on the next run.

**Rejected flight attribution:** Scrapes the "Rejected" panel after each upload. Tracks a baseline of pre-existing rejected flights so only *new* rejections are attributed to each file.

## Public repo — keep contributions standalone

This repo is public on GitHub. PRs, commit messages, code comments, log strings, and docs must be readable to someone with no context beyond this repo:

- **No references to private downstream consumers**, regardless of who owns them. If a change is motivated by a downstream project, frame it in standalone terms ("files stay in `CSV_DIR` for downstream consumers") without naming or linking the downstream.
- **No PII or operational context**: no hostnames, no usernames in paths, no SSH aliases, no real aircraft IDs, no real watermark values. Use placeholders (`/path/to/csvs`, `user@host:`) and generic terms.
- **PR test plans must be reproducible cold** by someone setting up this repo fresh — not "run on my host". Generic commands only; never "ssh my-host && …".
- **Public ↔ public cross-references are fine.** [flashair-sync](https://github.com/leithl/flashair-sync) is also public and is named here as part of the documented pipeline. The rule is one-way: PUBLIC repos must not reference PRIVATE repos.

## Code conventions

- Python dataclasses for structured data (`Config`, `UploadResult`, `RejectedFlight`)
- Private helpers prefixed with `_` (e.g., `_read_env`, `_try_msmtp`)
- Constants in UPPER_CASE at module level
- Shell scripts use `set -euo pipefail`
- No test suite — tested manually with `--headed` flag and debug screenshots

## External service: SavvyAviation.com

All interaction is via Playwright browser automation (no official API):
- Login: email/password form at `apps.savvyaviation.com`
- Upload: file form at `/files/upload/{AIRCRAFT_ID}`
- Verification: flights page at `/flights/aircraft/{AIRCRAFT_ID}`
- Status is polled from dynamically-rendered table rows on the upload page
