from datetime import datetime
from typing import Annotated

from pydantic import Field, StringConstraints

from meety_ai.core.schemas import Identifier, MeetingId, Message, NonNegativeInt


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


class SummarySegment(TranscriptionSegment):
    # Backend가 정한 현재 화자 표시 이름이며, 화자가 확정되지 않으면 null이다.
    display_name: str | None = None


class SummaryRequest(Message):
    request_id: Identifier
    meeting_id: MeetingId
    title: str
    purpose: str
    note: str
    meeting_started_at: Annotated[datetime, Field(strict=False)]
    # Backend는 현재 빈 목록을 보내며, 화자 이름은 세그먼트의 display_name을 사용한다.
    speakers: list[Speaker]
    segments: Annotated[list[SummarySegment], Field(min_length=1)]
    previous_summary: (
        Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)] | None
    ) = Field(default=None, exclude_if=lambda value: value is None)
    regeneration_reason: (
        Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)] | None
    ) = Field(default=None, exclude_if=lambda value: value is None)
