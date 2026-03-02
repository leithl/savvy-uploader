#!/usr/bin/env python3
"""
Upload engine monitor CSV files to SavvyAviation.com via browser automation.

Usage:
    # Upload all CSVs from CSV_DIR (set in .env):
    python3 savvy_upload.py

    # Upload from a specific directory or single file:
    python3 savvy_upload.py /path/to/csv_dir/
    python3 savvy_upload.py /path/to/data.csv

    # Headed mode for debugging:
    python3 savvy_upload.py --headed

Requirements:
    pip install playwright
    playwright install chromium

Configuration (.env file next to this script):
    SAVVY_EMAIL=you@example.com
    SAVVY_PASSWORD=yourpassword
    SAVVY_AIRCRAFT_ID=12345
    CSV_DIR=/path/to/engine/csv/files
    # USER_AGENT=...          (optional, defaults to Chrome 131 on Linux x86_64)

Email:
    On Linux with msmtp configured, sends a summary email to SAVVY_EMAIL.
    On macOS (or when msmtp is unavailable), prints the email to the terminal.
"""

import argparse
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
SAVVY_BASE = "https://apps.savvyaviation.com"
DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)

# Timeouts (ms)
NAV_TIMEOUT = 60_000
UPLOAD_TIMEOUT = 300_000       # 5 min per file (server can be slow with queued uploads)
LOGIN_TIMEOUT = 30_000

POLL_INTERVAL = 10  # seconds between status checks
RETRY_POLLS = 12    # extra polls per timed-out file during the retry pass
DEBUG_DIR = Path(__file__).parent
PENDING_FILE = DEBUG_DIR / "pending_uploads.json"
UNSENT_EMAIL_FILE = DEBUG_DIR / "unsent_email.json"

# Logging setup
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler(DEBUG_DIR / "savvy_upload.log", encoding="utf-8"),
    ],
)
log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Data
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class RejectedFlight:
    date: str
    departure: str
    destination: str
    duration: str


@dataclass
class UploadResult:
    filename: str
    status: str = "unknown"
    flights_accepted: int = 0
    rejected_flights: list[RejectedFlight] = field(default_factory=list)
    on_flights_page: bool = False


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
ENV_FILE = Path(__file__).parent / ".env"


def _read_env() -> dict[str, str]:
    """Read key=value pairs from the .env file."""
    env = {}
    if ENV_FILE.exists():
        for line in ENV_FILE.read_text().splitlines():
            line = line.strip()
            if line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            env[key.strip()] = value.strip().strip("\"'")
    return env


def _write_env(env: dict[str, str]) -> None:
    """Write key=value pairs back to the .env file, preserving order."""
    # Read existing lines to preserve comments and ordering
    lines: list[str] = []
    keys_written: set[str] = set()

    if ENV_FILE.exists():
        for line in ENV_FILE.read_text().splitlines():
            stripped = line.strip()
            if stripped.startswith("#") or "=" not in stripped:
                lines.append(line)
                continue
            key, _, _ = stripped.partition("=")
            key = key.strip()
            if key in env:
                lines.append(f"{key}={env[key]}")
                keys_written.add(key)
            else:
                lines.append(line)

    # Append any new keys not yet in the file
    for key, value in env.items():
        if key not in keys_written:
            lines.append(f"{key}={value}")

    ENV_FILE.write_text("\n".join(lines) + "\n")


@dataclass
class Config:
    email: str
    password: str
    aircraft_id: str
    csv_dir: str
    user_agent: str = DEFAULT_USER_AGENT
    upload_url: str = ""
    flights_url: str = ""

    def __post_init__(self):
        self.upload_url = f"{SAVVY_BASE}/files/upload/{self.aircraft_id}"
        self.flights_url = f"{SAVVY_BASE}/flights/aircraft/{self.aircraft_id}"


