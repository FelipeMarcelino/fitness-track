"""Voice ingestion and Groq transcription (S02-T07, spec 11 and 20.6).

Everything here runs against an injected transcriber: the unit suite must not
need a network, a credential or a container. The exception is the block that
drives :class:`GroqTranscriber` itself, and that one uses an httpx mock
transport — still no socket.

The rules of section 11.3 are the subject: a ceiling on the duration, a
threshold on ``no_speech_prob``, immediate deletion after a successful
transcription, six hours of retention after a failed one, and consent before
any of it. Two invariants of CLAUDE.md are also on trial — never discard the
user's input (6), and never let the Telegram access secret or the user's words
reach a log (10, spec 20.6).
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import time
from collections.abc import AsyncIterator, Callable, Sequence
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import httpx
import pytest
import yaml
from pydantic import SecretStr, ValidationError

from fittrack.channels.base import InboundMessage, OutboundBlock
from fittrack.config import ConfigError, SttConfig, configured_providers, load_config, load_models
from fittrack.security.crypto import ColumnCipher, DecryptionError, Keyring
from fittrack.security.tmpfile import create_private, open_no_follow, private_directory
from fittrack.services.stt import (
    CONSENT_PROMPT,
    DEFAULT_RETRY_DIR,
    GROQ_TRANSCRIPTIONS_URL,
    INAUDIBLE_PROMPT,
    MODEL_TRAINING_CONSENT,
    TOO_LONG_PROMPT,
    WORKOUT_DATA_CONSENT,
    AudioTranscriber,
    GroqTranscriber,
    SttError,
    SttRefusedError,
    SttTransportError,
    Transcription,
    VoiceIngestion,
    VoiceMessage,
    VoiceStatus,
    decrypt_transcript,
    encrypt_transcript,
    load_prompt,
    pending_audio_path,
    purge_stale_audio,
    upload_format,
)
from fittrack.services.webhook import IngressIdentity, _envelope
from fittrack.settings import ChannelKind

ROOT = Path(__file__).resolve().parents[2]
CONFIG_DIR = ROOT / "config"
PROMPTS = CONFIG_DIR / "prompts"

TENANT = 77
IDENTITY = 42
RAW_MESSAGE = 1234
MEDIA_REF = "AwACAgEAAxkBAAIC-secret-file-id"

# What a `LogRecord` always carries, so `rendered` can show only the extras.
_STANDARD_RECORD_FIELDS = frozenset(logging.LogRecord("", 0, "", 0, "", None, None).__dict__)


# --------------------------------------------------------------------------- #
# Fakes
# --------------------------------------------------------------------------- #


class FakeDownloader:
    """A ``download_media`` that writes bytes into tmpfs, like the adapter."""

    def __init__(
        self,
        directory: Path,
        *,
        fail: Exception | None = None,
        suffix: str = ".ogg",
    ) -> None:
        self.directory = directory
        self.fail = fail
        # Telegram delivers `voice` as ogg/opus, but `audio` and `video_note`
        # are the same inbound kind and arrive as mp3, m4a or mp4 (spec 11).
        self.suffix = suffix
        self.calls: list[str] = []

    async def download_media(self, media_ref: str) -> Path:
        self.calls.append(media_ref)
        if self.fail is not None:
            raise self.fail
        self.directory.mkdir(parents=True, exist_ok=True)
        destination = self.directory / f"{uuid4()}{self.suffix}"
        destination.write_bytes(b"ogg/opus")
        return destination


class FakeTranscriber:
    """An :class:`AudioTranscriber` that answers from a script."""

    def __init__(self, *results: Transcription | Exception) -> None:
        self.results = list(results)
        self.calls: list[tuple[Path, str]] = []
        self.on_call: Callable[[Path], None] | None = None

    async def transcribe(self, audio: Path, *, prompt: str) -> Transcription:
        self.calls.append((audio, prompt))
        if self.on_call is not None:
            self.on_call(audio)
        result = self.results.pop(0) if self.results else Transcription("ok")
        if isinstance(result, Exception):
            raise result
        return result


class FakeConsent:
    def __init__(self, *, granted: bool = True, retention: bool | None = None) -> None:
        self.granted = granted
        # §11.3 keeps the two apart: `workout_data` covers *using* the
        # recording, `model_training` covers keeping it. Defaults to the same
        # answer so the tests that do not care are unaffected.
        self.retention = granted if retention is None else retention
        self.calls: list[tuple[int, str]] = []

    async def has_consent(self, *, tenant_id: int, kind: str) -> bool:
        self.calls.append((tenant_id, kind))
        return self.retention if kind == MODEL_TRAINING_CONSENT else self.granted


class FakeTranscripts:
    """A transcript store that can record what the audio looked like on save."""

    def __init__(
        self,
        *,
        transcript: str | None = None,
        answered: bool = False,
        missing: bool = False,
        unreadable: bool = False,
    ) -> None:
        # `unreadable` is the store having decided the blob does not decrypt:
        # a retired key, most likely (§22.2). It answers `None`, like a row
        # that never had a transcript.
        self.unreadable = unreadable
        self.row = (
            None
            if missing
            else VoiceMessage(
                identity_id=IDENTITY,
                channel="telegram",
                answered=answered,
            )
        )
        # Keyed by row, like `raw_message`: a single slot let one item's
        # transcript answer for the next one's lookup.
        self.transcripts: dict[int, str] = {}
        self.default_transcript = transcript
        self.answered_flag = answered
        self.loaded: list[int] = []
        self.decrypted: list[int] = []
        self.saved: list[tuple[int, int, str]] = []
        self.answered: list[tuple[int, int]] = []
        self.watch: Path | None = None
        self.audio_existed_on_save: list[bool] = []

    async def load(self, *, tenant_id: int, raw_message_id: int) -> VoiceMessage | None:
        self.loaded.append(raw_message_id)
        if self.row is None:
            return None
        # Rebuilt from current state: `save` and `mark_answered` change what a
        # later `load` sees, which is the whole point of the D-3 scenario.
        return VoiceMessage(
            identity_id=self.row.identity_id,
            channel=self.row.channel,
            answered=self.answered_flag,
        )

    async def load_transcript(self, *, tenant_id: int, raw_message_id: int) -> str | None:
        # Standing in for the decryption the SQL store does here (§22.2): what
        # the test asserts is that nothing calls this before the consent gate.
        self.decrypted.append(raw_message_id)
        if self.unreadable:
            return None
        return self.transcripts.get(raw_message_id, self.default_transcript)

    async def save(self, *, tenant_id: int, raw_message_id: int, transcript: str) -> None:
        self.saved.append((tenant_id, raw_message_id, transcript))
        # Deliberately does not touch `answered_flag`: a transcription that
        # worked is not a fixed reply that was sent (D-3).
        self.transcripts[raw_message_id] = transcript
        if self.watch is not None:
            self.audio_existed_on_save.append(self.watch.is_file())

    async def mark_answered(self, *, tenant_id: int, raw_message_id: int) -> None:
        self.answered.append((tenant_id, raw_message_id))
        self.answered_flag = True


class FakeReplies:
    def __init__(self) -> None:
        self.groups: list[UUID] = []
        self.sent: list[tuple[int, int, ChannelKind, tuple[str | None, ...]]] = []

    async def enqueue_response(
        self,
        *,
        tenant_id: int,
        identity_id: int,
        channel: ChannelKind,
        blocks: Sequence[OutboundBlock],
        group_id: UUID | None = None,
    ) -> UUID:
        self.sent.append((tenant_id, identity_id, channel, tuple(b.text for b in blocks)))
        assigned = group_id or uuid4()
        self.groups.append(assigned)
        return assigned

    @property
    def texts(self) -> list[str]:
        return [text for *_, texts in self.sent for text in texts if text is not None]


class FakeUsage:
    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.records: list[tuple[int, str, str, float]] = []

    async def record_audio(
        self, *, tenant_id: int, provider: str, model: str, audio_seconds: float
    ) -> None:
        if self.fail:
            raise RuntimeError("the ledger is unavailable")
        self.records.append((tenant_id, provider, model, audio_seconds))


def voice_item(
    *,
    media_ref: str | None = MEDIA_REF,
    # `float`, like the channel sends it: Telegram declares whole seconds, but
    # the ceiling of §11.3 has to hold for a fractional one too.
    duration_s: float | None = 12,
    raw_message_id: int = RAW_MESSAGE,
) -> dict[str, object]:
    """One buffer envelope for a voice message, as `webhook.py` writes it."""
    return {
        "channel": "telegram",
        "external_id_hash": "beef",
        "channel_message_id": "9",
        "kind": "voice",
        "text": None,
        "media_ref": media_ref,
        "duration_s": duration_s,
        "button_payload": None,
        "sent_at": "2026-09-02T10:00:00+00:00",
        "raw_message_id": raw_message_id,
    }


def text_item(text: str = "supino 10kg", *, raw_message_id: int = 99) -> dict[str, object]:
    return {
        **voice_item(raw_message_id=raw_message_id),
        "kind": "text",
        "text": text,
        "media_ref": None,
        "duration_s": None,
    }


def config(**overrides: Any) -> SttConfig:
    return SttConfig.model_validate({"provider": "groq", "model": "test-stt", **overrides})


def ingestion(
    *,
    tmp_path: Path,
    downloader: FakeDownloader | None = None,
    transcriber: AudioTranscriber | None = None,
    consent: FakeConsent | None = None,
    transcripts: FakeTranscripts | None = None,
    replies: FakeReplies | None = None,
    stt: SttConfig | None = None,
    budget_s: float | None = None,
    usage: FakeUsage | None = None,
) -> VoiceIngestion:
    return VoiceIngestion(
        budget_s=budget_s,
        usage=usage,
        channel="telegram",
        downloader=downloader or FakeDownloader(tmp_path / "download"),
        transcriber=transcriber or FakeTranscriber(),
        consent=consent or FakeConsent(),
        transcripts=transcripts or FakeTranscripts(),
        replies=replies,
        config=stt or config(),
        prompt_dir=PROMPTS,
        retry_dir=tmp_path / "retry",
    )


# --------------------------------------------------------------------------- #
# The versioned prompts (spec 11.2, AD-27)
# --------------------------------------------------------------------------- #


def test_the_vocabulary_prompt_is_a_versioned_file(committed: SttConfig) -> None:
    """Named by the configuration, beside the model it belongs to (ADR-0007)."""
    path = PROMPTS / committed.prompt_file
    assert path.is_file()
    assert path.read_text(encoding="utf-8").strip()


@pytest.mark.parametrize("term", ["supino", "agachamento", "rpe", "repeti", "aquecimento"])
def test_the_vocabulary_carries_the_gym_jargon_of_the_spec(committed: SttConfig, term: str) -> None:
    """Spec 11.2: injecting the vocabulary is what cuts the jargon error rate."""
    vocabulary = (PROMPTS / committed.prompt_file).read_text(encoding="utf-8")
    assert term in vocabulary.lower()


def test_the_inaudible_reply_is_the_fixed_text_of_the_spec() -> None:
    """Section 11.3 fixes the wording, so it lives in configuration, not code."""
    assert load_prompt(INAUDIBLE_PROMPT, prompt_dir=PROMPTS) == "Não consegui ouvir, pode repetir?"


@pytest.mark.parametrize("name", [TOO_LONG_PROMPT, CONSENT_PROMPT])
def test_every_fixed_voice_reply_is_a_versioned_file(name: str) -> None:
    assert load_prompt(name, prompt_dir=PROMPTS)


def test_an_empty_prompt_file_is_refused(tmp_path: Path) -> None:
    (tmp_path / "blank.md").write_text("   \n", encoding="utf-8")
    with pytest.raises(ValueError, match=r"blank\.md"):
        load_prompt("blank.md", prompt_dir=tmp_path)


# A transcription model identifier, in the shape every provider spells it. The
# sibling check in `test_judge_config.py` covers the chat models; this one is
# its STT half, and the pattern is a *versioned identifier* rather than the
# bare family name so that naming the engine in prose stays possible — the
# migration target in spec 11.3 is a library, not a model.
_STT_MODEL_NAME = re.compile(r"whisper-[a-z0-9]", re.IGNORECASE)


def test_no_transcription_model_name_appears_in_python() -> None:
    """Invariant 4 covers the STT model too: it lives in config/models.yaml."""
    offenders = [
        f"{path.relative_to(ROOT)}:{number}: {line.strip()}"
        for directory in ("src", "evals", "scripts")
        for path in (ROOT / directory).rglob("*.py")
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1)
        if _STT_MODEL_NAME.search(line)
    ]
    assert not offenders, "STT model names belong in config/models.yaml:\n" + "\n".join(offenders)


def test_the_check_sees_a_real_identifier_and_not_the_engine_family() -> None:
    """A guard that fires on prose gets relaxed; one that misses is worse."""
    assert _STT_MODEL_NAME.search('model: "whisper-large-v3"')
    assert _STT_MODEL_NAME.search("WHISPER-1")
    assert not _STT_MODEL_NAME.search("migrating to faster-whisper self-hosted")


# --------------------------------------------------------------------------- #
# The committed configuration (spec 11.1 and 11.3)
# --------------------------------------------------------------------------- #


@pytest.fixture(scope="module")
def committed() -> SttConfig:
    models = load_models(CONFIG_DIR / "models.yaml")
    assert models.stt is not None, "config/models.yaml declares no stt section"
    return models.stt


def test_the_committed_stt_configuration_matches_the_spec(committed: SttConfig) -> None:
    assert committed.provider == "groq"
    assert committed.model
    assert committed.language == "pt"
    # verbose_json is not a preference: no_speech_prob and the segments arrive
    # in no other response format (spec 11.1).
    assert committed.response_format == "verbose_json"
    assert committed.no_speech_threshold == 0.6
    assert committed.max_audio_seconds == 300
    assert committed.retry_retention_hours == 6


def test_the_duration_ceiling_agrees_with_the_channel_parser(committed: SttConfig) -> None:
    """One ceiling, two enforcers: the parser drops the reference, the service replies.

    They have to be the same number. The parser's is a module constant and the
    service's is configuration, so raising only the second would leave a
    recording between the two refused as too long by a service configured to
    accept it — the setting would validate and never take effect.
    """
    from fittrack.channels.telegram.adapter import MAX_AUDIO_SECONDS

    assert committed.max_audio_seconds == MAX_AUDIO_SECONDS, (
        "raising stt.max_audio_seconds needs MAX_AUDIO_SECONDS in the Telegram "
        "parser raised with it, or the parser strips the media_ref first and the "
        "new ceiling never applies"
    )


@pytest.mark.parametrize("provider", ["openai", "anthropic", "xai"])
def test_a_provider_this_code_cannot_talk_to_fails_to_load(provider: str) -> None:
    """The wiring reads `{provider}_api_key` and always posts to Groq (spec 11.1).

    A configuration naming another provider would validate, pick up that
    provider's credential and send it to api.groq.com as a bearer token.
    """
    with pytest.raises(ValidationError):
        SttConfig.model_validate({"provider": provider, "model": "m"})


def test_the_request_timeout_fits_inside_the_job_that_runs_it(committed: SttConfig) -> None:
    """The transcription runs inside `flush_check`, and ARQ cancels at the cap.

    A request still pending when the job is cancelled never reaches the handler
    that keeps the recording for a retry, so the voice path would lose both the
    transcription and the retention rule of §11.3.
    """
    from fittrack.worker import JOB_TIMEOUT

    assert committed.timeout_s < JOB_TIMEOUT


def test_a_response_format_without_no_speech_prob_fails_to_load() -> None:
    with pytest.raises(ValidationError):
        SttConfig.model_validate({"provider": "groq", "model": "m", "response_format": "json"})


def test_a_threshold_outside_zero_to_one_fails_to_load() -> None:
    with pytest.raises(ValidationError):
        config(no_speech_threshold=1.5)


def test_a_stt_section_without_a_model_fails_to_load(tmp_path: Path) -> None:
    path = tmp_path / "models.yaml"
    payload = yaml.safe_load((CONFIG_DIR / "models.yaml").read_text(encoding="utf-8"))
    del payload["stt"]["model"]
    path.write_text(yaml.safe_dump(payload), encoding="utf-8")
    with pytest.raises(ConfigError, match=r"models\.yaml"):
        load_models(path)


def test_the_stt_provider_is_reported_as_needing_a_credential() -> None:
    """A provider the configuration names must be counted, STT included."""
    assert config().provider in configured_providers(load_config(CONFIG_DIR))


# --------------------------------------------------------------------------- #
# The happy path (spec 11.1)
# --------------------------------------------------------------------------- #


async def test_a_transcription_becomes_the_text_of_the_item(tmp_path: Path) -> None:
    transcriber = FakeTranscriber(Transcription("supino reto 10 kg 8 repeticoes"))

    outcome = await ingestion(tmp_path=tmp_path, transcriber=transcriber).ingest(
        voice_item(), tenant_id=TENANT
    )

    assert outcome.status is VoiceStatus.TRANSCRIBED
    assert outcome.text == "supino reto 10 kg 8 repeticoes"
    assert outcome.enters_batch


async def test_the_vocabulary_prompt_reaches_the_transcriber(tmp_path: Path) -> None:
    transcriber = FakeTranscriber()
    await ingestion(tmp_path=tmp_path, transcriber=transcriber).ingest(
        voice_item(), tenant_id=TENANT
    )

    [(_, prompt)] = transcriber.calls
    assert "supino" in prompt.lower()


async def test_the_audio_is_deleted_after_a_successful_transcription(tmp_path: Path) -> None:
    """Spec 11.3: immediate discard. The recording is the user's voice."""
    transcriber = FakeTranscriber()

    await ingestion(tmp_path=tmp_path, transcriber=transcriber).ingest(
        voice_item(), tenant_id=TENANT
    )

    [(audio, _)] = transcriber.calls
    assert not audio.exists()
    assert not list((tmp_path / "retry").glob("*"))


