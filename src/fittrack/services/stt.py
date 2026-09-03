"""Audio download and transcription (S02-T07, spec 11).

Both channels deliver voice as ogg/opus, so everything after the download is
identical and lives here; the download itself is the only channel-specific part
and stays behind `Channel.download_media` (spec 18.1). What this module owns is
the five rules of the table in 11.3 — the duration ceiling, the inaudibility
threshold, immediate discard on success, six hours of retention on failure, and
consent before any of it — plus the two invariants they are easiest to break:

* **Never discard the user's input** (invariant 6). A transcription that fails
  does not drop the message: the item reaches the batch with empty text and
  `status='incomplete'`, and the audio is kept so the retry costs one API call
  instead of a download and a call.
* **Never log the access secret or the words** (invariant 10, spec 20.6). The
  Telegram download URL carries the bot token and reads like a file path; the
  `file_id` is what fetches it. Neither the reference, nor the local path, nor
  the transcript appears in a log line here — only the tenant, the row and the
  outcome.

`AudioTranscriber` is the abstract interface the architecture note in 11.3 asks
for: the day LGPD tightens, `faster-whisper` self-hosted replaces
:class:`GroqTranscriber` and nothing else in this file changes.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Protocol
from uuid import UUID

import httpx
from pydantic import BaseModel, ConfigDict, ValidationError
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from fittrack.channels.base import OutboundBlock
from fittrack.config import SttConfig
from fittrack.db.engine import tenant_session
from fittrack.security.crypto import ColumnCipher, column_aad
from fittrack.security.tmpfile import open_no_follow
from fittrack.settings import ChannelKind

logger = logging.getLogger(__name__)

__all__ = [
    "CONSENT_PROMPT",
    "DEFAULT_RETRY_DIR",
    "GROQ_TRANSCRIPTIONS_URL",
    "INAUDIBLE_PROMPT",
    "TOO_LONG_PROMPT",
    "WORKOUT_DATA_CONSENT",
    "AudioTranscriber",
    "ConsentGate",
    "GroqTranscriber",
    "MediaDownloader",
    "ReplyQueue",
    "SqlConsentGate",
    "SqlTranscriptStore",
    "SttError",
    "SttRefusedError",
    "SttTransportError",
    "TranscriptStore",
    "Transcription",
    "VoiceIngestion",
    "VoiceMessage",
    "VoiceOutcome",
    "VoiceStatus",
    "decrypt_transcript",
    "encrypt_transcript",
    "load_prompt",
    "pending_audio_path",
    "purge_stale_audio",
]

# Spec 11.1. The path is in the spec and the model is not: the identifier lives
# in `config/models.yaml` (invariant 4, ADR-0007).
GROQ_TRANSCRIPTIONS_URL = "https://api.groq.com/openai/v1/audio/transcriptions"

# The three fixed replies of 11.3 and the onboarding gate. pt-BR content, which
# AD-27 allows in `config/prompts/`. The vocabulary prompt of 11.2 is not here:
# it is named by `stt.prompt_file` in `models.yaml`, beside the model it
# belongs to, and a second spelling of the filename would be a second thing to
# keep in step.
INAUDIBLE_PROMPT = "stt_inaudible.md"
TOO_LONG_PROMPT = "stt_too_long.md"
CONSENT_PROMPT = "voice_consent_required.md"

# The `consent_kind` of the schema (spec 5.2) that covers using the recording.
# Keeping the audio for training is a *different* consent (`model_training`),
# which is why nothing here retains anything.
WORKOUT_DATA_CONSENT = "workout_data"

# tmpfs, never a persistent volume: the file is the user's voice. Its own
# directory rather than `/tmp` itself, because the retention sweep deletes what
# it finds and must not be pointed at a directory it does not own.
DEFAULT_RETRY_DIR = Path("/tmp/fittrack-stt-retry")

AUDIO_SUFFIX = ".ogg"
UPLOAD_MIME = "audio/ogg"


# --------------------------------------------------------------------------- #
# Errors
# --------------------------------------------------------------------------- #


class SttError(Exception):
    """Transcription did not produce a result.

    Never carries the local path, the credential or the URL: a provider error
    is the most likely thing to be logged unhandled (spec 20.6).
    """


class SttTransportError(SttError):
    """No usable answer: a timeout, a connection failure, a 5xx, a 429.

    Worth another attempt, which is why the audio is kept (spec 11.3).
    """


class SttRefusedError(SttError):
    """The provider answered, and the answer was a refusal.

    A 4xx other than 429: a bad credential, an unsupported container, a file
    over the provider's ceiling. Repeating it changes nothing, but the item is
    still recorded rather than dropped (invariant 6).
    """


# --------------------------------------------------------------------------- #
# Value objects
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class Transcription:
    """What a transcriber returns, whichever engine produced it."""

    text: str
    # `None` where the engine reports no segments. Absent is not the same as
    # zero: the first means "nothing to judge by", and treating it as confident
    # speech is the only answer that does not discard the recording.
    no_speech_prob: float | None = None
    duration_s: float | None = None


class VoiceStatus(StrEnum):
    """What happened to one voice item, and therefore what the batch gets."""

    TRANSCRIBED = "transcribed"
    INAUDIBLE = "inaudible"
    TOO_LONG = "too_long"
    NO_CONSENT = "no_consent"
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class VoiceOutcome:
    """The result of ingesting one voice item.

    `reply` is a *decision*, not a send: the fixed answers of 11.3 are queued
    through the outbound service when one is wired, and the block travels here
    either way so the caller can see what the user was told.
    """

    status: VoiceStatus
    text: str = ""
    reply: OutboundBlock | None = None

    @property
    def enters_batch(self) -> bool:
        """Whether the item joins the burst the graph will see.

        A transcription does, and so does a failure — empty text, marked
        `incomplete`, because invariant 6 forbids dropping the input. The three
        cases that were answered with a fixed reply do not: the conversation is
        already closed, and an empty fragment in the burst would make the
        normalizer invent context for it (spec 9.3).
        """
        return self.status in {VoiceStatus.TRANSCRIBED, VoiceStatus.FAILED}


@dataclass(frozen=True, slots=True)
class VoiceMessage:
    """The `raw_message` row behind a buffered voice item.

    It answers three questions in one query: who to reply to, on which channel,
    and whether this recording was already transcribed by an attempt that then
    failed further down.
    """

    identity_id: int
    channel: ChannelKind
    transcript: str | None


# --------------------------------------------------------------------------- #
# Ports
# --------------------------------------------------------------------------- #


class MediaDownloader(Protocol):
    """The one method of the channel contract this service needs (spec 18.1)."""

    async def download_media(self, media_ref: str) -> Path:
        """Fetch the recording into tmpfs and return the local path."""


class AudioTranscriber(Protocol):
    """The engine, behind an interface (the architecture note of spec 11.3)."""

    async def transcribe(self, audio: Path, *, prompt: str) -> Transcription:
        """Transcribe one file, or raise an :class:`SttError`."""


class ConsentGate(Protocol):
    """The LGPD gate of spec 19.5, as the voice path needs it."""

    async def has_consent(self, *, tenant_id: int, kind: str) -> bool:
        """Whether this tenant has granted, and not revoked, that consent."""


class TranscriptStore(Protocol):
    """`raw_message.transcript`, encrypted at the application (spec 22.2)."""

    async def load(self, *, tenant_id: int, raw_message_id: int) -> VoiceMessage | None:
        """The row behind a buffered item, or `None` if it is gone."""

    async def save(self, *, tenant_id: int, raw_message_id: int, transcript: str) -> None:
        """Persist the transcription. Called before the audio is deleted."""


class ReplyQueue(Protocol):
    """The outbound service, as the fixed replies of 11.3 need it (S02-T06)."""

    async def enqueue_response(
        self,
        *,
        tenant_id: int,
        identity_id: int,
        channel: ChannelKind,
        blocks: Sequence[OutboundBlock],
    ) -> UUID:
        """Queue one answer, with its own group id."""


# --------------------------------------------------------------------------- #
# Prompts and the transcript column
# --------------------------------------------------------------------------- #


def load_prompt(name: str, *, prompt_dir: Path) -> str:
    """One versioned prompt or fixed reply, stripped.

    An empty file is refused rather than sent: a blank fixed reply is silence,
    and 18.4 says never silence.
    """
    text = (prompt_dir / name).read_text(encoding="utf-8").strip()
    if not text:
        raise ValueError(f"{name}: the file is empty")
    return text


def transcript_aad(*, tenant_id: int, raw_message_id: int) -> bytes:
    """Associated data for `raw_message.transcript` (spec 22.2)."""
    return column_aad(
        tenant_id=tenant_id,
        table="raw_message",
        column="transcript",
        row_id=raw_message_id,
    )


def encrypt_transcript(
    cipher: ColumnCipher, *, tenant_id: int, raw_message_id: int, text: str
) -> bytes:
    """The column value for a transcription, bound to its row and tenant."""
    return cipher.encrypt(
        text.encode(), transcript_aad(tenant_id=tenant_id, raw_message_id=raw_message_id)
    )


def decrypt_transcript(
    cipher: ColumnCipher,
    *,
    tenant_id: int,
    raw_message_id: int,
    blob: bytes,
    key_version: int | None = None,
) -> str:
    """The transcription in a column value, or `DecryptionError`."""
    return cipher.decrypt(
        blob,
        transcript_aad(tenant_id=tenant_id, raw_message_id=raw_message_id),
        key_version,
    ).decode()


# --------------------------------------------------------------------------- #
# The retry buffer in tmpfs (spec 11.3)
# --------------------------------------------------------------------------- #


def pending_audio_path(directory: Path, raw_message_id: int) -> Path:
    """Where a failed transcription leaves its audio for the next attempt.

    Named after the row, because that is what the retry knows: the buffered
    envelope and the persisted batch both carry `raw_message_id`, and the
    channel's own `file_id` must not become a filename (spec 20.6).
    """
    return directory / f"{raw_message_id}{AUDIO_SUFFIX}"


def purge_stale_audio(directory: Path, *, max_age_s: int, now: float | None = None) -> int:
    """Delete recordings past the retention window, returning how many.

    Spec 11.3 gives a failed transcription six hours and no more. The sweep is
    opportunistic — every ingestion runs it — so the rule holds without a
    scheduled job, in a directory this module owns and nothing else writes to.
    """
    if not directory.is_dir():
        return 0
    cutoff = (time.time() if now is None else now) - max_age_s
    removed = 0
    for path in directory.iterdir():
        try:
            if not path.is_file() or path.stat().st_mtime > cutoff:
                continue
            path.unlink()
        except OSError:
            # Another worker's sweep, or a file that vanished between the two
            # calls. Neither is worth failing an ingestion over.
            continue
        removed += 1
    return removed


# --------------------------------------------------------------------------- #
# Groq (spec 11.1)
# --------------------------------------------------------------------------- #


class _Segment(BaseModel):
    """One segment of a `verbose_json` response."""

    model_config = ConfigDict(extra="ignore")

    no_speech_prob: float | None = None


class _VerboseTranscription(BaseModel):
    """The provider's answer, validated rather than trusted.

    `extra="ignore"`: the response carries a dozen fields this does not read,
    and a new one appearing must not fail a transcription. The three that are
    read are typed, which is the part the convention is about — the validation
    is the source of truth, not the provider's promise.
    """

    model_config = ConfigDict(extra="ignore")

    text: str = ""
    duration: float | None = None
    segments: list[_Segment] = []

    def as_transcription(self) -> Transcription:
        probabilities = [
            segment.no_speech_prob
            for segment in self.segments
            if segment.no_speech_prob is not None
        ]
        return Transcription(
            text=self.text,
            # The mean, not the maximum. One silent gap in the middle of a set
            # description would discard the whole recording under a maximum,
            # and an entirely silent recording averages close to one either way.
            no_speech_prob=(sum(probabilities) / len(probabilities)) if probabilities else None,
            duration_s=self.duration,
        )


class GroqTranscriber:
    """`whisper` on Groq, over the OpenAI-shaped transcription endpoint.

    The model, the language, the response format and the timeout all come from
    `config/models.yaml`; none of them is written here (invariant 4).
    """

    def __init__(
        self,
        *,
        http: httpx.AsyncClient,
        api_key: object,
        config: SttConfig,
        url: str = GROQ_TRANSCRIPTIONS_URL,
    ) -> None:
        # `object` rather than `SecretStr`: what matters is that the value is
        # never formatted into a message, and `get_secret_value` is read once,
        # here, into a header.
        self._key = _secret(api_key)
        self._http = http
        self._config = config
        self._url = url

    async def transcribe(self, audio: Path, *, prompt: str) -> Transcription:
        data = {
            "model": self._config.model,
            "language": self._config.language,
            "response_format": self._config.response_format,
            "prompt": prompt,
        }
        try:
            with open_no_follow(audio) as source:
                response = await self._http.post(
                    self._url,
                    headers={"Authorization": f"Bearer {self._key}"},
                    data=data,
                    # A fixed name: the local one is the channel's download and
                    # says something about where the bytes came from. Only the
                    # suffix carries information the provider needs.
                    files={"file": (f"audio{AUDIO_SUFFIX}", source.read(), UPLOAD_MIME)},
                    timeout=self._config.timeout_s,
                )
        except OSError as error:
            # The path is deliberately absent: it is the one local name that
            # correlates with a channel download (spec 20.6).
            raise SttError(f"the recording could not be read: {type(error).__name__}") from None
        except httpx.HTTPError as error:
            # `str(error)` carries the URL, and a provider URL plus a bearer
            # header is one careless log line away from a leaked credential.
            raise SttTransportError(
                f"transcription did not complete: {type(error).__name__}"
            ) from None

        _raise_for_status(response)
        try:
            payload = response.json()
        except ValueError:
            raise SttError(
                f"transcription answered {response.status_code} with a body that is not JSON"
            ) from None
        try:
            return _VerboseTranscription.model_validate(payload).as_transcription()
        except ValidationError as error:
            raise SttError(
                f"transcription answered an unexpected shape: {error.error_count()} "
                "field(s) did not validate"
            ) from None


def _secret(api_key: object) -> str:
    """The credential as a string, from a `SecretStr` or a plain one."""
    getter = getattr(api_key, "get_secret_value", None)
    value = getter() if callable(getter) else api_key
    if not isinstance(value, str) or not value.strip():
        raise ValueError("the transcription provider needs a credential")
    return value


def _raise_for_status(response: httpx.Response) -> None:
    """Split the refusals from the failures worth repeating (spec 18.4)."""
    status = response.status_code
    if status < httpx.codes.BAD_REQUEST:
        return
    if status == httpx.codes.TOO_MANY_REQUESTS or status >= httpx.codes.INTERNAL_SERVER_ERROR:
        raise SttTransportError(f"transcription answered {status}")
    raise SttRefusedError(f"transcription refused the request with {status}")


# --------------------------------------------------------------------------- #
# The service
# --------------------------------------------------------------------------- #


class VoiceIngestion:
    """One voice item, from a buffered envelope to text the batch can hold.

    Nothing here is stateful (invariant 5): the durable state is
    `raw_message.transcript` and the file in tmpfs, and both are addressed by
    `raw_message_id`, so any worker can continue another worker's attempt.
    """

    def __init__(
        self,
        *,
        channel: ChannelKind,
        downloader: MediaDownloader,
        transcriber: AudioTranscriber,
        consent: ConsentGate,
        transcripts: TranscriptStore,
        config: SttConfig,
        prompt_dir: Path,
        replies: ReplyQueue | None = None,
        retry_dir: Path = DEFAULT_RETRY_DIR,
    ) -> None:
        # Which channel the downloader speaks. `download_media` takes a
        # reference and nothing else (spec 18.1), so a WhatsApp `media_id`
        # handed to the Telegram adapter would type-check perfectly and fail
        # against `getFile` — the same category error the channel registry
        # checks for at the other end.
        self._channel = channel
        self._downloader = downloader
        self._transcriber = transcriber
        self._consent = consent
        self._transcripts = transcripts
        self._config = config
        self._prompt_dir = prompt_dir
        self._replies = replies
        self._retry_dir = retry_dir
        self._prompts: dict[str, str] = {}

    # --- the batch integration point (S02-T05) ----------------------------- #

    async def resolve(
        self, items: list[dict[str, object]], *, tenant_id: int
    ) -> list[dict[str, object]]:
        """Turn the voice items of a burst into text, in arrival order.

        Called by `persist_batch` before `combined_text` is encrypted, so the
        batch the graph receives holds text and never a media reference. The
        items that were answered with a fixed reply are dropped; a failure
        stays, marked `incomplete` (invariant 6).
        """
        if not any(item.get("kind") == "voice" for item in items):
            return items

        kept: list[dict[str, object]] = []
        for item in items:
            if item.get("kind") != "voice":
                kept.append(item)
                continue
            outcome = await self.ingest(item, tenant_id=tenant_id)
            if not outcome.enters_batch:
                continue
            item["was_audio"] = True
            item["text"] = outcome.text
            if outcome.status is VoiceStatus.FAILED:
                # Spec 11.3 and invariant 6: recorded, and outside every
                # analysis. `v_set_volume` filters on `complete`.
                item["status"] = "incomplete"
            kept.append(item)
        return kept

    # --- one item ---------------------------------------------------------- #

    async def ingest(self, item: Mapping[str, object], *, tenant_id: int) -> VoiceOutcome:
        """Download, transcribe and persist one voice item.

        The order of the gates is the order of their cost: the retention sweep
        and the row lookup are local, consent is one indexed query, the ceiling
        is arithmetic, and only then does anything leave the process.
        """
        raw_message_id = _row_id(item)
        purge_stale_audio(self._retry_dir, max_age_s=self._config.retry_retention_hours * 3600)
        row = await self._transcripts.load(tenant_id=tenant_id, raw_message_id=raw_message_id)

        # Spec 11.3: the recording is covered by `workout_data`, and it is
        # checked on every use rather than only on the call that produced the
        # transcript — a consent revoked between the two is a revoked consent.
        if not await self._consent.has_consent(tenant_id=tenant_id, kind=WORKOUT_DATA_CONSENT):
            logger.info(
                "voice refused: no workout_data consent",
                extra={"tenant_id": tenant_id, "raw_message_id": raw_message_id},
            )
            return await self._refuse(
                VoiceStatus.NO_CONSENT, CONSENT_PROMPT, tenant_id=tenant_id, row=row
            )

        if row is not None and row.transcript:
            # A previous attempt already paid for the download and the call,
            # and the batch retry must not pay for either again.
            logger.info(
                "voice already transcribed, reusing the stored transcript",
                extra={"tenant_id": tenant_id, "raw_message_id": raw_message_id},
            )
            return VoiceOutcome(VoiceStatus.TRANSCRIBED, text=row.transcript)

        duration_s = _duration(item)
        media_ref = item.get("media_ref")
        if duration_s is not None and duration_s > self._config.max_audio_seconds:
            logger.info(
                "voice refused: past the duration ceiling",
                extra={
                    "tenant_id": tenant_id,
                    "raw_message_id": raw_message_id,
                    "duration_s": duration_s,
                },
            )
            return await self._refuse(
                VoiceStatus.TOO_LONG, TOO_LONG_PROMPT, tenant_id=tenant_id, row=row
            )
        item_channel = item.get("channel")
        if item_channel != self._channel:
            # Phase 2.0 wires one ingestion per channel (spec 24). Until then
            # this cannot happen, and if it does the message is recorded rather
            # than fetched with the wrong adapter (invariant 6).
            logger.warning(
                "voice arrived from a channel this worker cannot download from",
                extra={
                    "tenant_id": tenant_id,
                    "raw_message_id": raw_message_id,
                    "channel": item_channel,
                    "wired_channel": self._channel,
                },
            )
            return VoiceOutcome(VoiceStatus.FAILED)
        if not isinstance(media_ref, str) or not media_ref:
            # The parser drops the reference past the ceiling, so nothing
            # downstream can fetch what will not be transcribed (ADR-0006).
            # There is no other way for a voice item to arrive without one.
            logger.info(
                "voice refused: no media reference to fetch",
                extra={"tenant_id": tenant_id, "raw_message_id": raw_message_id},
            )
            return await self._refuse(
                VoiceStatus.TOO_LONG, TOO_LONG_PROMPT, tenant_id=tenant_id, row=row
            )

        audio = await self._audio_for(media_ref, raw_message_id=raw_message_id)
        if audio is None:
            return VoiceOutcome(VoiceStatus.FAILED)

        try:
            result = await self._transcriber.transcribe(
                audio, prompt=self._prompt(self._config.prompt_file)
            )
        except Exception as error:
            # Every failure, not only the ones this module names. An engine
            # that raised something unexpected is a bug, and a bug must not
            # cost the user their message (invariant 6): the item goes to the
            # batch as `incomplete` and the recording is kept for the retry.
            # The type is logged; the traceback is not, because a third party's
            # exception text can carry the URL that authenticates the download
            # (spec 20.6).
            self._keep_for_retry(audio, raw_message_id=raw_message_id)
            logger.warning(
                "transcription failed, the recording is kept for a retry",
                extra={
                    "tenant_id": tenant_id,
                    "raw_message_id": raw_message_id,
                    "error": type(error).__name__,
                    "retention_hours": self._config.retry_retention_hours,
                },
            )
            return VoiceOutcome(VoiceStatus.FAILED)

        # Not `text`: that name belongs to SQLAlchemy's constructor in this
        # module, and shadowing it here is one refactor away from a confusing
        # `NameError` in the store below.
        transcript = result.text.strip()
        if not transcript or self._is_inaudible(result):
            _discard(audio)
            logger.info(
                "voice was inaudible",
                extra={
                    "tenant_id": tenant_id,
                    "raw_message_id": raw_message_id,
                    "no_speech_prob": result.no_speech_prob,
                },
            )
            return await self._refuse(
                VoiceStatus.INAUDIBLE, INAUDIBLE_PROMPT, tenant_id=tenant_id, row=row
            )

        # Before the unlink, deliberately: a crash between the two would
        # otherwise leave the transcription nowhere and the audio gone.
        await self._transcripts.save(
            tenant_id=tenant_id, raw_message_id=raw_message_id, transcript=transcript
        )
        _discard(audio)
        logger.info(
            "voice transcribed",
            extra={
                "tenant_id": tenant_id,
                "raw_message_id": raw_message_id,
                "characters": len(transcript),
            },
        )
        return VoiceOutcome(VoiceStatus.TRANSCRIBED, text=transcript)

    # --- internals --------------------------------------------------------- #

    def _prompt(self, name: str) -> str:
        if name not in self._prompts:
            self._prompts[name] = load_prompt(name, prompt_dir=self._prompt_dir)
        return self._prompts[name]

    def _is_inaudible(self, result: Transcription) -> bool:
        """Spec 11.3: strictly greater than the threshold.

        `None` is not inaudible. An engine that reports no probability has said
        nothing about silence, and refusing on that would discard a recording
        that transcribed fine.
        """
        return (
            result.no_speech_prob is not None
            and result.no_speech_prob > self._config.no_speech_threshold
        )

    async def _audio_for(self, media_ref: str, *, raw_message_id: int) -> Path | None:
        """The kept file from a previous failure, or a fresh download."""
        pending = pending_audio_path(self._retry_dir, raw_message_id)
        if pending.is_file():
            return pending
        try:
            return await self._downloader.download_media(media_ref)
        except Exception as error:
            # Any channel failure: a 429, a network drop, a file the channel
            # will not serve. The message is not discarded — the item goes to
            # the batch as `incomplete` (invariant 6) and the next drain, or
            # the next message, tries again. The reference is not logged.
            logger.warning(
                "voice download failed",
                extra={"raw_message_id": raw_message_id, "error": type(error).__name__},
            )
            return None

    def _keep_for_retry(self, audio: Path, *, raw_message_id: int) -> None:
        """Move the recording to where the next attempt will look for it."""
        destination = pending_audio_path(self._retry_dir, raw_message_id)
        if audio == destination:
            return
        try:
            self._retry_dir.mkdir(parents=True, exist_ok=True)
            audio.replace(destination)
        except OSError:
            # Nothing is lost that was not already lost: without the file the
            # retry pays for the download again, which is the ordinary path.
            logger.warning(
                "the recording could not be kept for a retry",
                extra={"raw_message_id": raw_message_id},
            )

    async def _refuse(
        self,
        status: VoiceStatus,
        prompt: str,
        *,
        tenant_id: int,
        row: VoiceMessage | None,
    ) -> VoiceOutcome:
        """Answer with one of the fixed replies of 11.3, and queue it if wired."""
        block = OutboundBlock(kind="text", text=self._prompt(prompt))
        if self._replies is not None and row is not None:
            await self._replies.enqueue_response(
                tenant_id=tenant_id,
                identity_id=row.identity_id,
                channel=row.channel,
                blocks=[block],
            )
        elif self._replies is not None:
            # Without the row there is no identity to address, and inventing
            # one is worse than the missing reply.
            logger.warning(
                "a fixed voice reply has no identity to address",
                extra={"tenant_id": tenant_id, "voice_status": status.value},
            )
        return VoiceOutcome(status, reply=block)


def _row_id(item: Mapping[str, object]) -> int:
    """The `raw_message_id` of a buffered envelope, or a refusal.

    Every envelope has one — `RedisTenantBuffer.append` refuses to write one
    without it — and it is what both durable pieces of state are keyed by.
    """
    value = item.get("raw_message_id")
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        raise ValueError("a voice item needs an integer raw_message_id")
    return value


def _duration(item: Mapping[str, object]) -> int | None:
    """The recording length the channel declared, when it declared one."""
    value = item.get("duration_s")
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return int(value)


def _discard(audio: Path) -> None:
    """Spec 11.3: immediate discard. The recording is the user's voice."""
    try:
        audio.unlink(missing_ok=True)
    except OSError:
        logger.warning("a transcribed recording could not be deleted")