def load_config(cli_path: str = "") -> Config:
    """Load all config from env vars / .env file.

    cli_path overrides CSV_DIR if provided.
    """
    env = _read_env()

    def _get(key: str) -> str:
        return os.environ.get(key, "") or env.get(key, "")

    email = _get("SAVVY_EMAIL")
    password = _get("SAVVY_PASSWORD")
    aircraft_id = _get("SAVVY_AIRCRAFT_ID")
    csv_dir = cli_path or _get("CSV_DIR")
    user_agent = _get("USER_AGENT") or DEFAULT_USER_AGENT

    if not email or not password:
        log.error("Missing credentials. Set SAVVY_EMAIL and SAVVY_PASSWORD.")
        sys.exit(1)
    if not aircraft_id:
        log.error("Missing SAVVY_AIRCRAFT_ID. Set it in .env or as an env var.")
        sys.exit(1)
    if not csv_dir:
        log.error("No CSV path provided. Pass a path argument or set CSV_DIR in .env.")
        sys.exit(1)

    return Config(
        email=email,
        password=password,
        aircraft_id=aircraft_id,
        csv_dir=csv_dir,
        user_agent=user_agent,
    )


def load_last_uploaded() -> str:
    """Return the LAST_UPLOADED filename from .env, or empty string."""
    return _read_env().get("LAST_UPLOADED", "")


def save_last_uploaded(filename: str) -> None:
    """Update LAST_UPLOADED in the .env file."""
    env = _read_env()
    env["LAST_UPLOADED"] = filename
    _write_env(env)
    log.info(f"Updated LAST_UPLOADED={filename}")


def load_pending_uploads() -> list[str]:
    """Load filenames that timed out on a previous run and need verification."""
    if PENDING_FILE.exists():
        try:
            data = json.loads(PENDING_FILE.read_text())
            return data if isinstance(data, list) else []
        except (json.JSONDecodeError, OSError):
            return []
    return []


def save_pending_uploads(filenames: list[str]) -> None:
    """Persist filenames that still need verification to disk."""
    if filenames:
        PENDING_FILE.write_text(json.dumps(filenames, indent=2) + "\n")
        log.info(f"Saved {len(filenames)} file(s) to pending verification list.")
    elif PENDING_FILE.exists():
        PENDING_FILE.unlink()
        log.info("Cleared pending verification list.")


def savvy_display_name(csv_path: str) -> str:
    """Derive the name Savvy uses in the Recent Uploads table.

    Savvy strips the extension and airport-code suffix, keeping only
    the date/time portion: log_YYYYMMDD_HHMMSS
    """
    stem = Path(csv_path).stem  # e.g. "log_20260213_113501_KANK"
    parts = stem.split("_")
    if len(parts) >= 3 and parts[0] == "log":
        return "_".join(parts[:3])  # log_YYYYMMDD_HHMMSS
    return stem


def cleanup_csvs(
    directory: str,
    watermark: str,
    keep_recent: int = 10,
    pending_filenames: list[str] | None = None,
) -> int:
    """Delete already-uploaded CSVs, keeping the most recent *keep_recent*.

    Any CSV whose filename sorts <= *watermark* is considered uploaded.
    Files in *pending_filenames* are protected from deletion (they timed
    out and haven't been confirmed yet — we may need them for re-upload).
    Only operates when *directory* is a directory (not a single file).
    Returns the number of files deleted.
    """
    p = Path(directory).resolve()
    if not p.is_dir():
        return 0

    pending_set = set(pending_filenames or [])

    # All engine-monitor CSVs, sorted oldest-first by filename
    all_csvs = sorted(p.glob("log_*_*.csv"), key=lambda f: f.name)

    # Keep the most recent N regardless of upload status
    protected = {f.name for f in all_csvs[-keep_recent:]}

    deleted = 0
    for f in all_csvs:
        if f.name <= watermark and f.name not in protected and f.name not in pending_set:
            f.unlink()
            log.info(f"Cleaned up: {f.name}")
            deleted += 1
        elif f.name in pending_set:
            log.info(f"Kept (pending verification): {f.name}")

    if deleted:
        log.info(f"Deleted {deleted} uploaded CSV(s), kept {min(len(all_csvs), keep_recent)} most recent.")

    return deleted