async def test_the_transcript_is_persisted_before_the_audio_is_deleted(tmp_path: Path) -> None:
    """Otherwise a crash between the two loses the only copy of the message."""
    transcripts = FakeTranscripts()
    transcriber = FakeTranscriber()

    def remember(audio: Path) -> None:
        transcripts.watch = audio

    transcriber.on_call = remember

    await ingestion(tmp_path=tmp_path, transcriber=transcriber, transcripts=transcripts).ingest(
        voice_item(), tenant_id=TENANT
    )

    assert transcripts.saved == [(TENANT, RAW_MESSAGE, "ok")]
    assert transcripts.audio_existed_on_save == [True]


async def test_a_stored_transcript_repeats_neither_the_download_nor_the_call(
    tmp_path: Path,
) -> None:
    """Spec 11.1 and the T07 criterion: a batch retry must not pay for either twice."""
    downloader = FakeDownloader(tmp_path / "download")
    transcriber = FakeTranscriber()

    outcome = await ingestion(
        tmp_path=tmp_path,
        downloader=downloader,
        transcriber=transcriber,
        transcripts=FakeTranscripts(transcript="ja transcrito"),
    ).ingest(voice_item(), tenant_id=TENANT)

    assert outcome.status is VoiceStatus.TRANSCRIBED
    assert outcome.text == "ja transcrito"
    assert downloader.calls == []
    assert transcriber.calls == []


