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

class QnAMessage(Message):
    role: Literal["user", "assistant"]
    content: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]

class TranscriptionSegment(Message):
    segment_id: Annotated[int, Field(gt=0)]
    speaker_display_name: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)] | None
    sequence_number: Annotated[int, Field(ge=0)]
    content: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]
    started_at_ms: Annotated[int, Field(ge=0)]
    ended_at_ms: Annotated[int, Field(ge=0)]

class ChatRequestPayload(Message):
    team_id: Annotated[int, Field(gt=0)]
    question: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]
    conversation_history: list[QnAMessage]
    transcript_segments: list[TranscriptionSegment]

class ChatRequest(Message):
    type: Literal["chat.request"]
    meeting_id: Identifier
    ai_request_id: Identifier
    payload: ChatRequestPayload

class ChatAccepted(Message):
    type: Literal["chat.accepted"]
    meeting_id: Identifier
    ai_request_id: Identifier

class TranscriptCitation(Message):
    source_type: Literal["transcript"]
    meeting_id: Identifier
    segment_id: Annotated[int, Field(gt=0)]


class SummaryCitation(Message):
    source_type: Literal["summary"]
    meeting_id: Identifier
    summary_id: Annotated[int, Field(gt=0)]


Citation = Annotated[
    TranscriptCitation | SummaryCitation,
    Field(discriminator="source_type"),
]

class ChatCompletedPayload(Message):
    answer: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]
    citations: list[Citation]

class ChatCompleted(Message):
    type: Literal["chat.completed"]
    meeting_id: Identifier
    ai_request_id: Identifier
    payload: ChatCompletedPayload

class ChatFailedPayload(Message):
    code: Literal["invalid_request",
                  "forbidden",
                  "retrieval_failed",
                  "generation_failed",
                  "timeout",
                  "internal_error"]
    message: str

class ChatFailed(Message):
    type: Literal["chat.failed"]
    meeting_id: Identifier
    ai_request_id: Identifier
    payload: ChatFailedPayload