def collect_csv_files(path: str, after: str = "") -> tuple[list[str], list[str]]:
    """Return (to_upload, skipped) lists of CSV file paths.

    *after* is the LAST_UPLOADED filename; any file whose name sorts
    <= *after* is skipped.  Returns two sorted lists.
    """
    p = Path(path).resolve()
    if p.is_file():
        name = p.name
        if after and name <= after:
            return [], [str(p)]
        return [str(p)], []
    if p.is_dir():
        all_csvs = sorted(p.glob("log_*_*.csv"))
        to_upload = []
        skipped = []
        for f in all_csvs:
            if after and f.name <= after:
                skipped.append(str(f))
            else:
                to_upload.append(str(f))
        return to_upload, skipped
    log.error(f"Path not found: {path}")
    sys.exit(1)


# ---------------------------------------------------------------------------
# Browser automation
# ---------------------------------------------------------------------------
def do_login(page, email: str, password: str) -> None:
    """Log in to SavvyAviation if a login form is present."""
    login_needed = False
    email_selector = ""
    for selector in [
        'input[type="email"]',
        'input[name="email"]',
        'input[name="username"]',
        'input[placeholder*="email" i]',
        'input[placeholder*="username" i]',
    ]:
        if page.query_selector(selector):
            login_needed = True
            email_selector = selector
            log.info(f"Login form detected via: {selector}")
            break

    if not login_needed:
        log.info("No login form detected (may already be logged in).")
        return

    log.info("Logging in...")
    page.fill(email_selector, email)

    password_selector = None
    for sel in [
        'input[type="password"]',
        'input[name="password"]',
        'input[placeholder*="password" i]',
    ]:
        if page.query_selector(sel):
            password_selector = sel
            break

    if not password_selector:
        log.error("Could not find password field!")
        page.screenshot(path=str(DEBUG_DIR / "debug_no_password.png"))
        sys.exit(1)

    page.fill(password_selector, password)

    submitted = False
    for sel in [
        'button[type="submit"]',
        'input[type="submit"]',
        'button:has-text("Log In")',
        'button:has-text("Login")',
        'button:has-text("Sign In")',
        'button:has-text("Sign in")',
    ]:
        btn = page.query_selector(sel)
        if btn:
            btn.click()
            submitted = True
            log.info(f"Clicked login button: {sel}")
            break

    if not submitted:
        log.info("No login button found, pressing Enter...")
        page.press(password_selector, "Enter")

    page.wait_for_load_state("domcontentloaded", timeout=LOGIN_TIMEOUT)
    time.sleep(3)
    page.screenshot(path=str(DEBUG_DIR / "debug_after_login.png"))
    log.info("Login submitted.")


def upload_single_file(page, csv_path: str) -> None:
    """Select a file via the upload form's file input."""
    log.info("Looking for file input...")
    file_input = page.query_selector('input[type="file"]')
    if file_input:
        log.info("Found file input element, uploading...")
        file_input.set_input_files(csv_path)
        return

    log.info("No visible file input. Looking for upload button/zone...")
    for sel in [
        'button:has-text("Upload")',
        'button:has-text("Choose File")',
        'button:has-text("Select File")',
        'button:has-text("Browse")',
        '[class*="upload"]',
        '[class*="dropzone"]',
    ]:
        el = page.query_selector(sel)
        if el:
            el.click()
            log.info(f"Clicked upload element: {sel}")
            time.sleep(1)
            break

    file_input = page.query_selector('input[type="file"]')
    if file_input:
        log.info("File input appeared, uploading...")
        file_input.set_input_files(csv_path)
    else:
        log.info("Using file chooser dialog approach...")
        with page.expect_file_chooser(timeout=10_000) as fc_info:
            page.click('text="Upload"', timeout=5_000)
        fc_info.value.set_files(csv_path)


def poll_upload_status(page, search_name: str) -> str:
    """Poll the upload page until the file's status is no longer 'Processing...'.

    Returns the final status string, or 'timeout' if we gave up.
    """
    max_polls = int(UPLOAD_TIMEOUT / 1000 / POLL_INTERVAL)

    for attempt in range(max_polls):
        page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
        time.sleep(1)

        body_text = page.inner_text("body")

        if search_name not in body_text:
            log.info(f"  Poll {attempt + 1}: '{search_name}' not yet visible on page.")
        else:
            # Grab the ~200 chars after the filename to find the status
            after_name = body_text.split(search_name, 1)[-1][:200]

            if "Processing" in after_name:
                log.info(f"  Poll {attempt + 1}: Still processing...")
            else:
                # Extract the status text (e.g. "File Duplicated", "File Too Small")
                status = extract_status(after_name)
                log.info(f"  Poll {attempt + 1}: Done -> {status}")
                return status

        time.sleep(POLL_INTERVAL)
        page.reload(wait_until="domcontentloaded")
        time.sleep(3)

    return "timeout"