async def test_a_transcription_is_recorded_in_the_usage_ledger(tmp_path: Path) -> None:
    """§11.3: "Custo | Registrado em `usage_ledger.audio_seconds`"."""
    usage = FakeUsage()

    await ingestion(
        tmp_path=tmp_path,
        transcriber=FakeTranscriber(Transcription("supino", duration_s=12.5)),
        usage=usage,
    ).ingest(voice_item(), tenant_id=TENANT)

    assert usage.records == [(TENANT, "groq", "test-stt", 12.5)]


async def test_a_recording_the_provider_did_not_measure_bills_nothing(
    tmp_path: Path,
) -> None:
    """No duration, no line: an invented number is worse than a missing one."""
    usage = FakeUsage()

    await ingestion(
        tmp_path=tmp_path, transcriber=FakeTranscriber(Transcription("supino")), usage=usage
    ).ingest(voice_item(), tenant_id=TENANT)

    assert usage.records == []


async def test_a_refused_recording_bills_nothing(tmp_path: Path) -> None:
    usage = FakeUsage()

    await ingestion(tmp_path=tmp_path, consent=FakeConsent(granted=False), usage=usage).ingest(
        voice_item(), tenant_id=TENANT
    )

    assert usage.records == []


async def test_a_ledger_failure_does_not_cost_the_user_the_transcription(
    tmp_path: Path,
) -> None:
    """Accounting is not the message. It understates the month, and says so."""
    outcome = await ingestion(
        tmp_path=tmp_path,
        transcriber=FakeTranscriber(Transcription("supino", duration_s=3.0)),
        usage=FakeUsage(fail=True),
    ).ingest(voice_item(), tenant_id=TENANT)

    assert outcome.status is VoiceStatus.TRANSCRIBED
    assert outcome.text == "supino"


# --------------------------------------------------------------------------- #
# Inaudible (spec 11.3)
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "result",
    [
        pytest.param(Transcription(""), id="empty"),
        pytest.param(Transcription("   \n"), id="blank"),
        pytest.param(Transcription("alo", no_speech_prob=0.61), id="past the threshold"),
    ],
)
async def test_an_inaudible_recording_gets_the_fixed_reply(
    tmp_path: Path, result: Transcription
) -> None:
    replies = FakeReplies()

    outcome = await ingestion(
        tmp_path=tmp_path, transcriber=FakeTranscriber(result), replies=replies
    ).ingest(voice_item(), tenant_id=TENANT)

    assert outcome.status is VoiceStatus.INAUDIBLE
    assert not outcome.enters_batch
    assert replies.texts == ["Não consegui ouvir, pode repetir?"]
    assert replies.sent[0][1] == IDENTITY


async def test_the_threshold_is_strictly_greater(tmp_path: Path) -> None:
    """0.6 exactly is speech: the rule of 11.3 says `> 0.6`."""
    outcome = await ingestion(
        tmp_path=tmp_path, transcriber=FakeTranscriber(Transcription("alo", no_speech_prob=0.6))
    ).ingest(voice_item(), tenant_id=TENANT)

    assert outcome.status is VoiceStatus.TRANSCRIBED


async def test_an_inaudible_recording_records_no_transcript_and_keeps_no_file(
    tmp_path: Path,
) -> None:
    transcriber = FakeTranscriber(Transcription(""))
    transcripts = FakeTranscripts()

    await ingestion(tmp_path=tmp_path, transcriber=transcriber, transcripts=transcripts).ingest(
        voice_item(), tenant_id=TENANT
    )

    [(audio, _)] = transcriber.calls
    assert not audio.exists()
    assert transcripts.saved == []


# --------------------------------------------------------------------------- #
# Failure (invariant 6, spec 11.3)
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "error",
    [
        pytest.param(SttTransportError("timeout"), id="transport"),
        pytest.param(SttRefusedError("refused"), id="refused"),
        pytest.param(RuntimeError("an engine bug"), id="unexpected"),
    ],
)
async def test_a_failed_transcription_still_reaches_the_batch(
    tmp_path: Path, error: Exception
) -> None:
    """Invariant 6: never discard the user's input. Empty text, `incomplete`."""
    outcome = await ingestion(tmp_path=tmp_path, transcriber=FakeTranscriber(error)).ingest(
        voice_item(), tenant_id=TENANT
    )

    assert outcome.status is VoiceStatus.FAILED
    assert outcome.text == ""
    assert outcome.enters_batch


async def test_a_failed_transcription_keeps_the_audio_for_a_retry(tmp_path: Path) -> None:
    """Spec 11.3: six hours in tmpfs, named by the row the retry will ask for."""
    await ingestion(
        tmp_path=tmp_path, transcriber=FakeTranscriber(SttTransportError("timeout"))
    ).ingest(voice_item(), tenant_id=TENANT)

    kept = pending_audio_path(tmp_path / "retry", RAW_MESSAGE)
    assert kept.is_file()
    assert kept.read_bytes() == b"ogg/opus"


async def test_a_retry_reuses_the_kept_audio_instead_of_downloading_again(
    tmp_path: Path,
) -> None:
    downloader = FakeDownloader(tmp_path / "download")
    await ingestion(
        tmp_path=tmp_path,
        downloader=downloader,
        transcriber=FakeTranscriber(SttTransportError("timeout")),
    ).ingest(voice_item(), tenant_id=TENANT)
    assert downloader.calls == [MEDIA_REF]

    outcome = await ingestion(
        tmp_path=tmp_path,
        downloader=downloader,
        transcriber=FakeTranscriber(Transcription("na segunda vez")),
    ).ingest(voice_item(), tenant_id=TENANT)

    assert outcome.text == "na segunda vez"
    assert downloader.calls == [MEDIA_REF], "the retry downloaded the audio a second time"
    assert not pending_audio_path(tmp_path / "retry", RAW_MESSAGE).exists()


async def test_voice_from_another_channel_is_recorded_rather_than_fetched(
    tmp_path: Path,
) -> None:
    """A WhatsApp media id handed to the Telegram adapter would fail obscurely."""
    downloader = FakeDownloader(tmp_path / "download")
    item = {**voice_item(), "channel": "whatsapp"}

    outcome = await ingestion(tmp_path=tmp_path, downloader=downloader).ingest(
        item, tenant_id=TENANT
    )

    assert outcome.status is VoiceStatus.FAILED
    assert outcome.enters_batch
    assert downloader.calls == []


