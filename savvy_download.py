"""Download historical CSV files from SavvyAviation.com into a local archive.

Reuses the login flow from savvy_upload.py. Discovers files via the
FlightsByAircraft GraphQL query, fetches a presigned S3 URL for each via
EdfDownloadUrl, then saves the CSV to the archive directory (skipping any
already present).

Usage
-----
    python3 savvy_download.py              # download everything missing
    python3 savvy_download.py --limit 20   # only the 20 most recent
    python3 savvy_download.py --dry-run    # list what would be downloaded
    python3 savvy_download.py --since 2026-03  # filter by upload date prefix

Reads SAVVY_EMAIL / SAVVY_PASSWORD / SAVVY_AIRCRAFT_ID / ARCHIVE_DIR from .env
(same file as savvy_upload.py).
"""
import argparse
import json
import logging
import sys
import time
from pathlib import Path

from playwright.sync_api import sync_playwright

from savvy_upload import (
    SAVVY_BASE,
    DEFAULT_USER_AGENT,
    LOGIN_TIMEOUT,
    do_login,
    load_config,
)


GRAPHQL_URL = f"{SAVVY_BASE}/graphql"
GRAPHQL_STRAWBERRY_URL = f"{SAVVY_BASE}/graphql-strawberry"

FILES_QUERY = """query AircraftEngineDataFiles($id: Int) {
  me {
    id
    aircraft(id: $id) {
      id
      engineDataFiles { id name uploadDate __typename }
      flights(hideShortFlights: false) {
        id departureId destinationId duration
        importFile { id name __typename }
        __typename
      }
      __typename
    }
    __typename
  }
}"""

DOWNLOAD_URL_QUERY = """query EdfDownloadUrl($fileId: Int) {
  edfDownloadUrl(fileId: $fileId)
}"""

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("savvy_download")


def graphql_request(api_ctx, url: str, operation: str, query: str, variables: dict) -> dict:
    """Make an authenticated GraphQL POST via Playwright's request context."""
    payload = {"operationName": operation, "variables": variables, "query": query}
    resp = api_ctx.post(url, data=json.dumps(payload),
                        headers={"content-type": "application/json"})
    if resp.status != 200:
        raise RuntimeError(f"GraphQL {operation} HTTP {resp.status}: {resp.text()[:300]}")
    body = resp.json()
    if "errors" in body:
        raise RuntimeError(f"GraphQL {operation} errors: {body['errors']}")
    return body["data"]


def list_aircraft_files(api_ctx, aircraft_id: int) -> list[dict]:
    """Return list of {file_id, file_name, upload_date, departure, destination,
    has_flight}.

    Returns EVERY engine-data file Savvy knows about, not just the ones it
    successfully extracted a Flight from. This matters because Savvy reports
    'Success (0 flights)' for any file that doesn't contain a complete
    takeoff+landing cycle — e.g. partial uploads or pre-flight engine runs.
    Those files still exist in Savvy's storage but are absent from the
    legacy `flights` view, which is how we previously lost track of them
    (see commit history around 2026-05 for the silent-loss diagnosis).
    """
    data = graphql_request(
        api_ctx, GRAPHQL_URL, "AircraftEngineDataFiles", FILES_QUERY,
        {"id": aircraft_id},
    )
    aircraft_list = data["me"]["aircraft"]
    if not aircraft_list:
        return []
    edfs = aircraft_list[0].get("engineDataFiles", []) or []
    # Build {name: flight_dict} so we can annotate which files made flights
    flight_meta = {}
    for f in aircraft_list[0].get("flights", []) or []:
        imp = f.get("importFile") or {}
        n = imp.get("name")
        if n:
            flight_meta[n] = {
                "departure": f.get("departureId") or "",
                "destination": f.get("destinationId") or "",
                "duration": f.get("duration"),
            }
    out = []
    for e in edfs:
        if not e.get("name"):
            continue
        meta = flight_meta.get(e["name"], {})
        out.append({
            "file_id": int(e["id"]),
            "file_name": e["name"],
            "upload_date": e.get("uploadDate", ""),
            "departure": meta.get("departure", ""),
            "destination": meta.get("destination", ""),
            "has_flight": e["name"] in flight_meta,
        })
    return out


