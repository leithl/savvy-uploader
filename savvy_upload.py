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
import traceback
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

# Retries for an upload-page navigation aborted by the site's own in-flight
# redirect (see _goto_upload_page). The interrupting navigation targets the
# same URL, so a small bounded retry resolves it; real navigation failures
# carry a different error message and are never retried.
NAV_REDIRECT_RETRIES = 3

DEBUG_DIR = Path(__file__).parent
PENDING_FILE = DEBUG_DIR / "pending_uploads.json"
UNSENT_EMAIL_FILE = DEBUG_DIR / "unsent_email.json"

# Upload-page statuses that mean Savvy is done with the file. The poller
# stops only when one of these appears in the row text; everything else
# (e.g. "Processing", "Checking Duplicates") keeps polling. Single source
# of truth: extract_status only emits these phrases or the "unknown"
# fallback for genuinely novel server output.
TERMINAL_STATUS_PHRASES = (
    "Success",
    "File Duplicated",
    "File Too Small",
    "File Rejected",
    "Processed",
    "Error",
)

# Status recorded when the left "Recent Uploads" widget never reaches a
# terminal phrase but Savvy has clearly finished the file by listing its
# flight(s) as rejected in the right "Recent Flights" panel (e.g. a flight
# too short to keep). These files ARE on Savvy with 0 accepted flights, so
# this is a real terminal outcome — not the opaque "timeout" we reserve for
# genuine no-signal-anywhere hangs. See poll_upload_status / _new_rejected_in_text.
REJECTED_STATUS = "Rejected"

# Reason attributed to a flight in Savvy's "Rejected" panel. That panel is
# Savvy's short-flight bucket (the GraphQL `hideShortFlights` category) and
# carries no per-row reason text — its columns are only Date, Departure,
# Destination, Duration, Action — so every panel rejection is attributed
# this one reason. If Savvy ever surfaces an explicit reason string, capture
# it on RejectedFlight and prefer it over this default.
DEFAULT_REJECTION_REASON = "too short"

# Per-file outcome categories — the machine-readable classification a summary
# consumer renders from, so it never has to sniff the raw Savvy status string
# (the source of the "Success (1 flight)" vs "1 accepted flight" inconsistency
# we got bitten by). summary_line() is the matching human-readable projection.
OUTCOME_ACCEPTED = "accepted"     # >=1 accepted flight, none rejected
OUTCOME_MIXED = "mixed"           # >=1 accepted AND >=1 rejected
OUTCOME_REJECTED = "rejected"     # 0 accepted, >=1 rejected
OUTCOME_DUPLICATE = "duplicate"   # Savvy already had this file
OUTCOME_TOO_SMALL = "too_small"   # the file itself was too small to parse
OUTCOME_NONE = "none"             # processed, no flight (e.g. ground engine run)
OUTCOME_TIMEOUT = "timeout"       # no terminal signal anywhere; retry next run
OUTCOME_ERROR = "error"           # upload raised before Savvy acknowledged it
OUTCOME_UNKNOWN = "unknown"       # novel / unrecognised Savvy status

# Bump on any breaking change to the summary dict shape (see
# build_summary_dict). External consumers of the JSONL sidecar should
# check this and refuse unknown majors rather than guessing fields.
# v2: added per-file `outcome` + `summary`, per-rejection `reason`.
SUMMARY_SCHEMA_VERSION = 2

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

# Savvy GraphQL query that returns every engine-data FILE plus every
# Flight, joined back to its importFile. Two reasons we need both:
#
#   1. engineDataFiles is the authoritative list of files Savvy has
#      received — files reported as "Success (0 flights)" still appear
#      here. This is what the earlier silent-loss bug missed (we used to
#      query only `flights`, which hid every flightless file).
#   2. flights{importFile{name}} lets us count flights per file at query
#      time, bypassing the upload-page status string entirely. Savvy
#      parses CSVs asynchronously and the page status can capture a
#      Phase-2 reading where a flight hasn't been attributed yet (or
#      has been temporarily mis-attributed). GraphQL after a settle
#      delay is the source of truth.
SAVVY_FILES_QUERY = """query AircraftEngineDataFiles($id: Int) {
  me { id aircraft(id: $id) {
    id
    engineDataFiles { id name uploadDate __typename }
    flights(hideShortFlights: false) {
      id departureId destinationId duration
      importFile { id name __typename }
      __typename
    }
  } }
}"""

# Upload-page status strings that mean Savvy positively acknowledged the
# file. Anything else (timeouts, raw filename-fragments, "Error", etc.) is
# treated as unverified and the file is kept for retry.
SAVVY_ACCEPTED_PREFIXES = ("Success", "File Duplicated", "Processed")


def fetch_savvy_file_flight_counts(api_ctx, aircraft_id: str) -> dict[str, int] | None:
    """Return {filename: flight_count} for every CSV Savvy has for this
    aircraft. None on transport / parse error (caller falls back to the
    upload-page status).

    Every file known to Savvy gets an entry (default 0) even if no
    Flight has been attributed yet — so caller membership check
    ``name in result`` answers "is this file on Savvy?" while
    ``result[name]`` answers "how many flights did Savvy attribute to
    it?". A 0-count can legitimately mean either "pre-flight engine
    run, no flight expected" OR "Phase-3 attribution hasn't landed
    yet"; the caller's settle delay before this query is what keeps
    the second case rare.
    """
    try:
        resp = api_ctx.post(
            f"{SAVVY_BASE}/graphql",
            data=json.dumps({
                "operationName": "AircraftEngineDataFiles",
                "variables": {"id": int(aircraft_id)},
                "query": SAVVY_FILES_QUERY,
            }),
            headers={"content-type": "application/json"},
        )
        if resp.status != 200:
            log.warning(f"GraphQL verification HTTP {resp.status}")
            return None
        data = resp.json().get("data") or {}
        acft = (data.get("me") or {}).get("aircraft") or []
        if not acft:
            return {}
        edfs = acft[0].get("engineDataFiles") or []
        counts: dict[str, int] = {e["name"]: 0 for e in edfs if e.get("name")}
        for f in acft[0].get("flights") or []:
            imp = f.get("importFile") or {}
            name = imp.get("name")
            if name and name in counts:
                counts[name] += 1
        return counts
    except Exception as e:
        log.warning(f"GraphQL verification failed: {e}")
        return None


