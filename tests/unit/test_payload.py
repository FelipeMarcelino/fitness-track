"""Envelope parsing must never raise: Meta retries anything that is not a 200."""

from __future__ import annotations

from typing import Any

import pytest

from fittrack.channels.whatsapp.payload import parse


def _envelope(**value: Any) -> dict[str, Any]:
    return {"entry": [{"changes": [{"value": value}]}]}


def test_parses_a_text_message() -> None:
    messages, _ = parse(
        _envelope(
            messages=[
                {
                    "id": "wamid.1",
                    "from": "BSUID123",
                    "type": "text",
                    "timestamp": "1755780000",
                    "text": {"body": "Supino reto 80kg 8 reps"},
                }
            ]
        )
    )

    assert len(messages) == 1
    assert messages[0].text == "Supino reto 80kg 8 reps"
    assert messages[0].bsuid == "BSUID123"
    assert messages[0].is_actionable


def test_parses_an_audio_message() -> None:
    messages, _ = parse(
        _envelope(
            messages=[
                {
                    "id": "wamid.2",
                    "from": "B",
                    "type": "audio",
                    "timestamp": "1",
                    "audio": {"id": "media-42", "mime_type": "audio/ogg"},
                }
            ]
        )
    )

    assert messages[0].media_id == "media-42"


def test_parses_an_interactive_button_reply() -> None:
    """Answers to a clarification arrive this way (§8.6)."""
    messages, _ = parse(
        _envelope(
            messages=[
                {
                    "id": "wamid.3",
                    "from": "B",
                    "type": "interactive",
                    "timestamp": "1",
                    "interactive": {
                        "type": "button_reply",
                        "button_reply": {"id": "opt_1", "title": "Supino reto"},
                    },
                }
            ]
        )
    )

    assert messages[0].button_id == "opt_1"


def test_parses_status_updates() -> None:
    _, statuses = parse(
        _envelope(
            statuses=[
                {
                    "id": "wamid.out",
                    "status": "failed",
                    "recipient_id": "B",
                    "errors": [{"code": 131047, "title": "Re-engagement message"}],
                }
            ]
        )
    )

    assert statuses[0].status == "failed"
    assert statuses[0].error_code == "131047"


def test_non_actionable_types_are_parsed_but_flagged() -> None:
    """Reactions and stickers are acknowledged and ignored, not dropped
    silently: raw_message keeps them for audit."""
    messages, _ = parse(
        _envelope(messages=[{"id": "w", "from": "B", "type": "reaction", "timestamp": "1"}])
    )

    assert messages[0].msg_type == "reaction"
    assert not messages[0].is_actionable


@pytest.mark.parametrize(
    "envelope",
    [
        {},
        {"entry": None},
        {"entry": [{"changes": "not-a-list"}]},
        {"entry": [{"changes": [{"value": None}]}]},
        {"entry": [{"changes": [{"value": {"messages": [{"no": "id"}]}}]}]},
        {"entry": [{"changes": [{"value": {"messages": "wrong"}}]}]},
    ],
)
def test_malformed_payloads_yield_nothing_instead_of_raising(
    envelope: dict[str, Any],
) -> None:
    """A KeyError here becomes a 500, and Meta answers repeated 500s by
    disabling the webhook -- so one malformed delivery would take the bot down."""
    messages, statuses = parse(envelope)

    assert messages == []
    assert statuses == []
