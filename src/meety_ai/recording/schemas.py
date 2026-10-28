"""Backend와 주고받는 제어 메시지의 데이터 규약을 정의한다."""

from datetime import datetime
from typing import Annotated, Literal

from pydantic import Field, TypeAdapter, model_validator

from meety_ai.core.schemas import Identifier, MeetingId, Message, NonNegativeInt, RecordingSessionId
from meety_ai.stt.schemas import SpeakerEmbedding, SpeakerMapping


class SessionParticipant(Message):
    team_member_id: NonNegativeInt | None
    name: str
    # 사전 녹음이 없으면 null이며, 필드 누락도 같은 뜻으로 받는다.
    speaker_embedding: SpeakerEmbedding | None = None


class SessionStart(Message):
    type: Literal["session.start"]
    request_id: Identifier
    # ponytail: BE 문자열 ID 송신이 변경되면 strict=False를 제거한다(AI #77).
    meeting_id: MeetingId = Field(strict=False)
    recording_session_id: RecordingSessionId = Field(strict=False)
    # BE가 보내기 전까지는 과거 회의 조회 없이 STT 맥락을 만든다(BE #372).
    team_id: NonNegativeInt | None = None
    participant_count: NonNegativeInt
    participants: list[SessionParticipant]
    start_sequence: NonNegativeInt = Field(
        default=0, description="0부터 세는 녹음 전체 오디오 순번"
    )
    transcript_sequence_start: NonNegativeInt = Field(default=0, description="첫 전사에 붙일 순번")
    title: str = Field(default="", max_length=900)
    purpose: str = Field(default="", max_length=900)
    notes: str = Field(default="", max_length=900)

    @model_validator(mode="after")
    def validate_speaker_profiles(self):
        ids = [p.team_member_id for p in self.participants if p.speaker_embedding is not None]
        if None in ids or len(ids) != len(set(ids)):
            raise ValueError("등록 벡터가 있는 참가자는 중복되지 않는 teamMemberId가 필요합니다.")
        return self


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


class TranscriptCommittedPayload(Message):
    sequence_number: NonNegativeInt
    content: str
    started_at_ms: NonNegativeInt
    ended_at_ms: NonNegativeInt
    speaker_id: NonNegativeInt | None = None
    recognized_at: datetime


class TranscriptCommitted(Message):
    type: Literal["transcript.committed"]
    meeting_id: MeetingId
    recording_session_id: RecordingSessionId
    payload: TranscriptCommittedPayload


class SpeakerMappingUpdatedPayload(Message):
    mappings: list[SpeakerMapping]


class SpeakerMappingUpdated(Message):
    type: Literal["speaker.mapping.updated"]
    meeting_id: MeetingId
    recording_session_id: RecordingSessionId
    payload: SpeakerMappingUpdatedPayload


class AudioMetaPayload(Message):
    sequence: NonNegativeInt


class AudioMeta(Message):
    type: Literal["audio.meta"]
    payload: AudioMetaPayload


class AudioAck(Message):
    type: Literal["audio.ack"]
    recording_session_id: RecordingSessionId
    payload: AudioMetaPayload


class HeartbeatPing(Message):
    type: Literal["heartbeat.ping"]
    request_id: Identifier
    recording_session_id: RecordingSessionId = Field(strict=False)


class HeartbeatPong(Message):
    type: Literal["heartbeat.pong"]
    request_id: Identifier
    recording_session_id: RecordingSessionId


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
        "provider_unavailable",
        "processing_timeout",
        "audio_buffer_full",
    ]
    message: str
    retryable: Literal[True, False]


class SessionError(Message):
    type: Literal["error"]
    request_id: Identifier | None
    payload: SessionErrorPayload


ClientEvent = Annotated[
    SessionStart | AudioMeta | HeartbeatPing | SessionPause | SessionResume | SessionStop,
    Field(discriminator="type"),
]

client_event_adapter = TypeAdapter(ClientEvent)