def status_looks_accepted(status: str) -> bool:
    """True if the upload-page status indicates Savvy received the file
    successfully. Catches both 'Success' (with or without flight count)
    and 'File Duplicated' (Savvy already has it). Rejects bogus statuses
    like raw filename fragments or 'unknown'."""
    if not status:
        return False
    return any(status.startswith(p) for p in SAVVY_ACCEPTED_PREFIXES)


# Status strings that legitimately mean "this file has no flight and is
# fully processed" — used by the post-settle GraphQL override to decide
# whether a 0-flight GraphQL count is benign or suspicious (i.e. Phase-3
# attribution still in flight). "Success" is intentionally NOT here:
# "Success (0 flights)" can be a Phase-2 reading that flips later.
TERMINAL_NO_FLIGHT_PREFIXES = (
    "File Duplicated",
    "File Too Small",
    "File Rejected",
    "Rejected",  # REJECTED_STATUS: panel-detected rejection legitimately has 0 flights
    "Error",
    "error",  # the "error: <exception>" path produced by upload exceptions
)


def _is_terminal_no_flight_status(status: str) -> bool:
    """True if the upload-page status implies the file is fully
    processed AND legitimately produced no flight. False for anything
    that could plausibly be a Phase-2 reading (Success, Processed,
    unknown, timeout)."""
    if not status:
        return False
    return any(status.startswith(p) for p in TERMINAL_NO_FLIGHT_PREFIXES)


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


DEFAULT_GRAPHQL_SETTLE_SECONDS = 60


@dataclass
class Config:
    email: str
    password: str
    aircraft_id: str
    csv_dir: str
    summary_dir: str = ""
    user_agent: str = DEFAULT_USER_AGENT
    graphql_settle_seconds: int = DEFAULT_GRAPHQL_SETTLE_SECONDS
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
    summary_dir = _get("SUMMARY_DIR")
    user_agent = _get("USER_AGENT") or DEFAULT_USER_AGENT

    settle_raw = _get("GRAPHQL_SETTLE_SECONDS")
    try:
        settle = int(settle_raw) if settle_raw else DEFAULT_GRAPHQL_SETTLE_SECONDS
    except ValueError:
        log.warning(
            f"Invalid GRAPHQL_SETTLE_SECONDS={settle_raw!r}; "
            f"using default {DEFAULT_GRAPHQL_SETTLE_SECONDS}."
        )
        settle = DEFAULT_GRAPHQL_SETTLE_SECONDS

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
        summary_dir=summary_dir,
        user_agent=user_agent,
        graphql_settle_seconds=settle,
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


def _text_after_filename(body_text: str, filename: str) -> str | None:
    """Return up to 200 chars of body_text following the file's display name.

    The upload page may render the row with the full filename
    (log_YYYYMMDD_HHMMSS_KXXX[.csv]) or, on older UIs, just the
    date/time prefix (log_YYYYMMDD_HHMMSS). We try the full stem first
    so the split lands cleanly past the filename, falling back to the
    prefix and consuming any airport-code suffix that follows. Returns
    None if neither form is on the page.
    """
    full_stem = Path(filename).stem  # log_YYYYMMDD_HHMMSS_KXXX
    if full_stem in body_text:
        tail = body_text.split(full_stem, 1)[-1]
        return re.sub(r"^\.csv\b", "", tail)[:200]

    parts = full_stem.split("_")
    if len(parts) >= 4 and parts[0] == "log":
        prefix = "_".join(parts[:3])
        if prefix in body_text:
            tail = body_text.split(prefix, 1)[-1]
            return re.sub(r"^_[A-Z0-9]+(\.csv)?\b", "", tail)[:200]
    return None


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


def _goto_upload_page(page, upload_url: str) -> None:
    """Navigate to the upload page, tolerating the post-login redirect race.

    After a successful login the site fires its own redirect back to the
    originally-requested upload URL. An explicit ``page.goto()`` issued while
    that redirect is still in flight aborts with "interrupted by another
    navigation" — both navigations target the same URL, so it's a benign
    timing race rather than a real failure. Treat only that specific error as
    retryable: let the competing navigation settle, re-check the URL, and
    retry up to NAV_REDIRECT_RETRIES times. Every other navigation error
    (timeouts, DNS, connection refused, ...) propagates immediately, so real
    failures are never masked.
    """
    for attempt in range(1, NAV_REDIRECT_RETRIES + 1):
        try:
            page.goto(upload_url, wait_until="domcontentloaded")
            return
        except Exception as e:
            interrupted = "interrupted by another navigation" in str(e)
            if not (interrupted and attempt < NAV_REDIRECT_RETRIES):
                raise
            log.warning(
                f"Upload-page navigation interrupted by an in-flight redirect "
                f"(attempt {attempt}/{NAV_REDIRECT_RETRIES}); settling and retrying."
            )
            # Let the competing navigation finish. If it lands us on the
            # upload page, the redirect did our job for us and we're done.
            try:
                page.wait_for_url(lambda u: upload_url in u, timeout=NAV_TIMEOUT)
            except Exception:
                pass
            if upload_url in page.url:
                return


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


