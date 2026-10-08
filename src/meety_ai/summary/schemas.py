from datetime import datetime
from typing import Annotated

from pydantic import Field, StringConstraints

from meety_ai.core.schema import Identifier, MeetingId, Message, NonNegativeInt


class Speaker(Message):
    speaker_id: NonNegativeInt
    team_member_id: NonNegativeInt | None
    display_name: str | None


class TranscriptionSegment(Message):
    segment_id: NonNegativeInt
    # 화자 분리에서 대표 화자를 판단할 수 없던 구간은 null이다.
    speaker_id: NonNegativeInt | None
    sequence_number: NonNegativeInt
    content: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]
    started_at_ms: NonNegativeInt
    ended_at_ms: NonNegativeInt


class SummaryRequest(Message):
    request_id: Identifier
    meeting_id: MeetingId
    title: str
    purpose: str
    note: str
    meeting_started_at: Annotated[datetime, Field(strict=False)]
    speakers: list[Speaker]
    segments: Annotated[list[TranscriptionSegment], Field(min_length=1)]
    previous_summary: (
        Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)] | None
    ) = Field(default=None, exclude_if=lambda value: value is None)
    regeneration_reason: (
        Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)] | None
    ) = Field(default=None, exclude_if=lambda value: value is None)
