"""전역 AI 채팅의 BE 계약과 회의 초안 검증 규칙을 정의한다."""

from datetime import datetime
from typing import Annotated, Self

from pydantic import StringConstraints, field_validator, model_validator

from meety_ai.chat.schemas import QnAMessage
from meety_ai.core.schemas import Message, NonNegativeInt


class AssistantChatRequest(Message):
    ai_request_id: NonNegativeInt
    team_id: NonNegativeInt
    requester_team_member_id: NonNegativeInt
    question: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]
    recent_messages: list[QnAMessage]


class MeetingDraft(Message):
    """BE는 검증에 실패한 초안을 알리지 않고 버리므로 MeetingCreateRequest 규칙을 먼저 적용한다."""

    title: Annotated[str, StringConstraints(strip_whitespace=True, min_length=2, max_length=20)]
    purpose: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=100)]
    note: Annotated[str, StringConstraints(max_length=200)] | None = None
    target_duration_minutes: int
    attendee_team_member_ids: list[NonNegativeInt]
    custom_attendee_names: list[str]
    scheduled_at: str

    @field_validator("target_duration_minutes")
    @classmethod
    def validate_duration(cls, value: int) -> int:
        if not 1 <= value <= 60:
            raise ValueError("목표 시간은 1분 이상 60분 이하여야 합니다.")
        return value

    @field_validator("scheduled_at")
    @classmethod
    def validate_scheduled_at(cls, value: str) -> str:
        if datetime.fromisoformat(value).tzinfo is not None:
            raise ValueError("예정 시각은 시간대 없는 KST로 입력해야 합니다.")
        return value

    @field_validator("custom_attendee_names")
    @classmethod
    def validate_custom_names(cls, names: list[str]) -> list[str]:
        if len(names) > 2:
            raise ValueError("외부 참석자는 최대 2명입니다.")
        if any(not name.strip() or len(name.strip()) > 10 for name in names):
            raise ValueError("외부 참석자 이름은 1자 이상 10자 이하여야 합니다.")
        return names

    @model_validator(mode="after")
    def validate_attendees(self) -> Self:
        ids = self.attendee_team_member_ids
        if len(set(ids)) != len(ids):
            raise ValueError("참석자 ID가 중복되었습니다.")
        if not 1 <= len(ids) + len(self.custom_attendee_names) <= 10:
            raise ValueError("참석자는 1명 이상 10명 이하여야 합니다.")
        return self


class MeetingMetricsResponse(Message):
    metrics: dict | None
    speech_balance_score: float | None
    context_out_duration_ms: int | None


class TeamMember(Message):
    team_member_id: int
    display_name: str


class TeamMembersResponse(Message):
    members: list[TeamMember]
