#!/usr/bin/env python3
"""Tests for compose_failure_email — the fatal-run-failure notification.

Runs with the stdlib only — no pytest dependency:

    python3 tests/test_failure_email.py

A fatal crash skips the normal summary output (email or JSON sidecar), so
this notification is the only proactive signal an operator gets. These tests
pin the subject/body shape: a one-line subject (no multi-line bloat from
Playwright "Call log:" output), a length cap, a class-name fallback for
empty messages, and a body carrying the full traceback plus the
"watcher keeps running" reassurance.
"""
import sys
from pathlib import Path

# Make savvy_upload importable when running this file directly.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from savvy_upload import compose_failure_email  # noqa: E402


# ---------------------------------------------------------------------------
# Test plumbing (same shape as the other tests/ files)
# ---------------------------------------------------------------------------
def _fail(name: str, actual, expected) -> None:
    print(f"FAIL: {name}")
    print(f"--- actual ---\n{actual}")
    print(f"--- expected ---\n{expected}")
    sys.exit(1)


def _assert_eq(name: str, actual, expected) -> None:
    if actual != expected:
        _fail(name, actual, expected)
    print(f"OK: {name}")


def _assert_in(name: str, needle: str, haystack: str) -> None:
    if needle not in haystack:
        _fail(name, haystack, f"(should contain) {needle!r}")
    print(f"OK: {name}")


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------
def test_subject_and_body_basics() -> None:
    err = ValueError("boom")
    subject, body = compose_failure_email(err, "Traceback (most recent call last):\n  ...\n")
    _assert_eq("test_subject_and_body_basics.subject", subject, "Savvy upload FAILED: boom")
    _assert_in("test_subject_and_body_basics.body_error", "ValueError: boom", body)
    _assert_in("test_subject_and_body_basics.body_tb", "Traceback (most recent call last):", body)
    _assert_in("test_subject_and_body_basics.body_reassurance",
               "watcher keeps running", body)


def test_subject_uses_first_line_only() -> None:
    """The crash that motivated this: a Playwright error whose message has a
    multi-line "Call log:" tail. The subject must be the first line only."""
    err = Exception(
        'Page.goto: Navigation to ".../files/upload/<id>" is interrupted '
        'by another navigation to ".../files/upload/<id>"\n'
        "Call log:\n  - navigating to \".../files/upload/<id>\"\n"
    )
    subject, body = compose_failure_email(err, "tb\n")
    _assert_eq(
        "test_subject_uses_first_line_only.subject",
        subject,
        'Savvy upload FAILED: Page.goto: Navigation to ".../files/upload/<id>" '
        'is interrupted by another navigation to ".../files/upload/<id>"',
    )
    if "Call log" in subject:
        _fail("test_subject_uses_first_line_only.no_call_log", subject,
              "(subject must not contain the multi-line Call log tail)")
    print("OK: test_subject_uses_first_line_only.no_call_log")
    # The full multi-line message still belongs in the body.
    _assert_in("test_subject_uses_first_line_only.body_has_call_log", "Call log:", body)


def test_long_subject_is_truncated() -> None:
    err = RuntimeError("x" * 500)
    subject, _ = compose_failure_email(err, "tb\n")
    if len(subject) > 140:
        _fail("test_long_subject_is_truncated.len", len(subject), "<= 140")
    print(f"OK: test_long_subject_is_truncated.len ({len(subject)} <= 140)")
    if not subject.endswith("..."):
        _fail("test_long_subject_is_truncated.ellipsis", subject, "(should end with ...)")
    print("OK: test_long_subject_is_truncated.ellipsis")


def test_empty_message_falls_back_to_class_name() -> None:
    err = TimeoutError("")
    subject, body = compose_failure_email(err, "tb\n")
    _assert_eq("test_empty_message_falls_back_to_class_name.subject",
               subject, "Savvy upload FAILED: TimeoutError")
    _assert_in("test_empty_message_falls_back_to_class_name.body",
               "TimeoutError: (no message)", body)


def main() -> None:
    test_subject_and_body_basics()
    test_subject_uses_first_line_only()
    test_long_subject_is_truncated()
    test_empty_message_falls_back_to_class_name()
    print("\nAll tests passed.")


if __name__ == "__main__":
    main()
