"""Backend와 주고받는 제어 메시지의 데이터 규약을 정의한다."""

from datetime import datetime
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, TypeAdapter
from pydantic.alias_generators import to_camel


class Message(BaseModel):
    model_config = ConfigDict(
        alias_generator=to_camel, validate_by_name=True, extra="forbid", strict=True
    )


Identifier = Annotated[
    str,
    StringConstraints(
        min_length=1,
        max_length=128,
        pattern=r"^[A-Za-z0-9._-]+$",
    ),
]


class SessionStartPayload(Message):
    audio_format: Literal["webm_opus", "mp4_aac"]


class SessionStart(Message):
    type: Literal["session.start"]
    request_id: Identifier
    meeting_id: Identifier
    recording_session_id: Identifier
    payload: SessionStartPayload


class SessionReadyPayload(Message):
    status: Literal["READY"]
    input_audio_format: Literal["webm_opus", "mp4_aac"]
    output_audio_format: Literal["pcm_s16le"]
    output_sample_rate_hz: Literal[16000]
    output_channels: Literal[1]


class SessionReady(Message):
    type: Literal["session.ready"]
    request_id: Identifier
    meeting_id: Identifier
    recording_session_id: Identifier
    payload: SessionReadyPayload


class TranscriptCommittedPayload(Message):
    sequence_number: Annotated[int, Field(ge=0)]
    content: str
    started_at_ms: Annotated[int, Field(ge=0)]
    ended_at_ms: Annotated[int, Field(ge=0)]
    recognized_at: datetime


class TranscriptCommitted(Message):
    type: Literal["transcript.committed"]
    meeting_id: Identifier
    recording_session_id: Identifier
    payload: TranscriptCommittedPayload


class AudioMetaPayload(Message):
    sequence: Annotated[int, Field(ge=0)]


class AudioMeta(Message):
    type: Literal["audio.meta"]
    payload: AudioMetaPayload


class SessionPause(Message):
    type: Literal["session.pause"]
    request_id: Identifier


class SessionPausedPayload(Message):
    status: Literal["PAUSED"]


class SessionPaused(Message):
    type: Literal["session.paused"]
    request_id: Identifier
    payload: SessionPausedPayload


class SessionResume(Message):
    type: Literal["session.resume"]
    request_id: Identifier


class SessionResumedPayload(Message):
    status: Literal["READY"]


class SessionResumed(Message):
    type: Literal["session.resumed"]
    request_id: Identifier
    payload: SessionResumedPayload


class SessionStop(Message):
    type: Literal["session.stop"]
    request_id: Identifier


class SessionEndedPayload(Message):
    status: Literal["ENDED"]
    last_sequence: Annotated[int, Field(ge=0)] | None
    audio_duration_ms: Annotated[int, Field(ge=0)]


class SessionEnded(Message):
    type: Literal["session.ended"]
    request_id: Identifier
    meeting_id: Identifier
    recording_session_id: Identifier
    payload: SessionEndedPayload


class SessionErrorPayload(Message):
    code: Literal[
        "invalid_message",
        "invalid_state",
        "invalid_sequence",
        "unexpected_binary",
        "expected_binary",
        "message_too_large",
        "audio_decode_failed",
        "provider_unavailable",
        "processing_timeout",
    ]
    message: str
    retryable: Literal[True, False]


class SessionError(Message):
    type: Literal["error"]
    request_id: Identifier | None
    payload: SessionErrorPayload


ClientEvent = Annotated[
    SessionStart | AudioMeta | SessionPause | SessionResume | SessionStop,
    Field(discriminator="type"),
]

client_event_adapter = TypeAdapter(ClientEvent)
