"""Backend와 주고받는 제어 메시지의 데이터 규약을 정의한다."""

from datetime import datetime
from typing import Annotated, Literal

from pydantic import Field, TypeAdapter

from meety_ai.core.schema import Identifier, MeetingId, Message, NonNegativeInt, RecordingSessionId

AudioFormat = Literal["webm_opus", "mp4_aac"]


class SessionStart(Message):
    type: Literal["session.start"]
    request_id: Identifier
    # ponytail: BE 문자열 ID 송신이 변경되면 strict=False를 제거한다(AI #77).
    meeting_id: MeetingId = Field(strict=False)
    recording_session_id: RecordingSessionId = Field(strict=False)


class SessionReadyPayload(Message):
    status: Literal["READY"]
    output_audio_format: Literal["pcm_s16le"]
    output_sample_rate_hz: Literal[16000]
    output_channels: Literal[1]


class SessionReady(Message):
    type: Literal["session.ready"]
    request_id: Identifier
    meeting_id: MeetingId
    recording_session_id: RecordingSessionId
    payload: SessionReadyPayload


class DecoderResetPayload(Message):
    audio_format: AudioFormat


class DecoderReset(Message):
    type: Literal["decoder.reset"]
    request_id: Identifier
    payload: DecoderResetPayload


class DecoderReadyPayload(Message):
    status: Literal["READY"]
    input_audio_format: AudioFormat


class DecoderReady(Message):
    type: Literal["decoder.ready"]
    request_id: Identifier
    payload: DecoderReadyPayload


class TranscriptCommittedPayload(Message):
    sequence_number: NonNegativeInt
    content: str
    started_at_ms: NonNegativeInt
    ended_at_ms: NonNegativeInt
    recognized_at: datetime


class TranscriptCommitted(Message):
    type: Literal["transcript.committed"]
    meeting_id: MeetingId
    recording_session_id: RecordingSessionId
    payload: TranscriptCommittedPayload


class AudioMetaPayload(Message):
    sequence: NonNegativeInt


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
    last_sequence: NonNegativeInt | None
    audio_duration_ms: NonNegativeInt


class SessionEnded(Message):
    type: Literal["session.ended"]
    request_id: Identifier
    meeting_id: MeetingId
    recording_session_id: RecordingSessionId
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
    SessionStart | DecoderReset | AudioMeta | SessionPause | SessionResume | SessionStop,
    Field(discriminator="type"),
]

client_event_adapter = TypeAdapter(ClientEvent)
