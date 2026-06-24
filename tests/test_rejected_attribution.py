#!/usr/bin/env python3
"""Tests for capturing Savvy flight rejections as a terminal upload outcome.

Runs with the stdlib only — no pytest dependency:

    python3 tests/test_rejected_attribution.py

Fails fast on the first mismatch with a diff-friendly print, exits 1.
Prints "All tests passed." and exits 0 on success.

Background: when Savvy rejects a flight as too short, the left "Recent
Uploads" status widget can get stuck mid-phase (e.g. "Uploading… (0%)")
and never reach a terminal phrase. The old poller looped its whole window
and returned the opaque "timeout" with no rejection captured, so the
summary dict (and its JSONL sidecar) couldn't tell a rejected-too-short
flight apart from a genuine processing hang.

These tests cover the fix: the right "Recent Flights" Rejected panel is a
second terminal signal. A NEW rejected row attributable to the file being
polled resolves it to REJECTED_STATUS with the rejected flight captured —
in the main poller, the retry pass, and the pending-verification pass.
"""
import json
import sys
from pathlib import Path

# Make savvy_upload importable when running this file directly.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import savvy_upload  # noqa: E402
from savvy_upload import (  # noqa: E402
    REJECTED_STATUS,
    RejectedFlight,
    UploadResult,
    _is_terminal_status_visible,
    build_summary_dict,
    compose_email_from_summary,
    extract_status,
    parse_rejected_flights,
    poll_upload_status,
    retry_timed_out,
    verify_pending_on_page,
)


# ---------------------------------------------------------------------------
# Test plumbing (same shape as tests/test_polling_stability.py)
# ---------------------------------------------------------------------------
def _fail(name: str, actual, expected) -> None:
    print(f"FAIL: {name}")
    if isinstance(actual, str):
        print("--- actual ---")
        print(actual)
        print("--- expected ---")
        print(expected)
    else:
        print("--- actual ---")
        print(json.dumps(actual, indent=2, default=str))
        print("--- expected ---")
        print(json.dumps(expected, indent=2, default=str))
    sys.exit(1)


def _assert_eq(name: str, actual, expected) -> None:
    if actual != expected:
        _fail(name, actual, expected)
    print(f"OK: {name}")


def _assert_true(name: str, cond: bool) -> None:
    if not cond:
        _fail(name, cond, True)
    print(f"OK: {name}")


class _StickyPage:
    """Minimal Playwright page stand-in that returns a fixed body text for
    every inner_text() call. Unlike the strict queue in
    test_polling_stability, this sticks on the same state forever — handy
    for both the resolve-immediately path and the loop-to-timeout path
    (the body never changes, so the poller exhausts its budget)."""

    def __init__(self, body_text: str) -> None:
        self.body_text = body_text
        self.reload_calls = 0

    def evaluate(self, _script: str) -> None:
        return None

    def inner_text(self, _selector: str) -> str:
        return self.body_text

    def reload(self, **_kwargs) -> None:
        self.reload_calls += 1

    def screenshot(self, **_kwargs) -> None:
        return None


def _run_with_no_sleep(fn, *args, **kwargs):
    """Invoke fn with savvy_upload.time.sleep stubbed to a no-op so the
    test runs in milliseconds instead of minutes."""
    real_sleep = savvy_upload.time.sleep
    savvy_upload.time.sleep = lambda _s: None
    try:
        return fn(*args, **kwargs)
    finally:
        savvy_upload.time.sleep = real_sleep


# ---------------------------------------------------------------------------
# Fixture builders — keep the inline page text readable
# ---------------------------------------------------------------------------
def _rejected_panel(rows: list[tuple[str, str, str, str]]) -> str:
    """Render the right-side Recent Flights panel text. ``rows`` are
    (date, departure, destination, duration). Columns are double-spaced,
    matching how Playwright inner_text renders the table cells."""
    lines = ["Recent Flights", "Accepted", "Rejected"]
    for date, dep, dest, dur in rows:
        lines.append(f"{date}  {dep}  {dest}  {dur}  Reattribute")
    return "\n".join(lines) + "\n"


def _page_body(filename: str, status_text: str, rejected_rows=()) -> str:
    """Full page body: a Recent Uploads row for ``filename`` showing
    ``status_text``, followed by the Recent Flights panel."""
    return (
        "Recent Uploads\n"
        f"{filename}\n"
        f"●  {status_text}\n\n"
        + _rejected_panel(list(rejected_rows))
    )


_FILE = "log_20200101_120000_KAAA.csv"
_REJECTED_ROW = ("2020-01-01", "KLMO", "KLMO", "0h 1m 19s")
_REJECTED_FLIGHT = RejectedFlight("2020-01-01", "KLMO", "KLMO", "0h 1m 19s")


