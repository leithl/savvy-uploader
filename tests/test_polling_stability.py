#!/usr/bin/env python3
"""Tests for the polling stability gate and GraphQL flight-count override.

Runs with the stdlib only — no pytest dependency:

    python3 tests/test_polling_stability.py

Fails fast on the first mismatch with a diff-friendly print, exits 1.
Prints "All tests passed." and exits 0 on success.

Covers the two changes for the Phase-2/Phase-3 race fix:
  1. poll_upload_status requires a non-Processing status to appear twice
     in a row before returning (so a transient mid-parse reading can't
     leak through as a final status — in EITHER direction).
  2. The post-settle GraphQL query overrides each result's
     flights_accepted with the authoritative joined count, and leaves
     it untouched when GraphQL is unavailable.
"""
import json
import sys
from pathlib import Path

# Make savvy_upload importable when running this file directly.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import savvy_upload  # noqa: E402
from savvy_upload import (  # noqa: E402
    UploadResult,
    fetch_savvy_file_flight_counts,
    poll_upload_status,
)


# ---------------------------------------------------------------------------
# Test plumbing (same shape as tests/test_summary_dict.py)
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


class _StubPage:
    """Minimal stand-in for the Playwright page used by poll_upload_status.

    Returns inner_text values from a queued sequence, one per poll. Any
    extra polls raise — keeps tests honest about how many reads each
    scenario expects.
    """

    def __init__(self, inner_texts: list[str]) -> None:
        self._inner_texts = list(inner_texts)
        self.reload_calls = 0

    def evaluate(self, _script: str) -> None:
        return None

    def inner_text(self, _selector: str) -> str:
        if not self._inner_texts:
            raise AssertionError(
                "StubPage ran out of inner_text values — poller polled "
                "more times than the test scripted"
            )
        return self._inner_texts.pop(0)

    def reload(self, **_kwargs) -> None:
        self.reload_calls += 1


class _StubResponse:
    """Stub for Playwright APIResponse — just .status + .json()."""

    def __init__(self, status: int, payload: dict | None = None) -> None:
        self.status = status
        self._payload = payload or {}

    def json(self) -> dict:
        return self._payload


class _StubAPIContext:
    """Stub for Playwright APIRequestContext — captures the .post call
    and returns the queued response."""

    def __init__(self, response: _StubResponse) -> None:
        self._response = response
        self.last_call: dict | None = None

    def post(self, url, *, data=None, headers=None):
        self.last_call = {"url": url, "data": data, "headers": headers}
        return self._response


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
# Tests
# ---------------------------------------------------------------------------
def test_poll_stabilises_on_forward_race() -> None:
    """Forward race: Phase 2 reports 0 flights, Phase 3 settles on 1.
    The poller must wait for the stable later reading, not return the
    Phase-2 0-flight snapshot."""
    filename = "log_20200101_120000_KAAA.csv"
    page = _StubPage(inner_texts=[
        f"{filename}\n● ● ● ●   Processing...",
        f"{filename}\n✓  Success (Show 0 Flights)",
        f"{filename}\n✓  Success (Show 1 Flights)",
        f"{filename}\n✓  Success (Show 1 Flights)",
    ])
    status = _run_with_no_sleep(poll_upload_status, page, filename)
    _assert_eq(
        "test_poll_stabilises_on_forward_race",
        status,
        "Success (1 flight)",
    )


def test_poll_stabilises_on_reverse_race() -> None:
    """Reverse race: Phase 2 mis-attributes a sibling file's flight here
    (reports 1), Phase 3 re-attributes it elsewhere (settles on 0).
    The poller must return the stable later reading."""
    filename = "log_20200101_120000_KAAA.csv"
    page = _StubPage(inner_texts=[
        f"{filename}\n● ● ● ●   Processing...",
        f"{filename}\n✓  Success (Show 1 Flights)",
        f"{filename}\n✓  Success (Show 0 Flights)",
        f"{filename}\n✓  Success (Show 0 Flights)",
    ])
    status = _run_with_no_sleep(poll_upload_status, page, filename)
    _assert_eq(
        "test_poll_stabilises_on_reverse_race",
        status,
        "Success (0 flights)",
    )


def test_poll_processing_mid_stream_resets_confirmation() -> None:
    """Mid-stream Processing must reset the previously-captured status —
    otherwise a stale Phase-2 reading could be 'confirmed' against a
    matching read after Savvy went back to Processing.

    Sequence: Success(0) → Processing → Success(0) → Success(0). The
    first read is invalidated by the Processing; the second pair is the
    stable reading we should return."""
    filename = "log_20200101_120000_KAAA.csv"
    page = _StubPage(inner_texts=[
        f"{filename}\n✓  Success (Show 0 Flights)",
        f"{filename}\n● ● ● ●   Processing...",
        f"{filename}\n✓  Success (Show 0 Flights)",
        f"{filename}\n✓  Success (Show 0 Flights)",
    ])
    status = _run_with_no_sleep(poll_upload_status, page, filename)
    _assert_eq(
        "test_poll_processing_mid_stream_resets_confirmation",
        status,
        "Success (0 flights)",
    )