# --------------------------------------------------------------------------- #
# PostgreSQL implementations
# --------------------------------------------------------------------------- #


class SqlConsentGate:
    """The `consent` table of spec 5.2, read under the tenant's own context.

    The latest record per kind wins, and a revoked one does not: the schema
    keeps history rather than a flag, so a consent granted in onboarding and
    revoked later has two rows and only the second is the answer.
    """

    def __init__(self, sessions: async_sessionmaker[AsyncSession]) -> None:
        self._sessions = sessions

    async def has_consent(self, *, tenant_id: int, kind: str) -> bool:
        async with tenant_session(self._sessions, tenant_id) as session:
            granted = await session.scalar(
                text(
                    "SELECT granted FROM consent "
                    "WHERE kind = CAST(:kind AS consent_kind) AND revoked_at IS NULL "
                    "ORDER BY granted_at DESC, id DESC LIMIT 1"
                ),
                {"kind": kind},
            )
        return bool(granted)


class SqlTranscriptStore:
    """`raw_message.transcript`, encrypted before it reaches Postgres (22.2).

    Nothing aggregates or searches this column, which is what makes the
    encryption free of consequence here (invariant 8): it is read by exactly
    one row id, and only to avoid paying for a transcription twice.
    """

    def __init__(self, sessions: async_sessionmaker[AsyncSession], cipher: ColumnCipher) -> None:
        self._sessions = sessions
        self._cipher = cipher

    async def load(self, *, tenant_id: int, raw_message_id: int) -> VoiceMessage | None:
        async with tenant_session(self._sessions, tenant_id) as session:
            result = await session.execute(
                text(
                    "SELECT identity_id, channel, transcript "
                    "FROM raw_message WHERE id = :raw_message_id"
                ),
                {"raw_message_id": raw_message_id},
            )
            row = result.mappings().one_or_none()
        if row is None:
            return None
        blob = row["transcript"]
        return VoiceMessage(
            identity_id=int(row["identity_id"]),
            channel=str(row["channel"]),  # type: ignore[arg-type]  # channel_kind is the Literal
            # `key_version` is deliberately not passed: see `save`. The version
            # inside the blob is what selects the key, which is what putting it
            # there was for (spec 22.2).
            transcript=(
                decrypt_transcript(
                    self._cipher,
                    tenant_id=tenant_id,
                    raw_message_id=raw_message_id,
                    blob=bytes(blob),
                )
                if blob
                else None
            ),
        )

    async def save(self, *, tenant_id: int, raw_message_id: int, transcript: str) -> None:
        """Write the transcription, leaving the row's `key_version` alone.

        The column is shared with `payload`, which the ingress wrote minutes
        earlier and does not rewrite here. Moving it to the active version
        would make it disagree with the payload blob the moment a rotation
        deploy landed between the two writes — and a disagreement means a
        half-finished rotation, which is an error rather than something to read
        past (spec 22.2). The transcript blob carries its own version, so it is
        readable either way, and the rotation job rewrites both columns of the
        row when it reaches it.
        """
        encrypted = encrypt_transcript(
            self._cipher,
            tenant_id=tenant_id,
            raw_message_id=raw_message_id,
            text=transcript,
        )
        async with tenant_session(self._sessions, tenant_id) as session:
            await session.execute(
                text(
                    "UPDATE raw_message SET transcript = :transcript, processed_at = now() "
                    "WHERE id = :raw_message_id"
                ),
                {"transcript": encrypted, "raw_message_id": raw_message_id},
            )
