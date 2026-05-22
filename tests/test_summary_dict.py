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
    build_summary_dict,
    compose_email_from_summary,
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


def main() -> None:
    test_success_only()
    test_mixed_success_and_duplicate()
    test_success_with_rejected_flights()
    test_email_body_pins_to_fixture()
    print("\nAll tests passed.")


if __name__ == "__main__":
    main()
