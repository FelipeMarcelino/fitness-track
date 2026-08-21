"""Endpoint behaviour (§18.2).

Two properties are load-bearing: an unsigned delivery must not reach the queue,
and a signed one must be acknowledged fast enough that Meta keeps the webhook
enabled.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import time
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from fittrack.channels.whatsapp.payload import InboundMessage, StatusUpdate
from fittrack.channels.whatsapp.webhook import router
from fittrack.settings import get_settings
from tests.unit.test_settings import REQUIRED

SECRET = REQUIRED["WABA_APP_SECRET"].encode()


class SpyIngest:
    def __init__(self) -> None:
        self.messages: list[InboundMessage] = []
        self.statuses: list[StatusUpdate] = []

    async def accept_message(self, message: InboundMessage, _envelope: dict[str, Any]) -> None:
        self.messages.append(message)

    async def accept_status(self, update: StatusUpdate) -> None:
        self.statuses.append(update)


@pytest.fixture
def spy() -> SpyIngest:
    return SpyIngest()


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch, spy: SpyIngest) -> TestClient:
    for key, value in REQUIRED.items():
        monkeypatch.setenv(key, value)
    get_settings.cache_clear()

    app = FastAPI()
    app.include_router(router)
    app.state.ingest = spy
    return TestClient(app)


def _post(client: TestClient, envelope: dict[str, Any], *, sign: bool = True) -> Any:
    body = json.dumps(envelope).encode()
    headers = {"Content-Type": "application/json"}
    if sign:
        headers["X-Hub-Signature-256"] = (
            "sha256=" + hmac.new(SECRET, body, hashlib.sha256).hexdigest()
        )
    return client.post("/webhook/whatsapp", content=body, headers=headers)


TEXT_DELIVERY = {
    "entry": [
        {
            "changes": [
                {
                    "value": {
                        "messages": [
                            {
                                "id": "wamid.1",
                                "from": "BSUID1",
                                "type": "text",
                                "timestamp": "1",
                                "text": {"body": "supino 80kg 8 reps"},
                            }
                        ]
                    }
                }
            ]
        }
    ]
}


def test_verification_handshake_echoes_the_challenge(client: TestClient) -> None:
    response = client.get(
        "/webhook/whatsapp",
        params={
            "hub.mode": "subscribe",
            "hub.verify_token": REQUIRED["WABA_VERIFY_TOKEN"],
            "hub.challenge": "12345",
        },
    )

    assert response.status_code == 200
    assert response.text == "12345"


def test_verification_rejects_a_wrong_token(client: TestClient) -> None:
    response = client.get(
        "/webhook/whatsapp",
        params={
            "hub.mode": "subscribe",
            "hub.verify_token": "wrong",
            "hub.challenge": "12345",
        },
    )

    assert response.status_code == 403


def test_signed_delivery_reaches_ingest(client: TestClient, spy: SpyIngest) -> None:
    response = _post(client, TEXT_DELIVERY)

    assert response.status_code == 200
    assert [m.message_id for m in spy.messages] == ["wamid.1"]


def test_unsigned_delivery_is_rejected_and_never_reaches_ingest(
    client: TestClient, spy: SpyIngest
) -> None:
    """The endpoint is public. Without this, anyone could inject workout data
    into any account."""
    response = _post(client, TEXT_DELIVERY, sign=False)

    assert response.status_code == 403
    assert spy.messages == []


def test_tampered_body_is_rejected(client: TestClient, spy: SpyIngest) -> None:
    body = json.dumps(TEXT_DELIVERY).encode()
    signature = "sha256=" + hmac.new(SECRET, body, hashlib.sha256).hexdigest()

    response = client.post(
        "/webhook/whatsapp",
        content=body + b" ",
        headers={"X-Hub-Signature-256": signature},
    )

    assert response.status_code == 403
    assert spy.messages == []


def test_malformed_json_is_acknowledged_not_retried(client: TestClient, spy: SpyIngest) -> None:
    """Meta retries anything that is not a 200. Answering 400 to a body we
    cannot parse buys a retry storm and no information."""
    body = b"{not json"
    signature = "sha256=" + hmac.new(SECRET, body, hashlib.sha256).hexdigest()

    response = client.post(
        "/webhook/whatsapp",
        content=body,
        headers={"X-Hub-Signature-256": signature},
    )

    assert response.status_code == 200
    assert spy.messages == []


def test_status_updates_reach_ingest(client: TestClient, spy: SpyIngest) -> None:
    _post(
        client,
        {
            "entry": [
                {
                    "changes": [
                        {
                            "value": {
                                "statuses": [
                                    {
                                        "id": "wamid.out",
                                        "status": "failed",
                                        "recipient_id": "B",
                                        "errors": [{"code": 131047}],
                                    }
                                ]
                            }
                        }
                    ]
                }
            ]
        },
    )

    assert [s.error_code for s in spy.statuses] == ["131047"]


def test_response_is_fast(client: TestClient) -> None:
    """§18.2 requires p99 under 200 ms. This is a floor, not the load test:
    it catches someone adding a blocking call to the response path."""
    durations = []
    for _ in range(50):
        start = time.perf_counter()
        _post(client, TEXT_DELIVERY)
        durations.append(time.perf_counter() - start)

    durations.sort()
    p99 = durations[int(len(durations) * 0.99) - 1]
    assert p99 < 0.2, f"p99 was {p99 * 1000:.0f} ms"
