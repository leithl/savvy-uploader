#!/usr/bin/env python3
"""Tests for build_summary_dict and compose_email_from_summary.

Runs with the stdlib only — no pytest dependency:

    python3 tests/test_summary_dict.py

Fails fast on the first mismatch with a diff-friendly print, exits 1.
Prints "All tests passed." and exits 0 on success.
"""
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

# Make savvy_upload importable when running this file directly.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from savvy_upload import (  # noqa: E402
    RejectedFlight,
    UploadResult,
    _compact_duration,
    _is_terminal_status_visible,
    build_summary_dict,
    classify_outcome,
    compose_email_from_summary,
    extract_status,
    summary_line,
)

FIXED_TIME = datetime(2026, 5, 21, 14, 32, 14, tzinfo=timezone(timedelta(hours=-6)))
FIXTURES_DIR = Path(__file__).resolve().parent / "fixtures"


def _load_json(name: str) -> dict:
    return json.loads((FIXTURES_DIR / name).read_text())


def _load_text(name: str) -> str:
    # Trim at most one trailing newline so editor-saved fixtures match the
    # output of "\n".join(...) which has no terminal newline.
    text = (FIXTURES_DIR / name).read_text()
    if text.endswith("\n"):
        text = text[:-1]
    return text


def _fail(name: str, actual, expected) -> None:
    print(f"FAIL: {name}")
    if isinstance(actual, str):
        print("--- actual ---")
        print(actual)
        print("--- expected ---")
        print(expected)
    else:
        print("--- actual ---")
        print(json.dumps(actual, indent=2))
        print("--- expected ---")
        print(json.dumps(expected, indent=2))
    sys.exit(1)


def _assert_eq(name: str, actual, expected) -> None:
    if actual != expected:
        _fail(name, actual, expected)
    print(f"OK: {name}")


def test_success_only() -> None:
    results = [
        UploadResult(
            filename="log_20200101_120000_KAAA.csv",
            status="Success (2 flights)",
            flights_accepted=2,
            on_flights_page=True,
        ),
    ]
    summary = build_summary_dict(
        results, n_skipped=0, n_verified=0, n_pending=0, now=FIXED_TIME,
    )
    _assert_eq(
        "test_success_only",
        summary,
        _load_json("expected_summary_success_only.json"),
    )


def test_mixed_success_and_duplicate() -> None:
    results = [
        UploadResult(
            filename="log_20200101_120000_KAAA.csv",
            status="Success (2 flights)",
            flights_accepted=2,
            on_flights_page=True,
        ),
        UploadResult(
            filename="log_20200102_130000_KBBB.csv",
            status="File Duplicated",
            flights_accepted=0,
            on_flights_page=True,
        ),
    ]
    summary = build_summary_dict(
        results, n_skipped=3, n_verified=1, n_pending=0, now=FIXED_TIME,
    )
    _assert_eq(
        "test_mixed_success_and_duplicate",
        summary,
        _load_json("expected_summary_mixed.json"),
    )


def test_success_with_rejected_flights() -> None:
    results = [
        UploadResult(
            filename="log_20200103_140000_KCCC.csv",
            status="Success (1 flight)",
            flights_accepted=1,
            rejected_flights=[
                RejectedFlight(
                    date="2019-04-06",
                    departure="Unknown",
                    destination="Unknown",
                    duration="0h 0m 9s",
                ),
            ],
            on_flights_page=True,
        ),
    ]
    summary = build_summary_dict(
        results, n_skipped=0, n_verified=0, n_pending=1, now=FIXED_TIME,
    )
    _assert_eq(
        "test_success_with_rejected_flights",
        summary,
        _load_json("expected_summary_rejected.json"),
    )


def test_email_body_pins_to_fixture() -> None:
    """Manual byte-for-byte check: build_summary_dict + compose_email_from_summary
    on the mixed fixture renders identically to the hand-written expected body.
    This is what guarantees the refactor preserved the email's wire format."""
    results = [
        UploadResult(
            filename="log_20200101_120000_KAAA.csv",
            status="Success (2 flights)",
            flights_accepted=2,
            on_flights_page=True,
        ),
        UploadResult(
            filename="log_20200102_130000_KBBB.csv",
            status="File Duplicated",
            flights_accepted=0,
            on_flights_page=True,
        ),
    ]
    summary = build_summary_dict(
        results, n_skipped=3, n_verified=1, n_pending=0, now=FIXED_TIME,
    )
    subject, body = compose_email_from_summary(summary)
    expected_subject = "Savvy Upload Report - 2026-05-21 14:32 (2 files)"
    _assert_eq("test_email_subject", subject, expected_subject)
    _assert_eq(
        "test_email_body_pins_to_fixture",
        body,
        _load_text("expected_email_mixed.txt"),
    )


