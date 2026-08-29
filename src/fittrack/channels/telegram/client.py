"""The only code that holds the bot token (spec 18.2, 11.1, 20.6).

Every Telegram URL contains the token — `api.telegram.org/bot<TOKEN>/method` —
and so does every download URL. That makes the ordinary behaviour of an HTTP
library a leak: `httpx` puts the request URL into the text of its own
exceptions, so a single unhandled `HTTPStatusError` writes the bot token into a
log, a trace and an incident ticket at once.

So nothing from `httpx` escapes this module. Every call is wrapped, and the
errors raised in their place name the method and never the URL.

The same rule covers `file_path`, which `getFile` returns and the download URL
embeds. It reads as a path and is the access secret (spec 11.1); it is on the
redaction list of 20.2 and it never appears in a message, a log line or a
filename.
"""

from __future__ import annotations

import logging
import re
import uuid
from pathlib import Path
from typing import TYPE_CHECKING, Any

import httpx

from fittrack.channels.base import ChannelError

if TYPE_CHECKING:
    from collections.abc import Mapping

    from pydantic import SecretStr

__all__ = [
    "API_ROOT",
    "DEFAULT_TIMEOUT_SECONDS",
    "MAX_DOWNLOAD_BYTES",
    "TelegramApiError",
    "TelegramClient",
    "TelegramError",
    "TelegramTransportError",
    "redact_telegram_urls",
]

API_ROOT = "https://api.telegram.org"

# The public API's ceiling (spec 18.2). In practice the duration limit of 11.3
# bites first, but a malformed `file_id` can still point at something large, and
# the ingress is the wrong place to discover that.
MAX_DOWNLOAD_BYTES = 20 * 1024 * 1024

# The webhook must answer in under 200 ms (spec 18.2), and this client is not on
# that path — it runs in the worker. The timeout is here so a hung provider
# cannot hold a worker slot indefinitely.
DEFAULT_TIMEOUT_SECONDS = 30.0

# The one 400 that means "already done". `editMessage` with unchanged content
# answers with it, and 18.4 gives it no error class at all.
NOT_MODIFIED = "message is not modified"

# `https://api.telegram.org/bot<TOKEN>/method` and
# `https://api.telegram.org/file/bot<TOKEN>/<file_path>`. Both halves after
# `bot` are secrets: the token always, and the `file_path` because it is the
# access credential for the media (spec 11.1).
_URL = re.compile(r"(https://api\.telegram\.org/(?:file/)?bot)[^\s\"\']*")


def redact_telegram_urls(message: str) -> str:
    """Any Telegram URL in a string, with the secret parts removed."""
    return _URL.sub(r"\1<redacted>", message)


