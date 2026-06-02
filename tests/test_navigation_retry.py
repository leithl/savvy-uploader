#!/usr/bin/env python3
"""Tests for _goto_upload_page's redirect-race retry.

Runs with the stdlib only — no pytest dependency:

    python3 tests/test_navigation_retry.py

Fails fast on the first mismatch with a diff-friendly print, exits 1.
Prints "All tests passed." and exits 0 on success.

Covers the post-login navigation race fix: after login the site redirects
back to the upload URL, and an explicit page.goto() fired into that in-flight
redirect aborts with "interrupted by another navigation". _goto_upload_page
must treat ONLY that specific error as retryable (resolving via either the
settling redirect or a clean retry), while propagating real navigation
failures immediately so they are never masked.
"""
import sys
from pathlib import Path

# Make savvy_upload importable when running this file directly.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import savvy_upload  # noqa: E402
from savvy_upload import NAV_REDIRECT_RETRIES, _goto_upload_page  # noqa: E402

UPLOAD_URL = "https://apps.savvyaviation.com/files/upload/12345"

# The exact shape of the Playwright error that crashed production. The helper
# gates on the substring, so a plain Exception is a faithful stand-in.
INTERRUPT_MSG = (
    f'Page.goto: Navigation to "{UPLOAD_URL}" is interrupted '
    f'by another navigation to "{UPLOAD_URL}"'
)


# ---------------------------------------------------------------------------
# Test plumbing (same shape as tests/test_polling_stability.py)
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


class _StubNavPage:
    """Minimal stand-in for the Playwright page used by _goto_upload_page.

    goto() raises the queued error (or succeeds, setting .url) on each call;
    wait_for_url() optionally simulates the competing redirect "landing" us on
    *lands_on*. Tracks call counts so tests can assert retry behaviour.
    """

    def __init__(self, goto_errors: list, *, lands_on: str | None = None) -> None:
        # One entry per expected goto() call: an Exception to raise, or None to
        # succeed.
        self._goto_errors = list(goto_errors)
        self._lands_on = lands_on
        self.url = ""
        self.goto_calls = 0
        self.wait_for_url_calls = 0

    def goto(self, url: str, **_kwargs) -> None:
        self.goto_calls += 1
        err = self._goto_errors.pop(0) if self._goto_errors else None
        if err is not None:
            raise err
        self.url = url

    def wait_for_url(self, _predicate, **_kwargs) -> None:
        self.wait_for_url_calls += 1
        # Simulate the competing redirect completing (or not, if lands_on=None).
        if self._lands_on is not None:
            self.url = self._lands_on


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------
def test_succeeds_first_try_no_retry() -> None:
    """The common, race-free case: goto() succeeds, no retry, no settle."""
    page = _StubNavPage(goto_errors=[None])
    _goto_upload_page(page, UPLOAD_URL)
    _assert_eq("test_succeeds_first_try_no_retry.goto_calls", page.goto_calls, 1)
    _assert_eq("test_succeeds_first_try_no_retry.waits", page.wait_for_url_calls, 0)
    _assert_eq("test_succeeds_first_try_no_retry.url", page.url, UPLOAD_URL)


def test_interrupt_resolved_by_settling_redirect() -> None:
    """goto() is interrupted, but the competing redirect lands us on the
    upload page while we wait — so we return without a second goto()."""
    page = _StubNavPage(
        goto_errors=[Exception(INTERRUPT_MSG)],
        lands_on=UPLOAD_URL,
    )
    _goto_upload_page(page, UPLOAD_URL)
    _assert_eq("test_interrupt_resolved_by_settling_redirect.goto_calls",
               page.goto_calls, 1)
    _assert_eq("test_interrupt_resolved_by_settling_redirect.waits",
               page.wait_for_url_calls, 1)
    _assert_eq("test_interrupt_resolved_by_settling_redirect.url",
               page.url, UPLOAD_URL)


def test_interrupt_then_clean_retry() -> None:
    """goto() is interrupted and the redirect does NOT land us there, so the
    helper retries goto() and the second attempt succeeds."""
    page = _StubNavPage(
        goto_errors=[Exception(INTERRUPT_MSG), None],  # 1st interrupts, 2nd OK
        lands_on=None,  # settle wait times out without landing
    )
    _goto_upload_page(page, UPLOAD_URL)
    _assert_eq("test_interrupt_then_clean_retry.goto_calls", page.goto_calls, 2)
    _assert_eq("test_interrupt_then_clean_retry.url", page.url, UPLOAD_URL)


def test_persistent_interrupt_raises_after_bounded_retries() -> None:
    """If every attempt is interrupted and the redirect never lands us there,
    the helper gives up after NAV_REDIRECT_RETRIES and re-raises — it does not
    loop forever."""
    page = _StubNavPage(
        goto_errors=[Exception(INTERRUPT_MSG)] * NAV_REDIRECT_RETRIES,
        lands_on=None,
    )
    raised = None
    try:
        _goto_upload_page(page, UPLOAD_URL)
    except Exception as e:  # noqa: BLE001 - asserting the message below
        raised = e
    _assert_eq("test_persistent_interrupt_raises_after_bounded_retries.raised",
               raised is not None, True)
    _assert_eq("test_persistent_interrupt_raises_after_bounded_retries.is_interrupt",
               "interrupted by another navigation" in str(raised), True)
    _assert_eq("test_persistent_interrupt_raises_after_bounded_retries.goto_calls",
               page.goto_calls, NAV_REDIRECT_RETRIES)


def test_real_error_propagates_immediately() -> None:
    """A non-interrupt navigation failure must NOT be retried or masked — it
    propagates on the first attempt, with no settle wait."""
    real = Exception("Page.goto: net::ERR_CONNECTION_REFUSED at " + UPLOAD_URL)
    page = _StubNavPage(goto_errors=[real])
    raised = None
    try:
        _goto_upload_page(page, UPLOAD_URL)
    except Exception as e:  # noqa: BLE001 - asserting identity below
        raised = e
    _assert_eq("test_real_error_propagates_immediately.is_same_error",
               raised is real, True)
    _assert_eq("test_real_error_propagates_immediately.goto_calls",
               page.goto_calls, 1)
    _assert_eq("test_real_error_propagates_immediately.no_retry_wait",
               page.wait_for_url_calls, 0)


def main() -> None:
    test_succeeds_first_try_no_retry()
    test_interrupt_resolved_by_settling_redirect()
    test_interrupt_then_clean_retry()
    test_persistent_interrupt_raises_after_bounded_retries()
    test_real_error_propagates_immediately()
    print("\nAll tests passed.")


if __name__ == "__main__":
    main()