def extract_status(text_after_name: str) -> str:
    """Pull the first recognisable status phrase from the text following the filename.

    The upload page shows statuses like:
        ● ● ● ●  ✓  Success (Show 0 Flights)
        ● ● ● ●      File Duplicated  Show Duplicate
        ● ● ● ●      File Too Small
    """
    # "Success (Show N Flights)" - capture the full phrase
    m = re.search(r"Success\s*\(Show\s+(\d+)\s+Flights?\)", text_after_name)
    if m:
        return f"Success ({m.group(1)} flights)"

    # Known status phrases
    known = [
        "File Duplicated",
        "File Too Small",
        "File Rejected",
        "Processed",
        "Success",
        "Error",
    ]
    for phrase in known:
        if phrase in text_after_name:
            return phrase

    # Fallback: strip dates and dots, take first meaningful text
    cleaned = re.sub(r"\d{4}-\d{2}-\d{2}", "", text_after_name)
    cleaned = re.sub(r"[●•·✓]", "", cleaned).strip()
    first_line = cleaned.split("\n")[0].strip()[:40]
    return first_line if first_line else "completed"


def scrape_rejected_flights(page) -> list[RejectedFlight]:
    """Scrape the 'Rejected' table from the right-side Recent Flights panel.

    The panel has two sections: 'Accepted' and 'Rejected'.
    The Rejected table has columns: Date, Departure Airport,
    Destination Airport, Duration, Action.
    """
    rejected = []
    body_text = page.inner_text("body")

    # Find the Rejected section
    if "Rejected" not in body_text:
        return rejected

    rejected_section = body_text.split("Rejected", 1)[-1]

    # Parse rows: look for date patterns followed by airport info
    # Format: "2019-04-06  Unknown  Unknown  0h 0m 9s"
    pattern = re.compile(
        r"(\d{4}-\d{2}-\d{2})\s+"        # date
        r"(.+?)\s{2,}"                     # departure (up to double-space)
        r"(.+?)\s{2,}"                     # destination (up to double-space)
        r"(\d+h\s+\d+m(?:\s+\d+s)?)"      # duration like "0h 0m 9s"
    )
    for m in pattern.finditer(rejected_section):
        rejected.append(RejectedFlight(
            date=m.group(1),
            departure=m.group(2).strip(),
            destination=m.group(3).strip(),
            duration=m.group(4).strip(),
        ))

    # If regex didn't match, try a simpler line-by-line approach
    if not rejected:
        lines = rejected_section.split("\n")
        for line in lines:
            line = line.strip()
            # Look for lines starting with a date
            date_match = re.match(r"(\d{4}-\d{2}-\d{2})", line)
            if date_match:
                parts = re.split(r"\s{2,}", line)
                rejected.append(RejectedFlight(
                    date=parts[0] if len(parts) > 0 else "?",
                    departure=parts[1] if len(parts) > 1 else "?",
                    destination=parts[2] if len(parts) > 2 else "?",
                    duration=parts[3] if len(parts) > 3 else "?",
                ))

    return rejected


def check_file_status_on_page(page, search_name: str) -> str | None:
    """Check if a file's status is visible on the current page.

    Returns the status string if the file is done processing,
    None if not found or still processing.
    """
    body_text = page.inner_text("body")
    if search_name not in body_text:
        return None
    after_name = body_text.split(search_name, 1)[-1][:200]
    if "Processing" in after_name:
        return None
    return extract_status(after_name)