def _is_terminal_status_visible(text_after_name: str) -> bool:
    """True if the row text contains a terminal status phrase from
    TERMINAL_STATUS_PHRASES — the cue for the poller to stop.

    Transient statuses like "Processing" and "Checking Duplicates"
    don't match, so the poller keeps polling. This keeps a single
    source of truth: anything not listed here is treated as transient,
    so a new server-side transient state can't accidentally leak out
    as a final status.
    """
    return any(phrase in text_after_name for phrase in TERMINAL_STATUS_PHRASES)


def poll_upload_status(
    page, filename: str, seen_rejected: set[RejectedFlight] | None = None
) -> str:
    """Poll the upload page until the file's status stabilises on a terminal phrase.

    Savvy processes CSVs asynchronously in multiple phases — an early
    "Success (Show 0 Flights)" can flip to "Success (Show N Flights)"
    seconds later as the parser finishes. Returning the first terminal
    read therefore captures the wrong count.

    Stability gate: require the same terminal status to appear on two
    consecutive reads before returning. Any transient state (Processing,
    Checking Duplicates, the file vanishing from the row) resets the
    captured status so a later Phase-3 transition can't be confirmed
    against a stale Phase-2 reading. Same overall poll budget
    (UPLOAD_TIMEOUT) — just one extra confirmation cycle in the common case.

    Second terminal signal (the Rejected panel): for a flight Savvy rejects
    as too short, the left "Recent Uploads" status can get stuck mid-phase
    (e.g. "Uploading… (0%)") and never reach a terminal phrase — so the
    stability gate alone would loop to 'timeout'. When the caller passes a
    ``seen_rejected`` baseline, each cycle also checks the right "Recent
    Flights" Rejected panel; a NEW rejected row (one not in the baseline)
    means Savvy finished and rejected the flight, and we return
    REJECTED_STATUS. Only consulted when the left widget is NOT showing a
    terminal phrase, so a normal "Success (N flights)" file (which may also
    have a rejected leg) still resolves via the stability gate. Callers
    that don't pass a baseline (e.g. unit tests of the gate) keep the
    original left-widget-only behaviour.

    Returns the stable status string, REJECTED_STATUS, or 'timeout'.
    """
    max_polls = int(UPLOAD_TIMEOUT / 1000 / POLL_INTERVAL)
    last_status: str | None = None

    for attempt in range(max_polls):
        page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
        time.sleep(1)

        body_text = page.inner_text("body")
        after_name = _text_after_filename(body_text, filename)
        left_terminal = after_name is not None and _is_terminal_status_visible(after_name)

        if after_name is None:
            log.info(f"  Poll {attempt + 1}: '{filename}' not yet visible on page.")
            last_status = None
        elif not left_terminal:
            log.info(f"  Poll {attempt + 1}: Still processing...")
            last_status = None
        else:
            status = extract_status(after_name)
            if status == last_status:
                log.info(f"  Poll {attempt + 1}: Stable -> {status}")
                return status
            log.info(f"  Poll {attempt + 1}: Read -> {status} (awaiting confirmation)")
            last_status = status

        # Panel-based terminal signal — only when the left widget hasn't given
        # a terminal phrase this cycle, so it can't pre-empt a Success.
        if seen_rejected is not None and not left_terminal:
            new_rejected = _new_rejected_in_text(body_text, seen_rejected)
            if new_rejected:
                log.info(
                    f"  Poll {attempt + 1}: Savvy rejected {len(new_rejected)} "
                    f"flight(s) for '{filename}' (terminal via Rejected panel)."
                )
                return REJECTED_STATUS

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
        n = int(m.group(1))
        unit = "flight" if n == 1 else "flights"
        return f"Success ({n} {unit})"

    for phrase in TERMINAL_STATUS_PHRASES:
        if phrase in text_after_name:
            return phrase

    # With the terminal-phrase gate in the poller, this should only fire
    # for genuinely novel server output. Surface it as "unknown" so the
    # email flags it for investigation rather than smuggling a transient
    # status through as a final one.
    return "unknown"


def parse_rejected_flights(body_text: str) -> list[RejectedFlight]:
    """Parse the 'Rejected' table out of the page's body text.

    The right-side Recent Flights panel has two sections, 'Accepted' and
    'Rejected'. The Rejected table has columns: Date, Departure Airport,
    Destination Airport, Duration, Action. Pure function (no page) so the
    scraping can be unit-tested against fixture text.
    """
    rejected: list[RejectedFlight] = []

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


def scrape_rejected_flights(page) -> list[RejectedFlight]:
    """Scrape the 'Rejected' table from the right-side Recent Flights panel."""
    return parse_rejected_flights(page.inner_text("body"))


def _attribute_new_rejected(
    page, seen_rejected: set[RejectedFlight]
) -> tuple[list[RejectedFlight], set[RejectedFlight]]:
    """Scrape the Rejected panel and split out the rows not already in the
    baseline. Returns (newly-appeared rejected flights sorted by date, full
    current set). The full set becomes the next file's baseline so each file
    is attributed only its own new rejections.
    """
    all_rejected = set(scrape_rejected_flights(page))
    new = sorted(all_rejected - seen_rejected, key=lambda rf: rf.date)
    return new, all_rejected


def _new_rejected_in_text(
    body_text: str, seen_rejected: set[RejectedFlight]
) -> list[RejectedFlight]:
    """Rejected rows present in body_text but not in the baseline, sorted by
    date. Used as a terminal signal: a new rejected row attributable to the
    file being polled means Savvy finished and rejected it, even when the
    left 'Recent Uploads' widget is stuck mid-phase.
    """
    new = set(parse_rejected_flights(body_text)) - seen_rejected
    return sorted(new, key=lambda rf: rf.date)