async def test_a_download_failure_never_reaches_the_caller(tmp_path: Path) -> None:
    """A channel outage is a failed item, not a failed batch."""
    downloader = FakeDownloader(tmp_path / "download", fail=RuntimeError("telegram is down"))

    outcome = await ingestion(tmp_path=tmp_path, downloader=downloader).ingest(
        voice_item(), tenant_id=TENANT
    )

    assert outcome.status is VoiceStatus.FAILED
    assert outcome.enters_batch


async def test_the_recording_is_addressable_before_anything_can_fail(
    tmp_path: Path,
) -> None:
    """The channel names the file with a UUID only it knows (spec 11.1).

    If the process died between that name and a later rename, the recording
    would sit in tmpfs under a name no retry looks for and no sweep recognises.
    """
    transcriber = FakeTranscriber()

    await ingestion(tmp_path=tmp_path, transcriber=transcriber).ingest(
        voice_item(), tenant_id=TENANT
    )

    [(audio, _)] = transcriber.calls
    assert audio == pending_audio_path(tmp_path / "retry", RAW_MESSAGE)


async def test_a_transcript_that_cannot_be_stored_leaves_the_audio_addressable(
    tmp_path: Path,
) -> None:
    """A database error must not orphan the recording under an unknown name."""

    class FailingTranscripts(FakeTranscripts):
        async def save(self, *, tenant_id: int, raw_message_id: int, transcript: str) -> None:
            raise RuntimeError("the database is unavailable")

    with pytest.raises(RuntimeError):
        await ingestion(tmp_path=tmp_path, transcripts=FailingTranscripts()).ingest(
            voice_item(), tenant_id=TENANT
        )

    assert pending_audio_path(tmp_path / "retry", RAW_MESSAGE).is_file()


async def test_a_successful_transcription_does_not_suppress_a_later_refusal(
    tmp_path: Path,
) -> None:
    """D-3. Recording a transcription is not recording an answer to the user.

    Transcribe, then let the persistence fail so the drain is retried, then
    have consent revoked in between. The consent branch must still queue the
    fixed reply — a marker set by the successful transcription would read as
    "already answered", swallow the only message the user was going to get,
    and drop the item from the batch on top of it.
    """
    transcripts = FakeTranscripts()
    consent = FakeConsent()
    replies = FakeReplies()

    first = await ingestion(
        tmp_path=tmp_path,
        transcriber=FakeTranscriber(Transcription("supino dez quilos")),
        transcripts=transcripts,
        consent=consent,
        replies=replies,
    ).ingest(voice_item(), tenant_id=TENANT)

    assert first.status is VoiceStatus.TRANSCRIBED
    assert transcripts.saved, "the transcription was not persisted"
    assert replies.texts == [], "a successful transcription answered the user"

    # The batch never got enqueued, so the same drain comes round again — and
    # the tenant revoked `workout_data` in the meantime.
    consent.granted = False
    second = await ingestion(
        tmp_path=tmp_path,
        transcripts=transcripts,
        consent=consent,
        replies=replies,
    ).ingest(voice_item(), tenant_id=TENANT)

    assert second.status is VoiceStatus.NO_CONSENT
    assert replies.texts == [load_prompt(CONSENT_PROMPT, prompt_dir=PROMPTS)]


async def test_a_fixed_reply_is_not_sent_twice_for_the_same_recording(
    tmp_path: Path,
) -> None:
    """A drain is retried until the batch is persisted and enqueued (§17.3)."""
    replies = FakeReplies()
    transcripts = FakeTranscripts(answered=True)

    outcome = await ingestion(
        tmp_path=tmp_path,
        transcriber=FakeTranscriber(Transcription("")),
        transcripts=transcripts,
        replies=replies,
    ).ingest(voice_item(), tenant_id=TENANT)

    assert outcome.status is VoiceStatus.INAUDIBLE
    assert replies.texts == []
    assert transcripts.answered == []


async def test_answering_a_recording_marks_it_after_the_enqueue(tmp_path: Path) -> None:
    """After, so a failure to queue is retried rather than marked and dropped."""
    transcripts = FakeTranscripts()

    await ingestion(
        tmp_path=tmp_path,
        transcriber=FakeTranscriber(Transcription("")),
        transcripts=transcripts,
        replies=FakeReplies(),
    ).ingest(voice_item(), tenant_id=TENANT)

    assert transcripts.answered == [(TENANT, RAW_MESSAGE)]


async def test_a_missing_row_is_not_marked_and_gets_no_reply(tmp_path: Path) -> None:
    """Without the row there is no identity to address (an LGPD deletion in flight)."""
    replies = FakeReplies()
    transcripts = FakeTranscripts(missing=True)

    outcome = await ingestion(
        tmp_path=tmp_path,
        transcriber=FakeTranscriber(Transcription("")),
        transcripts=transcripts,
        replies=replies,
    ).ingest(voice_item(), tenant_id=TENANT)

    assert outcome.status is VoiceStatus.INAUDIBLE
    assert replies.texts == []
    assert transcripts.answered == []


def test_a_missing_prompt_file_fails_when_the_service_is_built(tmp_path: Path) -> None:
    """A misspelled `prompt_file` is a deployment error, not an incomplete message."""
    with pytest.raises(OSError):
        VoiceIngestion(
            channel="telegram",
            downloader=FakeDownloader(tmp_path / "download"),
            transcriber=FakeTranscriber(),
            consent=FakeConsent(),
            transcripts=FakeTranscripts(),
            config=config(prompt_file="not_a_file.md"),
            prompt_dir=PROMPTS,
            retry_dir=tmp_path / "retry",
        )


async def test_a_transcript_that_no_longer_decrypts_is_treated_as_absent(
    tmp_path: Path,
) -> None:
    """B-1. A retired key must not wedge the tenant's whole pipeline.

    The drain is kept until the batch is persisted (§17.3), so a `load` that
    raises is re-raised on every later `flush_check` — and `_GATED_DRAIN` never
    renames the buffer while an orphan drain exists, so every later message of
    that tenant, text included, would be stuck in Redis for good. Treating an
    unreadable transcript as no transcript costs one re-transcription and keeps
    the pipeline moving (invariant 6).
    """
    downloader = FakeDownloader(tmp_path / "download")

    outcome = await ingestion(
        tmp_path=tmp_path,
        downloader=downloader,
        transcripts=FakeTranscripts(unreadable=True),
        transcriber=FakeTranscriber(Transcription("transcrito de novo")),
    ).ingest(voice_item(), tenant_id=TENANT)

    assert outcome.status is VoiceStatus.TRANSCRIBED
    assert outcome.text == "transcrito de novo"
    assert downloader.calls == [MEDIA_REF], "the unreadable transcript blocked the retry"


async def test_an_unreadable_transcript_with_nothing_left_to_fetch_is_incomplete(
    tmp_path: Path,
) -> None:
    """B-1, the other half: the item is still recorded, never discarded."""
    downloader = FakeDownloader(tmp_path / "download", fail=RuntimeError("gone"))

    outcome = await ingestion(
        tmp_path=tmp_path,
        downloader=downloader,
        transcripts=FakeTranscripts(unreadable=True),
    ).ingest(voice_item(), tenant_id=TENANT)

    assert outcome.status is VoiceStatus.FAILED
    assert outcome.enters_batch


async def test_a_revoked_tenant_never_has_its_words_decrypted(tmp_path: Path) -> None:
    """C-2. §11.3: the consent gate comes before anything reads the recording.

    Nothing is disclosed either way, but decrypting the words of a tenant who
    revoked consent, and only then noticing, is the wrong order.
    """
    transcripts = FakeTranscripts(transcript="ja transcrito")

    outcome = await ingestion(
        tmp_path=tmp_path,
        consent=FakeConsent(granted=False),
        transcripts=transcripts,
        replies=FakeReplies(),
    ).ingest(voice_item(), tenant_id=TENANT)

    assert outcome.status is VoiceStatus.NO_CONSENT
    assert transcripts.loaded == [RAW_MESSAGE], "the identity to reply to is still needed"
    assert transcripts.decrypted == [], "the transcript was decrypted before the gate"