def verify_pending_on_page(page, filenames: list[str]) -> tuple[list[UploadResult], list[str]]:
    """Check the upload page for status of previously timed-out files.

    Returns (resolved_results, still_pending_filenames).
    """
    resolved: list[UploadResult] = []
    still_pending: list[str] = []

    page.reload(wait_until="domcontentloaded")
    time.sleep(3)

    for filename in filenames:
        search_name = savvy_display_name(filename)
        status = check_file_status_on_page(page, search_name)
        if status:
            result = UploadResult(filename=filename, status=f"{status} (verified after retry)")
            if "Success" in status:
                flights_match = re.search(r"(\d+) flights?", status)
                if flights_match:
                    result.flights_accepted = int(flights_match.group(1))
            resolved.append(result)
            log.info(f"  Verified: {filename} -> {status}")
        else:
            still_pending.append(filename)
            log.info(f"  Still pending: {filename}")

    return resolved, still_pending


def retry_timed_out(page, timed_out_filenames: list[str]) -> tuple[list[UploadResult], list[str]]:
    """Retry pass: poll timed-out files from the current batch.

    Reloads the page and polls each file for up to RETRY_POLLS cycles.
    Returns (resolved_results, still_pending_filenames).
    """
    if not timed_out_filenames:
        return [], []

    log.info(f"Retry pass: re-checking {len(timed_out_filenames)} timed-out file(s)...")
    resolved: list[UploadResult] = []
    still_pending: list[str] = []

    page.reload(wait_until="domcontentloaded")
    time.sleep(3)

    for filename in timed_out_filenames:
        search_name = savvy_display_name(filename)
        status = None

        for attempt in range(RETRY_POLLS):
            status = check_file_status_on_page(page, search_name)
            if status:
                break
            log.info(f"  Retry poll {attempt + 1}/{RETRY_POLLS}: {filename} still processing...")
            time.sleep(POLL_INTERVAL)
            page.reload(wait_until="domcontentloaded")
            time.sleep(3)

        if status:
            result = UploadResult(filename=filename, status=f"{status} (after retry)")
            if "Success" in status:
                flights_match = re.search(r"(\d+) flights?", status)
                if flights_match:
                    result.flights_accepted = int(flights_match.group(1))
            resolved.append(result)
            log.info(f"  Retry resolved: {filename} -> {status}")
        else:
            still_pending.append(filename)
            log.info(f"  Retry exhausted: {filename} still not resolved")

    return resolved, still_pending


# ---------------------------------------------------------------------------
# Email
# ---------------------------------------------------------------------------
def compose_email(
    results: list[UploadResult],
    n_skipped: int = 0,
    n_cleaned: int = 0,
    n_verified: int = 0,
    n_still_pending: int = 0,
) -> tuple[str, str]:
    """Build an email subject and body summarising the upload run."""
    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    n_success = sum(1 for r in results if "Success" in r.status)
    n_dup = sum(1 for r in results if "Duplicated" in r.status)
    n_err = sum(1 for r in results if r.status.startswith("error"))
    n_timeout = sum(1 for r in results if r.status == "timeout")
    n_other = len(results) - n_success - n_dup - n_err - n_timeout
    total_rejected = sum(len(r.rejected_flights) for r in results)

    subject = f"Savvy Upload Report - {now} ({len(results)} files)"

    lines = [
        "Savvy Aviation Upload Report",
        f"Run at: {now}",
        f"Files processed: {len(results)}",
        f"  Successful: {n_success}",
        f"  Duplicated: {n_dup}",
        f"  Errors:     {n_err}",
    ]
    if n_timeout:
        lines.append(f"  Timed out:  {n_timeout}")
    if n_other:
        lines.append(f"  Other:      {n_other}")
    if n_verified:
        lines.append(f"  Verified from previous run: {n_verified}")
    if n_skipped:
        lines.append(f"  Skipped (already uploaded): {n_skipped}")
    if n_cleaned:
        lines.append(f"  Cleaned up: {n_cleaned} old CSV(s) deleted")
    if n_still_pending:
        lines.append(f"  Still pending verification: {n_still_pending}")

    lines += [
        "",
        "File Details:",
        "-" * 70,
    ]
    for r in results:
        flights_note = ""
        if r.on_flights_page:
            flights_note = " (verified on flights page)"
        lines.append(f"  {r.filename}")
        lines.append(f"    Status: {r.status}{flights_note}")

        if r.rejected_flights:
            lines.append(f"    Rejected flights ({len(r.rejected_flights)}):")
            for rf in r.rejected_flights:
                lines.append(
                    f"      {rf.date}  {rf.departure} -> {rf.destination}"
                    f"  ({rf.duration})"
                )
        lines.append("")

    if total_rejected:
        lines.append(f"NOTE: {total_rejected} flight(s) were rejected "
                      "(e.g. too short, unknown airports).")
        lines.append("")

    if n_err:
        lines.append(f"WARNING: {n_err} file(s) failed to upload. "
                      "Check the log for details.")
        lines.append("")

    if n_still_pending:
        lines.append(f"NOTE: {n_still_pending} file(s) still pending server-side "
                      "processing. They will be re-checked on the next run.")
        lines.append("")

    lines.append("-" * 70)
    return subject, "\n".join(lines)


