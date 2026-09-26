from typing import Annotated

from pydantic import Field, StringConstraints, model_validator

from meety_ai.summary.schemas import Identifier, Message


class DiarizationSegment(Message):
    segment_id: int
    content: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]
    started_at_ms: Annotated[int, Field(ge=0)]
    ended_at_ms: Annotated[int, Field(ge=0)]

    @model_validator(mode="after")
    def _check_range(self) -> "DiarizationSegment":
        if self.started_at_ms > self.ended_at_ms:
            raise ValueError("startedAtMs는 endedAtMs보다 클 수 없습니다.")
        return self


class DiarizationRequest(Message):
    request_id: Identifier
    meeting_id: int
    # presigned URL은 서명을 포함하므로 로그·오류 응답에 원문을 남기지 않는다.
    audio_url: Annotated[str, StringConstraints(pattern=r"^https://\S+$", max_length=4096)]
    segments: Annotated[list[DiarizationSegment], Field(min_length=1)]


class AttributedSegment(DiarizationSegment):
    # 판단할 수 없으면 임의의 화자 대신 null
    speaker_id: int | None


class DiarizationResponse(Message):
    meeting_id: int
    segments: list[AttributedSegment]