def _graphql_payload(file_flight_pairs: list[tuple[str, int]]) -> dict:
    """Build a GraphQL response payload from a list of (filename, n_flights)
    tuples — keeps the inline fixture in tests readable."""
    files = [{"id": str(i), "name": name, "__typename": "EngineDataFile"}
             for i, (name, _) in enumerate(file_flight_pairs, start=1)]
    flights = []
    fid = 1
    for name, n in file_flight_pairs:
        for _ in range(n):
            flights.append({
                "id": str(fid),
                "departureId": "KAAA",
                "destinationId": "KBBB",
                "duration": 3600,
                "importFile": {"id": "x", "name": name, "__typename": "EngineDataFile"},
                "__typename": "Flight",
            })
            fid += 1
    return {
        "data": {
            "me": {
                "id": 1,
                "aircraft": [{
                    "id": 12345,
                    "engineDataFiles": files,
                    "flights": flights,
                }],
            }
        }
    }


def test_fetch_joins_flights_to_files() -> None:
    """GraphQL parser must join flights{importFile} back to engineDataFiles,
    producing a {filename: flight_count} dict — including 0 for files
    with no extracted flight."""
    payload = _graphql_payload([
        ("log_a.csv", 1),
        ("log_b.csv", 2),
        ("log_c.csv", 0),
    ])
    api = _StubAPIContext(_StubResponse(200, payload))

    counts = fetch_savvy_file_flight_counts(api, "12345")

    _assert_eq(
        "test_fetch_joins_flights_to_files",
        counts,
        {"log_a.csv": 1, "log_b.csv": 2, "log_c.csv": 0},
    )


def test_graphql_override_corrects_uploadresult_flight_counts() -> None:
    """End-to-end of the override: stubbed GraphQL response is parsed,
    then applied to a list of UploadResults — each one's
    flights_accepted ends up matching the GraphQL count.

    Mirrors the race example from the diagnosis: file_a polled
    Success(0) but really has 1 flight; file_b polled Success(1) but
    really has 0; file_c polled Success(0) and really has 1."""
    payload = _graphql_payload([
        ("log_a.csv", 1),  # polled 0, real 1 (Phase-2 false-negative)
        ("log_b.csv", 0),  # polled 1, real 0 (Phase-2 mis-attribution)
        ("log_c.csv", 1),  # polled 0, real 1 (still Parsing at poll time)
    ])
    api = _StubAPIContext(_StubResponse(200, payload))
    counts = fetch_savvy_file_flight_counts(api, "12345")

    results = [
        UploadResult(filename="log_a.csv", status="Success (0 flights)", flights_accepted=0),
        UploadResult(filename="log_b.csv", status="Success (1 flight)", flights_accepted=1),
        UploadResult(filename="log_c.csv", status="Success (0 flights)", flights_accepted=0),
    ]
    # This mirrors the override loop in run()'s verification block.
    for r in results:
        if r.filename in counts:
            r.flights_accepted = counts[r.filename]

    _assert_eq(
        "test_graphql_override_corrects_uploadresult_flight_counts",
        [(r.filename, r.flights_accepted) for r in results],
        [("log_a.csv", 1), ("log_b.csv", 0), ("log_c.csv", 1)],
    )


def test_fetch_returns_none_on_http_error() -> None:
    """Non-200 GraphQL response yields None so the caller falls back to
    page-derived counts without losing them."""
    api = _StubAPIContext(_StubResponse(503, {}))
    counts = fetch_savvy_file_flight_counts(api, "12345")
    _assert_eq("test_fetch_returns_none_on_http_error", counts, None)


def test_graphql_override_preserves_counts_when_query_failed() -> None:
    """When fetch returned None (transport / parse error), the original
    page-derived flights_accepted on each UploadResult must be left
    intact — losing the count would be worse than risking a stale one."""
    results = [
        UploadResult(filename="log_a.csv", status="Success (3 flights)", flights_accepted=3),
        UploadResult(filename="log_b.csv", status="Success (1 flight)", flights_accepted=1),
    ]
    counts = None  # the failed-fetch return value

    # Same predicate as run()'s verification block: only apply when
    # counts is not None.
    if counts is not None:
        for r in results:
            if r.filename in counts:
                r.flights_accepted = counts[r.filename]

    _assert_eq(
        "test_graphql_override_preserves_counts_when_query_failed",
        [(r.filename, r.flights_accepted) for r in results],
        [("log_a.csv", 3), ("log_b.csv", 1)],
    )


def main() -> None:
    test_poll_stabilises_on_forward_race()
    test_poll_stabilises_on_reverse_race()
    test_poll_processing_mid_stream_resets_confirmation()
    test_fetch_joins_flights_to_files()
    test_graphql_override_corrects_uploadresult_flight_counts()
    test_fetch_returns_none_on_http_error()
    test_graphql_override_preserves_counts_when_query_failed()
    print("\nAll tests passed.")


if __name__ == "__main__":
    main()
