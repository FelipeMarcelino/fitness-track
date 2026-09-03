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

import asyncio
import logging
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from time import monotonic
from typing import Protocol
from uuid import UUID, uuid5

import httpx
from pydantic import BaseModel, ConfigDict, ValidationError
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from fittrack.channels.base import OutboundBlock
from fittrack.config import SttConfig
from fittrack.db.engine import tenant_session
from fittrack.security.crypto import ColumnCipher, DecryptionError, column_aad
from fittrack.security.tmpfile import open_no_follow, private_directory
from fittrack.settings import MEDIA_TMPFS_DIR, ChannelKind

logger = logging.getLogger(__name__)

__all__ = [
    "CONSENT_PROMPT",
    "DEFAULT_RETRY_DIR",
    "GROQ_TRANSCRIPTIONS_URL",
    "INAUDIBLE_PROMPT",
    "MODEL_TRAINING_CONSENT",
    "TOO_LONG_PROMPT",
    "WORKOUT_DATA_CONSENT",
    "AudioTranscriber",
    "ConsentGate",
    "GroqTranscriber",
    "MediaDownloader",
    "ReplyQueue",
    "SqlConsentGate",
    "SqlTranscriptStore",
    "SqlUsageLedger",
    "SttError",
    "SttRefusedError",
    "SttTransportError",
    "TranscriptStore",
    "Transcription",
    "UsageLedger",
    "VoiceIngestion",
    "VoiceMessage",
    "VoiceOutcome",
    "VoiceStatus",
    "decrypt_transcript",
    "encrypt_transcript",
    "find_pending_audio",
    "load_prompt",
    "pending_audio_path",
    "purge_stale_audio",
    "upload_format",
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

# The two `consent_kind` values of the schema (spec 5.2) this path needs, and
# they are not interchangeable: §11.3 says the *use* of the audio is covered by
# `workout_data`, and "retenção só com `model_training`". Transcribing is use;
# keeping the recording for six hours so a retry can repeat the call is
# retention, and it asks the second question.
WORKOUT_DATA_CONSENT = "workout_data"
MODEL_TRAINING_CONSENT = "model_training"

# tmpfs, never a persistent volume: the file is the user's voice. Its own
# directory rather than `/tmp` itself, because the retention sweep deletes what
# it finds and must not be pointed at a directory it does not own.
#
# The same directory the channel downloads into, and deliberately so: a
# recording is swept whatever name it still carries — the channel's UUID, after
# an interrupted download or a rename that failed, or its `raw_message_id`
# after a failed transcription. A second directory would leave the first
# unswept, which is half of the rule in 11.3.
DEFAULT_RETRY_DIR = MEDIA_TMPFS_DIR

# Both channels deliver a *voice note* as ogg/opus (spec 11.1), and that is the
# fallback. It is not the only thing that arrives, though: `audio` and
# `video_note` are the same inbound kind and come as mp3, m4a or mp4, and a
# provider that picks its decoder from the multipart filename would refuse a
# recording it supports. The suffix is channel-supplied text, so it is matched
# against what the provider documents rather than passed through.
# The group id of a fixed reply is derived from the message it answers, so a
# retry that re-enqueues it lands on the same row instead of a second bubble
# (see `_reply_group`).
VOICE_REPLY_NAMESPACE = UUID("f17b9a54-6a71-5c2b-9a0e-2b2f0b1d0c11")

AUDIO_SUFFIX = ".ogg"
UPLOAD_MIME = "audio/ogg"
UPLOAD_FORMATS: Mapping[str, str] = {
    ".ogg": "audio/ogg",
    ".oga": "audio/ogg",
    ".opus": "audio/ogg",
    ".mp3": "audio/mpeg",
    ".mpga": "audio/mpeg",
    ".mpeg": "audio/mpeg",
    ".m4a": "audio/mp4",
    ".mp4": "video/mp4",
    ".wav": "audio/wav",
    ".webm": "audio/webm",
    ".flac": "audio/flac",
}


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

    It answers the questions that come before the recording itself: who to
    reply to, on which channel, and whether this message has already been
    answered. The transcription is deliberately *not* here — reading it means
    decrypting the user's words, and that must not happen before the consent
    gate of 11.3 has passed. `TranscriptStore.load_transcript` is that later,
    separate step.
    """

    identity_id: int
    channel: ChannelKind
    # `answered_at`, as a boolean: has this message already had its one fixed
    # reply? A drain is kept until the batch is persisted and enqueued (§17.3),
    # so a failure after the reply was queued means the same burst is processed
    # again, and without a durable marker the user is told twice.
    #
    # Its own column rather than `processed_at` (ADR-0008). The two facts are
    # different, and conflating them cost a user their only reply: a
    # transcription that succeeded stamped `processed_at`, and the retry — with
    # consent revoked in between — read that as "already answered", suppressed
    # the consent reply and dropped the item from the batch.
    answered: bool = False


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
        """The row behind a buffered item, or `None` if it is gone.

        Does not read the transcription: see :class:`VoiceMessage`.
        """

    async def load_transcript(self, *, tenant_id: int, raw_message_id: int) -> str | None:
        """The stored transcription, decrypted, or `None` if there is none.

        `None` also covers a blob that no longer opens. That is not the same
        thing, but it is the same *answer*: produce it again.
        """

    async def save(self, *, tenant_id: int, raw_message_id: int, transcript: str) -> None:
        """Persist the transcription. Called before the audio is deleted."""

    async def mark_answered(self, *, tenant_id: int, raw_message_id: int) -> None:
        """Record that this message has had its fixed reply, once."""


class UsageLedger(Protocol):
    """`usage_ledger`, as the cost line of §11.3 needs it."""

    async def record_audio(
        self,
        *,
        tenant_id: int,
        provider: str,
        model: str,
        audio_seconds: float,
    ) -> None:
        """Record the seconds a transcription billed for."""


class ReplyQueue(Protocol):
    """The outbound service, as the fixed replies of 11.3 need it (S02-T06)."""

    async def enqueue_response(
        self,
        *,
        tenant_id: int,
        identity_id: int,
        channel: ChannelKind,
        blocks: Sequence[OutboundBlock],
        group_id: UUID | None = None,
    ) -> UUID:
        """Queue one answer under the given group, or a fresh one."""


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


def upload_format(audio: Path) -> tuple[str, str]:
    """The container to declare for a recording, and its media type.

    Chosen from a known set rather than trusted: the local name comes from
    Telegram's `file_path`, which is not ours. Anything unrecognised becomes
    the ogg/opus both channels send for a voice note.
    """
    suffix = audio.suffix.lower()
    mime = UPLOAD_FORMATS.get(suffix)
    return (suffix, mime) if mime is not None else (AUDIO_SUFFIX, UPLOAD_MIME)


def pending_audio_path(directory: Path, raw_message_id: int, suffix: str = AUDIO_SUFFIX) -> Path:
    """Where a failed transcription leaves its audio for the next attempt.

    Named after the row, because that is what the retry knows: the buffered
    envelope and the persisted batch both carry `raw_message_id`, and the
    channel's own `file_id` must not become a filename (spec 20.6). The
    container is kept, because renaming an mp4 to `.ogg` is how a supported
    recording gets refused by the provider on the retry.
    """
    return directory / f"{raw_message_id}{suffix}"


def find_pending_audio(directory: Path, raw_message_id: int) -> Path | None:
    """The recording a previous attempt kept, whatever container it is in."""
    if not directory.is_dir() or directory.is_symlink():
        return None
    return next(
        (
            candidate
            for candidate in sorted(directory.glob(f"{raw_message_id}.*"))
            if candidate.is_file() and candidate.suffix.lower() in UPLOAD_FORMATS
        ),
        None,
    )


def purge_stale_audio(directory: Path, *, max_age_s: int, now: float | None = None) -> int:
    """Delete recordings past the retention window, returning how many.

    Spec 11.3 gives a failed transcription six hours and no more. Two things
    call this: every ingestion, which covers a worker that keeps receiving
    voice, and the half-hourly cron job registered in ``worker.py``, which
    covers the one that does not — a replica holding one failed recording and
    then going quiet would otherwise keep it for the life of the container.

    The directory is one this module owns and nothing else writes to, which is
    what makes deleting by age safe.
    """
    if not directory.is_dir() or directory.is_symlink():
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
        suffix, mime = upload_format(audio)
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
                    # A fixed stem: the local one is the channel's download and
                    # says something about where the bytes came from. The
                    # suffix is the part the provider needs, so it is the part
                    # that travels.
                    files={"file": (f"audio{suffix}", source.read(), mime)},
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
    """The credential as a string, from a `SecretStr` or a plain one.

    Padding is refused rather than trimmed, exactly as the channel registry
    refuses it: a token that kept the newline of the file it was mounted from
    is a broken deployment, and quietly repairing one hides what needs fixing.
    It also cannot work — `Authorization` with a newline is rejected by the
    HTTP stack itself, and the broad failure handler would turn that into
    `incomplete` for every recording, which is an incident rather than a
    deployment that never happened. The complaint never quotes the value.
    """
    getter = getattr(api_key, "get_secret_value", None)
    value = getter() if callable(getter) else api_key
    if not isinstance(value, str) or not value.strip():
        raise ValueError("the transcription provider needs a credential")
    if value != value.strip():
        raise ValueError(
            "the transcription credential carries whitespace around it; set it without the padding"
        )
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
        budget_s: float | None = None,
        downloader: MediaDownloader,
        transcriber: AudioTranscriber,
        consent: ConsentGate,
        transcripts: TranscriptStore,
        config: SttConfig,
        prompt_dir: Path,
        replies: ReplyQueue | None = None,
        usage: UsageLedger | None = None,
        retry_dir: Path = DEFAULT_RETRY_DIR,
    ) -> None:
        # Which channel the downloader speaks. `download_media` takes a
        # reference and nothing else (spec 18.1), so a WhatsApp `media_id`
        # handed to the Telegram adapter would type-check perfectly and fail
        # against `getFile` — the same category error the channel registry
        # checks for at the other end.
        self._channel = channel
        # How long the whole voice step of one burst may take. The transcription
        # runs inside `flush_check`, and ARQ kills a job at `job_timeout`
        # without retrying it — `TimeoutError` is not `CancelledError`, so its
        # "cancelled, will be run again" branch never fires. The drain is then
        # left orphaned in Redis and nothing re-drives it until the tenant sends
        # another message, so an unbounded loop over a burst can strand it
        # indefinitely. `None` leaves it unbounded, which only tests want.
        self._budget_s = budget_s
        self._downloader = downloader
        self._transcriber = transcriber
        self._consent = consent
        self._transcripts = transcripts
        self._config = config
        self._prompt_dir = prompt_dir
        self._replies = replies
        self._usage = usage
        self._retry_dir = retry_dir
        # Read now, not on the first voice message. A misspelled `prompt_file`
        # or a missing fixed reply is a deployment error, and discovering it
        # after a recording has been downloaded turns it into an `incomplete`
        # message for every voice note instead of a worker that never started.
        self._prompts = {
            name: load_prompt(name, prompt_dir=prompt_dir)
            for name in (config.prompt_file, INAUDIBLE_PROMPT, TOO_LONG_PROMPT, CONSENT_PROMPT)
        }

    # --- the batch integration point (S02-T05) ----------------------------- #

    async def may_process_voice(self, *, tenant_id: int) -> bool:
        """Whether this tenant's recordings may still be used (§11.3).

        The gate on its own, without the transcription behind it. `persist_batch`
        asks before handing a batch it already persisted to the graph: the row
        may have been written before the tenant withdrew consent, and the reuse
        path would otherwise step around the check that `ingest` makes.
        """
        return await self._consent.has_consent(tenant_id=tenant_id, kind=WORKOUT_DATA_CONSENT)

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

        deadline = None if self._budget_s is None else monotonic() + self._budget_s
        kept: list[dict[str, object]] = []
        for item in items:
            if item.get("kind") != "voice":
                kept.append(item)
                continue
            outcome = await self._within_budget(item, tenant_id=tenant_id, deadline=deadline)
            if outcome is None:
                # Out of budget. The item stays, in the shape of a failed
                # transcription — recorded, outside every analysis (invariant
                # 6). Better than the whole burst being stranded by a job the
                # queue will not run again.
                item["was_audio"] = True
                item["text"] = ""
                item["status"] = "incomplete"
                item["media_ref"] = None
                kept.append(item)
                continue
            if not outcome.enters_batch:
                continue
            item["was_audio"] = True
            item["text"] = outcome.text
            # The `file_id` fetches the recording from the channel and has no
            # reader downstream — the graph gets text (spec 11.1). Leaving it
            # in would serialise a reusable channel access reference into
            # `combined_text` and into every trace built from it.
            item["media_ref"] = None
            if outcome.status is VoiceStatus.FAILED:
                # Spec 11.3 and invariant 6: recorded, and outside every
                # analysis. `v_set_volume` filters on `complete`.
                item["status"] = "incomplete"
            kept.append(item)
        return kept

    async def _within_budget(
        self, item: dict[str, object], *, tenant_id: int, deadline: float | None
    ) -> VoiceOutcome | None:
        """One item, or ``None`` when the burst has run out of time.

        A hard deadline rather than an estimate: the download and the provider
        call have their own timeouts, and adding them up would still be a guess
        about which of the two is slow today.
        """
        if deadline is None:
            return await self.ingest(item, tenant_id=tenant_id)
        remaining = deadline - monotonic()
        if remaining <= 0:
            logger.warning(
                "voice budget exhausted, the recording was not transcribed",
                extra={"tenant_id": tenant_id, "raw_message_id": item.get("raw_message_id")},
            )
            return None
        try:
            return await asyncio.wait_for(self.ingest(item, tenant_id=tenant_id), remaining)
        except TimeoutError:
            # The recording is already at its `raw_message_id` path and inside
            # the swept directory, so nothing is stranded by the cancellation.
            logger.warning(
                "voice transcription outran the budget for this burst",
                extra={"tenant_id": tenant_id, "raw_message_id": item.get("raw_message_id")},
            )
            return None

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
            # Anything a previous attempt kept goes with the refusal. §11.3
            # allows a recording to be held for a retry that may now never
            # happen, and the permission it was held under has just been
            # withdrawn — the rule is not only checked at the moment of failure.
            self._discard_pending(raw_message_id)
            logger.info(
                "voice refused: no workout_data consent",
                extra={"tenant_id": tenant_id, "raw_message_id": raw_message_id},
            )
            return await self._refuse(
                VoiceStatus.NO_CONSENT,
                CONSENT_PROMPT,
                tenant_id=tenant_id,
                raw_message_id=raw_message_id,
                row=row,
            )

        # Only now, with the gate passed: reading this decrypts what the user
        # said (§22.2), and a tenant who revoked consent must not have their
        # words decrypted on the way to being told so.
        stored = (
            await self._transcripts.load_transcript(
                tenant_id=tenant_id, raw_message_id=raw_message_id
            )
            if row is not None
            else None
        )
        if stored:
            # A previous attempt already paid for the download and the call,
            # and the batch retry must not pay for either again.
            logger.info(
                "voice already transcribed, reusing the stored transcript",
                extra={"tenant_id": tenant_id, "raw_message_id": raw_message_id},
            )
            return VoiceOutcome(VoiceStatus.TRANSCRIBED, text=stored)

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
                VoiceStatus.TOO_LONG,
                TOO_LONG_PROMPT,
                tenant_id=tenant_id,
                raw_message_id=raw_message_id,
                row=row,
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
                VoiceStatus.TOO_LONG,
                TOO_LONG_PROMPT,
                tenant_id=tenant_id,
                raw_message_id=raw_message_id,
                row=row,
            )

        audio = await self._audio_for(media_ref, tenant_id=tenant_id, raw_message_id=raw_message_id)
        if audio is None:
            return VoiceOutcome(VoiceStatus.FAILED)

        # Everything from here owns a file, and the deadline of E-3 can cut in at
        # any await inside it. `CancelledError` is a `BaseException`, so it reaches
        # none of the handlers below — the `finally` is the only thing that runs on
        # that path, and the recording must not outlive it.
        settled = False
        try:
            try:
                result = await self._transcriber.transcribe(
                    audio, prompt=self._prompt(self._config.prompt_file)
                )
            except Exception as error:
                # Every failure, not only the ones this module names. An engine
                # that raised something unexpected is a bug, and a bug must not
                # cost the user their message (invariant 6): the item goes to the
                # batch as `incomplete` either way. The type is logged; the
                # traceback is not, because a third party's exception text can
                # carry the URL that authenticates the download (spec 20.6).
                #
                # Whether the *recording* survives is a separate question, and
                # §11.3 answers it with a separate consent.
                await self._retain_or_discard(audio, tenant_id=tenant_id, error=error)
                settled = True
                return VoiceOutcome(VoiceStatus.FAILED)

            # Not `text`: that name belongs to SQLAlchemy's constructor in this
            # module, and shadowing it here is one refactor away from a confusing
            # `NameError` in the store below.
            transcript = result.text.strip()
            if not transcript or self._is_inaudible(result):
                _discard(audio)
                settled = True
                logger.info(
                    "voice was inaudible",
                    extra={
                        "tenant_id": tenant_id,
                        "raw_message_id": raw_message_id,
                        "no_speech_prob": result.no_speech_prob,
                    },
                )
                return await self._refuse(
                    VoiceStatus.INAUDIBLE,
                    INAUDIBLE_PROMPT,
                    tenant_id=tenant_id,
                    raw_message_id=raw_message_id,
                    row=row,
                )

            # Before the unlink, deliberately: a crash between the two would
            # otherwise leave the transcription nowhere and the audio gone. The
            # opposite order of failure is safe — the recording sits at its
            # `raw_message_id` path, so a retry finds it and the sweep expires it.
            await self._transcripts.save(
                tenant_id=tenant_id, raw_message_id=raw_message_id, transcript=transcript
            )
            await self._record_usage(tenant_id=tenant_id, seconds=result.duration_s)
            _discard(audio)
            settled = True
            logger.info(
                "voice transcribed",
                extra={
                    "tenant_id": tenant_id,
                    "raw_message_id": raw_message_id,
                    "characters": len(transcript),
                },
            )
            return VoiceOutcome(VoiceStatus.TRANSCRIBED, text=transcript)
        except Exception as error:
            # Something below the transcription failed — the store, most
            # likely — and is on its way to the caller. We can still await, so
            # the recording gets the same consent-aware decision a failed
            # transcription gets rather than the blunt one in `finally`.
            if not settled:
                await self._retain_or_discard(audio, tenant_id=tenant_id, error=error)
                settled = True
            raise
        finally:
            if not settled:
                # Cancellation, and only cancellation: every ordinary path
                # above has settled the file. Deleting is the conservative
                # answer, because keeping the recording needs the
                # `model_training` consent of §11.3 and asking for it is an
                # await that would be cancelled in turn. The cost is one more
                # download if the message comes round again.
                _discard(audio)

    # --- internals --------------------------------------------------------- #

    def _discard_pending(self, raw_message_id: int) -> None:
        """Delete whatever a previous attempt left waiting, if anything."""
        pending = find_pending_audio(self._retry_dir, raw_message_id)
        if pending is not None:
            _discard(pending)

    async def _record_usage(self, *, tenant_id: int, seconds: float | None) -> None:
        """The cost line of §11.3: `usage_ledger.audio_seconds`.

        Accounting, not the message: a ledger that refuses must not cost the
        user their transcription, so the failure is logged and the turn goes
        on. It does understate the month, which is why it is a warning.

        `cost_usd` stays at its default. §7.2 prices tokens, not audio minutes,
        and inventing a rate here would put a number in the quota ceiling of
        §19.3 that nobody chose.
        """
        if self._usage is None or seconds is None:
            return
        try:
            await self._usage.record_audio(
                tenant_id=tenant_id,
                provider=self._config.provider,
                model=self._config.model,
                audio_seconds=seconds,
            )
        except Exception as error:
            logger.warning(
                "the transcription was not recorded in the usage ledger",
                extra={"tenant_id": tenant_id, "error": type(error).__name__},
            )

    async def _retain_or_discard(self, audio: Path, *, tenant_id: int, error: Exception) -> None:
        """Keep the recording for a retry, but only where §11.3 allows it.

        "Retenção só com `model_training`" — and six hours of somebody's voice
        in tmpfs is retention, whatever it is kept for. Without that consent the
        file goes now and the retry pays for the download again, which is the
        ordinary cost of not being allowed to keep it.

        The item is unaffected either way: `raw_message.payload` holds the
        original update, so nothing the user sent is discarded (invariant 6).
        """
        retained = await self._consent.has_consent(tenant_id=tenant_id, kind=MODEL_TRAINING_CONSENT)
        if not retained:
            _discard(audio)
        logger.warning(
            "transcription failed",
            extra={
                "tenant_id": tenant_id,
                "error": type(error).__name__,
                "audio_retained": retained,
                "retention_hours": self._config.retry_retention_hours if retained else 0,
            },
        )

    def _prompt(self, name: str) -> str:
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

    async def _audio_for(
        self, media_ref: str, *, tenant_id: int, raw_message_id: int
    ) -> Path | None:
        """The kept file from a previous failure, or a fresh download.

        A fresh download is moved to its `raw_message_id` path immediately,
        before anything can fail. The channel names the file with a UUID it
        alone knows; if the process died between that name and a later rename,
        the recording would sit in tmpfs under a name no retry could look for
        and no sweep would recognise as ours.
        """
        pending = find_pending_audio(self._retry_dir, raw_message_id)
        if pending is not None:
            if await self._consent.has_consent(tenant_id=tenant_id, kind=MODEL_TRAINING_CONSENT):
                return pending
            # Kept under a consent that no longer holds. Delete it and pay for
            # the download again, which is the cost of not being allowed to
            # keep it (§11.3).
            logger.info(
                "a retained recording lost its retention consent and was deleted",
                extra={"tenant_id": tenant_id, "raw_message_id": raw_message_id},
            )
            _discard(pending)
        try:
            downloaded = await self._downloader.download_media(media_ref)
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
        # The container the channel actually sent, not the one a voice note
        # usually is: renaming an mp4 to `.ogg` is how the provider comes to
        # refuse a recording it supports.
        suffix, _ = upload_format(downloaded)
        return self._adopt(
            downloaded,
            destination=pending_audio_path(self._retry_dir, raw_message_id, suffix),
            raw_message_id=raw_message_id,
        )

    def _adopt(self, downloaded: Path, *, destination: Path, raw_message_id: int) -> Path:
        """Put a fresh download where every later attempt can address it."""
        try:
            private_directory(self._retry_dir)
            downloaded.replace(destination)
        except OSError:
            # Transcribe from where it landed rather than refusing the message.
            # The cost is that this one recording is not retryable and not
            # swept, which is strictly better than discarding the input.
            logger.warning(
                "the recording could not be moved into the retry buffer",
                extra={"raw_message_id": raw_message_id},
            )
            return downloaded
        return destination

    async def _refuse(
        self,
        status: VoiceStatus,
        prompt: str,
        *,
        tenant_id: int,
        raw_message_id: int,
        row: VoiceMessage | None,
    ) -> VoiceOutcome:
        """Answer with one of the fixed replies of 11.3, and queue it if wired."""
        block = OutboundBlock(kind="text", text=self._prompt(prompt))
        if self._replies is not None and row is not None and row.answered:
            # A retried drain reaching the same refusal a second time must not
            # tell the user a second time.
            logger.info(
                "voice was already answered, not queueing the reply again",
                extra={"tenant_id": tenant_id, "voice_status": status.value},
            )
        elif self._replies is not None and row is not None:
            await self._replies.enqueue_response(
                tenant_id=tenant_id,
                identity_id=row.identity_id,
                channel=row.channel,
                blocks=[block],
                # Derived, not fresh: the marker below and this insert commit in
                # separate transactions, so the enqueue has to be idempotent on
                # its own or a retry between the two tells the user twice.
                group_id=_reply_group(raw_message_id, status),
            )
            # After the enqueue, so a failure there is retried rather than
            # marked as answered and dropped.
            await self._transcripts.mark_answered(
                tenant_id=tenant_id, raw_message_id=raw_message_id
            )
        elif self._replies is not None:
            # Without the row there is no identity to address, and inventing
            # one is worse than the missing reply.
            logger.warning(
                "a fixed voice reply has no identity to address",
                extra={"tenant_id": tenant_id, "voice_status": status.value},
            )
        return VoiceOutcome(status, reply=block)


def _reply_group(raw_message_id: int, status: VoiceStatus) -> UUID:
    """One group per message and refusal, stable across retries.

    `outbound_queue` has carried `UNIQUE (group_id, seq)` since the initial
    schema (spec 5.2), so re-enqueueing the same refusal is a no-op rather than
    a second bubble — the insert says `ON CONFLICT DO NOTHING`, and the
    constraint it infers was already there.
    The status is in the name because a message can be refused twice for
    different reasons — too long today, unconsented on the retry — and the
    second refusal is a different thing to say.
    """
    return uuid5(VOICE_REPLY_NAMESPACE, f"{raw_message_id}:{status.value}")


def _row_id(item: Mapping[str, object]) -> int:
    """The `raw_message_id` of a buffered envelope, or a refusal.

    Every envelope has one — `RedisTenantBuffer.append` refuses to write one
    without it — and it is what both durable pieces of state are keyed by.
    """
    value = item.get("raw_message_id")
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        raise ValueError("a voice item needs an integer raw_message_id")
    return value


def _duration(item: Mapping[str, object]) -> float | None:
    """The recording length the channel declared, when it declared one.

    Returned as it arrived. Truncating to `int` first put every length under
    301 seconds below a 300 second ceiling, so a recording the rule refuses
    was downloaded and transcribed anyway.
    """
    value = item.get("duration_s")
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


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
                    # The latest row first, *then* the two conditions. Filtering
                    # `revoked_at IS NULL` before ordering would drop the
                    # revocation and answer from the older grant behind it —
                    # which is a revoked consent reading as granted.
                    "SELECT granted AND revoked_at IS NULL FROM consent "
                    "WHERE kind = CAST(:kind AS consent_kind) "
                    "ORDER BY granted_at DESC, id DESC LIMIT 1"
                ),
                {"kind": kind},
            )
        return bool(granted)


class SqlUsageLedger:
    """`usage_ledger` (spec 5.2), for the audio line of §11.3.

    One row per transcription. `agent` is the free-text label of §20.3 rather
    than an `LLMRole`: transcription is not one (ADR-0007), and the ledger has
    always keyed cost by the thing that spent it.
    """

    AGENT = "stt"

    def __init__(self, sessions: async_sessionmaker[AsyncSession]) -> None:
        self._sessions = sessions

    async def record_audio(
        self,
        *,
        tenant_id: int,
        provider: str,
        model: str,
        audio_seconds: float,
    ) -> None:
        async with tenant_session(self._sessions, tenant_id) as session:
            await session.execute(
                text(
                    "INSERT INTO usage_ledger "
                    "(tenant_id, agent, provider, model, audio_seconds) "
                    "VALUES (:tenant_id, :agent, :provider, :model, :audio_seconds)"
                ),
                {
                    "tenant_id": tenant_id,
                    "agent": self.AGENT,
                    "provider": provider,
                    "model": model,
                    "audio_seconds": round(audio_seconds, 2),
                },
            )


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
        """Who to answer, on which channel, and whether we already have.

        The transcript column is not read here — see `load_transcript`.
        """
        async with tenant_session(self._sessions, tenant_id) as session:
            result = await session.execute(
                text(
                    "SELECT identity_id, channel, answered_at "
                    "FROM raw_message WHERE id = :raw_message_id"
                ),
                {"raw_message_id": raw_message_id},
            )
            row = result.mappings().one_or_none()
        if row is None:
            return None
        return VoiceMessage(
            identity_id=int(row["identity_id"]),
            channel=str(row["channel"]),  # type: ignore[arg-type]  # channel_kind is the Literal
            answered=row["answered_at"] is not None,
        )

    async def load_transcript(self, *, tenant_id: int, raw_message_id: int) -> str | None:
        """The stored transcription, or `None` — including when it will not open.

        A blob that does not authenticate is answered as absent rather than
        raised, and that is a deliberate trade. There is no rotation job for
        this column yet (see `save`), so a key retired while a drain sat
        unprocessed leaves the row permanently unreadable. Raising here would
        not stay local: the drain is kept until its batch is persisted (§17.3),
        every later `flush_check` reads the same orphan and fails on it, and
        the gated drain will not rename the buffer while an orphan exists — so
        every subsequent message from that tenant, plain text included, would
        sit in Redis behind one unreadable transcript. That is a silent,
        deterministic loss of user input, which is what invariant 6 forbids.

        Answering "no transcript" costs one download and one transcription, and
        cannot confuse two rows: the associated data of 22.2 binds a blob to
        its tenant and row, so an unreadable blob is unreadable *here* rather
        than readable as something else. If the recording has expired too, the
        item reaches the batch as `incomplete` — recorded, never discarded.
        """
        async with tenant_session(self._sessions, tenant_id) as session:
            blob = await session.scalar(
                text("SELECT transcript FROM raw_message WHERE id = :raw_message_id"),
                {"raw_message_id": raw_message_id},
            )
        if not blob:
            return None
        try:
            # `key_version` is deliberately not passed: see `save`. The version
            # inside the blob is what selects the key, which is what putting it
            # there was for (spec 22.2).
            return decrypt_transcript(
                self._cipher,
                tenant_id=tenant_id,
                raw_message_id=raw_message_id,
                blob=bytes(blob),
            )
        except DecryptionError:
            # No detail, by the rule of `crypto.py`: one message for every
            # cause. The row id is enough to find it.
            logger.warning(
                "a stored transcript could not be read and will be produced again",
                extra={"tenant_id": tenant_id, "raw_message_id": raw_message_id},
            )
            return None

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
                text("UPDATE raw_message SET transcript = :transcript WHERE id = :raw_message_id"),
                {"transcript": encrypted, "raw_message_id": raw_message_id},
            )

    async def mark_answered(self, *, tenant_id: int, raw_message_id: int) -> None:
        """Stamp `answered_at`, once, so a retried drain does not reply twice.

        `WHERE answered_at IS NULL` keeps the first timestamp: the question is
        whether the user was answered, not when we last looked.
        """
        async with tenant_session(self._sessions, tenant_id) as session:
            await session.execute(
                text(
                    "UPDATE raw_message SET answered_at = now() "
                    "WHERE id = :raw_message_id AND answered_at IS NULL"
                ),
                {"raw_message_id": raw_message_id},
            )