class _RedactingFilter(logging.Filter):
    """Keeps `httpx`'s request log without letting it publish our URLs.

    `httpx` logs every request at INFO, URL included — which for Telegram is the
    bot token on every single call, and the `file_path` on every download. That
    is invariant 10 by accident: nobody wrote the log line, so nobody reviews it.

    Silencing the logger outright would also silence it for the provider SDKs
    that share this process, so the filter redacts instead of muting.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        message = record.getMessage()
        if API_ROOT in message:
            record.msg = redact_telegram_urls(message)
            record.args = ()
        return True


def _install_redaction() -> None:
    """Attach the filter to `httpx`, once per process."""
    logger = logging.getLogger("httpx")
    if not any(isinstance(existing, _RedactingFilter) for existing in logger.filters):
        logger.addFilter(_RedactingFilter())


class TelegramError(ChannelError):
    """Anything the Telegram transport or API refused."""


class TelegramTransportError(TelegramError):
    """The request never got an answer: timeout, connection, malformed body."""


class TelegramApiError(TelegramError):
    """The API answered, and the answer was a refusal.

    Carries what `classify_error` needs and nothing more: the method, the
    status, the description Telegram wrote, and the `parameters` object that
    holds `retry_after` on a 429 (spec 18.4).
    """

    def __init__(
        self,
        *,
        method: str,
        status_code: int,
        description: str,
        parameters: Mapping[str, Any] | None = None,
    ) -> None:
        # The message is the method and the description. Never the URL: it is
        # the token. Never the payload: it is the user's message.
        super().__init__(f"{method} failed with {status_code}: {description}")
        self.method = method
        self.status_code = status_code
        self.description = description
        self.parameters: Mapping[str, Any] = dict(parameters or {})


class TelegramClient:
    """Telegram's HTTP surface, with the token kept inside it.

    The `httpx.AsyncClient` is injected rather than built here: the adapter does
    not open connections of its own, the worker owns the connection pool, and
    the tests hand it a `MockTransport` instead of a socket.
    """

    def __init__(self, token: SecretStr, *, http: httpx.AsyncClient) -> None:
        # Before the first request, because the first request is what leaks.
        _install_redaction()
        self._token = token
        self._http = http

    def __repr__(self) -> str:
        # The default would print the SecretStr, which prints as `**********`,
        # but the client is also held by the adapter and by tracebacks. Say
        # nothing rather than rely on somebody else's masking.
        return "TelegramClient(...)"

    async def call(self, method: str, payload: Mapping[str, Any]) -> Any:
        """One Bot API method, as JSON.

        Returns the `result` field. Raises `TelegramApiError` on a refusal and
        `TelegramTransportError` when there was no answer to refuse with.
        """
        response = await self._request("POST", self._method_url(method), method, json=payload)
        return self._result(method, response)

    async def upload(
        self,
        method: str,
        data: Mapping[str, Any],
        files: Mapping[str, tuple[str, bytes, str]],
    ) -> Any:
        """One Bot API method, as multipart — `sendPhoto` and its bytes.

        Telegram's media upload is a single request (`media_upload="inline"`,
        spec 18.2), which is why there is no separate upload step to strand.
        """
        response = await self._request(
            "POST", self._method_url(method), method, data=dict(data), files=dict(files)
        )
        return self._result(method, response)

    async def download(self, file_path: str, *, into: Path) -> Path:
        """The second half of a media fetch (spec 11.1).

        `file_path` comes from `getFile` and goes into the URL, which is why
        neither it nor the URL appears in anything this method raises or logs.
        The local name is ours and keeps only the extension.
        """
        into.mkdir(parents=True, exist_ok=True)
        destination = into / f"{uuid.uuid4()}{Path(file_path).suffix}"
        url = f"{API_ROOT}/file/bot{self._token.get_secret_value()}/{file_path}"

        written = 0
        try:
            async with self._http.stream("GET", url) as response:
                if response.status_code >= httpx.codes.BAD_REQUEST:
                    await response.aread()
                    raise TelegramApiError(
                        method="download",
                        status_code=response.status_code,
                        description="the file could not be downloaded",
                    )
                with destination.open("wb") as sink:
                    async for chunk in response.aiter_bytes():
                        written += len(chunk)
                        if written > MAX_DOWNLOAD_BYTES:
                            raise TelegramTransportError(
                                f"the download is over the {MAX_DOWNLOAD_BYTES} byte ceiling"
                            )
                        sink.write(chunk)
        except httpx.HTTPError as error:
            destination.unlink(missing_ok=True)
            raise TelegramTransportError(f"download failed: {type(error).__name__}") from None
        except TelegramError:
            # A refused or oversized download leaves no half-file behind: the
            # next stage would read it as a truncated recording.
            destination.unlink(missing_ok=True)
            raise
        return destination

    async def aclose(self) -> None:
        await self._http.aclose()

    # --- internals --------------------------------------------------------- #

    def _method_url(self, method: str) -> str:
        return f"{API_ROOT}/bot{self._token.get_secret_value()}/{method}"

    async def _request(self, verb: str, url: str, method: str, **kwargs: Any) -> httpx.Response:
        try:
            return await self._http.request(verb, url, **kwargs)
        except httpx.HTTPError as error:
            # `str(error)` carries the URL, and the URL is the token. The type
            # name is all the diagnosis this needs; the class is what the retry
            # policy reads anyway.
            raise TelegramTransportError(
                f"{method} did not complete: {type(error).__name__}"
            ) from None

    def _result(self, method: str, response: httpx.Response) -> Any:
        try:
            body = response.json()
        except ValueError:
            raise TelegramTransportError(
                f"{method} answered {response.status_code} with a body that is not JSON"
            ) from None

        if response.status_code < httpx.codes.BAD_REQUEST and body.get("ok"):
            return body.get("result")

        description = str(body.get("description", ""))
        if NOT_MODIFIED in description:
            # Not a failure: the message already says what we wanted it to say,
            # so the edit is a no-op and 18.4 gives the row no class at all.
            return True
        raise TelegramApiError(
            method=method,
            status_code=int(body.get("error_code", response.status_code)),
            description=description or f"HTTP {response.status_code}",
            parameters=body.get("parameters"),
        )