async def test_a_consented_tenant_still_reuses_its_stored_transcript(
    tmp_path: Path,
) -> None:
    """The lazy read must not cost the retry saving of criterion 7."""
    downloader = FakeDownloader(tmp_path / "download")
    transcripts = FakeTranscripts(transcript="ja transcrito")

    outcome = await ingestion(
        tmp_path=tmp_path, downloader=downloader, transcripts=transcripts
    ).ingest(voice_item(), tenant_id=TENANT)

    assert outcome.text == "ja transcrito"
    assert transcripts.decrypted == [RAW_MESSAGE]
    assert downloader.calls == []


@pytest.mark.parametrize("duration_s", [300.9, 300.5, 300.1])
async def test_a_fractional_second_over_the_ceiling_is_still_over_it(
    tmp_path: Path, duration_s: float
) -> None:
    """C-1. Truncating to `int` let anything below 301 seconds through."""
    downloader = FakeDownloader(tmp_path / "download")

    outcome = await ingestion(tmp_path=tmp_path, downloader=downloader).ingest(
        voice_item(duration_s=duration_s), tenant_id=TENANT
    )

    assert outcome.status is VoiceStatus.TOO_LONG
    assert downloader.calls == []


async def test_a_recording_left_by_a_failed_adoption_is_still_swept(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """B-2. The download lands where the retention sweep looks, always.

    When the move into its `raw_message_id` name fails, the file keeps the
    channel's UUID name. It has to be inside the swept directory all the same,
    or §11.3's "and then delete it" never happens for it.
    """
    retry_dir = tmp_path / "retry"

    def refuse(path: Path) -> Path:
        raise OSError("the directory could not be prepared")

    monkeypatch.setattr("fittrack.services.stt.private_directory", refuse)
    transcriber = FakeTranscriber(SttTransportError("timeout"))

    await ingestion(
        tmp_path=tmp_path,
        downloader=FakeDownloader(retry_dir),
        transcriber=transcriber,
    ).ingest(voice_item(), tenant_id=TENANT)

    [(audio, _)] = transcriber.calls
    assert audio.parent == retry_dir, "the recording landed outside the swept directory"
    stale = time.time() - 7 * 3600
    os.utime(audio, (stale, stale))
    assert purge_stale_audio(retry_dir, max_age_s=6 * 3600) == 1
    assert not audio.exists()


def test_the_channel_writes_where_the_sweep_looks() -> None:
    """B-2. One directory for voice, so nothing is downloaded outside the sweep.

    A download that landed in `/tmp` itself could not be swept — the retention
    rule deletes by age, and `/tmp` holds other processes' files.
    """
    from fittrack.channels.telegram.adapter import DEFAULT_DOWNLOAD_DIR

    assert DEFAULT_DOWNLOAD_DIR == DEFAULT_RETRY_DIR
    assert Path("/tmp") != DEFAULT_RETRY_DIR


@pytest.mark.parametrize(
    ("suffix", "mime"),
    [
        pytest.param(".ogg", "audio/ogg", id="voice"),
        pytest.param(".mp4", "video/mp4", id="video note"),
        pytest.param(".m4a", "audio/mp4", id="audio m4a"),
        pytest.param(".mp3", "audio/mpeg", id="audio mp3"),
    ],
)
async def test_the_container_the_channel_sent_survives_the_upload(
    tmp_path: Path, suffix: str, mime: str
) -> None:
    """D-4. `audio` and `video_note` are voice too, and are not ogg (spec 11).

    A provider that picks its decoder from the multipart filename refuses a
    recording it supports, and two inbound kinds the parser accepts become
    `incomplete` for a reason nothing in the logs would explain.
    """
    seen: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["body"] = request.content.decode("utf-8", "replace")
        return httpx.Response(200, json={"text": "ok"})

    audio = tmp_path / f"recording{suffix}"
    audio.write_bytes(b"bytes")

    async with groq(handler) as transcriber:
        await transcriber.transcribe(audio, prompt="p")

    assert f'filename="audio{suffix}"' in seen["body"]
    assert mime in seen["body"]


@pytest.mark.parametrize("suffix", [".mp4", ".m4a"])
async def test_the_container_survives_the_retention_cycle(tmp_path: Path, suffix: str) -> None:
    """The retry path renamed every format to `.ogg` on its way to the buffer."""
    downloader = FakeDownloader(tmp_path / "download", suffix=suffix)
    transcriber = FakeTranscriber(SttTransportError("timeout"))

    await ingestion(tmp_path=tmp_path, downloader=downloader, transcriber=transcriber).ingest(
        voice_item(), tenant_id=TENANT
    )

    [(first, _)] = transcriber.calls
    assert first.suffix == suffix
    assert first.is_file(), "the recording was not kept for the retry"

    # And the retry finds it under whatever name it kept.
    retried = FakeTranscriber(Transcription("na segunda vez"))
    outcome = await ingestion(tmp_path=tmp_path, downloader=downloader, transcriber=retried).ingest(
        voice_item(), tenant_id=TENANT
    )

    assert outcome.text == "na segunda vez"
    assert downloader.calls == [MEDIA_REF], "the retry downloaded it again"
    [(second, _)] = retried.calls
    assert second.suffix == suffix


def test_an_unknown_container_falls_back_to_the_channel_default(tmp_path: Path) -> None:
    """A suffix is channel-supplied text, so it is chosen from a known set.

    Telegram's `file_path` decides the local name, and it is not ours. Anything
    outside the formats the provider documents becomes the ogg/opus that both
    channels actually deliver for a voice note (spec 11.1).
    """
    assert upload_format(Path("x.ogg")) == (".ogg", "audio/ogg")
    assert upload_format(Path("x.MP3")) == (".mp3", "audio/mpeg")
    assert upload_format(Path("x.exe")) == (".ogg", "audio/ogg")
    assert upload_format(Path("x")) == (".ogg", "audio/ogg")


# --------------------------------------------------------------------------- #
# Retention needs its own consent (spec 11.3, E-1)
# --------------------------------------------------------------------------- #


async def test_a_failed_transcription_keeps_the_audio_only_with_model_training(
    tmp_path: Path,
) -> None:
    """§11.3: "retenção só com `model_training`". The six hour buffer is retention."""
    consent = FakeConsent(granted=True, retention=True)

    await ingestion(
        tmp_path=tmp_path,
        consent=consent,
        transcriber=FakeTranscriber(SttTransportError("timeout")),
    ).ingest(voice_item(), tenant_id=TENANT)

    assert pending_audio_path(tmp_path / "retry", RAW_MESSAGE).is_file()
    assert (TENANT, MODEL_TRAINING_CONSENT) in consent.calls


@pytest.mark.parametrize("retention", [False], ids=["revoked or never granted"])
async def test_without_model_training_a_failed_recording_is_deleted_at_once(
    tmp_path: Path, retention: bool
) -> None:
    """Keeping somebody's voice for six hours is retention, and needs its own yes.

    The item is still recorded — that is `raw_message.payload`, and it does not
    depend on this consent (invariant 6). Only the audio goes.
    """
    transcriber = FakeTranscriber(SttTransportError("timeout"))

    outcome = await ingestion(
        tmp_path=tmp_path,
        consent=FakeConsent(granted=True, retention=retention),
        transcriber=transcriber,
    ).ingest(voice_item(), tenant_id=TENANT)

    assert outcome.status is VoiceStatus.FAILED
    assert outcome.enters_batch, "the item was discarded, not just the audio"
    [(audio, _)] = transcriber.calls
    assert not audio.exists()
    assert list((tmp_path / "retry").glob("*")) == []


async def test_a_retention_refusal_still_marks_the_item_incomplete(tmp_path: Path) -> None:
    [resolved] = await ingestion(
        tmp_path=tmp_path,
        consent=FakeConsent(granted=True, retention=False),
        transcriber=FakeTranscriber(SttTransportError("timeout")),
    ).resolve([voice_item()], tenant_id=TENANT)

    assert resolved["status"] == "incomplete"
    assert resolved["was_audio"] is True


# --------------------------------------------------------------------------- #
# The burst fits inside the job that runs it (E-3)
# --------------------------------------------------------------------------- #


async def test_a_slow_burst_stops_transcribing_before_the_job_is_killed(
    tmp_path: Path,
) -> None:
    """ARQ cancels at `job_timeout` and does not retry: `TimeoutError` is not
    `CancelledError`, so the drain is left orphaned and nothing re-drives it
    until the user sends another message. The voice step has to fit.
    """

    class SlowTranscriber:
        def __init__(self) -> None:
            self.calls = 0

        async def transcribe(self, audio: Path, *, prompt: str) -> Transcription:
            self.calls += 1
            await asyncio.sleep(0.20)
            return Transcription("demorou")

    transcriber = SlowTranscriber()
    items = [voice_item(raw_message_id=raw_id) for raw_id in (11, 12, 13)]

    resolved = await ingestion(
        tmp_path=tmp_path, transcriber=transcriber, stt=config(timeout_s=1), budget_s=0.30
    ).resolve(items, tenant_id=TENANT)

    assert transcriber.calls < 3, "the whole burst was transcribed past the budget"
    assert len(resolved) == 3, "an item was dropped instead of recorded"
    assert resolved[-1]["status"] == "incomplete"


async def test_an_item_that_outruns_the_budget_is_recorded_not_lost(
    tmp_path: Path,
) -> None:
    """A single call that never returns must not take the drain with it."""

    class HangingTranscriber:
        async def transcribe(self, audio: Path, *, prompt: str) -> Transcription:
            await asyncio.sleep(30)
            raise AssertionError("the budget did not cut this short")

    resolved = await ingestion(
        tmp_path=tmp_path, transcriber=HangingTranscriber(), budget_s=0.15
    ).resolve([voice_item()], tenant_id=TENANT)

    assert len(resolved) == 1
    assert resolved[0]["status"] == "incomplete"
    assert resolved[0]["was_audio"] is True


async def test_a_burst_within_the_budget_is_transcribed_in_full(tmp_path: Path) -> None:
    transcriber = FakeTranscriber(Transcription("um"), Transcription("dois"), Transcription("tres"))
    items = [voice_item(raw_message_id=raw_id) for raw_id in (21, 22, 23)]

    resolved = await ingestion(tmp_path=tmp_path, transcriber=transcriber, budget_s=30).resolve(
        items, tenant_id=TENANT
    )

    assert [item["text"] for item in resolved] == ["um", "dois", "tres"]


# --------------------------------------------------------------------------- #
# The credential is usable, or the deployment does not start (E-6)
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "credential",
    ["gsk-key\n", " gsk-key", "gsk-key ", "gsk-key\t"],
    ids=["newline", "leading", "trailing", "tab"],
)
def test_a_credential_wearing_whitespace_is_refused_at_startup(credential: str) -> None:
    """A header value with a newline is refused by the HTTP stack itself.

    The broad handler then turns that into `incomplete` for *every* recording,
    which is an incident. The channel registry already refuses padding rather
    than trimming it, for the same reason: the deployment is what needs fixing.
    """
    with pytest.raises(ValueError, match="whitespace"):
        GroqTranscriber(http=httpx.AsyncClient(), api_key=SecretStr(credential), config=config())