# ---------------------------------------------------------------------------
# Pure parser
# ---------------------------------------------------------------------------
def test_parse_rejected_flights_reads_rows() -> None:
    body = _page_body(
        _FILE,
        "Uploading… (0%)",
        rejected_rows=[
            ("2020-01-01", "KLMO", "KLMO", "0h 1m 19s"),
            ("2019-04-06", "Unknown", "Unknown", "0h 0m 9s"),
        ],
    )
    flights = parse_rejected_flights(body)
    _assert_eq(
        "test_parse_rejected_flights_reads_rows",
        flights,
        [
            RejectedFlight("2020-01-01", "KLMO", "KLMO", "0h 1m 19s"),
            RejectedFlight("2019-04-06", "Unknown", "Unknown", "0h 0m 9s"),
        ],
    )


def test_parse_rejected_flights_empty_when_no_panel() -> None:
    body = "Recent Uploads\nlog_x.csv\n●  Processing...\n"
    _assert_eq(
        "test_parse_rejected_flights_empty_when_no_panel",
        parse_rejected_flights(body),
        [],
    )


def test_stuck_uploading_is_not_a_terminal_status() -> None:
    """Guard the premise of the bug: the stuck left-widget text must not be
    mistaken for a terminal phrase (else the poller would 'resolve' on junk
    instead of consulting the Rejected panel)."""
    _assert_true(
        "test_stuck_uploading_is_not_a_terminal_status",
        not _is_terminal_status_visible("Uploading… (0%)")
        and extract_status("Uploading… (0%)") == "unknown",
    )


# ---------------------------------------------------------------------------
# poll_upload_status — the main poller
# ---------------------------------------------------------------------------
def test_poll_returns_rejected_when_panel_shows_new_rejection() -> None:
    """The bug: left widget stuck on 'Uploading… (0%)', but Savvy has
    rejected the flight in the panel. With a baseline supplied, the poller
    must return REJECTED_STATUS instead of looping to 'timeout'."""
    page = _StickyPage(_page_body(_FILE, "Uploading… (0%)", [_REJECTED_ROW]))
    status = _run_with_no_sleep(
        poll_upload_status, page, _FILE, set()
    )
    _assert_eq(
        "test_poll_returns_rejected_when_panel_shows_new_rejection",
        status,
        REJECTED_STATUS,
    )


def test_poll_without_baseline_keeps_timeout_behaviour() -> None:
    """Existing callers (and the gate's own unit tests) pass no baseline —
    panel detection stays off, so a stuck widget still yields 'timeout'.
    Proves the new signal is strictly opt-in and can't change old behaviour."""
    page = _StickyPage(_page_body(_FILE, "Uploading… (0%)", [_REJECTED_ROW]))
    status = _run_with_no_sleep(poll_upload_status, page, _FILE)
    _assert_eq(
        "test_poll_without_baseline_keeps_timeout_behaviour",
        status,
        "timeout",
    )


def test_poll_ignores_preexisting_rejection_in_baseline() -> None:
    """A rejected row already present at baseline belongs to an earlier file
    and must NOT be re-attributed: only rows new since the baseline count."""
    page = _StickyPage(_page_body(_FILE, "Uploading… (0%)", [_REJECTED_ROW]))
    baseline = {_REJECTED_FLIGHT}
    status = _run_with_no_sleep(poll_upload_status, page, _FILE, baseline)
    _assert_eq(
        "test_poll_ignores_preexisting_rejection_in_baseline",
        status,
        "timeout",
    )


def test_poll_success_wins_over_panel_signal() -> None:
    """A normal accepted file whose left widget reads 'Success' must resolve
    to Success even if a rejected row is also on the page (multi-leg file):
    the panel signal only fires when the left widget is non-terminal."""
    body = _page_body(_FILE, "✓  Success (Show 1 Flights)", [_REJECTED_ROW])
    page = _StickyPage(body)
    status = _run_with_no_sleep(poll_upload_status, page, _FILE, set())
    _assert_eq(
        "test_poll_success_wins_over_panel_signal",
        status,
        "Success (1 flight)",
    )


# ---------------------------------------------------------------------------
# retry_timed_out — late rejection that surfaces during the retry pass
# ---------------------------------------------------------------------------
def test_retry_resolves_late_rejection() -> None:
    page = _StickyPage(_page_body(_FILE, "Uploading… (0%)", [_REJECTED_ROW]))
    baseline: set = set()
    resolved, still_pending = _run_with_no_sleep(
        retry_timed_out, page, [_FILE], baseline
    )
    _assert_eq(
        "test_retry_resolves_late_rejection.still_pending",
        still_pending,
        [],
    )
    _assert_eq(
        "test_retry_resolves_late_rejection.status",
        resolved[0].status,
        f"{REJECTED_STATUS} (after retry)",
    )
    _assert_eq(
        "test_retry_resolves_late_rejection.rejected_flights",
        resolved[0].rejected_flights,
        [_REJECTED_FLIGHT],
    )
    _assert_eq(
        "test_retry_resolves_late_rejection.flights_accepted",
        resolved[0].flights_accepted,
        0,
    )