def _terminal_status_in_text(body_text: str, filename: str) -> str | None:
    """Terminal status for a file from page body text, or None if the file
    isn't visible yet or is still processing. Pure (no page) for testing."""
    after_name = _text_after_filename(body_text, filename)
    if after_name is None or not _is_terminal_status_visible(after_name):
        return None
    return extract_status(after_name)


def verify_pending_on_page(
    page, filenames: list[str], seen_rejected: set[RejectedFlight] | None = None
) -> tuple[list[UploadResult], list[str]]:
    """Check the upload page for status of previously timed-out files.

    Reads the body once per file and consults two terminal signals: the
    left "Recent Uploads" status, and — when ``seen_rejected`` is supplied —
    a NEW row in the right "Recent Flights" Rejected panel (a rejection that
    surfaced after the original run timed out). Mutates ``seen_rejected`` so
    a row attributed to one file isn't re-attributed to the next.

    Returns (resolved_results, still_pending_filenames).
    """
    resolved: list[UploadResult] = []
    still_pending: list[str] = []

    page.reload(wait_until="domcontentloaded")
    time.sleep(3)

    for filename in filenames:
        body_text = page.inner_text("body")
        status = _terminal_status_in_text(body_text, filename)
        rejected_for_file: list[RejectedFlight] = []
        if status is None and seen_rejected is not None:
            rejected_for_file = _new_rejected_in_text(body_text, seen_rejected)
            if rejected_for_file:
                seen_rejected |= set(rejected_for_file)
                status = REJECTED_STATUS

        if status:
            result = UploadResult(
                filename=filename,
                status=f"{status} (verified after retry)",
                rejected_flights=rejected_for_file,
            )
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


def retry_timed_out(
    page, timed_out_filenames: list[str], seen_rejected: set[RejectedFlight] | None = None
) -> tuple[list[UploadResult], list[str]]:
    """Retry pass: poll timed-out files from the current batch.

    Reloads the page and polls each file for up to RETRY_POLLS cycles,
    applying the same consecutive-match stability gate as the main
    poller so we don't latch a Phase-2 reading here either. When
    ``seen_rejected`` is supplied, each cycle also treats a NEW row in the
    Rejected panel as a terminal outcome (REJECTED_STATUS) — the same
    late-rejection signal the main poller uses, for a file whose rejection
    only surfaced after the first poll window. Mutates ``seen_rejected``.

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
        status: str | None = None
        last_status: str | None = None
        rejected_for_file: list[RejectedFlight] = []

        for attempt in range(RETRY_POLLS):
            body_text = page.inner_text("body")
            current = _terminal_status_in_text(body_text, filename)
            if current is None:
                log.info(f"  Retry poll {attempt + 1}/{RETRY_POLLS}: {filename} still processing...")
                last_status = None
            elif current == last_status:
                status = current
                log.info(f"  Retry poll {attempt + 1}/{RETRY_POLLS}: {filename} stable -> {status}")
                break
            else:
                log.info(f"  Retry poll {attempt + 1}/{RETRY_POLLS}: {filename} -> {current} (awaiting confirmation)")
                last_status = current

            # Panel-based terminal signal — only when the left widget isn't
            # already reporting a terminal status this cycle.
            if current is None and seen_rejected is not None:
                rejected_for_file = _new_rejected_in_text(body_text, seen_rejected)
                if rejected_for_file:
                    seen_rejected |= set(rejected_for_file)
                    status = REJECTED_STATUS
                    log.info(
                        f"  Retry poll {attempt + 1}/{RETRY_POLLS}: {filename} "
                        f"rejected {len(rejected_for_file)} flight(s) (Rejected panel)."
                    )
                    break

            time.sleep(POLL_INTERVAL)
            page.reload(wait_until="domcontentloaded")
            time.sleep(3)

        if status:
            result = UploadResult(
                filename=filename,
                status=f"{status} (after retry)",
                rejected_flights=rejected_for_file,
            )
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
# Summary (canonical dict + projections: email body, JSON sidecar)
# ---------------------------------------------------------------------------
def _compact_duration(duration: str) -> str:
    """'0h 2m 3s' -> '2m 3s', '0h 0m 9s' -> '9s', '1h 0m 5s' -> '1h 0m 5s'.

    Drops leading zero-valued units so the common short-flight case reads
    cleanly, then keeps every unit once a non-zero one appears. Returns the
    input unchanged if it doesn't match Savvy's 'Xh Ym Zs' shape (e.g. '?')."""
    m = re.match(r"(\d+)h\s+(\d+)m(?:\s+(\d+)s)?", duration.strip())
    if not m:
        return duration.strip()
    units = [
        (int(m.group(1)), "h"),
        (int(m.group(2)), "m"),
        (int(m.group(3) or 0), "s"),
    ]
    while len(units) > 1 and units[0][0] == 0:
        units.pop(0)
    return " ".join(f"{v}{u}" for v, u in units)


def classify_outcome(status: str, flights_accepted: int, n_rejected: int) -> str:
    """Map a file's (raw status, accepted count, rejected count) to one of the
    OUTCOME_* categories.

    Keys off the structured counts before the raw status, so a file accepted
    via the post-settle GraphQL re-count — whose raw status may never have said
    'Success' — still classifies as accepted. That status-vs-count split is
    exactly what made two identical outcomes render two different ways."""
    s = (status or "").strip()
    if s == "timeout":
        return OUTCOME_TIMEOUT
    if s.startswith(("error", "Error")):
        return OUTCOME_ERROR
    if flights_accepted > 0 and n_rejected > 0:
        return OUTCOME_MIXED
    if flights_accepted > 0:
        return OUTCOME_ACCEPTED
    if n_rejected > 0:
        return OUTCOME_REJECTED
    if s.startswith("File Duplicated"):
        return OUTCOME_DUPLICATE
    if s.startswith("File Too Small"):
        return OUTCOME_TOO_SMALL
    if s.startswith(("File Rejected", REJECTED_STATUS)):
        return OUTCOME_REJECTED
    if not s or s == "unknown":
        return OUTCOME_UNKNOWN
    return OUTCOME_NONE


def _flights_accepted_phrase(n: int) -> str:
    return f"{n} flight accepted" if n == 1 else f"{n} flights accepted"


def _rf_duration(rf) -> str:
    """Duration of a rejected flight, whether it's a RejectedFlight or the
    dict form from a summary sidecar — so summary_line works on both."""
    return rf["duration"] if isinstance(rf, dict) else rf.duration


def _rejected_phrase(rejected_flights: list, *, standalone: bool) -> str:
    """Human phrase for a file's rejected flights, e.g.:

        standalone=True  -> 'rejected — too short (2m 3s)'   (rejected-only file)
        standalone=False -> '1 rejected (too short, 2m 3s)'  (appended to a mix)

    For >1 rejection the per-flight duration is dropped (the detail lines carry
    it); the reason is uniform so it's stated once."""
    n = len(rejected_flights)
    reason = DEFAULT_REJECTION_REASON
    if n == 1:
        dur = _compact_duration(_rf_duration(rejected_flights[0]))
        if standalone:
            return f"rejected — {reason} ({dur})"
        return f"1 rejected ({reason}, {dur})"
    if standalone:
        return f"rejected — {n} flights ({reason})"
    return f"{n} rejected ({reason})"