def test_a_usable_credential_is_accepted() -> None:
    transcriber = GroqTranscriber(
        http=httpx.AsyncClient(), api_key=SecretStr("gsk-key"), config=config()
    )

    assert transcriber is not None


# --------------------------------------------------------------------------- #
# The fixed reply is idempotent by construction (E-5)
# --------------------------------------------------------------------------- #


async def test_the_same_refusal_reuses_one_response_group(tmp_path: Path) -> None:
    """The marker and the enqueue commit separately, so the enqueue itself has
    to be idempotent: a group id derived from the row makes the retry's insert
    a no-op instead of a second bubble at the user.
    """
    replies = FakeReplies()
    first = ingestion(
        tmp_path=tmp_path,
        transcriber=FakeTranscriber(Transcription("")),
        transcripts=FakeTranscripts(),
        replies=replies,
    )
    await first.ingest(voice_item(), tenant_id=TENANT)

    # The marker write failed, so the retry sees `answered=False` again.
    second = ingestion(
        tmp_path=tmp_path,
        transcriber=FakeTranscriber(Transcription("")),
        transcripts=FakeTranscripts(),
        replies=replies,
    )
    await second.ingest(voice_item(), tenant_id=TENANT)

    assert len(replies.groups) == 2
    assert replies.groups[0] == replies.groups[1], "the retry opened a second group"


async def test_two_different_refusals_do_not_share_a_group(tmp_path: Path) -> None:
    replies = FakeReplies()

    await ingestion(
        tmp_path=tmp_path,
        transcriber=FakeTranscriber(Transcription("")),
        replies=replies,
    ).ingest(voice_item(), tenant_id=TENANT)
    await ingestion(tmp_path=tmp_path, consent=FakeConsent(granted=False), replies=replies).ingest(
        voice_item(), tenant_id=TENANT
    )

    assert replies.groups[0] != replies.groups[1]


# --------------------------------------------------------------------------- #
# The single output path (E-4, invariant 2)
# --------------------------------------------------------------------------- #


def test_the_service_never_writes_the_outbound_queue_itself() -> None:
    """Invariant 2 and ADR-0009: one path out, and this is not it.

    The fixed replies of §11.3 exist before `voice_agent` and `deliver` do, so
    the service decides them — but it hands a block to the outbound service of
    S02-T06 and never touches the queue, the channel API, or a delivery.
    """
    source = (ROOT / "src" / "fittrack" / "services" / "stt.py").read_text(encoding="utf-8")
    statements = [
        line
        for line in source.splitlines()
        if "outbound" in line.lower() and not line.lstrip().startswith("#")
    ]

    # Prose about the queue is fine; a statement that writes it is not.
    assert not [line for line in statements if "INSERT" in line or "UPDATE" in line]
    assert "PostgresOutboundQueueStore" not in source
    assert "from fittrack.services.outbound import" not in source


# --------------------------------------------------------------------------- #
# Retention (spec 11.3)
# --------------------------------------------------------------------------- #


def test_audio_older_than_the_retention_window_is_purged(tmp_path: Path) -> None:
    directory = tmp_path / "retry"
    directory.mkdir()
    stale, fresh = directory / "1.ogg", directory / "2.ogg"
    stale.write_bytes(b"old")
    fresh.write_bytes(b"new")
    now = time.time()
    os.utime(stale, (now - 7 * 3600, now - 7 * 3600))

    removed = purge_stale_audio(directory, max_age_s=6 * 3600, now=now)

    assert removed == 1
    assert not stale.exists()
    assert fresh.is_file()


def test_purging_a_directory_that_does_not_exist_is_not_an_error(tmp_path: Path) -> None:
    assert purge_stale_audio(tmp_path / "absent", max_age_s=1) == 0


async def test_ingestion_purges_what_the_retention_window_has_expired(tmp_path: Path) -> None:
    """The sweep is opportunistic: no scheduler is needed for the rule to hold."""
    directory = tmp_path / "retry"
    directory.mkdir()
    expired = pending_audio_path(directory, 5555)
    expired.write_bytes(b"forgotten")
    stale = time.time() - 7 * 3600
    os.utime(expired, (stale, stale))

    await ingestion(tmp_path=tmp_path).ingest(voice_item(), tenant_id=TENANT)

    assert not expired.exists()


# --------------------------------------------------------------------------- #
# Duration ceiling (spec 11.3)
# --------------------------------------------------------------------------- #


async def test_a_recording_past_the_ceiling_is_refused_before_any_download(
    tmp_path: Path,
) -> None:
    downloader = FakeDownloader(tmp_path / "download")
    transcriber = FakeTranscriber()
    replies = FakeReplies()

    outcome = await ingestion(
        tmp_path=tmp_path, downloader=downloader, transcriber=transcriber, replies=replies
    ).ingest(voice_item(duration_s=301), tenant_id=TENANT)

    assert outcome.status is VoiceStatus.TOO_LONG
    assert not outcome.enters_batch
    assert downloader.calls == []
    assert transcriber.calls == []
    assert replies.texts == [load_prompt(TOO_LONG_PROMPT, prompt_dir=PROMPTS)]


async def test_the_ceiling_itself_is_accepted(tmp_path: Path) -> None:
    outcome = await ingestion(tmp_path=tmp_path).ingest(
        voice_item(duration_s=300), tenant_id=TENANT
    )
    assert outcome.status is VoiceStatus.TRANSCRIBED


async def test_a_voice_item_with_nothing_to_fetch_is_refused(tmp_path: Path) -> None:
    """The parser drops the media reference past the ceiling (ADR-0006)."""
    replies = FakeReplies()

    outcome = await ingestion(tmp_path=tmp_path, replies=replies).ingest(
        voice_item(media_ref=None, duration_s=None), tenant_id=TENANT
    )

    assert outcome.status is VoiceStatus.TOO_LONG
    assert replies.texts == [load_prompt(TOO_LONG_PROMPT, prompt_dir=PROMPTS)]


# --------------------------------------------------------------------------- #
# Consent (spec 11.3, 19.5)
# --------------------------------------------------------------------------- #


