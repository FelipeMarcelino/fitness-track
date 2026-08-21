"""What to do when the Cloud API refuses a send (§18.5).

"The message failed" means two different things with opposite treatments. A
processing failure -- the burst never became a reply -- is the ARQ queue's
problem. This module is about the other one: the reply exists and did not
arrive.

Blind retry is wrong here. Half of the Cloud API's errors do not improve with
repetition, and some get worse: a send that actually succeeded but timed out on
our side arrives twice if we retry it. So the policy is per error class, and an
error class nobody has classified yet is treated as not retryable rather than
assumed transient.
"""

from __future__ import annotations

import random
from dataclasses import dataclass
from enum import Enum, auto
from typing import Final


class Action(Enum):
    """What the dispatcher does with a bubble after a failure."""

    RETRY = auto()
    # The 24h window closed. The same free-form message will never be
    # accepted; a template can be, if one exists for this content.
    TEMPLATE = auto()
    # The recipient cannot receive anything. Proactive sends have to stop for
    # this tenant, not just this message.
    SUSPEND = auto()
    # Nothing to retry and nobody downstream can fix it: an operator has to
    # look. Account restrictions and our own malformed payloads land here.
    ALERT = auto()


@dataclass(frozen=True)
class Decision:
    action: Action
    retryable: bool
    max_attempts: int
    # Multiplies the backoff table. The pair rate limit (131056) is caused by
    # sending too hard at one recipient, so backing off at the same rate as a
    # global limit would keep causing it.
    backoff_factor: float = 1.0


# 2s, 8s, 32s, 2min, 8min (§18.5).
BACKOFF_TABLE: Final[tuple[int, ...]] = (2, 8, 32, 120, 480)

# Without this, every tenant queued behind one Meta outage retries in the same
# second and causes the next outage.
JITTER: Final = 0.25

_RETRY = Decision(Action.RETRY, retryable=True, max_attempts=5)
_ALERT = Decision(Action.ALERT, retryable=False, max_attempts=0)

BY_CODE: Final[dict[str, Decision]] = {
    # Out of the 24h window.
    "131047": Decision(Action.TEMPLATE, retryable=False, max_attempts=0),
    # Recipient cannot receive.
    "131026": Decision(Action.SUSPEND, retryable=False, max_attempts=0),
    # Meta rate limit.
    "130429": _RETRY,
    # (from, to) pair rate limit.
    "131056": Decision(Action.RETRY, retryable=True, max_attempts=3, backoff_factor=5.0),
    # Account restricted or blocked -- an account problem, not a message one.
    "368": _ALERT,
    "131031": _ALERT,
    # Our bug: invalid payload, malformed template.
    "100": _ALERT,
    "132000": _ALERT,
}


def classify(code: str | None, status: int | None = None) -> Decision:
    """Maps a Cloud API error onto the §18.5 policy.

    `status` covers the cases with no error code at all: a 5xx from Meta, or a
    timeout where we never learned whether the message went out.
    """
    if code is not None:
        known = BY_CODE.get(str(code))
        if known is not None:
            return known
        # Deliberately not retried. An unrecognised code retried on the
        # assumption that it is transient is how a user receives the same
        # message five times.
        return _ALERT

    if status is None or status >= 500:
        # Timeout or Meta-side failure: the one genuinely transient class.
        return _RETRY
    # A 4xx with no code we understand is still our problem, not a blip.
    return _ALERT


def backoff_seconds(decision: Decision, attempt: int) -> float:
    """Delay before attempt number `attempt` (1-based), with jitter.

    Past the end of the table the delay holds at the last step rather than
    wrapping: an attempt beyond the last row must not come back sooner than
    the one before it.
    """
    index = min(max(attempt, 1), len(BACKOFF_TABLE)) - 1
    base = BACKOFF_TABLE[index] * decision.backoff_factor
    return base * random.uniform(1 - JITTER, 1 + JITTER)