def summary_line(status: str, flights_accepted: int, rejected_flights: list) -> str:
    """One-line human outcome for a file — the consistent per-file phrasing the
    email and JSONL sidecar render instead of echoing Savvy's raw status. Two
    files with the same real outcome always produce the same line here."""
    outcome = classify_outcome(status, flights_accepted, len(rejected_flights))
    if outcome == OUTCOME_ACCEPTED:
        return _flights_accepted_phrase(flights_accepted)
    if outcome == OUTCOME_MIXED:
        accepted = "1 accepted" if flights_accepted == 1 else f"{flights_accepted} accepted"
        return f"{accepted}, {_rejected_phrase(rejected_flights, standalone=False)}"
    if outcome == OUTCOME_REJECTED:
        if rejected_flights:
            return _rejected_phrase(rejected_flights, standalone=True)
        return "rejected by Savvy"
    if outcome == OUTCOME_DUPLICATE:
        return "already uploaded (duplicate)"
    if outcome == OUTCOME_TOO_SMALL:
        return "file too small — no usable data"
    if outcome == OUTCOME_TIMEOUT:
        return "still processing — will re-check next run"
    if outcome == OUTCOME_ERROR:
        return "upload error — see log"
    if outcome == OUTCOME_UNKNOWN:
        return "unrecognised status — see log"
    return "no flight recorded"


def build_summary_dict(
    results: list[UploadResult],
    n_skipped: int = 0,
    n_verified: int = 0,
    n_pending: int = 0,
    *,
    now: datetime | None = None,
) -> dict:
    """Build the canonical summary dict for a single upload run.

    Both the email renderer and the optional JSON sidecar are projections
    of this dict — there is one source of truth for what an upload run
    "looks like" so the two outputs can't drift. Derived stats (n_success,
    totals) are intentionally NOT pre-computed here; renderers compute
    them from `files` so the persisted shape stays minimal.

    ``now`` is keyword-only and exists for testability; production callers
    leave it as ``None`` to capture the current local time.
    """
    when = now if now is not None else datetime.now().astimezone()
    return {
        "schema_version": SUMMARY_SCHEMA_VERSION,
        "run_at": when.isoformat(timespec="seconds"),
        "n_skipped": n_skipped,
        "n_verified": n_verified,
        "n_pending": n_pending,
        "files": [
            {
                "filename": r.filename,
                "status": r.status,
                "outcome": classify_outcome(
                    r.status, r.flights_accepted, len(r.rejected_flights)
                ),
                "summary": summary_line(
                    r.status, r.flights_accepted, r.rejected_flights
                ),
                "flights_accepted": r.flights_accepted,
                "on_savvy": r.on_flights_page,
                "rejected_flights": [
                    {
                        "date": rf.date,
                        "departure": rf.departure,
                        "destination": rf.destination,
                        "duration": rf.duration,
                        "reason": DEFAULT_REJECTION_REASON,
                    }
                    for rf in r.rejected_flights
                ],
            }
            for r in results
        ],
    }