def _try_msmtp(recipient: str, msg: str) -> bool:
    """Attempt a single msmtp send. Returns True on success."""
    proc = subprocess.run(
        ["msmtp", recipient],
        input=msg,
        capture_output=True,
        text=True,
    )
    if proc.returncode == 0:
        return True
    log.error(f"msmtp failed: {proc.stderr}")
    return False


def _save_unsent_email(subject: str, body: str, recipient: str) -> None:
    """Persist an unsent email to disk so the next run can retry it."""
    emails: list[dict] = []
    if UNSENT_EMAIL_FILE.exists():
        try:
            emails = json.loads(UNSENT_EMAIL_FILE.read_text())
        except (json.JSONDecodeError, OSError):
            pass
    emails.append({"subject": subject, "body": body, "recipient": recipient})
    UNSENT_EMAIL_FILE.write_text(json.dumps(emails, indent=2) + "\n")
    log.info(f"Saved unsent email to {UNSENT_EMAIL_FILE} for retry on next run.")


def retry_unsent_emails() -> None:
    """Try to send any emails that failed on previous runs."""
    if not UNSENT_EMAIL_FILE.exists():
        return
    try:
        emails = json.loads(UNSENT_EMAIL_FILE.read_text())
    except (json.JSONDecodeError, OSError):
        return
    if not emails or not shutil.which("msmtp"):
        return

    log.info(f"Found {len(emails)} unsent email(s) from previous run(s), retrying...")
    still_unsent: list[dict] = []
    for em in emails:
        msg = f"To: {em['recipient']}\nSubject: {em['subject']}\n\n{em['body']}\n"
        if _try_msmtp(em["recipient"], msg):
            log.info(f"Sent previously-unsent email: {em['subject']}")
        else:
            still_unsent.append(em)

    if still_unsent:
        UNSENT_EMAIL_FILE.write_text(json.dumps(still_unsent, indent=2) + "\n")
        log.warning(f"{len(still_unsent)} email(s) still unsent.")
    else:
        UNSENT_EMAIL_FILE.unlink(missing_ok=True)
        log.info("All previously-unsent emails sent successfully.")


def send_email(subject: str, body: str, recipient: str, max_retries: int = 3) -> None:
    """Send via msmtp if available, otherwise print to terminal.

    If all retries fail, saves the email to disk so the next run can retry.
    """
    if shutil.which("msmtp"):
        msg = f"To: {recipient}\nSubject: {subject}\n\n{body}\n"
        for attempt in range(1, max_retries + 1):
            log.info(f"Sending email to {recipient} via msmtp (attempt {attempt}/{max_retries})...")
            if _try_msmtp(recipient, msg):
                log.info("Email sent.")
                return
            if attempt < max_retries:
                delay = 10 * attempt
                log.info(f"Retrying in {delay}s...")
                time.sleep(delay)
        # All retries exhausted — save for next run
        log.warning("All email send attempts failed. Saving to disk for retry.")
        _save_unsent_email(subject, body, recipient)
    else:
        # No msmtp (e.g. macOS dev machine) - just dump to terminal
        log.info("msmtp not available, printing email to terminal:\n")
        print("=" * 60)
        print(f"To: {recipient}")
        print(f"Subject: {subject}")
        print("-" * 60)
        print(body)
        print("=" * 60)