def test_compact_duration() -> None:
    cases = {
        "0h 2m 3s": "2m 3s",
        "0h 0m 9s": "9s",
        "0h 1m 19s": "1m 19s",
        "1h 0m 5s": "1h 0m 5s",
        "2h 14m 0s": "2h 14m 0s",
        "0h 0m 0s": "0s",
        "0h 2m": "2m 0s",
        "?": "?",
    }
    for raw, want in cases.items():
        _assert_eq(f"test_compact_duration[{raw!r}]", _compact_duration(raw), want)


def test_classify_outcome() -> None:
    cases = [
        # (status, accepted, n_rejected) -> outcome
        (("Success (2 flights)", 2, 0), "accepted"),
        # Accepted via the GraphQL re-count: status never said "Success".
        (("Processed", 1, 0), "accepted"),
        (("Rejected", 1, 1), "mixed"),
        (("Success (1 flight)", 1, 1), "mixed"),
        (("Rejected", 0, 1), "rejected"),
        (("File Duplicated", 0, 0), "duplicate"),
        (("File Too Small", 0, 0), "too_small"),
        (("Success (0 flights)", 0, 0), "none"),
        (("timeout", 0, 0), "timeout"),
        (("error: boom", 0, 0), "error"),
        (("unknown", 0, 0), "unknown"),
    ]
    for (status, acc, rej), want in cases:
        _assert_eq(
            f"test_classify_outcome[{status!r},{acc},{rej}]",
            classify_outcome(status, acc, rej),
            want,
        )


def _rej(duration: str) -> RejectedFlight:
    return RejectedFlight("2026-06-18", "KLMO", "KLMO", duration)


def test_summary_line_reproduces_confusing_email() -> None:
    """The four lines from the report that prompted this change — now each
    file's outcome reads consistently and the rejection states its reason."""
    # File 1: accepted via the count, raw status was not "Success".
    line1 = summary_line("Processed", 1, [])
    # File 2: same real outcome, raw status WAS "Success (1 flight)".
    line2 = summary_line("Success (1 flight)", 1, [])
    _assert_eq("test_summary_line.file1", line1, "1 flight accepted")
    _assert_eq("test_summary_line.file2", line2, "1 flight accepted")
    _assert_eq(
        "test_summary_line.identical_outcomes_render_identically", line1, line2
    )
    # File 3: rejected-only, too short.
    _assert_eq(
        "test_summary_line.file3",
        summary_line("Rejected", 0, [_rej("0h 2m 3s")]),
        "rejected — too short (2m 3s)",
    )
    # File 4: one accepted leg AND one rejected leg in the same CSV.
    _assert_eq(
        "test_summary_line.file4",
        summary_line("Rejected", 1, [_rej("0h 2m 14s")]),
        "1 accepted, 1 rejected (too short, 2m 14s)",
    )


def test_summary_line_other_outcomes() -> None:
    cases = [
        (("Success (3 flights)", 3, []), "3 flights accepted"),
        (("File Duplicated", 0, []), "already uploaded (duplicate)"),
        (("File Too Small", 0, []), "file too small — no usable data"),
        (("Success (0 flights)", 0, []), "no flight recorded"),
        (("timeout", 0, []), "still processing — will re-check next run"),
        (("error: boom", 0, []), "upload error — see log"),
        (("unknown", 0, []), "unrecognised status — see log"),
    ]
    for (status, acc, rej), want in cases:
        _assert_eq(
            f"test_summary_line_other[{status!r}]",
            summary_line(status, acc, rej),
            want,
        )
    # Multiple rejected legs: duration is dropped, reason stated once.
    _assert_eq(
        "test_summary_line_other.multi_rejected",
        summary_line("Rejected", 0, [_rej("0h 2m 3s"), _rej("0h 1m 0s")]),
        "rejected — 2 flights (too short)",
    )
    _assert_eq(
        "test_summary_line_other.multi_rejected_mixed",
        summary_line("Success (1 flight)", 1, [_rej("0h 2m 3s"), _rej("0h 1m 0s")]),
        "1 accepted, 2 rejected (too short)",
    )


def test_extract_status_success_singular() -> None:
    _assert_eq(
        "test_extract_status_success_singular",
        extract_status("✓  Success (Show 1 Flights)"),
        "Success (1 flight)",
    )


