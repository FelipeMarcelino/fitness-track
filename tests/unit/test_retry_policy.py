"""Per-error-class send policy (§18.5).

Blind retry is wrong here: half the Cloud API's errors do not improve with
repetition, and some get worse -- a duplicate message the user reads twice.
Every row of the §18.5 table is a separate decision, so every row gets a test.
"""

from __future__ import annotations

import pytest

from fittrack.services.retry_policy import Action, backoff_seconds, classify


def test_out_of_window_becomes_a_template_instead_of_a_retry() -> None:
    """131047 means the 24h window closed. The same free-form message will
    never be accepted again, so repeating it only burns quota."""
    decision = classify("131047")
    assert decision.action is Action.TEMPLATE
    assert not decision.retryable


def test_an_undeliverable_recipient_suspends_the_tenant() -> None:
    """131026 is about the recipient, not the message: retrying any message to
    them fails the same way, and proactive sends have to stop."""
    decision = classify("131026")
    assert decision.action is Action.SUSPEND
    assert not decision.retryable


def test_a_rate_limit_is_retried_with_backoff() -> None:
    decision = classify("130429")
    assert decision.action is Action.RETRY
    assert decision.retryable
    assert decision.max_attempts == 5


def test_a_pair_rate_limit_backs_off_harder_and_gives_up_sooner() -> None:
    """131056 is the (from, to) pair specifically. Hammering it is what caused
    it, so the same backoff as a global rate limit would be too tight."""
    pair = classify("131056")
    glob = classify("130429")
    assert pair.retryable
    assert pair.max_attempts == 3
    assert backoff_seconds(pair, attempt=1) > backoff_seconds(glob, attempt=1)


@pytest.mark.parametrize("code", ["368", "131031"])
def test_an_account_level_block_is_not_a_message_problem(code: str) -> None:
    """Retrying cannot fix a restricted account, and the operator has to hear
    about it."""
    decision = classify(code)
    assert decision.action is Action.ALERT
    assert not decision.retryable


@pytest.mark.parametrize("code", ["100", "132000"])
def test_a_malformed_payload_is_our_bug_and_never_repeats(code: str) -> None:
    """The exact same payload would be rejected the exact same way."""
    decision = classify(code)
    assert decision.action is Action.ALERT
    assert not decision.retryable


@pytest.mark.parametrize("status", [500, 502, 503, 504])
def test_a_transient_meta_failure_is_retried(status: int) -> None:
    decision = classify(None, status=status)
    assert decision.action is Action.RETRY
    assert decision.max_attempts == 5


def test_a_timeout_is_retried() -> None:
    decision = classify(None, status=None)
    assert decision.action is Action.RETRY


def test_an_unknown_code_is_not_retried_blindly() -> None:
    """The failure mode this policy exists to avoid.

    An unrecognised code retried on the assumption it is transient is how a
    user receives the same message five times. Silence plus an alert is the
    safer default; the code gets a row once we know what it means.
    """
    decision = classify("999999")
    assert not decision.retryable
    assert decision.action is Action.ALERT


def test_backoff_grows_and_is_jittered() -> None:
    """Without jitter, every tenant queued behind one Meta outage retries in
    the same second and causes the next one."""
    decision = classify("130429")
    base = [2, 8, 32, 120, 480]
    for attempt, expected in enumerate(base, start=1):
        samples = {backoff_seconds(decision, attempt=attempt) for _ in range(40)}
        assert len(samples) > 1, f"attempt {attempt} is not jittered"
        for value in samples:
            assert expected * 0.75 <= value <= expected * 1.25


def test_backoff_past_the_table_does_not_shrink() -> None:
    """An attempt beyond the last row must not fall back to the first delay."""
    decision = classify("130429")
    assert backoff_seconds(decision, attempt=9) >= 480 * 0.75