def test_retry_without_baseline_stays_pending() -> None:
    """No baseline -> no panel detection -> a genuinely stuck file stays
    pending (reserved for the next run), unchanged from old behaviour."""
    page = _StickyPage(_page_body(_FILE, "Uploading… (0%)", [_REJECTED_ROW]))
    resolved, still_pending = _run_with_no_sleep(
        retry_timed_out, page, [_FILE]
    )
    _assert_eq("test_retry_without_baseline_stays_pending.resolved", resolved, [])
    _assert_eq(
        "test_retry_without_baseline_stays_pending.still_pending",
        still_pending,
        [_FILE],
    )


# ---------------------------------------------------------------------------
# verify_pending_on_page — late rejection for a prior-run pending file
# ---------------------------------------------------------------------------
def test_verify_pending_resolves_rejection() -> None:
    page = _StickyPage(_page_body(_FILE, "Uploading… (0%)", [_REJECTED_ROW]))
    baseline: set = set()
    resolved, still_pending = _run_with_no_sleep(
        verify_pending_on_page, page, [_FILE], baseline
    )
    _assert_eq(
        "test_verify_pending_resolves_rejection.still_pending",
        still_pending,
        [],
    )
    _assert_eq(
        "test_verify_pending_resolves_rejection.status",
        resolved[0].status,
        f"{REJECTED_STATUS} (verified after retry)",
    )
    _assert_eq(
        "test_verify_pending_resolves_rejection.rejected_flights",
        resolved[0].rejected_flights,
        [_REJECTED_FLIGHT],
    )


# ---------------------------------------------------------------------------
# Downstream shape — the whole point: a rejection is distinguishable from a hang
# ---------------------------------------------------------------------------
def test_summary_distinguishes_rejection_from_timeout() -> None:
    """A rejected-too-short file lands in the summary (and its JSONL sidecar)
    with flights_accepted=0 AND rejected_flights populated AND a status that
    is not the opaque 'timeout' — so a downstream consumer can render
    'No accepted flights (1 rejected)' instead of guessing at a hang."""
    rejected_result = UploadResult(
        filename=_FILE,
        status=REJECTED_STATUS,
        flights_accepted=0,
        rejected_flights=[_REJECTED_FLIGHT],
        on_flights_page=True,
    )
    hung_result = UploadResult(
        filename="log_20200101_130000_KBBB.csv",
        status="timeout",
        flights_accepted=0,
        rejected_flights=[],
        on_flights_page=True,
    )
    summary = build_summary_dict([rejected_result, hung_result])

    rejected_entry = summary["files"][0]
    _assert_eq(
        "test_summary_distinguishes_rejection_from_timeout.rejected_entry",
        {
            "status": rejected_entry["status"],
            "flights_accepted": rejected_entry["flights_accepted"],
            "n_rejected": len(rejected_entry["rejected_flights"]),
        },
        {"status": "Rejected", "flights_accepted": 0, "n_rejected": 1},
    )
    # The genuine hang stays "timeout" with no rejected flights — the two
    # outcomes remain telling-apart-able.
    hung_entry = summary["files"][1]
    _assert_eq(
        "test_summary_distinguishes_rejection_from_timeout.hung_entry",
        {
            "status": hung_entry["status"],
            "n_rejected": len(hung_entry["rejected_flights"]),
        },
        {"status": "timeout", "n_rejected": 0},
    )

    # And the email projection renders the clean per-file outcome line plus
    # the rejected flight's detail line — not the raw "Rejected" status.
    _subject, body = compose_email_from_summary(summary)
    _assert_true(
        "test_summary_distinguishes_rejection_from_timeout.email_shows_rejection",
        "Flights rejected: 1" in body
        and "rejected — too short (1m 19s)" in body
        and "KLMO -> KLMO  (too short, 1m 19s)" in body,
    )


def main() -> None:
    test_parse_rejected_flights_reads_rows()
    test_parse_rejected_flights_empty_when_no_panel()
    test_stuck_uploading_is_not_a_terminal_status()
    test_poll_returns_rejected_when_panel_shows_new_rejection()
    test_poll_without_baseline_keeps_timeout_behaviour()
    test_poll_ignores_preexisting_rejection_in_baseline()
    test_poll_success_wins_over_panel_signal()
    test_retry_resolves_late_rejection()
    test_retry_without_baseline_stays_pending()
    test_verify_pending_resolves_rejection()
    test_summary_distinguishes_rejection_from_timeout()
    print("\nAll tests passed.")


if __name__ == "__main__":
    main()
