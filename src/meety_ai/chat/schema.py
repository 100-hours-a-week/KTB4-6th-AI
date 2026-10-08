from typing import Annotated, Literal

from pydantic import StringConstraints

from meety_ai.core.schema import Identifier, MeetingId, Message, NonNegativeInt


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
    ended_at_ms: NonNegativeInt


class ChatRequest(Message):
    request_id: Identifier
    meeting_id: MeetingId
    ai_request_id: NonNegativeInt
    question: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]
    conversation_history: list[QnAMessage]
    transcript_segments: list[TranscriptSegment]


class Citation(Message):
    meeting_id: MeetingId


class ChatResponse(Message):
    request_id: Identifier
    meeting_id: MeetingId
    ai_request_id: NonNegativeInt
    answer: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]
    citations: list[Citation]
