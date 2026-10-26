"""AI와 Modal 사이의 메시지와 PCM 입력 한도를 정의한다."""

from typing import Annotated, Literal

from pydantic import Field, TypeAdapter, model_validator

from meety_ai.core.schemas import MeetingId, Message, NonNegativeInt, RecordingSessionId

SAMPLE_RATE = 16000
MAX_AUDIO_MESSAGE_BYTES = 256 * 1024
MAX_TEXT_MESSAGE_BYTES = 16 * 1024


class SessionStart(Message):
    type: Literal["session.start"]
    meeting_id: MeetingId
    recording_session_id: RecordingSessionId
    context: str = Field(default="", max_length=3000)
    audio_format: Literal["pcm_s16le"] = "pcm_s16le"
    sample_rate_hz: Literal[16000] = SAMPLE_RATE
    channels: Literal[1] = 1


class SessionStop(Message):
    type: Literal["session.stop"]


class SessionReady(Message):
    type: Literal["session.ready"]
    metadata: dict


class SegmentCommitted(Message):
    type: Literal["segment.committed"]
    segment_id: NonNegativeInt
    speaker_id: Annotated[int, Field(ge=0, le=7)] | None
    started_at_ms: NonNegativeInt
    ended_at_ms: NonNegativeInt
    content: str = Field(min_length=1)
    reason: str
    recognized_at: str
    timings: dict

    @model_validator(mode="after")
    def validate_segment(self):
        if self.started_at_ms >= self.ended_at_ms or not self.content.strip():
            raise ValueError("비어 있지 않은 시간 구간과 전사가 필요합니다.")
        return self


class SegmentFailed(Message):
    type: Literal["segment.failed"]
    segment_id: NonNegativeInt
    speaker_id: Annotated[int, Field(ge=0, le=7)] | None
    started_at_ms: NonNegativeInt
    ended_at_ms: NonNegativeInt
    reason: str
    recognized_at: str
    timings: dict
    code: str

    @model_validator(mode="after")
    def validate_segment(self):
        if self.started_at_ms >= self.ended_at_ms:
            raise ValueError("비어 있지 않은 시간 구간이 필요합니다.")
        return self


class SessionEnded(Message):
    type: Literal["session.ended"]
    status: Literal["completed"]
    succeeded_segments: NonNegativeInt
    failed_segments: NonNegativeInt
    received_samples: NonNegativeInt
    audio_duration_ms: NonNegativeInt
    unscored_tail_samples: NonNegativeInt


class PipelineFailure(Message):
    type: Literal["error"]
    code: str


result_adapter = TypeAdapter(
    Annotated[
        SessionReady | SegmentCommitted | SegmentFailed | SessionEnded | PipelineFailure,
        Field(discriminator="type"),
    ]
)