def get_download_url(api_ctx, file_id: int) -> str:
    """Resolve the presigned S3 download URL for a given importFile id."""
    data = graphql_request(
        api_ctx, GRAPHQL_STRAWBERRY_URL, "EdfDownloadUrl", DOWNLOAD_URL_QUERY,
        {"fileId": file_id},
    )
    url = data.get("edfDownloadUrl")
    if not url:
        raise RuntimeError(f"No download URL returned for fileId={file_id}")
    return url


def download_csv(api_ctx, url: str, dest: Path) -> int:
    """Stream a CSV to disk. Returns bytes written."""
    resp = api_ctx.get(url)
    if resp.status != 200:
        raise RuntimeError(f"S3 GET HTTP {resp.status}: {resp.text()[:200]}")
    body = resp.body()
    dest.write_bytes(body)
    return len(body)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--archive-dir", default=None,
                        help="Override ARCHIVE_DIR for this run")
    parser.add_argument("--limit", type=int, default=0,
                        help="Only download N most recent (0 = all)")
    parser.add_argument("--since", default="",
                        help="Filter by file name prefix (e.g. 'log_2026' or 'log_202603')")
    parser.add_argument("--dry-run", action="store_true",
                        help="List what would be downloaded, do nothing")
    parser.add_argument("--force", action="store_true",
                        help="Re-download even if file already exists")
    args = parser.parse_args()

    cfg = load_config()
    if not cfg.aircraft_id:
        log.error("SAVVY_AIRCRAFT_ID not set")
        sys.exit(1)

    archive_dir = Path(args.archive_dir or cfg.archive_dir)
    if not archive_dir:
        log.error("No archive directory. Set ARCHIVE_DIR in .env or use --archive-dir.")
        sys.exit(1)
    archive_dir.mkdir(parents=True, exist_ok=True)
    log.info(f"Archive directory: {archive_dir}")

    aircraft_id = int(cfg.aircraft_id)

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        ctx = browser.new_context(user_agent=cfg.user_agent, accept_downloads=True)
        page = ctx.new_page()

        # Login (the flow is on the upload page)
        login_url = f"{SAVVY_BASE}/files/upload/{cfg.aircraft_id}"
        log.info(f"Navigating to {login_url}")
        page.goto(login_url, wait_until="domcontentloaded", timeout=LOGIN_TIMEOUT)
        time.sleep(2)
        do_login(page, cfg.email, cfg.password)
        time.sleep(3)

        # All subsequent GraphQL + S3 calls go through ctx.request — it
        # automatically includes our session cookies.
        api = ctx.request

        log.info("Listing flights for aircraft...")
        files = list_aircraft_files(api, aircraft_id)
        log.info(f"Found {len(files)} files on Savvy")

        # Filter
        if args.since:
            files = [f for f in files if f["file_name"].startswith(args.since)]
            log.info(f"After --since filter: {len(files)} files")

        # Sort newest first
        files.sort(key=lambda f: f["file_name"], reverse=True)
        if args.limit > 0:
            files = files[: args.limit]

        # Filter out those already on disk
        to_get = []
        existing = 0
        for f in files:
            dest = archive_dir / f["file_name"]
            if dest.exists() and not args.force:
                existing += 1
                continue
            to_get.append(f)

        log.info(f"To download: {len(to_get)}  (already present: {existing})")

        if args.dry_run:
            for f in to_get:
                tag = f"{f['departure']}->{f['destination']}" if f['has_flight'] else "[no-flight]"
                log.info(f"  WOULD download {f['file_name']}  ({tag})  fileId={f['file_id']}")
            return

        n_ok = 0
        n_fail = 0
        for i, f in enumerate(to_get, start=1):
            dest = archive_dir / f["file_name"]
            try:
                url = get_download_url(api, f["file_id"])
                size = download_csv(api, url, dest)
                tag = f"{f['departure']}->{f['destination']}" if f['has_flight'] else "[no-flight]"
                log.info(f"[{i}/{len(to_get)}] {f['file_name']}  ({tag})  {size:,} bytes")
                n_ok += 1
            except Exception as e:
                log.error(f"[{i}/{len(to_get)}] {f['file_name']}: {e}")
                n_fail += 1

        log.info(f"Done. Downloaded {n_ok}, failed {n_fail}, already-present {existing}.")
        browser.close()


if __name__ == "__main__":
    main()