def test_extract_status_success_plural() -> None:
    _assert_eq(
        "test_extract_status_success_plural",
        extract_status("✓  Success (Show 2 Flights)"),
        "Success (2 flights)",
    )


def test_extract_status_success_zero_is_plural() -> None:
    # 0 takes the plural form in English ("0 flights", not "0 flight").
    _assert_eq(
        "test_extract_status_success_zero_is_plural",
        extract_status("✓  Success (Show 0 Flights)"),
        "Success (0 flights)",
    )


def test_is_terminal_status_visible_checking_duplicates_is_transient() -> None:
    # "Checking Duplicates" is a transient state Savvy shows while
    # parsing; the poller must keep polling, not stop and return it.
    _assert_eq(
        "test_is_terminal_status_visible_checking_duplicates_is_transient",
        _is_terminal_status_visible("● ● ● ●   Checking Duplicates"),
        False,
    )


def test_is_terminal_status_visible_processing_is_transient() -> None:
    _assert_eq(
        "test_is_terminal_status_visible_processing_is_transient",
        _is_terminal_status_visible("● ● ● ●   Processing..."),
        False,
    )


def test_is_terminal_status_visible_success_is_terminal() -> None:
    _assert_eq(
        "test_is_terminal_status_visible_success_is_terminal",
        _is_terminal_status_visible("✓  Success (Show 1 Flights)"),
        True,
    )


def test_is_terminal_status_visible_file_duplicated_is_terminal() -> None:
    _assert_eq(
        "test_is_terminal_status_visible_file_duplicated_is_terminal",
        _is_terminal_status_visible("● ● ● ●   File Duplicated  Show Duplicate"),
        True,
    )


class _StubPage:
    """Minimal stand-in for the Playwright page passed to poll_upload_status.

    Returns inner_text values from a queued sequence so the test can
    simulate "Checking Duplicates ... Checking Duplicates ... Success".
    """

    def __init__(self, inner_texts: list[str]) -> None:
        self._inner_texts = list(inner_texts)
        self.reload_calls = 0

    def evaluate(self, _script: str) -> None:
        return None

    def inner_text(self, _selector: str) -> str:
        if not self._inner_texts:
            raise AssertionError("StubPage ran out of inner_text values — poller polled more times than the test scripted")
        return self._inner_texts.pop(0)

    def reload(self, **_kwargs) -> None:
        self.reload_calls += 1


def test_poll_does_not_leak_checking_duplicates_as_final_status() -> None:
    """With 'Checking Duplicates' on the first poll, the poller must
    wait for the terminal state — not return 'Checking Duplicates' as
    the final status. Two consecutive 'Success (Show 1 Flights)' reads
    are needed to satisfy the stability gate."""
    import savvy_upload  # noqa: PLC0415 — local import to monkey-patch

    filename = "log_20200101_120000_KAAA.csv"
    page = _StubPage(inner_texts=[
        f"{filename}\n● ● ● ●   Checking Duplicates",
        f"{filename}\n✓  Success (Show 1 Flights)",
        f"{filename}\n✓  Success (Show 1 Flights)",
    ])

    # Strip the time.sleep / POLL_INTERVAL waits and the per-call 3s reload
    # delay so the test runs in milliseconds, not minutes.
    real_sleep = savvy_upload.time.sleep
    savvy_upload.time.sleep = lambda _seconds: None
    try:
        status = savvy_upload.poll_upload_status(page, filename)
    finally:
        savvy_upload.time.sleep = real_sleep

    _assert_eq(
        "test_poll_does_not_leak_checking_duplicates_as_final_status",
        status,
        "Success (1 flight)",
    )


def main() -> None:
    test_success_only()
    test_mixed_success_and_duplicate()
    test_success_with_rejected_flights()
    test_email_body_pins_to_fixture()
    test_compact_duration()
    test_classify_outcome()
    test_summary_line_reproduces_confusing_email()
    test_summary_line_other_outcomes()
    test_extract_status_success_singular()
    test_extract_status_success_plural()
    test_extract_status_success_zero_is_plural()
    test_is_terminal_status_visible_checking_duplicates_is_transient()
    test_is_terminal_status_visible_processing_is_transient()
    test_is_terminal_status_visible_success_is_terminal()
    test_is_terminal_status_visible_file_duplicated_is_terminal()
    test_poll_does_not_leak_checking_duplicates_as_final_status()
    print("\nAll tests passed.")


if __name__ == "__main__":
    main()