# ---------------------------------------------------------------------------
# Main flow
# ---------------------------------------------------------------------------
def run(
    path: str = "",
    headed: bool = False,
    slow_mo: int = 0,
    reupload: bool = False,
) -> None:
    """Upload all CSVs from *path*, verify on flights page, email summary."""
    from playwright.sync_api import sync_playwright

    cfg = load_config(cli_path=path)
    log.info(f"Aircraft ID: {cfg.aircraft_id}")

    # --- Retry any unsent emails from previous runs ---
    retry_unsent_emails()

    # --- Filter already-uploaded files ---
    last_uploaded = "" if reupload else load_last_uploaded()
    if last_uploaded:
        log.info(f"Last uploaded file: {last_uploaded}")

    csv_files, skipped_files = collect_csv_files(cfg.csv_dir, after=last_uploaded)

    if skipped_files:
        log.info(f"Skipping {len(skipped_files)} already-uploaded file(s).")

    # --- Load pending files from previous runs ---
    pending_filenames = load_pending_uploads()
    if pending_filenames:
        log.info(f"Found {len(pending_filenames)} file(s) pending verification from previous run(s).")

    if not csv_files and not pending_filenames:
        if skipped_files:
            log.info("No new CSV files to upload (all already processed).")
        else:
            log.error(f"No CSV files found in: {path}")
        sys.exit(0)

    if csv_files:
        log.info(f"Found {len(csv_files)} new CSV file(s) to upload.")

    results: list[UploadResult] = []
    verified_results: list[UploadResult] = []
    n_skipped = len(skipped_files)

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=not headed, slow_mo=slow_mo)
        context = browser.new_context(user_agent=cfg.user_agent)
        page = context.new_page()
        page.set_default_timeout(NAV_TIMEOUT)

        try:
            # --- Login once ---
            log.info("Navigating to SavvyAviation...")
            page.goto(cfg.upload_url, wait_until="domcontentloaded")
            time.sleep(2)
            do_login(page, cfg.email, cfg.password)

            # Make sure we're on the upload page after login
            if cfg.upload_url not in page.url:
                page.goto(cfg.upload_url, wait_until="domcontentloaded")
                time.sleep(2)

            # --- Verify pending files from previous runs ---
            if pending_filenames:
                log.info(f"Verifying {len(pending_filenames)} file(s) from previous run(s)...")
                verified, still_pending = verify_pending_on_page(page, pending_filenames)
                verified_results.extend(verified)
                pending_filenames = still_pending

            # --- Snapshot baseline rejected flights already on the page ---
            page.evaluate("window.scrollTo(0, 0)")
            time.sleep(1)
            seen_rejected: set[RejectedFlight] = set(scrape_rejected_flights(page))
            if seen_rejected:
                log.info(f"Baseline: {len(seen_rejected)} pre-existing rejected flight(s) on page.")

            # --- Upload each file ---
            for i, csv_path in enumerate(csv_files, 1):
                filename = Path(csv_path).name
                search_name = savvy_display_name(csv_path)
                result = UploadResult(filename=filename)
                log.info(f"[{i}/{len(csv_files)}] Uploading: {filename}")

                try:
                    upload_single_file(page, csv_path)
                    log.info("File selected, waiting for upload to process...")
                    time.sleep(5)

                    status = poll_upload_status(page, search_name)
                    result.status = status
                    log.info(f"[{i}/{len(csv_files)}] {filename} -> {status}")

                    # If upload succeeded, check the right-side panel for
                    # accepted/rejected flights
                    if "Success" in status:
                        # Scroll back up to see the Recent Flights panel
                        page.evaluate("window.scrollTo(0, 0)")
                        time.sleep(2)

                        # Parse flight count from status like "Success (3 flights)"
                        flights_match = re.search(r"(\d+) flights?", status)
                        if flights_match:
                            result.flights_accepted = int(flights_match.group(1))

                        # Check for rejected flights - only attribute NEW ones
                        all_rejected = set(scrape_rejected_flights(page))
                        new_rejected = all_rejected - seen_rejected
                        if new_rejected:
                            result.rejected_flights = sorted(
                                new_rejected, key=lambda rf: rf.date
                            )
                            for rf in result.rejected_flights:
                                log.info(
                                    f"    Rejected flight: {rf.date} "
                                    f"{rf.departure} -> {rf.destination} "
                                    f"({rf.duration})"
                                )
                        # Update baseline so next file only sees its own
                        seen_rejected = all_rejected

                except Exception as e:
                    result.status = f"error: {e}"
                    log.error(f"[{i}/{len(csv_files)}] {filename} failed: {e}")
                    page.screenshot(
                        path=str(DEBUG_DIR / f"debug_error_{i}.png")
                    )

                results.append(result)

                # Reload upload page for next file
                if i < len(csv_files):
                    page.goto(cfg.upload_url, wait_until="domcontentloaded")
                    time.sleep(2)

            # --- Retry pass for timed-out files from this batch ---
            timed_out = [r.filename for r in results if r.status == "timeout"]
            if timed_out:
                page.goto(cfg.upload_url, wait_until="domcontentloaded")
                time.sleep(2)
                retry_resolved, retry_still_pending = retry_timed_out(page, timed_out)

                # Update original results for resolved files
                resolved_map = {r.filename: r for r in retry_resolved}
                for r in results:
                    if r.filename in resolved_map:
                        resolved = resolved_map[r.filename]
                        r.status = resolved.status
                        r.flights_accepted = resolved.flights_accepted

                # Any files still not resolved go to persistent pending list
                pending_filenames.extend(retry_still_pending)

            # --- Verify on flights page ---
            all_to_verify = results + verified_results
            log.info(f"Navigating to flights page: {cfg.flights_url}")
            page.goto(cfg.flights_url, wait_until="domcontentloaded")
            time.sleep(3)
            page.screenshot(path=str(DEBUG_DIR / "debug_flights_page.png"))

            flights_body = page.inner_text("body")
            for r in all_to_verify:
                # Flights page shows full filename (e.g. log_20260213_114814_KANK.csv)
                if r.filename in flights_body:
                    r.on_flights_page = True
                    log.info(f"  Verified on flights page: {r.filename}")

            page.screenshot(path=str(DEBUG_DIR / "debug_final.png"))

        except Exception as e:
            log.error(f"Fatal error: {e}")
            try:
                page.screenshot(path=str(DEBUG_DIR / "debug_error.png"))
            except Exception:
                pass
            raise
        finally:
            browser.close()

    # --- Persist any still-pending files ---
    save_pending_uploads(pending_filenames)

    # --- Update watermark ---
    # Advance to the newest file we attempted, so we don't retry it.
    if results:
        newest = max(r.filename for r in results)
        save_last_uploaded(newest)

    # --- Clean up already-uploaded CSVs ---
    watermark = load_last_uploaded()
    n_cleaned = cleanup_csvs(
        cfg.csv_dir, watermark, keep_recent=10, pending_filenames=pending_filenames,
    ) if watermark else 0

    # --- Email summary ---
    all_results = verified_results + results
    n_verified = len(verified_results)
    subject, body = compose_email(
        all_results, n_skipped=n_skipped, n_cleaned=n_cleaned, n_verified=n_verified,
        n_still_pending=len(pending_filenames),
    )
    send_email(subject, body, cfg.email)
    log.info("All done.")


def main():
    parser = argparse.ArgumentParser(
        description="Upload engine monitor CSVs to SavvyAviation.com"
    )
    parser.add_argument(
        "path",
        nargs="?",
        default="",
        help="Path to a CSV file or directory (defaults to CSV_DIR from .env)",
    )
    parser.add_argument(
        "--headed",
        action="store_true",
        help="Run with visible browser (for debugging)",
    )
    parser.add_argument(
        "--slow-mo",
        type=int,
        default=0,
        help="Slow down actions by N ms (for debugging)",
    )
    parser.add_argument(
        "--reupload",
        action="store_true",
        help="Ignore LAST_UPLOADED watermark and process all files",
    )
    args = parser.parse_args()

    run(args.path, headed=args.headed, slow_mo=args.slow_mo, reupload=args.reupload)


if __name__ == "__main__":
    main()