def compose_email_from_summary(summary: dict) -> tuple[str, str]:
    """Render an email subject + plain-text body from the summary dict."""
    files = summary["files"]
    n_skipped = summary["n_skipped"]
    n_verified = summary["n_verified"]
    n_still_pending = summary["n_pending"]

    now = datetime.fromisoformat(summary["run_at"]).strftime("%Y-%m-%d %H:%M")

    # Count by the machine-readable `outcome`, not by sniffing the raw status
    # string — so a file accepted via the GraphQL re-count (status not
    # "Success") still tallies as accepted. Older sidecars without `outcome`
    # fall back to a re-classification from the fields they do carry.
    def _outcome(f: dict) -> str:
        return f.get("outcome") or classify_outcome(
            f["status"], f["flights_accepted"], len(f["rejected_flights"])
        )

    n_accepted = sum(1 for f in files if _outcome(f) in (OUTCOME_ACCEPTED, OUTCOME_MIXED))
    n_rejected_files = sum(1 for f in files if _outcome(f) == OUTCOME_REJECTED)
    n_dup = sum(1 for f in files if _outcome(f) == OUTCOME_DUPLICATE)
    n_err = sum(1 for f in files if _outcome(f) == OUTCOME_ERROR)
    n_timeout = sum(1 for f in files if _outcome(f) == OUTCOME_TIMEOUT)
    n_other = (
        len(files) - n_accepted - n_rejected_files - n_dup - n_err - n_timeout
    )
    total_flights = sum(f["flights_accepted"] for f in files)
    total_rejected = sum(len(f["rejected_flights"]) for f in files)

    subject = f"Savvy Upload Report - {now} ({len(files)} files)"

    lines = [
        "Savvy Aviation Upload Report",
        f"Run at: {now}",
        f"Files processed: {len(files)}",
        f"  Files accepted:   {n_accepted}",
        f"  Files rejected:   {n_rejected_files}",
        f"  Files duplicated: {n_dup}",
        f"  Files errored:    {n_err}",
    ]
    if total_flights:
        lines.append(f"  Flights accepted: {total_flights}")
    if total_rejected:
        lines.append(f"  Flights rejected: {total_rejected}")
    if n_timeout:
        lines.append(f"  Timed out:        {n_timeout}")
    if n_other:
        lines.append(f"  Other:            {n_other}")
    if n_verified:
        lines.append(f"  Verified from previous run: {n_verified}")
    if n_skipped:
        lines.append(f"  Skipped (already uploaded): {n_skipped}")
    if n_still_pending:
        lines.append(f"  Still pending verification: {n_still_pending}")

    # Loud banner if any file reported "success" by the upload page but is
    # not actually visible in Savvy's flights list. That's the silent-loss
    # bug we got bitten by; surface it so we never miss again.
    unverified = [f for f in files if not f["on_savvy"]]
    if unverified:
        lines += [
            "",
            "*" * 70,
            f"WARNING: {len(unverified)} uploaded file(s) NOT on Savvy.",
            f"They've been kept in CSV_DIR for retry on the next run:",
        ]
        for f in unverified:
            lines.append(f"  - {f['filename']}  (status was: {f['status']!r})")
        lines.append("*" * 70)

    lines += [
        "",
        "File Details:",
        "-" * 70,
    ]
    for f in files:
        summary_text = f.get("summary") or summary_line(
            f["status"], f["flights_accepted"], f["rejected_flights"]
        )
        savvy_note = "" if f["on_savvy"] else "  !! NOT on Savvy"
        lines.append(f"  {f['filename']}")
        lines.append(f"    {summary_text}{savvy_note}")

        for rf in f["rejected_flights"]:
            reason = rf.get("reason", DEFAULT_REJECTION_REASON)
            lines.append(
                f"      {rf['date']}  {rf['departure']} -> {rf['destination']}"
                f"  ({reason}, {_compact_duration(rf['duration'])})"
            )
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


def append_summary_jsonl(summary: dict, summary_dir: str) -> None:
    """Append the summary as one JSON line to {summary_dir}/savvy-{YYYY-MM-DD}.jsonl.

    Creates the directory if missing. POSIX append is atomic for small
    writes, so plain "a" mode is sufficient — no temp-file dance needed.
    """
    dir_path = Path(summary_dir)
    dir_path.mkdir(parents=True, exist_ok=True)
    date_str = datetime.now().date().isoformat()
    out_path = dir_path / f"savvy-{date_str}.jsonl"
    with out_path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(summary) + "\n")
    log.info(f"Wrote summary to {out_path}")


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