async def test_without_workout_data_consent_nothing_is_transcribed(tmp_path: Path) -> None:
    downloader = FakeDownloader(tmp_path / "download")
    transcriber = FakeTranscriber()
    replies = FakeReplies()

    outcome = await ingestion(
        tmp_path=tmp_path,
        downloader=downloader,
        transcriber=transcriber,
        consent=FakeConsent(granted=False),
        replies=replies,
    ).ingest(voice_item(), tenant_id=TENANT)

    assert outcome.status is VoiceStatus.NO_CONSENT
    assert not outcome.enters_batch
    assert downloader.calls == []
    assert transcriber.calls == []
    assert replies.texts == [load_prompt(CONSENT_PROMPT, prompt_dir=PROMPTS)]


async def test_a_stored_transcript_is_not_replayed_without_consent(tmp_path: Path) -> None:
    """Consent is checked on every use, not only on the call that produced it."""
    outcome = await ingestion(
        tmp_path=tmp_path,
        consent=FakeConsent(granted=False),
        transcripts=FakeTranscripts(transcript="ja transcrito"),
    ).ingest(voice_item(), tenant_id=TENANT)

    assert outcome.status is VoiceStatus.NO_CONSENT


async def test_the_gate_asks_for_the_workout_data_consent(tmp_path: Path) -> None:
    consent = FakeConsent()
    await ingestion(tmp_path=tmp_path, consent=consent).ingest(voice_item(), tenant_id=TENANT)

    assert consent.calls == [(TENANT, WORKOUT_DATA_CONSENT)]
    assert WORKOUT_DATA_CONSENT == "workout_data"


# --------------------------------------------------------------------------- #
# Redaction (spec 20.6, invariant 10)
# --------------------------------------------------------------------------- #


def rendered(caplog: pytest.LogCaptureFixture) -> str:
    """Every captured record, message and structured extras alike."""
    parts: list[str] = []
    for record in caplog.records:
        parts.append(record.getMessage())
        parts.extend(
            f"{name}={value!r}"
            for name, value in record.__dict__.items()
            if name not in _STANDARD_RECORD_FIELDS
        )
    return "\n".join(parts)


