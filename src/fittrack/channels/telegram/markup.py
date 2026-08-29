"""Neutral markup to Telegram HTML, and the small limits of the API (spec 13.4).

The agent writes one dialect — `**b**`, `__i__` — and each adapter translates.
Asking the LLM for Telegram HTML would add a second failure mode on top of the
one we already handle: malformed HTML is a 400, and a `<` in an exercise name
would break a message that was otherwise correct.

Escaping happens before translation, and that order is the point. Escape after
and the tags this module just wrote would be escaped too; escape never and any
`<` from a user's private exercise name reaches the API as markup.
"""

from __future__ import annotations

import logging
import re
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Sequence

__all__ = [
    "MAX_CALLBACK_DATA_BYTES",
    "callback_data",
    "clip_caption",
    "inline_keyboard",
    "to_telegram_html",
]

logger = logging.getLogger(__name__)

# Telegram's ceiling on `callback_data` (spec 18.2). An index is a handful of
# bytes, so this is a guard against a future edit, not a live constraint.
MAX_CALLBACK_DATA_BYTES = 64

# `**b**` and `__i__` are the spec's; the other two follow the same convention.
# Non-greedy, and the delimiters may not wrap nothing.
_TRANSLATIONS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"\*\*(.+?)\*\*", re.DOTALL), r"<b>\1</b>"),
    (re.compile(r"__(.+?)__", re.DOTALL), r"<i>\1</i>"),
    (re.compile(r"~~(.+?)~~", re.DOTALL), r"<s>\1</s>"),
    (re.compile(r"`(.+?)`", re.DOTALL), r"<code>\1</code>"),
)


# Only the four this module writes; nothing else survives the escaping above.
_TAG = re.compile(r"</?(b|i|s|code)>")


def _well_nested(html: str) -> bool:
    """Whether the tags this module produced open and close in order."""
    stack: list[str] = []
    for match in _TAG.finditer(html):
        tag = match.group(1)
        if match.group(0).startswith("</"):
            if not stack or stack.pop() != tag:
                return False
        else:
            stack.append(tag)
    return not stack


def to_telegram_html(text: str) -> str:
    """The neutral dialect as Telegram HTML, everything else escaped.

    Crossed delimiters — `**bold __italic** tail__` — would translate into
    crossed tags, and Telegram answers those with a non-retryable parse error:
    the user loses the whole response over an emphasis. 13.4 put the translation
    here precisely so that malformed markup is not a 400, which only holds if
    this function refuses to emit HTML it can see is invalid. The delimiters
    stay as written, so the message arrives and the agent's bug is visible in
    the log rather than in a dead letter.
    """
    escaped = text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    rendered = escaped
    for pattern, replacement in _TRANSLATIONS:
        rendered = pattern.sub(replacement, rendered)
    if not _well_nested(rendered):
        logger.warning("neutral markup crossed its delimiters; sending it as text")
        return escaped
    return rendered


def callback_data(index: int) -> str:
    """The position of an option, which is all that may travel to the client.

    Whatever goes in here comes back as user input on the button press. A
    `callback_data` carrying an `exercise_id` is a client-controlled parameter
    walking into the domain (spec 18.2); the options themselves live in Redis
    beside the interrupt, and this is the index into them.
    """
    data = f"opt:{index}"
    if len(data.encode()) > MAX_CALLBACK_DATA_BYTES:  # pragma: no cover - arithmetic
        raise ValueError(f"callback_data is over {MAX_CALLBACK_DATA_BYTES} bytes: {len(data)}")
    return data


def inline_keyboard(options: Sequence[str]) -> dict[str, list[list[dict[str, str]]]]:
    """One option per row, each labelled by its text and keyed by its index."""
    return {
        "inline_keyboard": [
            [{"text": option, "callback_data": callback_data(index)}]
            for index, option in enumerate(options)
        ]
    }


def clip_caption(text: str, *, limit: int) -> str:
    """A caption the API will accept.

    Clipping beats a 400: the caption labels a chart the user asked for, and
    losing the chart to a long label is the worse of the two outcomes.
    """
    return text if len(text) <= limit else text[:limit]
