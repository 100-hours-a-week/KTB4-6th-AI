from datetime import datetime
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, StringConstraints
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


class Speaker(Message):
    speaker_id: int
    team_member_id: int | None
    display_name: str | None


class TranscriptionSegment(Message):
    segment_id: int
    speaker_id: int
    sequence_number: Annotated[int, Field(ge=0)]
    content: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]
    started_at_ms: int
    ended_at_ms: int


class SummaryRequest(Message):
    request_id: Identifier
    meeting_id: int
    title: str
    purpose: str
    note: str
    meeting_started_at: Annotated[datetime, Field(strict=False)]
    speakers: list[Speaker]
    segments: Annotated[list[TranscriptionSegment], Field(min_length=1)]
