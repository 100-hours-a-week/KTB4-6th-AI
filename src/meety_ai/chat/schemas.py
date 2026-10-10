from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, StringConstraints, model_validator

from meety_ai.core.schemas import MeetingId, Message, NonNegativeInt


class QnAMessage(Message):
    role: Literal["user", "assistant"]
    content: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]


class TranscriptSegment(Message):
    segment_id: NonNegativeInt
    speaker_display_name: (
        Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)] | None
    )
    sequence_number: NonNegativeInt
    content: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]
    started_at_ms: NonNegativeInt
    ended_at_ms: NonNegativeInt | None


class ChatRequest(Message):
    team_id: NonNegativeInt
    meeting_id: MeetingId
    ai_request_id: NonNegativeInt
    question: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]
    conversation_history: list[QnAMessage]
    transcript_segments: list[TranscriptSegment]


class Citation(Message):
    source_type: Literal["transcript", "summary"]
    meeting_id: MeetingId
    segment_id: NonNegativeInt | None = None
    summary_id: NonNegativeInt | None = None

    @model_validator(mode="after")
    def validate_source_id(self):
        if self.source_type == "transcript" and self.segment_id is None:
            raise ValueError("전사 근거에는 segmentId가 필요합니다.")
        if self.source_type == "summary" and self.summary_id is None:
            raise ValueError("요약 근거에는 summaryId가 필요합니다.")
        return self


class ChatAnswer(Message):
    answer: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]
    citations: list[Citation]


class ChatResponse(ChatAnswer):
    ai_request_id: NonNegativeInt


# 백엔드 조회 tool용
class BackendResponse(BaseModel):
    success: bool
    data: dict | None
    error: dict | None


class MeetingListItem(Message):
    meeting_id: int
    title: str
    scheduled_at: str | None
    started_at: str | None
    ended_at: str | None
    target_duration_minutes: int | None
    status: Literal["WAITING", "IN_PROGRESS", "COMPLETED"]


class MeetingGroup(Message):
    date: str
    meeting_count: int
    meetings: list[MeetingListItem]


class MeetingListResponse(Message):
    groups: list[MeetingGroup]
    next_cursor: str | None
    has_next: bool


class TranscriptResponseSegment(TranscriptSegment):
    model_config = ConfigDict(extra="ignore")


class TranscriptResponse(Message):
    segments: list[TranscriptResponseSegment]


class SummaryResponse(Message):
    model_config = ConfigDict(extra="ignore")

    summary_id: int
    status: str
    content: str | None


class InternalSummaryResponse(Message):
    summary: SummaryResponse | None