async def test_no_log_line_carries_the_media_reference_or_the_local_path(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """The Telegram file URL is the bot token; the `file_id` is what fetches it."""
    transcriber = FakeTranscriber()
    with caplog.at_level(logging.DEBUG):
        await ingestion(tmp_path=tmp_path, transcriber=transcriber).ingest(
            voice_item(), tenant_id=TENANT
        )

    log = rendered(caplog)
    assert MEDIA_REF not in log
    [(audio, _)] = transcriber.calls
    assert audio.name not in log


async def test_a_failure_log_carries_neither_the_reference_nor_the_kept_path(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.DEBUG):
        await ingestion(
            tmp_path=tmp_path, transcriber=FakeTranscriber(SttTransportError("timeout"))
        ).ingest(voice_item(), tenant_id=TENANT)

    log = rendered(caplog)
    assert MEDIA_REF not in log
    assert pending_audio_path(tmp_path / "retry", RAW_MESSAGE).name not in log


async def test_no_log_line_carries_the_transcribed_text(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """Spec 20.6: the user's words belong in Langfuse, never in a log."""
    secret = "fiz supino com quarenta quilos"
    with caplog.at_level(logging.DEBUG):
        await ingestion(
            tmp_path=tmp_path, transcriber=FakeTranscriber(Transcription(secret))
        ).ingest(voice_item(), tenant_id=TENANT)

    assert secret not in rendered(caplog)


# --------------------------------------------------------------------------- #
# The transcript column (spec 22.2)
# --------------------------------------------------------------------------- #


@pytest.fixture
def cipher() -> ColumnCipher:
    return ColumnCipher(Keyring(keys={1: bytes(range(32))}, active_version=1))


def test_a_transcript_round_trips_through_its_own_associated_data(
    cipher: ColumnCipher,
) -> None:
    blob = encrypt_transcript(cipher, tenant_id=TENANT, raw_message_id=RAW_MESSAGE, text="oi")

    assert blob != b"oi"
    assert (
        decrypt_transcript(cipher, tenant_id=TENANT, raw_message_id=RAW_MESSAGE, blob=blob) == "oi"
    )


@pytest.mark.parametrize(
    ("tenant_id", "raw_message_id"),
    [(TENANT + 1, RAW_MESSAGE), (TENANT, RAW_MESSAGE + 1)],
    ids=["another tenant", "another row"],
)
def test_a_transcript_does_not_decrypt_in_another_row_or_tenant(
    cipher: ColumnCipher, tenant_id: int, raw_message_id: int
) -> None:
    """Spec 22.2: an intact blob moved elsewhere must fail to authenticate."""
    blob = encrypt_transcript(cipher, tenant_id=TENANT, raw_message_id=RAW_MESSAGE, text="oi")

    with pytest.raises(DecryptionError):
        decrypt_transcript(cipher, tenant_id=tenant_id, raw_message_id=raw_message_id, blob=blob)


# --------------------------------------------------------------------------- #
# The batch integration point (S02-T05)
# --------------------------------------------------------------------------- #


async def test_resolve_marks_voice_items_and_keeps_arrival_order(tmp_path: Path) -> None:
    items: list[dict[str, object]] = [
        text_item("primeiro", raw_message_id=1),
        voice_item(raw_message_id=2),
        text_item("terceiro", raw_message_id=3),
    ]

    resolved = await ingestion(
        tmp_path=tmp_path, transcriber=FakeTranscriber(Transcription("do meio"))
    ).resolve(items, tenant_id=TENANT)

    assert [item["text"] for item in resolved] == ["primeiro", "do meio", "terceiro"]
    assert resolved[1]["was_audio"] is True
    assert "was_audio" not in resolved[0]


async def test_a_resolved_voice_item_carries_no_channel_reference(tmp_path: Path) -> None:
    """The `file_id` fetches the recording and has no reader downstream (§20.6)."""
    [resolved] = await ingestion(
        tmp_path=tmp_path, transcriber=FakeTranscriber(Transcription("transcrito"))
    ).resolve([voice_item()], tenant_id=TENANT)

    assert resolved["media_ref"] is None
    assert MEDIA_REF not in json.dumps(resolved)


async def test_resolve_flags_a_failed_transcription_as_incomplete(tmp_path: Path) -> None:
    """Invariant 6: the item is recorded, outside every analysis."""
    [resolved] = await ingestion(
        tmp_path=tmp_path, transcriber=FakeTranscriber(SttTransportError("timeout"))
    ).resolve([voice_item()], tenant_id=TENANT)

    assert resolved["was_audio"] is True
    assert resolved["text"] == ""
    assert resolved["status"] == "incomplete"


async def test_resolve_drops_the_items_that_were_answered_instead(tmp_path: Path) -> None:
    resolved = await ingestion(
        tmp_path=tmp_path, transcriber=FakeTranscriber(Transcription(""))
    ).resolve([voice_item(), text_item("mas este fica")], tenant_id=TENANT)

    assert [item["text"] for item in resolved] == ["mas este fica"]


async def test_resolve_leaves_a_burst_without_voice_untouched(tmp_path: Path) -> None:
    downloader = FakeDownloader(tmp_path / "download")
    items: list[dict[str, object]] = [text_item("so texto")]

    resolved = await ingestion(tmp_path=tmp_path, downloader=downloader).resolve(
        items, tenant_id=TENANT
    )

    assert resolved == items
    assert downloader.calls == []


# --------------------------------------------------------------------------- #
# GroqTranscriber (spec 11.1)
# --------------------------------------------------------------------------- #


def audio_file(tmp_path: Path, name: str = "voice.ogg") -> Path:
    path = tmp_path / name
    path.write_bytes(b"ogg/opus bytes")
    return path


@asynccontextmanager
async def groq(
    handler: Callable[[httpx.Request], httpx.Response], *, stt: SttConfig | None = None
) -> AsyncIterator[GroqTranscriber]:
    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    try:
        yield GroqTranscriber(
            http=http, api_key=SecretStr("gsk-not-a-real-key"), config=stt or config()
        )
    finally:
        await http.aclose()


def answering(payload: object, *, status: int = 200) -> Callable[[httpx.Request], httpx.Response]:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, json=payload)

    return handler


async def test_the_request_carries_the_configured_model_language_and_format(
    tmp_path: Path,
) -> None:
    seen: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["auth"] = request.headers.get("authorization", "")
        seen["body"] = request.content.decode("utf-8", "replace")
        return httpx.Response(200, json={"text": "ok", "segments": []})

    async with groq(handler) as transcriber:
        await transcriber.transcribe(audio_file(tmp_path), prompt="supino, agachamento")

    assert seen["url"] == GROQ_TRANSCRIPTIONS_URL
    assert seen["auth"] == "Bearer gsk-not-a-real-key"
    for expected in ("test-stt", "verbose_json", "supino, agachamento"):
        assert expected in seen["body"]
    assert 'name="language"' in seen["body"]
    assert "\r\npt\r\n" in seen["body"]


async def test_the_uploaded_filename_reveals_nothing_about_the_download(
    tmp_path: Path,
) -> None:
    """The local name comes from the channel download; only the suffix matters."""
    seen: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["body"] = request.content.decode("utf-8", "replace")
        return httpx.Response(200, json={"text": "ok"})

    async with groq(handler) as transcriber:
        await transcriber.transcribe(audio_file(tmp_path, "d3adb33f-shaped.ogg"), prompt="p")

    assert "d3adb33f" not in seen["body"]
    assert 'filename="audio.ogg"' in seen["body"]


async def test_the_no_speech_probability_is_averaged_over_the_segments(
    tmp_path: Path,
) -> None:
    """A single silent gap must not discard a whole recording."""
    payload = {
        "text": "alo",
        "duration": 3.5,
        "segments": [{"no_speech_prob": 0.1}, {"no_speech_prob": 0.9}],
    }

    async with groq(answering(payload)) as transcriber:
        result = await transcriber.transcribe(audio_file(tmp_path), prompt="p")

    assert result.text == "alo"
    assert result.no_speech_prob == pytest.approx(0.5)
    assert result.duration_s == pytest.approx(3.5)


async def test_a_response_without_segments_carries_no_probability(tmp_path: Path) -> None:
    async with groq(answering({"text": "alo"})) as transcriber:
        result = await transcriber.transcribe(audio_file(tmp_path), prompt="p")

    assert result.no_speech_prob is None


@pytest.mark.parametrize("status", [500, 502, 503])
async def test_a_server_failure_is_retryable(tmp_path: Path, status: int) -> None:
    async with groq(answering({"error": "unwell"}, status=status)) as transcriber:
        with pytest.raises(SttTransportError):
            await transcriber.transcribe(audio_file(tmp_path), prompt="p")


@pytest.mark.parametrize("status", [400, 401, 413])
async def test_a_refusal_is_not_retryable(tmp_path: Path, status: int) -> None:
    async with groq(answering({"error": {"message": "no"}}, status=status)) as transcriber:
        with pytest.raises(SttRefusedError):
            await transcriber.transcribe(audio_file(tmp_path), prompt="p")


async def test_a_rate_limit_is_retryable(tmp_path: Path) -> None:
    """429 is transient by nature: the ladder of 18.4 exists for exactly this."""
    async with groq(answering({"error": {"message": "slow down"}}, status=429)) as transcriber:
        with pytest.raises(SttTransportError):
            await transcriber.transcribe(audio_file(tmp_path), prompt="p")


async def test_a_timeout_is_retryable(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("too slow", request=request)

    async with groq(handler) as transcriber:
        with pytest.raises(SttTransportError):
            await transcriber.transcribe(audio_file(tmp_path), prompt="p")


async def test_a_body_that_is_not_json_is_an_error(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="<html>nope</html>")

    async with groq(handler) as transcriber:
        with pytest.raises(SttError):
            await transcriber.transcribe(audio_file(tmp_path), prompt="p")


async def test_a_response_whose_shape_is_wrong_is_an_error(tmp_path: Path) -> None:
    """Pydantic is the source of truth, not the provider's promise."""
    async with groq(answering({"text": {"not": "a string"}})) as transcriber:
        with pytest.raises(SttError):
            await transcriber.transcribe(audio_file(tmp_path), prompt="p")


async def test_no_failure_names_the_credential_or_the_local_path(tmp_path: Path) -> None:
    async with groq(answering({"error": "unwell"}, status=500)) as transcriber:
        with pytest.raises(SttError) as raised:
            await transcriber.transcribe(audio_file(tmp_path, "b33f-shaped.ogg"), prompt="p")

    message = str(raised.value)
    assert "gsk-not-a-real-key" not in message
    assert "b33f" not in message


async def test_a_missing_audio_file_is_an_error_and_not_a_crash(tmp_path: Path) -> None:
    async with groq(answering({"text": "ok"})) as transcriber:
        with pytest.raises(SttError):
            await transcriber.transcribe(tmp_path / "gone.ogg", prompt="p")


# --------------------------------------------------------------------------- #
# O_NOFOLLOW (spec 11.1, criterion 4)
# --------------------------------------------------------------------------- #


def test_a_new_file_is_created_privately(tmp_path: Path) -> None:
    path = tmp_path / "fresh.ogg"

    with create_private(path) as sink:
        sink.write(b"bytes")

    assert path.read_bytes() == b"bytes"
    assert path.stat().st_mode & 0o777 == 0o600


def test_creating_over_an_existing_file_is_refused(tmp_path: Path) -> None:
    path = tmp_path / "taken.ogg"
    path.write_bytes(b"mine")

    with pytest.raises(FileExistsError), create_private(path):
        pass


@pytest.mark.skipif(not hasattr(os, "O_NOFOLLOW"), reason="the platform has no O_NOFOLLOW")
def test_a_symlinked_destination_is_refused(tmp_path: Path) -> None:
    """tmpfs is shared; a planted symlink must not redirect somebody's voice."""
    target = tmp_path / "elsewhere"
    target.write_bytes(b"")
    link = tmp_path / "link.ogg"
    link.symlink_to(target)

    with pytest.raises(OSError), create_private(link):
        pass
    with pytest.raises(OSError), open_no_follow(link):
        pass


@pytest.mark.skipif(not hasattr(os, "O_NOFOLLOW"), reason="the platform has no O_NOFOLLOW")
def test_a_symlinked_directory_is_refused(tmp_path: Path) -> None:
    """`/tmp` is shared and the retry directory's name is predictable.

    `mkdir(exist_ok=True)` accepts a symlink *to* a directory, and everything
    written afterwards — and everything the sweep deletes — would land in the
    target.
    """
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    link = tmp_path / "retry"
    link.symlink_to(elsewhere, target_is_directory=True)

    with pytest.raises(NotADirectoryError):
        private_directory(link)


def test_a_private_directory_is_created_owner_only(tmp_path: Path) -> None:
    directory = private_directory(tmp_path / "retry")

    assert directory.is_dir()
    assert directory.stat().st_mode & 0o777 == 0o700
    # Idempotent: every ingestion calls it.
    assert private_directory(directory) == directory


def test_the_sweep_refuses_to_traverse_a_symlinked_directory(tmp_path: Path) -> None:
    """Otherwise the retention rule deletes files in somebody else's directory."""
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    victim = elsewhere / "1.ogg"
    victim.write_bytes(b"not ours")
    stale = time.time() - 7 * 3600
    os.utime(victim, (stale, stale))
    link = tmp_path / "retry"
    link.symlink_to(elsewhere, target_is_directory=True)

    assert purge_stale_audio(link, max_age_s=6 * 3600) == 0
    assert victim.is_file()


def test_reading_a_regular_file_is_allowed(tmp_path: Path) -> None:
    path = tmp_path / "plain.ogg"
    path.write_bytes(b"ogg")

    with open_no_follow(path) as source:
        assert source.read() == b"ogg"


def test_the_downloaded_destination_is_opened_without_following_links() -> None:
    """The channel client writes the file, so the hardening belongs where it opens."""
    source = (ROOT / "src" / "fittrack" / "channels" / "telegram" / "client.py").read_text(
        encoding="utf-8"
    )

    assert "create_private" in source
    assert 'destination.open("wb")' not in source


# --------------------------------------------------------------------------- #
# The envelope carries what the ceiling needs (ADR-0006)
# --------------------------------------------------------------------------- #


def test_the_envelope_declares_the_recording_duration() -> None:
    message = InboundMessage(
        channel="telegram",
        external_id="chat",
        channel_message_id="9",
        kind="voice",
        text=None,
        media_ref=None,
        button_payload=None,
        sent_at=datetime(2026, 9, 2, tzinfo=UTC),
        raw={},
        media_duration_s=612,
    )

    envelope = _envelope(
        identity=IngressIdentity(tenant_id=TENANT, identity_id=IDENTITY, external_id_hash=b"\x01"),
        message=message,
        raw_message_id=RAW_MESSAGE,
    )

    assert envelope["duration_s"] == 612
    # It has to survive Redis: the buffer stores JSON.
    assert json.loads(json.dumps(envelope))["duration_s"] == 612