def compose_failure_email(error: Exception, tb: str) -> tuple[str, str]:
    """Subject + body for a fatal-run-failure notification.

    Called from run()'s fatal handler. A crash skips the normal summary
    output entirely (whether that's an email or a JSON sidecar), so this is
    the only proactive signal an operator gets. The subject carries a
    one-line error summary; the full traceback goes in the body. Delivery,
    retries and the on-disk queue are all handled by send_email.
    """
    msg = str(error).strip()
    # The subject takes only the first line — Playwright errors, for one,
    # append a multi-line "Call log:" that would bloat it.
    first_line = msg.splitlines()[0] if msg else error.__class__.__name__
    subject = f"Savvy upload FAILED: {first_line}"
    if len(subject) > 140:
        subject = subject[:137] + "..."
    body = (
        "A Savvy upload run crashed before completing.\n\n"
        f"Error: {error.__class__.__name__}: {msg or '(no message)'}\n\n"
        f"Traceback:\n{tb}\n"
        "The file watcher keeps running and will retry on the next new file; "
        "any un-watermarked files are re-attempted automatically on the next run.\n"
    )
    return subject, body


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

            # After login the site redirects back to the originally-requested
            # upload URL. Let that in-flight redirect settle before deciding
            # whether we still need to navigate — firing our own goto() into
            # the redirect aborts with "interrupted by another navigation"
            # (an intermittent production crash). Best-effort: if it doesn't
            # land us there, the explicit nav below still gets us on the page.
            try:
                page.wait_for_url(
                    lambda u: cfg.upload_url in u, timeout=LOGIN_TIMEOUT
                )
            except Exception:
                pass

            # Make sure we're on the upload page after login. _goto_upload_page
            # tolerates a late redirect that races our goto().
            if cfg.upload_url not in page.url:
                _goto_upload_page(page, cfg.upload_url)
                time.sleep(2)

            # --- Snapshot baseline rejected flights already on the page ---
            # Taken BEFORE verifying pending files so a late-surfacing
            # rejection for one of them can be attributed (diffed against
            # this baseline) rather than swallowed into it.
            page.evaluate("window.scrollTo(0, 0)")
            time.sleep(1)
            seen_rejected: set[RejectedFlight] = set(scrape_rejected_flights(page))
            if seen_rejected:
                log.info(f"Baseline: {len(seen_rejected)} pre-existing rejected flight(s) on page.")

            # --- Verify pending files from previous runs ---
            if pending_filenames:
                log.info(f"Verifying {len(pending_filenames)} file(s) from previous run(s)...")
                verified, still_pending = verify_pending_on_page(
                    page, pending_filenames, seen_rejected
                )
                verified_results.extend(verified)
                pending_filenames = still_pending

            # --- Upload each file ---
            for i, csv_path in enumerate(csv_files, 1):
                filename = Path(csv_path).name
                result = UploadResult(filename=filename)
                log.info(f"[{i}/{len(csv_files)}] Uploading: {filename}")

                try:
                    upload_single_file(page, csv_path)
                    log.info("File selected, waiting for upload to process...")
                    time.sleep(5)

                    status = poll_upload_status(page, filename, seen_rejected)
                    result.status = status
                    log.info(f"[{i}/{len(csv_files)}] {filename} -> {status}")

                    # Both a successful upload and a panel-detected rejection
                    # need the right-side Recent Flights panel scraped to
                    # attribute this file's NEW rejected flights. (A "Success"
                    # file can still have a rejected leg; a REJECTED_STATUS
                    # file is one whose left widget never went terminal but
                    # whose flight Savvy rejected — e.g. too short.)
                    if "Success" in status or status == REJECTED_STATUS:
                        # Scroll back up to see the Recent Flights panel
                        page.evaluate("window.scrollTo(0, 0)")
                        time.sleep(2)

                        # Parse flight count from status like "Success (3 flights)"
                        flights_match = re.search(r"(\d+) flights?", status)
                        if flights_match:
                            result.flights_accepted = int(flights_match.group(1))

                        # Attribute only rejected rows NEW since the last file.
                        new_rejected, seen_rejected = _attribute_new_rejected(
                            page, seen_rejected
                        )
                        if new_rejected:
                            result.rejected_flights = new_rejected
                            for rf in new_rejected:
                                log.info(
                                    f"    Rejected flight: {rf.date} "
                                    f"{rf.departure} -> {rf.destination} "
                                    f"({rf.duration})"
                                )

                except Exception as e:
                    result.status = f"error: {e}"
                    log.error(f"[{i}/{len(csv_files)}] {filename} failed: {e}")
                    try:
                        page.screenshot(
                            path=str(DEBUG_DIR / f"debug_error_{i}.png")
                        )
                    except Exception:
                        pass

                results.append(result)

                # Reload upload page for next file
                if i < len(csv_files):
                    try:
                        _goto_upload_page(page, cfg.upload_url)
                        time.sleep(2)
                    except Exception as e:
                        log.error(f"Failed to reload upload page: {e}")
                        log.info("Stopping batch early — remaining files will be retried next run.")
                        break

            # --- Retry pass for timed-out files from this batch ---
            timed_out = [r.filename for r in results if r.status == "timeout"]
            if timed_out:
                _goto_upload_page(page, cfg.upload_url)
                time.sleep(2)
                retry_resolved, retry_still_pending = retry_timed_out(
                    page, timed_out, seen_rejected
                )

                # Update original results for resolved files
                resolved_map = {r.filename: r for r in retry_resolved}
                for r in results:
                    if r.filename in resolved_map:
                        resolved = resolved_map[r.filename]
                        r.status = resolved.status
                        r.flights_accepted = resolved.flights_accepted
                        r.rejected_flights = resolved.rejected_flights

                # Any files still not resolved go to persistent pending list
                pending_filenames.extend(retry_still_pending)

            # --- Settle, then verify uploads landed on Savvy ---
            # Savvy parses uploaded CSVs in async phases. Phase 3 — real
            # flight extraction and re-attribution across sibling files —
            # can land seconds-to-minutes after the upload page first
            # reports "Success". Sleep before the GraphQL query so the
            # authoritative count we read isn't a Phase-2 snapshot.
            # Skip the wait when no new uploads happened this run (purely
            # pending-verification path): those files were uploaded on a
            # previous run and have already settled.
            if results and cfg.graphql_settle_seconds > 0:
                log.info(
                    f"Sleeping {cfg.graphql_settle_seconds}s before GraphQL "
                    f"flight-count query (lets Savvy finish async parsing)..."
                )
                time.sleep(cfg.graphql_settle_seconds)

            # Dual-signal verification:
            #   1. GraphQL AircraftEngineDataFiles — Savvy's authoritative
            #      list of files it has (regardless of whether they produced
            #      a Flight). Files reported as "Success (0 flights)" still
            #      appear here. The joined flights{importFile{name}} gives
            #      us the AUTHORITATIVE flight count per file, bypassing
            #      the racy upload-page status string entirely.
            #   2. Upload-page status string — if Savvy explicitly returned
            #      "Success*" or "File Duplicated", we trust that too. This
            #      handles the case where GraphQL is briefly out of sync
            #      with what was just uploaded (Savvy might take a few
            #      seconds to surface the new file).
            # A file is verified if EITHER signal says yes. Both signals
            # have to fail for us to mark it unverified.
            all_to_verify = results + verified_results
            log.info("Querying Savvy GraphQL for authoritative file list...")
            savvy_counts = fetch_savvy_file_flight_counts(context.request, cfg.aircraft_id)
            graphql_ok = savvy_counts is not None
            flights_body = ""
            if not graphql_ok:
                log.warning("GraphQL verification unavailable; falling back to flights-page text + status check.")
                page.goto(cfg.flights_url, wait_until="domcontentloaded")
                time.sleep(3)
                page.screenshot(path=str(DEBUG_DIR / "debug_flights_page.png"))
                flights_body = page.inner_text("body")
            else:
                log.info(f"Savvy reports {len(savvy_counts)} file(s) for this aircraft.")
            for r in all_to_verify:
                in_savvy = graphql_ok and (r.filename in savvy_counts)
                status_ok = status_looks_accepted(r.status)
                text_ok = (not graphql_ok) and (r.filename in flights_body)
                if in_savvy or status_ok or text_ok:
                    r.on_flights_page = True
                    reason = ("graphql" if in_savvy else
                              "status='%s'" % r.status if status_ok else "text-scrape")
                    log.info(f"  Verified ({reason}): {r.filename}")
                else:
                    log.warning(
                        f"  NOT on Savvy: {r.filename} "
                        f"(status reported by upload page: {r.status!r}). "
                        f"Will keep local copy for retry."
                    )

                # GraphQL count overrides the page-derived flights_accepted.
                # Why authoritative: the page status is one snapshot taken
                # mid-parse; the GraphQL join after settle reflects Savvy's
                # post-Phase-3 attribution, which is what actually populates
                # the user's flight log. When GraphQL is unavailable we
                # leave the page count alone — it's the best we have.
                if in_savvy:
                    graphql_count = savvy_counts[r.filename]
                    if r.flights_accepted != graphql_count:
                        log.info(
                            f"  flights_accepted override: {r.filename} "
                            f"page={r.flights_accepted} -> graphql={graphql_count}"
                        )
                    r.flights_accepted = graphql_count
                    if graphql_count == 0 and not _is_terminal_no_flight_status(r.status):
                        # GraphQL says 0 but the page status doesn't imply
                        # "no flight expected" — Phase 3 may still be in
                        # flight. Scope cut: original spec was to defer to
                        # next run for a re-check; left as a follow-up. For
                        # now, surface the ambiguity so a long-running
                        # pattern is visible in the email/log.
                        log.warning(
                            f"  {r.filename}: GraphQL reports 0 flights but "
                            f"status {r.status!r} is not a terminal-no-flight "
                            f"state. Possible late-parsing race; consider "
                            f"raising GRAPHQL_SETTLE_SECONDS."
                        )

            page.screenshot(path=str(DEBUG_DIR / "debug_final.png"))

        except Exception as e:
            log.error(f"Fatal error: {e}")
            try:
                page.screenshot(path=str(DEBUG_DIR / "debug_error.png"))
            except Exception:
                pass
            # A crash skips the normal summary output below, so without this a
            # fatal run is silent outside the logs. Send a proactive alert —
            # best-effort: an email problem must not mask the original error.
            try:
                subject, body = compose_failure_email(e, traceback.format_exc())
                send_email(subject, body, cfg.email)
            except Exception as mail_err:
                log.error(f"Could not send failure email: {mail_err}")
            raise
        finally:
            browser.close()

    # --- Persist any still-pending files ---
    save_pending_uploads(pending_filenames)

    # --- Update watermark ---
    # Advance only across the longest CONTIGUOUS verified prefix of this
    # run's results. Stopping at the first unverified file means subsequent
    # runs will retry it (and any later files re-uploaded by then will be
    # flagged by Savvy as duplicates, which is fine).
    #
    # Pre-fix behaviour was: advance to the newest attempted file regardless
    # of verification. That silently dropped files Savvy never received
    # (e.g. log_20260416_101401_KLMO.csv on 2026-04-16: upload page reported
    # the bogus '_KLMO.csv' status, watermark advanced past it, file got
    # cleaned up locally, never seen again).
    #
    # The watermark also only ever moves FORWARD. --reupload runs (especially
    # on a single old file passed as a path arg) would otherwise rewrite the
    # watermark backward to that old filename, making the next normal run
    # re-attempt every file uploaded since.
    sorted_results = sorted(results + verified_results, key=lambda r: r.filename)
    new_watermark = None
    for r in sorted_results:
        if r.on_flights_page:
            new_watermark = r.filename
        else:
            break
    if new_watermark:
        current_watermark = load_last_uploaded()
        if new_watermark > current_watermark:
            save_last_uploaded(new_watermark)
            log.info(f"Watermark advanced to last contiguous-verified: {new_watermark}")
        else:
            log.info(
                f"Watermark unchanged ({current_watermark}); "
                f"last contiguous-verified ({new_watermark}) is not newer."
            )

    unverified_attempted = [r.filename for r in results if not r.on_flights_page]
    if unverified_attempted:
        log.warning(
            f"{len(unverified_attempted)} file(s) NOT verified on Savvy "
            f"and will be retried next run: {unverified_attempted}"
        )

    # --- Summary (email or JSON sidecar) ---
    # No file movement: uploaded CSVs stay in CSV_DIR (permanent home).
    # LAST_UPLOADED watermark is the only state tracking "what's done"; any
    # downstream consumer reading the same dir does so safely. Files that timed
    # out / didn't verify are tracked by pending_filenames for retry next run.
    #
    # build_summary_dict produces the canonical shape; email body and JSON
    # sidecar are both projections of it. SUMMARY_DIR toggles which output
    # runs (set = sidecar only, unset = email only).
    all_results = verified_results + results
    summary = build_summary_dict(
        all_results,
        n_skipped=n_skipped,
        n_verified=len(verified_results),
        n_pending=len(pending_filenames),
    )
    if cfg.summary_dir:
        append_summary_jsonl(summary, cfg.summary_dir)
    else:
        subject, body = compose_email_from_summary(summary)
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
