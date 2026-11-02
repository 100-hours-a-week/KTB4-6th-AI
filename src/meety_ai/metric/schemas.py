from typing import Annotated

from pydantic import Field, StringConstraints, model_validator

from meety_ai.core.schemas import Identifier, MeetingId, Message, NonNegativeInt
from meety_ai.summary.schemas import TranscriptionSegment

Text = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]


class MetricRequest(Message):
    request_id: Identifier
    meeting_id: MeetingId
    summary: Text
    segments: list[TranscriptionSegment]

    @model_validator(mode="after")
    def validate_snapshot(self):
        for field in ("segment_id", "sequence_number"):
            values = [getattr(segment, field) for segment in self.segments]
            if len(values) != len(set(values)):
                raise ValueError(f"전사의 {field}는 중복될 수 없습니다.")
        for segment in self.segments:
            if segment.ended_at_ms < segment.started_at_ms:
                raise ValueError("전사 종료 시각은 시작 시각보다 빠를 수 없습니다.")
        return self


class SegmentRange(Message):
    start_index: NonNegativeInt
    end_index: NonNegativeInt


class ClassifiedTopic(Message):
    title: Text
    ranges: Annotated[list[SegmentRange], Field(min_length=1)]


class OffTopicRange(Message):
    ranges: Annotated[list[SegmentRange], Field(min_length=1)]
    reason: Text


class MetricClassification(Message):
    agendas: list[ClassifiedTopic]
    off_topic: list[OffTopicRange]


class DurationMetric(Message):
    duration_ms: NonNegativeInt | None


class AgendaMetric(Message):
    title: Text
    duration_ms: NonNegativeInt


class MetricResponse(Message):
    request_id: Identifier
    meeting_id: MeetingId
    off_topic: DurationMetric
    agendas: list[AgendaMetric] | None
